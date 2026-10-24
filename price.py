# ============================================================
# ASSET PRICE TRACKER BOT — крипта и акции
# aiogram 3 + SQLite + httpx
# Python 3.11+
#
# Установка:  pip install aiogram httpx
# Запуск:     python price.py
#
# КАК ДОБАВЛЯТЬ ОБНОВЛЕНИЯ:
#   * новый рынок          -> KINDS (раздел 1) + список источников в PROVIDERS (раздел 6)
#   * новый источник цен   -> функция provider_xxx (раздел 6) + добавить её в PROVIDERS
#   * новая колонка в БД   -> MIGRATIONS (раздел 2)
#   * новая кнопка/раздел  -> клавиатуры (раздел 3) + handler по образцу раздела 9
#   * новая админ-функция  -> admin_keyboard() + handler в разделах 13-16
# ============================================================

import asyncio
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import httpx

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramAPIError


# ============================================================
# 1. НАСТРОЙКИ — РЕДАКТИРУЙ ЭТОТ РАЗДЕЛ
# ============================================================

# Токен от @BotFather
BOT_TOKEN = "8460061736:AAFdOGa74cPArtA1OwnvR7yNqYsd4cpS-mo"

# Твой числовой Telegram ID (узнать: бот @userinfobot).
# Несколько админов: {111, 222}
ADMIN_IDS = {5796754554}

# Пароль админки (команда /админ или /admin)
ADMIN_PASSWORD = "2525"

# Контакт, куда писать за подпиской (показывается в разделе «Подписка»)
SUPPORT_CONTACT = "@Do_need"

# База данных. Пользователи и подписки из старой базы (OLD_DB_NAME)
# один раз копируются автоматически, если она лежит рядом.
DB_NAME = "asset_tracker.db"
OLD_DB_NAME = "price_tracker.db"

# Часовой пояс для дат в сообщениях (Москва = +3)
TZ_OFFSET_HOURS = 3
TZ_LABEL = "МСК"

# Лимиты активных отслеживаний
FREE_TRACK_LIMIT = 3
SUB_TRACK_LIMIT = 50

# Минимальный интервал проверки для бесплатных (сек). 900 = 15 минут.
# Поставь 60, если хочешь без ограничений.
FREE_MIN_INTERVAL = 900

# Если цель пропущена — уведомлять при движении цены на столько %
DEFAULT_MOVE_PERCENT = 5

# Не слать уведомления по одному активу чаще, чем раз в N секунд
NOTIFY_COOLDOWN = 600

# Предупредить пользователя, если цену не удаётся получить столько секунд подряд
FAIL_WARN_MIN_SECONDS = 3600

# Кэш цен: один и тот же тикер у разных людей запрашивается один раз
PRICE_CACHE_TTL = 20

# Как часто просыпается монитор и сколько запросов к API идёт параллельно
LOOP_SLEEP = 15
MAX_PARALLEL_FETCH = 5

# Защита админки от подбора пароля
ADMIN_MAX_ATTEMPTS = 5
ADMIN_LOCK_SECONDS = 600

# Рынки. Ключ — внутреннее имя, его же ищи в PROVIDERS (раздел 6).
KINDS = {
    "crypto": {
        "title": "🪙 Крипта",
        "currency": "USDT",
        "hint": "BTC, ETH, SOL, TON",
        "popular": ["BTC", "ETH", "SOL", "TON", "XRP", "DOGE"],
    },
    "moex": {
        "title": "🇷🇺 Акции MOEX",
        "currency": "RUB",
        "hint": "SBER, GAZP, LKOH, YDEX",
        "popular": ["SBER", "GAZP", "LKOH", "YDEX", "ROSN", "VTBR"],
    },
    "us": {
        "title": "🇺🇸 Акции США",
        "currency": "USD",
        "hint": "AAPL, TSLA, NVDA, MSFT",
        "popular": ["AAPL", "TSLA", "NVDA", "MSFT", "AMZN", "GOOGL"],
    },
}

KIND_BY_TITLE = {info["title"]: key for key, info in KINDS.items()}

CURRENCY_SIGN = {"USDT": "USDT", "RUB": "₽", "USD": "$"}

# Как часто проверять цену (подпись кнопки -> секунды)
INTERVALS = {
    "1 мин": 60,
    "5 мин": 300,
    "15 мин": 900,
    "30 мин": 1800,
    "1 час": 3600,
    "6 часов": 21600,
    "1 день": 86400,
    "7 дней": 604800,
}

# Как долго следить (максимум 30 дней)
DURATIONS = {
    "1 день": 86400,
    "7 дней": 7 * 86400,
    "14 дней": 14 * 86400,
    "30 дней": 30 * 86400,
}


# ============================================================
# 2. БОТ И БАЗА ДАННЫХ
# ============================================================

if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬ_ТОКЕН_СЮДА":
    raise RuntimeError("Вставь токен бота в BOT_TOKEN вверху файла.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

admin_sessions = set()   # админы, которые ввели пароль
admin_view = set()       # админы, у которых сейчас открыта админ-панель
admin_attempts = {}      # user_id -> [неверных попыток, заблокирован до]
check_cooldown = {}      # user_id -> время последней ручной проверки
http = None              # общий httpx-клиент, создаётся в main()

# Новые колонки для таблицы tracks добавляй сюда: имя -> описание колонки.
# Они автоматически добавятся в уже существующую базу при запуске.
# Пример:  "note": "TEXT",
MIGRATIONS = {}


def run(sql, params=()):
    """Выполняет SQL, возвращает список строк. Соединение закрывается."""
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def one(sql, params=()):
    rows = run(sql, params)
    return rows[0] if rows else None


def init_db():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            created_at INTEGER NOT NULL,
            banned INTEGER NOT NULL DEFAULT 0,
            subscription_until INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            symbol TEXT NOT NULL,
            display_name TEXT,
            currency TEXT NOT NULL,
            target_mode TEXT NOT NULL,
            target_value REAL NOT NULL,
            target_dir TEXT NOT NULL,
            base_price REAL NOT NULL,
            last_price REAL,
            interval_seconds INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            next_check INTEGER NOT NULL,
            notified_target INTEGER NOT NULL DEFAULT 0,
            last_notified_at INTEGER NOT NULL DEFAULT 0,
            fail_since INTEGER NOT NULL DEFAULT 0,
            warned_fail INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_tracks_due ON tracks(active, next_check);
        CREATE INDEX IF NOT EXISTS idx_tracks_user ON tracks(user_id);
        """)

        existing = {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}
        for column, ddl in MIGRATIONS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE tracks ADD COLUMN {column} {ddl}")

        conn.commit()
    finally:
        conn.close()


def import_old_users():
    """Один раз копирует пользователей и подписки из старой базы магазинов."""
    if not os.path.exists(OLD_DB_NAME):
        return
    if os.path.abspath(OLD_DB_NAME) == os.path.abspath(DB_NAME):
        return

    conn = sqlite3.connect(DB_NAME, timeout=10)
    try:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
            return

        conn.execute("ATTACH DATABASE ? AS old", (OLD_DB_NAME,))
        conn.execute("""
            INSERT OR IGNORE INTO users
                (user_id, username, created_at, banned, subscription_until)
            SELECT user_id, username, created_at, banned, subscription_until
            FROM old.users
        """)
        conn.commit()

        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        print(f"Из старой базы перенесено пользователей: {total}")
    except sqlite3.Error as exc:
        print(f"[IMPORT ERROR] {type(exc).__name__}: {exc}")
    finally:
        conn.close()


def get_user(user_id):
    return one("SELECT * FROM users WHERE user_id = ?", (user_id,))


def register_user(user):
    run("""
        INSERT INTO users(user_id, username, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
    """, (user.id, user.username, int(time.time())))


def is_banned(user_id):
    row = get_user(user_id)
    return bool(row and row["banned"])


def subscription_active(user_id):
    row = get_user(user_id)
    return bool(row and row["subscription_until"] > int(time.time()))


def active_track_count(user_id):
    row = one(
        "SELECT COUNT(*) AS n FROM tracks WHERE user_id = ? AND active = 1",
        (user_id,),
    )
    return row["n"]


def track_limit(user_id):
    return SUB_TRACK_LIMIT if subscription_active(user_id) else FREE_TRACK_LIMIT


# ---------- форматирование ----------

def fmt_date(timestamp):
    if not timestamp:
        return "нет"
    tz = timezone(timedelta(hours=TZ_OFFSET_HOURS))
    text = datetime.fromtimestamp(timestamp, tz).strftime("%d.%m.%Y %H:%M")
    return f"{text} {TZ_LABEL}"


def fmt_num(value):
    """Число с разумным количеством знаков: BTC — 2, копеечные монеты — до 8."""
    if value >= 1000:
        decimals = 2
    elif value >= 1:
        decimals = 4
    else:
        decimals = 8

    text = f"{value:,.{decimals}f}"
    integer, fraction = text.split(".")
    fraction = fraction.rstrip("0").ljust(2, "0")
    return f"{integer}.{fraction}".replace(",", " ")


def fmt_price(value, currency):
    if value is None:
        return "неизвестна"
    return f"{fmt_num(value)} {CURRENCY_SIGN.get(currency, currency)}"


def pct_change(base, price):
    if not base or price is None:
        return None
    return (price - base) / base * 100


def fmt_change(base, price):
    change = pct_change(base, price)
    return "—" if change is None else f"{change:+.2f}%"


def interval_text(seconds):
    for label, value in INTERVALS.items():
        if value == seconds:
            return label
    return f"{seconds} сек."


def asset_title(row):
    symbol = row["symbol"]
    name = (row["display_name"] or "").strip()

    if name and name.upper() != symbol:
        return f"{symbol} ({name[:40]})"
    return symbol


def nice(value):
    """Округляет до 3 значащих цифр — для красивых примеров."""
    return float(f"{value:.3g}")


# ============================================================
# 3. КЛАВИАТУРЫ
# ============================================================

def kb(labels, per_row=2, cancel=True):
    """Собирает клавиатуру из списка подписей."""
    rows = [labels[i:i + per_row] for i in range(0, len(labels), per_row)]
    keyboard = [[KeyboardButton(text=text) for text in row] for row in rows]

    if cancel:
        keyboard.append([KeyboardButton(text="❌ Отмена")])

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить актив"),
             KeyboardButton(text="📊 Мои активы")],
            [KeyboardButton(text="💳 Подписка"),
             KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
    )


def admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Пользователи"),
             KeyboardButton(text="📦 Все отслеживания")],
            [KeyboardButton(text="📣 Рассылка"),
             KeyboardButton(text="🚫 Бан")],
            [KeyboardButton(text="✅ Разбан"),
             KeyboardButton(text="⏳ Продлить подписку")],
            [KeyboardButton(text="📊 Статистика"),
             KeyboardButton(text="🧪 Тест цены")],
            [KeyboardButton(text="🏠 Главное меню"),
             KeyboardButton(text="🚪 Выйти из админки")],
        ],
        resize_keyboard=True,
    )


def is_admin(user_id):
    return user_id in ADMIN_IDS and user_id in admin_sessions


def keyboard_for(user_id):
    """Админ-клавиатура только пока открыта админ-панель."""
    if is_admin(user_id) and user_id in admin_view:
        return admin_keyboard()
    return main_keyboard()


# ============================================================
# 4. СОСТОЯНИЯ (FSM)
# ============================================================

class TrackForm(StatesGroup):
    kind = State()
    symbol = State()
    target = State()
    interval = State()
    duration = State()


class AdminForm(StatesGroup):
    password = State()
    broadcast = State()
    ban_id = State()
    unban_id = State()
    extend_id = State()
    extend_days = State()
    test_price = State()


# ============================================================
# 5. MIDDLEWARE: регистрация и бан для ВСЕХ действий
# ============================================================

class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")

        if user and not user.is_bot:
            register_user(user)

            if is_banned(user.id) and user.id not in ADMIN_IDS:
                if isinstance(event, Message):
                    await event.answer("Доступ к боту ограничен.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("Доступ ограничен.", show_alert=True)
                return

        return await handler(event, data)


dp.message.outer_middleware(BanMiddleware())
dp.callback_query.outer_middleware(BanMiddleware())


# ============================================================
# 6. ПОЛУЧЕНИЕ ЦЕН
#
# Для каждого рынка есть список источников (PROVIDERS). Бот пробует их
# по очереди: если первый недоступен (блокировка региона, лимиты),
# берётся следующий.
#
# Источник: async def provider_xxx(client, symbol) -> dict | None
#   dict  — {"price": float, "currency": str (необязательно), "name": str (необязательно)}
#   None  — источник ответил, но такого тикера нет
#   исключение — источник недоступен (сеть, 403, 429 и т.п.)
#
# Если цены не читаются — открой админку -> «🧪 Тест цены»:
# бот покажет, что ответил каждый источник.
# ============================================================

class PriceError(Exception):
    def __init__(self, message, notfound=False):
        super().__init__(message)
        self.notfound = notfound


def to_float(value):
    try:
        number = float(str(value).replace(" ", "").replace(",", "."))
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) and 0 < number < 1e12 else None


def normalize_symbol(kind, raw):
    """Приводит ввод пользователя к тикеру или возвращает None."""
    text = (raw or "").strip().upper().replace(" ", "")

    if kind == "crypto":
        # BTC, BTC/USDT, BTC-USDT, BTCUSDT -> BTC
        for separator in ("/", "-", "_"):
            text = text.replace(separator, "")
        if text.endswith("USDT") and len(text) > 4:
            text = text[:-4]
        return text if re.fullmatch(r"[A-Z0-9]{2,15}", text) else None

    if kind == "moex":
        return text if re.fullmatch(r"[A-Z0-9]{2,12}", text) else None

    if kind == "us":
        text = text.replace(".", "-")  # BRK.B -> BRK-B
        return text if re.fullmatch(r"[A-Z]{1,6}(-[A-Z]{1,2})?", text) else None

    return None


# ---------- крипта (котировки к USDT) ----------

async def _binance(client, base_url, symbol):
    response = await client.get(
        f"{base_url}/api/v3/ticker/price",
        params={"symbol": f"{symbol}USDT"},
    )
    if response.status_code in (400, 404):
        return None  # такой пары нет
    response.raise_for_status()

    price = to_float(response.json().get("price"))
    return {"price": price, "currency": "USDT"} if price else None


async def provider_binance_vision(client, symbol):
    return await _binance(client, "https://data-api.binance.vision", symbol)


async def provider_binance(client, symbol):
    return await _binance(client, "https://api.binance.com", symbol)


async def provider_bybit(client, symbol):
    response = await client.get(
        "https://api.bybit.com/v5/market/tickers",
        params={"category": "spot", "symbol": f"{symbol}USDT"},
    )
    response.raise_for_status()

    items = (response.json().get("result") or {}).get("list") or []
    if not items:
        return None

    price = to_float(items[0].get("lastPrice"))
    return {"price": price, "currency": "USDT"} if price else None


async def provider_okx(client, symbol):
    response = await client.get(
        "https://www.okx.com/api/v5/market/ticker",
        params={"instId": f"{symbol}-USDT"},
    )
    response.raise_for_status()

    items = response.json().get("data") or []
    if not items:
        return None

    price = to_float(items[0].get("last"))
    return {"price": price, "currency": "USDT"} if price else None


# ---------- акции MOEX (в рублях) ----------

async def provider_moex(client, symbol):
    response = await client.get(
        "https://iss.moex.com/iss/engines/stock/markets/shares/"
        f"boards/TQBR/securities/{symbol}.json",
        params={
            "iss.meta": "off",
            "iss.only": "marketdata,securities",
            "marketdata.columns": "SECID,LAST,LCURRENTPRICE,MARKETPRICE",
            "securities.columns": "SECID,SHORTNAME,PREVPRICE",
        },
    )
    response.raise_for_status()
    payload = response.json()

    market = payload.get("marketdata") or {}
    if not market.get("data"):
        return None

    values = dict(zip(market.get("columns") or [], market["data"][0]))

    info = payload.get("securities") or {}
    meta = {}
    if info.get("data"):
        meta = dict(zip(info.get("columns") or [], info["data"][0]))

    # Вне торгов LAST пустой — берём последнюю цену или цену закрытия.
    price = (
        to_float(values.get("LAST"))
        or to_float(values.get("LCURRENTPRICE"))
        or to_float(values.get("MARKETPRICE"))
        or to_float(meta.get("PREVPRICE"))
    )
    if not price:
        return None

    return {"price": price, "currency": "RUB", "name": meta.get("SHORTNAME")}


# ---------- акции США ----------

async def provider_yahoo(client, symbol):
    response = await client.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"interval": "1d", "range": "1d"},
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()

    results = (response.json().get("chart") or {}).get("result")
    if not results:
        return None

    meta = results[0].get("meta") or {}
    price = to_float(meta.get("regularMarketPrice"))
    if not price:
        return None

    return {
        "price": price,
        "currency": meta.get("currency") or "USD",
        "name": meta.get("shortName") or meta.get("longName"),
    }


async def provider_stooq(client, symbol):
    """Запасной источник США: котировки с небольшой задержкой."""
    response = await client.get(
        f"https://stooq.com/q/l/?s={symbol.lower()}.us&f=sd2t2ohlcv&h&e=csv"
    )
    response.raise_for_status()

    lines = response.text.strip().splitlines()
    if len(lines) < 2:
        return None

    values = dict(zip(lines[0].split(","), lines[1].split(",")))
    price = to_float(values.get("Close"))  # у несуществующих тикеров там N/D

    return {"price": price, "currency": "USD"} if price else None


PROVIDERS = {
    "crypto": [provider_binance_vision, provider_binance,
               provider_bybit, provider_okx],
    "moex": [provider_moex],
    "us": [provider_yahoo, provider_stooq],
}


async def fetch_price(kind, symbol, trace=None):
    """
    Возвращает {"price", "currency", "name"} или бросает PriceError.
    PriceError.notfound=True — все источники ответили, но тикера нет.
    trace — список, в который пишется ход проверки (для «Тест цены»).
    """
    if http is None:
        raise PriceError("HTTP-клиент не запущен")

    def note(text):
        if trace is not None:
            trace.append(text)

    had_error = False

    for provider in PROVIDERS[kind]:
        name = provider.__name__.replace("provider_", "")

        try:
            result = await provider(http, symbol)
        except Exception as exc:
            had_error = True
            note(f"{name}: ошибка {type(exc).__name__}: {exc}")
            print(f"[PRICE ERROR] {kind}/{symbol} {name}: "
                  f"{type(exc).__name__}: {exc}")
            continue

        if result:
            note(f"{name}: OK, цена {result['price']}")
            return {
                "price": result["price"],
                "currency": result.get("currency") or KINDS[kind]["currency"],
                "name": result.get("name"),
            }

        note(f"{name}: тикер не найден")

    if had_error:
        raise PriceError("источники недоступны")
    raise PriceError("тикер не найден", notfound=True)


_price_cache = {}   # (kind, symbol) -> (время, результат)
_price_locks = {}   # (kind, symbol) -> Lock, чтобы не дублировать запросы


async def get_price(kind, symbol, force=False):
    """fetch_price с кэшем. force=True — всегда свежий запрос."""
    key = (kind, symbol)
    lock = _price_locks.setdefault(key, asyncio.Lock())

    async with lock:
        cached = _price_cache.get(key)

        if (
            not force
            and cached
            and time.time() - cached[0] < PRICE_CACHE_TTL
        ):
            return cached[1]

        result = await fetch_price(kind, symbol)
        _price_cache[key] = (time.time(), result)
        return result


def purge_cache():
    cutoff = time.time() - 300

    for key in [k for k, (ts, _) in _price_cache.items() if ts < cutoff]:
        del _price_cache[key]
        lock = _price_locks.get(key)
        if lock and not lock.locked():
            del _price_locks[key]


# ============================================================
# 7. ЦЕЛИ: УРОВЕНЬ, РОСТ, ПАДЕНИЕ
#
# target_mode:
#   level — достичь цены X           («70000»; направление определяется
#                                      автоматически: выше или ниже текущей)
#   abs   — сдвиг на N единиц валюты («+500», «-500», «±500»)
#   pct   — сдвиг на N %             («+5%», «-5%», «±5%», «5%» = в обе стороны)
# target_dir: up / down / both
# ============================================================

class TargetError(ValueError):
    pass


def parse_target(raw, base):
    """Возвращает (mode, value, direction) или бросает TargetError."""
    text = (
        (raw or "").strip().lower()
        .replace(" ", "")
        .replace("₽", "").replace("$", "")
        .replace("usdt", "").replace("руб", "")
        .replace(",", ".")
        .replace("−", "-").replace("–", "-").replace("—", "-")
    )

    sign = None

    if text.startswith("±") or text.startswith("+-"):
        sign = "both"
        text = text.lstrip("±+-")
    elif text[:1] in ("+", "-"):
        sign = "up" if text[0] == "+" else "down"
        text = text[1:]

    is_percent = text.endswith("%")
    if is_percent:
        text = text[:-1]

    try:
        value = float(text)
    except ValueError:
        raise TargetError(
            "Не понял. Примеры: 70000, +5%, -5%, +500, ±3%.\n"
            "Или нажми «Пропустить»."
        )

    if not math.isfinite(value) or value <= 0:
        raise TargetError("Число должно быть больше нуля.")

    if is_percent:
        direction = sign or "both"

        if direction in ("down", "both") and value >= 100:
            raise TargetError("Падение не может быть 100% и больше.")
        if value > 10000:
            raise TargetError("Слишком большой процент.")

        return "pct", value, direction

    if sign is None:
        if value == base:
            raise TargetError("Цена уже равна этому уровню. Укажи другой.")

        return "level", value, ("up" if value > base else "down")

    if sign in ("down", "both") and value >= base:
        raise TargetError(
            f"Падение на {fmt_num(value)} невозможно: "
            f"цена сейчас {fmt_num(base)}."
        )

    return "abs", value, sign


def triggers(row):
    """Возвращает (цена_роста, цена_падения); None — сторона не отслеживается."""
    mode, value = row["target_mode"], row["target_value"]
    direction, base = row["target_dir"], row["base_price"]

    if mode == "level":
        return (value, None) if direction == "up" else (None, value)

    step = value if mode == "abs" else base * value / 100
    up = base + step if direction in ("up", "both") else None
    down = base - step if direction in ("down", "both") else None
    return up, down


def price_side(row, price):
    """'up' / 'down', если цель достигнута, иначе None."""
    up, down = triggers(row)

    if up is not None and price >= up:
        return "up"
    if down is not None and price <= down:
        return "down"
    return None


def target_text(row):
    up, down = triggers(row)
    currency = row["currency"]

    parts = []
    if up is not None:
        parts.append(f"≥ {fmt_price(up, currency)}")
    if down is not None:
        parts.append(f"≤ {fmt_price(down, currency)}")
    condition = " или ".join(parts)

    mode, value, direction = (
        row["target_mode"], row["target_value"], row["target_dir"]
    )

    if mode == "level":
        return f"цена {condition}"

    amount = f"{value:g}%" if mode == "pct" else fmt_price(value, currency)
    word = {"up": "рост на", "down": "падение на", "both": "движение на ±"}[direction]
    return f"{word} {amount} ({condition})"


# ============================================================
# 8. СТАРТ, ОТМЕНА, ПОМОЩЬ
# ============================================================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()

    await message.answer(
        "👋 Привет! Это трекер цен на крипту и акции.\n\n"
        "Слежу за ценой и пишу, когда она дойдёт до нужного уровня "
        "или изменится на нужный процент.\n\n"
        "Нажми «➕ Добавить актив», чтобы начать.",
        reply_markup=keyboard_for(message.from_user.id),
    )


@dp.message(Command("cancel"))
@dp.message(F.text == "❌ Отмена")
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Действие отменено.",
        reply_markup=keyboard_for(message.from_user.id),
    )


@dp.message(F.text == "ℹ️ Помощь")
async def help_message(message: Message):
    await message.answer(
        "ℹ️ Как пользоваться:\n\n"
        "1. «Добавить актив» → выбери рынок: крипта, акции MOEX "
        "или акции США.\n"
        "2. Введи тикер (BTC, SBER, AAPL) или выбери из популярных.\n"
        "3. Укажи цель:\n"
        "   • 70000 — когда цена дойдёт до этого уровня\n"
        "   • +5% / -5% — рост / падение на 5%\n"
        "   • +500 / -500 — рост / падение на сумму\n"
        "   • ±3% — движение в любую сторону\n"
        f"   • «Пропустить» — движение на ±{DEFAULT_MOVE_PERCENT:g}%\n"
        "4. Выбери, как часто проверять и как долго следить "
        "(до 30 дней).\n\n"
        "После уведомления бот ждёт, пока цена отойдёт от цели, "
        "и сообщит снова, если она вернётся "
        f"(не чаще раза в {NOTIFY_COOLDOWN // 60} мин).\n\n"
        "Цены: крипта — в USDT, акции MOEX — в ₽, акции США — в $. "
        "Данные по акциям могут идти с небольшой задержкой. "
        "Это не инвестиционная рекомендация.",
        reply_markup=keyboard_for(message.from_user.id),
    )


# ============================================================
# 9. ПОДПИСКА
# ============================================================

@dp.message(F.text == "💳 Подписка")
async def subscription(message: Message):
    row = get_user(message.from_user.id)
    until = row["subscription_until"] if row else 0
    active = until > int(time.time())

    status = (
        f"✅ Активна до {fmt_date(until)}"
        if active else "❌ Не активна"
    )

    await message.answer(
        "💳 Твоя подписка\n\n"
        f"Статус: {status}\n\n"
        f"Без подписки: до {FREE_TRACK_LIMIT} активов, "
        f"проверка от {interval_text(FREE_MIN_INTERVAL)}.\n"
        f"С подпиской: до {SUB_TRACK_LIMIT} активов, "
        "проверка от 1 минуты.\n\n"
        f"Подключить или продлить: {SUPPORT_CONTACT}\n"
        f"Твой ID: {message.from_user.id}",
        reply_markup=keyboard_for(message.from_user.id),
    )


# ============================================================
# 9б. ОБЩИЕ КОМАНДЫ, КОТОРЫЕ ДОЛЖНЫ РАБОТАТЬ В ЛЮБОЙ МОМЕНТ
#
# ВАЖНО: aiogram выбирает первый подходящий handler по порядку в файле.
# Всё, что должно срабатывать даже посреди мастера добавления
# (кнопки меню, /admin), объявляй ВЫШЕ раздела 10.
# ============================================================

@dp.message(F.text == "📊 Мои активы")
async def my_tracks(message: Message):
    rows = run("""
        SELECT * FROM tracks
        WHERE user_id = ? AND active = 1
        ORDER BY id DESC
    """, (message.from_user.id,))

    if not rows:
        await message.answer(
            "У тебя пока нет активных отслеживаний.",
            reply_markup=keyboard_for(message.from_user.id),
        )
        return

    for row in rows:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🔍 Проверить сейчас",
                callback_data=f"check:{row['id']}",
            ),
            InlineKeyboardButton(
                text="🗑 Удалить",
                callback_data=f"deltrack:{row['id']}",
            ),
        ]])

        await message.answer(
            f"{KINDS[row['kind']]['title']} · №{row['id']}\n"
            f"Актив: {asset_title(row)}\n"
            f"Цена: {fmt_price(row['last_price'], row['currency'])} "
            f"({fmt_change(row['base_price'], row['last_price'])} с начала)\n"
            f"Цель: {target_text(row)}\n"
            f"Следующая проверка: {fmt_date(row['next_check'])}\n"
            f"Следим до: {fmt_date(row['expires_at'])}",
            reply_markup=keyboard,
        )


async def begin_admin(message: Message, state: FSMContext):
    user_id = message.from_user.id

    if user_id not in ADMIN_IDS:
        await message.answer("Доступ запрещён.")
        return

    if user_id in admin_sessions:
        await state.clear()
        admin_view.add(user_id)
        await message.answer("🔐 Админ-панель.", reply_markup=admin_keyboard())
        return

    if admin_attempts.get(user_id, [0, 0])[1] > time.time():
        await message.answer("Слишком много попыток. Попробуй позже.")
        return

    await state.clear()
    await state.set_state(AdminForm.password)
    await message.answer(
        "Введи пароль администратора:",
        reply_markup=kb([], cancel=True),
    )


@dp.message(Command("admin"))
async def admin_command(message: Message, state: FSMContext):
    await begin_admin(message, state)


@dp.message(F.text.casefold() == "/админ")
async def admin_cyrillic_command(message: Message, state: FSMContext):
    await begin_admin(message, state)


# ============================================================
# 10. ДОБАВЛЕНИЕ АКТИВА (МАСТЕР)
# ============================================================

def target_prompt(price, currency):
    up_level = fmt_num(nice(price * 1.1))
    step = fmt_num(nice(price * 0.05))

    return (
        "🎯 Когда уведомить?\n\n"
        f"Сейчас: {fmt_price(price, currency)}\n\n"
        f"• {up_level} — когда цена дойдёт до этого уровня\n"
        "• +5% или -5% — рост или падение на 5%\n"
        f"• +{step} или -{step} — рост или падение на сумму\n"
        "• ±3% — движение в любую сторону\n\n"
        f"«Пропустить» — уведомлю при движении на ±{DEFAULT_MOVE_PERCENT:g}%."
    )


@dp.message(F.text == "➕ Добавить актив")
async def add_track(message: Message, state: FSMContext):
    user_id = message.from_user.id
    limit = track_limit(user_id)

    if active_track_count(user_id) >= limit:
        await message.answer(
            f"Достигнут лимит: {limit} активных отслеживаний.\n"
            "Удали ненужные в «📊 Мои активы» или оформи подписку "
            "в разделе «💳 Подписка»."
        )
        return

    await state.clear()
    await state.set_state(TrackForm.kind)
    await message.answer(
        "Что отслеживаем?",
        reply_markup=kb(list(KIND_BY_TITLE), per_row=1),
    )


@dp.message(TrackForm.kind)
async def select_kind(message: Message, state: FSMContext):
    kind = KIND_BY_TITLE.get(message.text or "")

    if not kind:
        await message.answer("Выбери рынок кнопкой ниже.")
        return

    info = KINDS[kind]

    await state.update_data(kind=kind)
    await state.set_state(TrackForm.symbol)

    await message.answer(
        f"{info['title']}\n\n"
        f"Введи тикер, например: {info['hint']}.\n"
        "Или выбери из популярных кнопками ниже.",
        reply_markup=kb(info["popular"], per_row=3),
    )


@dp.message(TrackForm.symbol)
async def receive_symbol(message: Message, state: FSMContext):
    data = await state.get_data()
    kind = data["kind"]
    info = KINDS[kind]

    symbol = normalize_symbol(kind, message.text)

    if not symbol:
        await message.answer(
            "Не похоже на тикер. "
            f"Примеры: {info['hint']}."
        )
        return

    await message.answer("⏳ Получаю текущую цену...")

    try:
        result = await get_price(kind, symbol)
    except PriceError as exc:
        if exc.notfound:
            await message.answer(
                f"❌ Не нашёл {symbol} в разделе «{info['title']}».\n"
                "Проверь тикер и попробуй ещё раз."
            )
        else:
            await message.answer(
                "⚠️ Сейчас не удаётся получить цену: источники "
                "недоступны. Попробуй чуть позже."
            )
        return

    await state.update_data(
        symbol=symbol,
        price=result["price"],
        currency=result["currency"],
        name=result["name"] or "",
    )
    await state.set_state(TrackForm.target)

    await message.answer(
        target_prompt(result["price"], result["currency"]),
        reply_markup=kb(["Пропустить"], per_row=1),
    )


@dp.message(TrackForm.target)
async def receive_target(message: Message, state: FSMContext):
    data = await state.get_data()
    raw = (message.text or "").strip()

    if raw.casefold() == "пропустить":
        mode, value, direction = "pct", float(DEFAULT_MOVE_PERCENT), "both"
    else:
        try:
            mode, value, direction = parse_target(raw, data["price"])
        except TargetError as exc:
            await message.answer(str(exc))
            return

    await state.update_data(
        target_mode=mode, target_value=value, target_dir=direction
    )
    await state.set_state(TrackForm.interval)

    await message.answer(
        "⏱ Как часто проверять цену?",
        reply_markup=kb(list(INTERVALS), per_row=2),
    )


@dp.message(TrackForm.interval)
async def receive_interval(message: Message, state: FSMContext):
    interval = INTERVALS.get(message.text or "")

    if not interval:
        await message.answer("Выбери интервал кнопкой.")
        return

    if (
        interval < FREE_MIN_INTERVAL
        and not subscription_active(message.from_user.id)
    ):
        await message.answer(
            "Без подписки минимальный интервал — "
            f"{interval_text(FREE_MIN_INTERVAL)}. Выбери другой "
            "или оформи подписку в разделе «💳 Подписка»."
        )
        return

    await state.update_data(interval=interval)
    await state.set_state(TrackForm.duration)

    await message.answer(
        "📅 Как долго следить?\nМаксимум — 30 дней.",
        reply_markup=kb(list(DURATIONS), per_row=2),
    )


@dp.message(TrackForm.duration)
async def receive_duration(message: Message, state: FSMContext):
    duration = DURATIONS.get(message.text or "")

    if not duration:
        await message.answer("Выбери срок кнопкой.")
        return

    data = await state.get_data()
    interval = data["interval"]

    if interval > duration:
        await message.answer(
            "Интервал проверки не может быть больше срока "
            "отслеживания. Выбери другой срок."
        )
        return

    user_id = message.from_user.id

    # Лимит могли исчерпать, пока шёл мастер
    if active_track_count(user_id) >= track_limit(user_id):
        await state.clear()
        await message.answer(
            "Лимит отслеживаний исчерпан.",
            reply_markup=keyboard_for(user_id),
        )
        return

    now = int(time.time())

    run("""
        INSERT INTO tracks (
            user_id, kind, symbol, display_name, currency,
            target_mode, target_value, target_dir,
            base_price, last_price,
            interval_seconds, expires_at, next_check, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id, data["kind"], data["symbol"], data["name"], data["currency"],
        data["target_mode"], data["target_value"], data["target_dir"],
        data["price"], data["price"],
        interval, now + duration, now + interval, now,
    ))

    await state.clear()

    preview = {
        "target_mode": data["target_mode"],
        "target_value": data["target_value"],
        "target_dir": data["target_dir"],
        "base_price": data["price"],
        "currency": data["currency"],
    }

    title = data["symbol"]
    if data["name"] and data["name"].upper() != title:
        title = f"{title} ({data['name'][:40]})"

    await message.answer(
        "✅ Отслеживание добавлено!\n\n"
        f"{KINDS[data['kind']]['title']}\n"
        f"Актив: {title}\n"
        f"Текущая цена: {fmt_price(data['price'], data['currency'])}\n"
        f"Цель: {target_text(preview)}\n"
        f"Проверка: каждые {interval_text(interval)}\n"
        f"Следим до: {fmt_date(now + duration)}",
        reply_markup=keyboard_for(user_id),
    )


# ============================================================
# 11. МОИ АКТИВЫ
# ============================================================

def own_track(track_id, user_id):
    row = one("SELECT * FROM tracks WHERE id = ?", (track_id,))
    if row and row["user_id"] == user_id and row["active"]:
        return row
    return None


@dp.callback_query(F.data.startswith("deltrack:"))
async def delete_track(callback: CallbackQuery):
    try:
        track_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный номер.")
        return

    if not own_track(track_id, callback.from_user.id):
        await callback.answer("Не найдено.")
        return

    run("UPDATE tracks SET active = 0 WHERE id = ?", (track_id,))

    await callback.message.edit_text("🗑 Отслеживание отключено.")
    await callback.answer("Удалено.")


@dp.callback_query(F.data.startswith("check:"))
async def check_now(callback: CallbackQuery):
    try:
        track_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный номер.")
        return

    row = own_track(track_id, callback.from_user.id)
    if not row:
        await callback.answer("Не найдено.")
        return

    now = time.time()
    if now - check_cooldown.get(callback.from_user.id, 0) < 10:
        await callback.answer("Подожди 10 секунд.", show_alert=True)
        return
    check_cooldown[callback.from_user.id] = now

    await callback.answer("Проверяю...")

    try:
        result = await get_price(row["kind"], row["symbol"], force=True)
    except Exception:
        await callback.message.answer(
            f"⚠️ {asset_title(row)}: сейчас не удалось получить цену."
        )
        return

    price = result["price"]
    run("UPDATE tracks SET last_price = ? WHERE id = ?", (price, track_id))

    await callback.message.answer(
        f"🔍 {asset_title(row)}: {fmt_price(price, row['currency'])} "
        f"({fmt_change(row['base_price'], price)} с начала)"
    )


# ============================================================
# 12. МОНИТОРИНГ ЦЕН (фоновая задача)
# ============================================================

async def safe_send(user_id, text):
    try:
        await bot.send_message(user_id, text)
    except TelegramAPIError as exc:
        print(f"[SEND ERROR] {user_id}: {exc}")


async def finish_expired(now):
    expired = run(
        "SELECT * FROM tracks WHERE active = 1 AND expires_at <= ?",
        (now,),
    )

    for row in expired:
        run("UPDATE tracks SET active = 0 WHERE id = ?", (row["id"],))
        await safe_send(
            row["user_id"],
            "⌛ Отслеживание завершено\n\n"
            f"Актив: {asset_title(row)}\n"
            f"Последняя цена: "
            f"{fmt_price(row['last_price'], row['currency'])} "
            f"({fmt_change(row['base_price'], row['last_price'])} с начала)",
        )


async def process_track(row, outcome, now):
    """outcome — результат fetch_price или исключение."""
    title = asset_title(row)

    # --- цену получить не удалось ---
    if isinstance(outcome, Exception):
        since = row["fail_since"] or now
        limit = max(FAIL_WARN_MIN_SECONDS, 3 * row["interval_seconds"])
        warn = (not row["warned_fail"]) and now - since >= limit

        run(
            "UPDATE tracks SET fail_since = ?, warned_fail = ? WHERE id = ?",
            (since, 1 if (warn or row["warned_fail"]) else 0, row["id"]),
        )

        if warn:
            await safe_send(
                row["user_id"],
                "⚠️ Не получается узнать цену\n\n"
                f"Актив: {title}\n"
                "Источники данных долго недоступны. "
                "Отслеживание продолжается, как только они "
                "заработают, проверки возобновятся.",
            )
        return

    # --- цена получена ---
    price = outcome["price"]
    side = price_side(row, price)

    notified = row["notified_target"]
    last_notified_at = row["last_notified_at"]
    notify = False

    if side and not notified and now - last_notified_at >= NOTIFY_COOLDOWN:
        notify, notified, last_notified_at = True, 1, now
    elif not side and notified:
        notified = 0  # цена отошла от цели — снова ждём

    run("""
        UPDATE tracks
        SET last_price = ?, notified_target = ?, last_notified_at = ?,
            fail_since = 0, warned_fail = 0
        WHERE id = ?
    """, (price, notified, last_notified_at, row["id"]))

    if notify:
        emoji = "📈" if side == "up" else "📉"
        await safe_send(
            row["user_id"],
            "🔔 Цель достигнута!\n\n"
            f"{emoji} {title}\n"
            f"Сейчас: {fmt_price(price, row['currency'])}\n"
            f"При добавлении: "
            f"{fmt_price(row['base_price'], row['currency'])} "
            f"({fmt_change(row['base_price'], price)})\n"
            f"Цель: {target_text(row)}",
        )


async def check_prices_loop():
    semaphore = asyncio.Semaphore(MAX_PARALLEL_FETCH)

    async def fetch_one(key):
        async with semaphore:
            try:
                return key, await get_price(*key)
            except Exception as exc:
                return key, exc

    while True:
        try:
            now = int(time.time())

            await finish_expired(now)

            due = run("""
                SELECT * FROM tracks
                WHERE active = 1 AND next_check <= ?
                ORDER BY next_check
                LIMIT 300
            """, (now,))

            if due:
                # Каждый тикер запрашиваем один раз, даже если его
                # отслеживают десятки человек.
                keys = {(row["kind"], row["symbol"]) for row in due}
                results = dict(
                    await asyncio.gather(*(fetch_one(key) for key in keys))
                )

                for row in due:
                    fresh = one(
                        "SELECT active FROM tracks WHERE id = ?", (row["id"],)
                    )
                    if not fresh or not fresh["active"]:
                        continue

                    run(
                        "UPDATE tracks SET next_check = ? WHERE id = ?",
                        (now + row["interval_seconds"], row["id"]),
                    )

                    try:
                        await process_track(
                            row,
                            results[(row["kind"], row["symbol"])],
                            now,
                        )
                    except Exception as exc:
                        print(f"[TRACK ERROR] #{row['id']}: "
                              f"{type(exc).__name__}: {exc}")

                    await asyncio.sleep(0.05)

            purge_cache()

        except Exception as exc:
            print(f"[MONITOR ERROR] {type(exc).__name__}: {exc}")

        await asyncio.sleep(LOOP_SLEEP)


# ============================================================
# 13. АДМИН: ВХОД
# ============================================================

# Команды /admin и /админ объявлены выше, перед мастером добавления (см. раздел 9б).


@dp.message(AdminForm.password)
async def admin_password(message: Message, state: FSMContext):
    user_id = message.from_user.id

    if user_id not in ADMIN_IDS:
        await state.clear()
        await message.answer("Доступ запрещён.")
        return

    attempts = admin_attempts.setdefault(user_id, [0, 0])

    if attempts[1] > time.time():
        await message.answer("Слишком много попыток. Попробуй позже.")
        return

    if (message.text or "").strip() != ADMIN_PASSWORD:
        attempts[0] += 1

        if attempts[0] >= ADMIN_MAX_ATTEMPTS:
            attempts[0] = 0
            attempts[1] = time.time() + ADMIN_LOCK_SECONDS
            await state.clear()
            await message.answer(
                "Слишком много попыток. Вход заблокирован на 10 минут.",
                reply_markup=main_keyboard(),
            )
        else:
            await message.answer("Неверный пароль.")
        return

    admin_attempts.pop(user_id, None)
    admin_sessions.add(user_id)
    admin_view.add(user_id)
    await state.clear()

    # Пароль лучше не оставлять в чате
    try:
        await message.delete()
    except TelegramAPIError:
        pass

    await message.answer("🔐 Админ-панель открыта.", reply_markup=admin_keyboard())


@dp.message(F.text == "🚪 Выйти из админки")
async def admin_logout(message: Message):
    admin_sessions.discard(message.from_user.id)
    admin_view.discard(message.from_user.id)
    await message.answer("Ты вышел из админки.", reply_markup=main_keyboard())


@dp.message(F.text == "🏠 Главное меню")
async def back_to_main(message: Message, state: FSMContext):
    await state.clear()
    admin_view.discard(message.from_user.id)
    await message.answer("Главное меню.", reply_markup=main_keyboard())


# ============================================================
# 14. АДМИН: СПИСКИ, СТАТИСТИКА, ТЕСТ ЦЕНЫ
# ============================================================

async def send_chunks(message: Message, lines):
    chunk = ""

    for line in lines:
        if len(chunk) + len(line) > 3500:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"

    if chunk:
        await message.answer(chunk)


@dp.message(F.text == "👥 Пользователи")
async def admin_users(message: Message):
    if not is_admin(message.from_user.id):
        return

    rows = run("""
        SELECT user_id, username, banned, subscription_until
        FROM users ORDER BY created_at DESC
    """)

    if not rows:
        await message.answer("Пользователей пока нет.")
        return

    now = int(time.time())
    lines = [f"👥 Пользователей: {len(rows)}\n"]

    for row in rows:
        username = f"@{row['username']}" if row["username"] else "без username"
        status = "БАН" if row["banned"] else "активен"
        sub = (
            f"подписка до {fmt_date(row['subscription_until'])}"
            if row["subscription_until"] > now else "без подписки"
        )
        lines.append(f"ID: {row['user_id']} | {username} | {status} | {sub}")

    await send_chunks(message, lines)


@dp.message(F.text == "📦 Все отслеживания")
async def admin_tracks(message: Message):
    if not is_admin(message.from_user.id):
        return

    rows = run("""
        SELECT id, user_id, kind, symbol, currency, last_price, active
        FROM tracks ORDER BY id DESC LIMIT 100
    """)

    if not rows:
        await message.answer("Отслеживаний пока нет.")
        return

    lines = ["📦 Последние 100 отслеживаний:\n"]

    for row in rows:
        lines.append(
            f"#{row['id']} | user={row['user_id']} | "
            f"{row['kind']} {row['symbol']} | "
            f"{fmt_price(row['last_price'], row['currency'])} | "
            f"active={row['active']}"
        )

    await send_chunks(message, lines)


@dp.message(F.text == "📊 Статистика")
async def admin_stats(message: Message):
    if not is_admin(message.from_user.id):
        return

    now = int(time.time())

    def count(sql, params=()):
        return one(sql, params)["n"]

    by_kind = run("""
        SELECT kind, COUNT(*) AS n FROM tracks
        WHERE active = 1 GROUP BY kind
    """)
    kinds_text = "\n".join(
        f"   • {KINDS[r['kind']]['title']}: {r['n']}"
        for r in by_kind if r["kind"] in KINDS
    ) or "   —"

    await message.answer(
        "📊 Статистика\n\n"
        f"Пользователей: {count('SELECT COUNT(*) AS n FROM users')}\n"
        f"Заблокировано: "
        f"{count('SELECT COUNT(*) AS n FROM users WHERE banned = 1')}\n"
        f"Активных подписок: "
        f"{count('SELECT COUNT(*) AS n FROM users WHERE subscription_until > ?', (now,))}\n"
        f"Активных отслеживаний: "
        f"{count('SELECT COUNT(*) AS n FROM tracks WHERE active = 1')}\n"
        f"{kinds_text}\n"
        f"Всего за всё время: "
        f"{count('SELECT COUNT(*) AS n FROM tracks')}"
    )


@dp.message(F.text == "🧪 Тест цены")
async def admin_test_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(AdminForm.test_price)
    await message.answer(
        "Проверка источников цен. Напиши рынок и тикер:\n\n"
        "crypto BTC\nmoex SBER\nus AAPL\n\n"
        "Бот покажет, что ответил каждый источник.",
        reply_markup=kb([], cancel=True),
    )


@dp.message(AdminForm.test_price)
async def admin_test_price(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    parts = (message.text or "").split()

    if len(parts) != 2 or parts[0].lower() not in KINDS:
        await message.answer("Формат: crypto BTC, moex SBER или us AAPL.")
        return

    kind = parts[0].lower()
    symbol = normalize_symbol(kind, parts[1])

    if not symbol:
        await message.answer("Некорректный тикер.")
        return

    await state.clear()

    trace = []
    try:
        result = await fetch_price(kind, symbol, trace)
        verdict = (
            f"✅ {symbol}: {fmt_price(result['price'], result['currency'])}"
            f"\nНазвание: {result['name'] or '—'}"
        )
    except PriceError as exc:
        verdict = f"❌ {symbol}: {exc}"

    await message.answer(
        f"{verdict}\n\nХод проверки:\n" + "\n".join(trace),
        reply_markup=admin_keyboard(),
    )


# ============================================================
# 15. АДМИН: РАССЫЛКА
# ============================================================

@dp.message(F.text == "📣 Рассылка")
async def admin_broadcast_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(AdminForm.broadcast)
    await message.answer(
        "Отправь текст рассылки.\n"
        "Получат все незаблокированные пользователи.\n\n"
        "Отмена — кнопка ниже или /cancel.",
        reply_markup=kb([], cancel=True),
    )


@dp.message(AdminForm.broadcast)
async def admin_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    text = (message.text or "").strip()

    if not text:
        await message.answer("Отправь текстовое сообщение.")
        return

    await state.clear()

    rows = run("SELECT user_id FROM users WHERE banned = 0")
    sent = failed = 0

    await message.answer(f"Начинаю рассылку для {len(rows)} пользователей...")

    for row in rows:
        try:
            await bot.send_message(row["user_id"], text)
            sent += 1
        except TelegramAPIError:
            failed += 1

        await asyncio.sleep(0.05)

    await message.answer(
        f"📣 Рассылка завершена.\nУспешно: {sent}\nОшибок: {failed}",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# 16. АДМИН: БАН / РАЗБАН / ПОДПИСКА
# ============================================================

@dp.message(F.text == "🚫 Бан")
async def admin_ban_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(AdminForm.ban_id)
    await message.answer(
        "Введи Telegram ID пользователя для бана.",
        reply_markup=kb([], cancel=True),
    )


@dp.message(AdminForm.ban_id)
async def admin_ban(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    if user_id in ADMIN_IDS:
        await message.answer("Админа банить нельзя.")
        return

    if not get_user(user_id):
        await state.clear()
        await message.answer(
            "Пользователь не найден в базе.",
            reply_markup=admin_keyboard(),
        )
        return

    run("UPDATE users SET banned = 1 WHERE user_id = ?", (user_id,))
    run("UPDATE tracks SET active = 0 WHERE user_id = ?", (user_id,))

    await state.clear()
    await message.answer(
        f"🚫 Пользователь {user_id} заблокирован, "
        "его отслеживания остановлены.",
        reply_markup=admin_keyboard(),
    )


@dp.message(F.text == "✅ Разбан")
async def admin_unban_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(AdminForm.unban_id)
    await message.answer(
        "Введи Telegram ID пользователя для разбана.",
        reply_markup=kb([], cancel=True),
    )


@dp.message(AdminForm.unban_id)
async def admin_unban(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    if not get_user(user_id):
        await state.clear()
        await message.answer(
            "Пользователь не найден в базе.",
            reply_markup=admin_keyboard(),
        )
        return

    run("UPDATE users SET banned = 0 WHERE user_id = ?", (user_id,))

    await state.clear()
    await message.answer(
        f"✅ Пользователь {user_id} разблокирован.",
        reply_markup=admin_keyboard(),
    )


@dp.message(F.text == "⏳ Продлить подписку")
async def admin_extend_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(AdminForm.extend_id)
    await message.answer(
        "Введи Telegram ID пользователя, которому продлить подписку.",
        reply_markup=kb([], cancel=True),
    )


@dp.message(AdminForm.extend_id)
async def admin_extend_id(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    if not get_user(user_id):
        await state.clear()
        await message.answer(
            "Пользователь не найден. Он должен сначала запустить бота.",
            reply_markup=admin_keyboard(),
        )
        return

    await state.update_data(extend_user_id=user_id)
    await state.set_state(AdminForm.extend_days)
    await message.answer("На сколько дней продлить? Например: 30")


@dp.message(AdminForm.extend_days)
async def admin_extend_days(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    try:
        days = int((message.text or "").strip())
        if not 1 <= days <= 3650:
            raise ValueError
    except ValueError:
        await message.answer("Введи число дней от 1 до 3650.")
        return

    data = await state.get_data()
    user_id = data["extend_user_id"]

    row = get_user(user_id)
    base = max(row["subscription_until"], int(time.time()))
    new_until = base + days * 86400

    run(
        "UPDATE users SET subscription_until = ? WHERE user_id = ?",
        (new_until, user_id),
    )

    await state.clear()
    await message.answer(
        f"✅ Подписка пользователя {user_id} продлена на {days} дн.\n"
        f"Действует до: {fmt_date(new_until)}",
        reply_markup=admin_keyboard(),
    )

    await safe_send(
        user_id,
        "💳 Твоя подписка продлена!\n"
        f"Действует до: {fmt_date(new_until)}",
    )


# ============================================================
# 17. ВСЁ ОСТАЛЬНОЕ
# ============================================================

@dp.message()
async def unknown_message(message: Message):
    await message.answer(
        "Выбери действие кнопкой внизу.",
        reply_markup=keyboard_for(message.from_user.id),
    )


# ============================================================
# 18. ЗАПУСК
# ============================================================

async def main():
    global http

    init_db()
    import_old_users()

    http = httpx.AsyncClient(
        timeout=12,
        follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/csv, */*",
        },
    )

    print("Asset Tracker started.")
    print("Database:", DB_NAME)

    monitor = asyncio.create_task(check_prices_loop())

    try:
        await dp.start_polling(bot)
    finally:
        monitor.cancel()

        try:
            await monitor
        except asyncio.CancelledError:
            pass

        await http.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
