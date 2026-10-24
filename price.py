# ============================================================
# PRICE TRACKER BOT  (aiogram 3 + SQLite + httpx)
# Python 3.11+
#
# Установка:  pip install aiogram httpx beautifulsoup4
# Запуск:     python price_tracker_bot.py
#
# КАК ДОБАВЛЯТЬ ОБНОВЛЕНИЯ:
#   * новый магазин        -> SHOPS (раздел 1) + кнопка в shops_keyboard()
#   * парсер магазина      -> функция-адаптер в разделе 6 + запись в ADAPTERS
#   * новая кнопка/раздел  -> keyboards (3) + handler (по образцу раздела 8)
#   * новая админ-функция  -> admin_keyboard() + handler в разделах 13-16
# ============================================================

import asyncio
import json
import re
import sqlite3
import time
from datetime import datetime
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

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
SUPPORT_CONTACT = "@your_username"

DB_NAME = "price_tracker.db"

# Лимиты активных товаров
FREE_TRACK_LIMIT = 3
SUB_TRACK_LIMIT = 50

# Минимальный интервал проверки для бесплатных (сек).
# 3600 = без ограничений. Поставь 21600, чтобы бесплатным было от 6 часов.
FREE_MIN_INTERVAL = 3600

# Если цель не задана — уведомлять при падении цены на столько %
DEFAULT_DROP_PERCENT = 5

# Сколько неудачных чтений подряд до предупреждения пользователю
FAIL_WARN_AFTER = 5

# Защита админки от подбора пароля
ADMIN_MAX_ATTEMPTS = 5
ADMIN_LOCK_SECONDS = 600

INTERVALS = {
    "1 час": 3600,
    "3 часа": 10800,
    "6 часов": 21600,
    "12 часов": 43200,
    "1 день": 86400,
    "3 дня": 259200,
    "7 дней": 604800,
    "30 дней": 2592000,
}

DURATIONS = {
    "1 день": 86400,
    "7 дней": 7 * 86400,
    "14 дней": 14 * 86400,
    "30 дней": 30 * 86400,
}

# Магазины и их домены
SHOPS = {
    "Wildberries": ("wildberries.ru",),
    "Ozon": ("ozon.ru",),
    "Яндекс Маркет": ("market.yandex.ru",),
    "Мегамаркет": ("megamarket.ru",),
    "AliExpress": ("aliexpress.ru", "aliexpress.com"),
}


# ============================================================
# 2. БОТ И БАЗА ДАННЫХ
# ============================================================

if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬ_ТОКЕН_СЮДА":
    raise RuntimeError("Вставь токен бота в BOT_TOKEN вверху файла.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

admin_sessions = set()
admin_attempts = {}   # user_id -> [count, locked_until]
check_cooldown = {}   # user_id -> timestamp последней ручной проверки


def run(sql, params=()):
    """Выполняет SQL, возвращает список строк. Соединение закрывается."""
    conn = sqlite3.connect(DB_NAME)
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
    conn = sqlite3.connect(DB_NAME)
    try:
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
            shop TEXT NOT NULL,
            url TEXT NOT NULL,
            product_name TEXT,
            base_price REAL,
            last_price REAL,
            interval_seconds INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            next_check INTEGER NOT NULL,
            notified_target INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_tracks_due ON tracks(active, next_check);
        CREATE INDEX IF NOT EXISTS idx_tracks_user ON tracks(user_id);
        """)

        # Миграции для баз, созданных старой версией бота
        existing = {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}
        migrations = {
            "target_mode": "TEXT NOT NULL DEFAULT 'pct'",
            "target_value": f"REAL NOT NULL DEFAULT {DEFAULT_DROP_PERCENT}",
            "fail_count": "INTEGER NOT NULL DEFAULT 0",
            "warned_fail": "INTEGER NOT NULL DEFAULT 0",
        }
        for col, ddl in migrations.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE tracks ADD COLUMN {col} {ddl}")
        conn.commit()
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


def fmt_date(timestamp):
    if not timestamp:
        return "нет"
    return datetime.fromtimestamp(timestamp).strftime("%d.%m.%Y %H:%M")


def fmt_price(value):
    if value is None:
        return "неизвестна"
    return f"{value:,.2f}".replace(",", " ") + " ₽"


def interval_text(seconds):
    for label, value in INTERVALS.items():
        if value == seconds:
            return label
    return f"{seconds // 3600} ч."


# ============================================================
# 3. КЛАВИАТУРЫ
# ============================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить товар"),
             KeyboardButton(text="📦 Мои товары")],
            [KeyboardButton(text="💳 Подписка"),
             KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
    )


def shops_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Wildberries"), KeyboardButton(text="Ozon")],
            [KeyboardButton(text="Яндекс Маркет"), KeyboardButton(text="Мегамаркет")],
            [KeyboardButton(text="AliExpress"), KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


def skip_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Пропустить"),
                   KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


def cancel_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


def interval_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="1 час"), KeyboardButton(text="3 часа")],
            [KeyboardButton(text="6 часов"), KeyboardButton(text="12 часов")],
            [KeyboardButton(text="1 день"), KeyboardButton(text="3 дня")],
            [KeyboardButton(text="7 дней"), KeyboardButton(text="30 дней")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


def duration_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="1 день"), KeyboardButton(text="7 дней")],
            [KeyboardButton(text="14 дней"), KeyboardButton(text="30 дней")],
            [KeyboardButton(text="❌ Отмена")],
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
             KeyboardButton(text="🏠 Главное меню")],
            [KeyboardButton(text="🚪 Выйти из админки")],
        ],
        resize_keyboard=True,
    )


def keyboard_for(user_id):
    return admin_keyboard() if is_admin(user_id) else main_keyboard()


# ============================================================
# 4. СОСТОЯНИЯ (FSM)
# ============================================================

class TrackForm(StatesGroup):
    shop = State()
    url = State()
    name = State()
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
# 6. ПРОВЕРКА ССЫЛОК И ПОЛУЧЕНИЕ ЦЕНЫ
#
# Каждый магазин может иметь свой адаптер в ADAPTERS.
# Адаптер: async def fn(client, url) -> {"price": float, "name": str|None} | None
# Если адаптера нет или он не сработал — работает общий парсер HTML.
#
# ВАЖНО: Ozon, Яндекс Маркет, Мегамаркет и AliExpress часто отдают
# антибот-страницу или рисуют цену через JS — общий парсер там может
# не работать. Бот не обходит капчу/защиту; для таких магазинов
# нужны свои адаптеры (официальные API/партнёрские фиды).
# ============================================================

def host_allowed(shop, url):
    try:
        host = (urlparse(url.strip()).hostname or "").lower()
    except Exception:
        return False
    return any(
        host == d or host.endswith("." + d)
        for d in SHOPS.get(shop, ())
    )


def valid_shop_url(shop, url):
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme in ("http", "https") and host_allowed(shop, url)
    except Exception:
        return False


def to_float(value):
    try:
        number = float(str(value).replace(" ", "").replace(",", "."))
    except (ValueError, TypeError):
        return None
    return number if 0 < number < 100_000_000 else None


def find_prices(obj):
    """Рекурсивно ищет цены (price / lowPrice) в JSON-LD."""
    found = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("price", "lowPrice"):
                price = to_float(value)
                if price:
                    found.append(price)
            elif isinstance(value, (dict, list)):
                found.extend(find_prices(value))

    elif isinstance(obj, list):
        for item in obj:
            found.extend(find_prices(item))

    return found


def parse_product_page(html):
    """Общий парсер: JSON-LD, затем meta-теги."""
    soup = BeautifulSoup(html, "html.parser")
    name = None

    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        name = og["content"].strip()[:150]

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        prices = find_prices(data)
        if prices:
            return {"price": min(prices), "name": name}

    for attrs in (
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
        {"itemprop": "price"},
        {"name": "price"},
    ):
        element = soup.find("meta", attrs=attrs)
        if element:
            price = to_float(element.get("content"))
            if price:
                return {"price": price, "name": name}

    return None


async def adapter_wildberries(client, url):
    """
    Wildberries: цена берётся из публичного JSON карточки товара.
    Формат ответа WB меняется — при поломке поправь только эту функцию.
    """
    match = re.search(r"/catalog/(\d+)", url)
    if not match:
        return None

    response = await client.get(
        "https://card.wb.ru/cards/v2/detail",
        params={
            "appType": 1,
            "curr": "rub",
            "dest": -1257786,
            "spp": 30,
            "nm": match.group(1),
        },
    )
    response.raise_for_status()

    products = (response.json().get("data") or {}).get("products") or []
    if not products:
        return None

    product = products[0]
    kopecks = None

    for size in product.get("sizes") or []:
        price_block = size.get("price") or {}
        kopecks = price_block.get("product") or price_block.get("total")
        if kopecks:
            break

    if not kopecks:
        kopecks = product.get("salePriceU")  # старый формат

    price = to_float(kopecks)
    if not price:
        return None

    return {"price": price / 100, "name": (product.get("name") or "")[:150] or None}


ADAPTERS = {
    "Wildberries": adapter_wildberries,
    # "Ozon": adapter_ozon,   # добавишь позже по образцу выше
}


async def fetch_price(shop, url):
    """Возвращает {"price", "name"} или None, если цену прочитать не удалось."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    }

    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=True, headers=headers
        ) as client:
            adapter = ADAPTERS.get(shop)

            if adapter:
                try:
                    result = await adapter(client, url)
                    if result:
                        return result
                except Exception as exc:
                    print(f"[ADAPTER ERROR] {shop}: {type(exc).__name__}: {exc}")

            response = await client.get(url)
            response.raise_for_status()

            # Не доверяем редиректам на чужие домены
            if not host_allowed(shop, str(response.url)):
                return None

            return parse_product_page(response.text)

    except Exception as exc:
        print(f"[PRICE ERROR] {shop}: {type(exc).__name__}: {exc}")
        return None


# ============================================================
# 7. ЦЕЛЕВАЯ ЦЕНА / РАЗНИЦА
#
# Режимы target_mode:
#   price — ждём цену не выше target_value      («5000»)
#   abs   — падение на N ₽ от стартовой цены     («-500»)
#   pct   — падение на N % от стартовой цены     («-10%» или «10%»)
# ============================================================

def parse_target(raw):
    text = (
        raw.strip().lower()
        .replace(" ", "").replace("₽", "").replace("руб", "")
        .replace(",", ".").replace("−", "-").replace("–", "-")
    )

    try:
        if text.endswith("%"):
            value = float(text[:-1].lstrip("-"))
            if not 0 < value < 100:
                return None
            return "pct", value

        if text.startswith("-"):
            value = float(text[1:])
            if value <= 0:
                return None
            return "abs", value

        value = float(text)
        if not 1 <= value <= 100_000_000:
            return None
        return "price", value

    except ValueError:
        return None


def threshold(row):
    """Цена, при которой нужно уведомить. None — пока неизвестна."""
    mode, value, base = row["target_mode"], row["target_value"], row["base_price"]

    if mode == "price":
        return value
    if base is None:
        return None
    if mode == "abs":
        return base - value
    if mode == "pct":
        return base * (1 - value / 100)
    return None


def target_text(row):
    mode, value = row["target_mode"], row["target_value"]
    limit = threshold(row)

    if mode == "price":
        return f"не выше {fmt_price(value)}"

    if mode == "abs":
        text = f"падение на {value:g} ₽"
    else:
        text = f"падение на {value:g}%"

    if limit is not None:
        text += f" (≤ {fmt_price(limit)})"
    return text


# ============================================================
# 8. СТАРТ, ОТМЕНА, ПОМОЩЬ
# ============================================================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()

    await message.answer(
        "👋 Привет! Это Price Tracker.\n\n"
        "Я слежу за ценами на товары в российских магазинах "
        "и пишу, когда цена достигнет нужного значения.\n\n"
        "Нажми «➕ Добавить товар», чтобы начать.",
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
        "1. «Добавить товар» → выбери магазин.\n"
        "2. Отправь ссылку на товар.\n"
        "3. Название — по желанию.\n"
        "4. Укажи цель:\n"
        "   • 5000 — уведомить, когда цена ≤ 5000 ₽\n"
        "   • -500 — когда цена упадёт на 500 ₽\n"
        "   • -10% — когда цена упадёт на 10%\n"
        f"   • «Пропустить» — падение на {DEFAULT_DROP_PERCENT}%\n"
        "5. Выбери, как часто проверять и как долго следить "
        "(до 30 дней).\n\n"
        "В «Мои товары» можно проверить цену сразу или удалить слежение.\n\n"
        "Важно: если магазин скрывает цену от ботов, автоматическое "
        "чтение может не работать — бот сообщит об этом.",
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

    free_interval = interval_text(FREE_MIN_INTERVAL)

    await message.answer(
        "💳 Твоя подписка\n\n"
        f"Статус: {status}\n\n"
        f"Без подписки: до {FREE_TRACK_LIMIT} товаров, "
        f"проверка от {free_interval}.\n"
        f"С подпиской: до {SUB_TRACK_LIMIT} товаров, "
        "проверка от 1 часа.\n\n"
        f"Подключить или продлить: {SUPPORT_CONTACT}\n"
        f"Твой ID: {message.from_user.id}",
        reply_markup=keyboard_for(message.from_user.id),
    )


# ============================================================
# 10. ДОБАВЛЕНИЕ ТОВАРА (МАСТЕР)
# ============================================================

@dp.message(F.text == "➕ Добавить товар")
async def add_track(message: Message, state: FSMContext):
    user_id = message.from_user.id
    limit = track_limit(user_id)

    if active_track_count(user_id) >= limit:
        await message.answer(
            f"Достигнут лимит: {limit} активных товаров.\n"
            "Удали ненужные в «📦 Мои товары» или оформи подписку "
            "в разделе «💳 Подписка»."
        )
        return

    await state.clear()
    await state.set_state(TrackForm.shop)
    await message.answer("Выбери магазин:", reply_markup=shops_keyboard())


@dp.message(TrackForm.shop)
async def select_shop(message: Message, state: FSMContext):
    if message.text not in SHOPS:
        await message.answer("Выбери магазин кнопкой ниже.")
        return

    await state.update_data(shop=message.text)
    await state.set_state(TrackForm.url)

    await message.answer(
        "Отправь полную ссылку на товар (из браузера или «Поделиться» "
        "в приложении магазина).\n\n"
        "Ссылка должна вести на выбранный магазин.",
        reply_markup=cancel_keyboard(),
    )


@dp.message(TrackForm.url)
async def receive_url(message: Message, state: FSMContext):
    data = await state.get_data()
    url = (message.text or "").strip()

    if not valid_shop_url(data["shop"], url):
        await message.answer(
            "❌ Ссылка не подходит выбранному магазину.\n"
            "Отправь правильную ссылку или нажми «Отмена»."
        )
        return

    await state.update_data(url=url)
    await state.set_state(TrackForm.name)

    await message.answer(
        "Введи название товара для удобства или нажми «Пропустить».",
        reply_markup=skip_keyboard(),
    )


@dp.message(TrackForm.name)
async def receive_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()

    if name.casefold() == "пропустить":
        name = ""

    await state.update_data(name=name[:150])
    await state.set_state(TrackForm.target)

    await message.answer(
        "🎯 Когда уведомить?\n\n"
        "• 5000 — когда цена станет не выше 5000 ₽\n"
        "• -500 — когда цена упадёт на 500 ₽\n"
        "• -10% — когда цена упадёт на 10%\n\n"
        f"«Пропустить» — уведомлю при падении на {DEFAULT_DROP_PERCENT}%.",
        reply_markup=skip_keyboard(),
    )


@dp.message(TrackForm.target)
async def receive_target(message: Message, state: FSMContext):
    raw = (message.text or "").strip()

    if raw.casefold() == "пропустить":
        mode, value = "pct", float(DEFAULT_DROP_PERCENT)
    else:
        parsed = parse_target(raw)

        if not parsed:
            await message.answer(
                "Не понял. Примеры: 5000, -500, -10%.\n"
                "Или нажми «Пропустить»."
            )
            return

        mode, value = parsed

    await state.update_data(target_mode=mode, target_value=value)
    await state.set_state(TrackForm.interval)

    await message.answer(
        "⏱ Как часто проверять цену?\n"
        "От 1 часа до 30 дней.",
        reply_markup=interval_keyboard(),
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
            f"Без подписки минимальный интервал — "
            f"{interval_text(FREE_MIN_INTERVAL)}. Выбери другой."
        )
        return

    await state.update_data(interval=interval)
    await state.set_state(TrackForm.duration)

    await message.answer(
        "📅 Как долго следить за товаром?\nМаксимум — 30 дней.",
        reply_markup=duration_keyboard(),
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

    # Защита от гонки: лимит могли исчерпать, пока шёл мастер
    if active_track_count(message.from_user.id) >= track_limit(message.from_user.id):
        await state.clear()
        await message.answer(
            "Лимит товаров исчерпан.",
            reply_markup=keyboard_for(message.from_user.id),
        )
        return

    await message.answer("⏳ Проверяю, удастся ли прочитать цену...")

    result = await fetch_price(data["shop"], data["url"])
    price = result["price"] if result else None
    name = data["name"] or (result["name"] if result and result["name"] else "")

    now = int(time.time())

    run("""
        INSERT INTO tracks (
            user_id, shop, url, product_name,
            target_mode, target_value,
            base_price, last_price,
            interval_seconds, expires_at, next_check
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        message.from_user.id, data["shop"], data["url"], name,
        data["target_mode"], data["target_value"],
        price, price,
        interval, now + duration, now + interval,
    ))

    await state.clear()

    if price is None:
        price_status = (
            "⚠️ Цену сейчас прочитать не удалось. Бот будет пробовать "
            "дальше; если не получится — напишу."
        )
    else:
        price_status = f"Текущая цена: {fmt_price(price)}"

    preview = {
        "target_mode": data["target_mode"],
        "target_value": data["target_value"],
        "base_price": price,
    }

    await message.answer(
        "✅ Товар добавлен!\n\n"
        f"Магазин: {data['shop']}\n"
        f"Название: {name or 'не указано'}\n"
        f"{price_status}\n"
        f"Цель: {target_text(preview)}\n"
        f"Проверка: каждые {interval_text(interval)}\n"
        f"Следим до: {fmt_date(now + duration)}",
        reply_markup=keyboard_for(message.from_user.id),
    )


# ============================================================
# 11. МОИ ТОВАРЫ
# ============================================================

@dp.message(F.text == "📦 Мои товары")
async def my_tracks(message: Message):
    rows = run("""
        SELECT * FROM tracks
        WHERE user_id = ? AND active = 1
        ORDER BY id DESC
    """, (message.from_user.id,))

    if not rows:
        await message.answer(
            "У тебя пока нет активных товаров.",
            reply_markup=keyboard_for(message.from_user.id),
        )
        return

    for row in rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
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
            f"📦 Товар №{row['id']}\n"
            f"Магазин: {row['shop']}\n"
            f"Название: {row['product_name'] or 'не указано'}\n"
            f"Цена: {fmt_price(row['last_price'])}\n"
            f"Цель: {target_text(row)}\n"
            f"Следующая проверка: {fmt_date(row['next_check'])}\n"
            f"Следим до: {fmt_date(row['expires_at'])}\n"
            f"{row['url']}",
            reply_markup=kb,
            disable_web_page_preview=True,
        )


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
        await callback.answer("Товар не найден.")
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
        await callback.answer("Товар не найден.")
        return

    now = time.time()
    if now - check_cooldown.get(callback.from_user.id, 0) < 30:
        await callback.answer("Подожди 30 секунд между проверками.", show_alert=True)
        return
    check_cooldown[callback.from_user.id] = now

    await callback.answer("Проверяю...")

    result = await fetch_price(row["shop"], row["url"])

    if not result:
        await callback.message.answer(
            f"⚠️ Товар №{track_id}: цену прочитать не удалось."
        )
        return

    price = result["price"]
    run("""
        UPDATE tracks
        SET last_price = ?, base_price = COALESCE(base_price, ?)
        WHERE id = ?
    """, (price, price, track_id))

    await callback.message.answer(
        f"🔍 Товар №{track_id}: сейчас {fmt_price(price)}"
    )


# ============================================================
# 12. МОНИТОРИНГ ЦЕН (фоновая задача)
# ============================================================

async def safe_send(user_id, text):
    try:
        await bot.send_message(user_id, text, disable_web_page_preview=True)
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
            f"Товар: {row['product_name'] or row['shop']}\n"
            f"Последняя цена: {fmt_price(row['last_price'])}\n"
            f"{row['url']}",
        )


async def process_track(row):
    result = await fetch_price(row["shop"], row["url"])

    # --- цену прочитать не удалось ---
    if result is None:
        fails = row["fail_count"] + 1
        warn = fails >= FAIL_WARN_AFTER and not row["warned_fail"]

        run(
            "UPDATE tracks SET fail_count = ?, warned_fail = ? WHERE id = ?",
            (fails, 1 if (warn or row["warned_fail"]) else 0, row["id"]),
        )

        if warn:
            await safe_send(
                row["user_id"],
                "⚠️ Не получается прочитать цену\n\n"
                f"Товар: {row['product_name'] or row['shop']}\n"
                f"Подряд неудачных проверок: {fails}.\n"
                "Возможно, магазин блокирует автоматический доступ. "
                "Проверь цену вручную:\n"
                f"{row['url']}",
            )
        return

    price = result["price"]
    base = row["base_price"] if row["base_price"] is not None else price

    current = dict(row)
    current["base_price"] = base
    limit = threshold(current)

    reached = limit is not None and price <= limit
    notified = row["notified_target"]
    notify = False

    if reached and not notified:
        notify, notified = True, 1
    elif not reached and notified:
        notified = 0  # цена вернулась выше цели — снова ждём

    run("""
        UPDATE tracks
        SET last_price = ?, base_price = ?, notified_target = ?,
            fail_count = 0, warned_fail = 0
        WHERE id = ?
    """, (price, base, notified, row["id"]))

    if notify:
        await safe_send(
            row["user_id"],
            "🔔 Цена достигла цели!\n\n"
            f"Товар: {row['product_name'] or row['shop']}\n"
            f"Сейчас: {fmt_price(price)}\n"
            f"Было при добавлении: {fmt_price(base)}\n"
            f"Цель: {target_text(current)}\n\n"
            f"{row['url']}",
        )


async def check_prices_loop():
    while True:
        try:
            now = int(time.time())

            await finish_expired(now)

            due = run("""
                SELECT * FROM tracks
                WHERE active = 1 AND next_check <= ?
                ORDER BY next_check
                LIMIT 25
            """, (now,))

            for row in due:
                # Сначала планируем следующую проверку,
                # чтобы сбой не вызвал бесконечные повторы
                run(
                    "UPDATE tracks SET next_check = ? WHERE id = ?",
                    (now + row["interval_seconds"], row["id"]),
                )

                try:
                    await process_track(row)
                except Exception as exc:
                    print(f"[TRACK ERROR] #{row['id']}: "
                          f"{type(exc).__name__}: {exc}")

                await asyncio.sleep(1)

        except Exception as exc:
            print(f"[MONITOR ERROR] {type(exc).__name__}: {exc}")

        await asyncio.sleep(60)


# ============================================================
# 13. АДМИН: ВХОД
# ============================================================

def is_admin(user_id):
    return user_id in ADMIN_IDS and user_id in admin_sessions


async def begin_admin(message: Message, state: FSMContext):
    user_id = message.from_user.id

    if user_id not in ADMIN_IDS:
        await message.answer("Доступ запрещён.")
        return

    if user_id in admin_sessions:
        await state.clear()
        await message.answer("🔐 Админ-панель.", reply_markup=admin_keyboard())
        return

    locked_until = admin_attempts.get(user_id, [0, 0])[1]
    if locked_until > time.time():
        await message.answer("Слишком много попыток. Попробуй позже.")
        return

    await state.clear()
    await state.set_state(AdminForm.password)
    await message.answer(
        "Введи пароль администратора:",
        reply_markup=cancel_keyboard(),
    )


@dp.message(Command("admin"))
async def admin_command(message: Message, state: FSMContext):
    await begin_admin(message, state)


@dp.message(F.text.casefold() == "/админ")
async def admin_cyrillic_command(message: Message, state: FSMContext):
    await begin_admin(message, state)


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
    await state.clear()

    # Пароль в чате лучше удалить
    try:
        await message.delete()
    except TelegramAPIError:
        pass

    await message.answer("🔐 Админ-панель открыта.", reply_markup=admin_keyboard())


@dp.message(F.text == "🚪 Выйти из админки")
async def admin_logout(message: Message):
    admin_sessions.discard(message.from_user.id)
    await message.answer("Ты вышел из админки.", reply_markup=main_keyboard())


@dp.message(F.text == "🏠 Главное меню")
async def back_to_main(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню.", reply_markup=main_keyboard())


# ============================================================
# 14. АДМИН: СПИСКИ И СТАТИСТИКА
# ============================================================

async def send_chunks(message: Message, lines):
    chunk = ""

    for line in lines:
        if len(chunk) + len(line) > 3500:
            await message.answer(chunk, disable_web_page_preview=True)
            chunk = ""
        chunk += line + "\n"

    if chunk:
        await message.answer(chunk, disable_web_page_preview=True)


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
        SELECT id, user_id, shop, product_name, active, last_price, url
        FROM tracks ORDER BY id DESC LIMIT 100
    """)

    if not rows:
        await message.answer("Отслеживаний пока нет.")
        return

    lines = ["📦 Последние 100 отслеживаний:\n"]

    for row in rows:
        lines.append(
            f"#{row['id']} | user={row['user_id']} | {row['shop']} | "
            f"{row['product_name'] or 'без названия'} | "
            f"{fmt_price(row['last_price'])} | active={row['active']}\n"
            f"{row['url']}\n"
        )

    await send_chunks(message, lines)


@dp.message(F.text == "📊 Статистика")
async def admin_stats(message: Message):
    if not is_admin(message.from_user.id):
        return

    now = int(time.time())

    def count(sql, params=()):
        return one(sql, params)["n"]

    await message.answer(
        "📊 Статистика\n\n"
        f"Пользователей: {count('SELECT COUNT(*) AS n FROM users')}\n"
        f"Заблокировано: {count('SELECT COUNT(*) AS n FROM users WHERE banned = 1')}\n"
        f"Активных подписок: "
        f"{count('SELECT COUNT(*) AS n FROM users WHERE subscription_until > ?', (now,))}\n"
        f"Активных отслеживаний: "
        f"{count('SELECT COUNT(*) AS n FROM tracks WHERE active = 1')}\n"
        f"Всего отслеживаний за всё время: "
        f"{count('SELECT COUNT(*) AS n FROM tracks')}"
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
        "Отмена — /cancel.",
        reply_markup=cancel_keyboard(),
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
        reply_markup=cancel_keyboard(),
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
        f"🚫 Пользователь {user_id} заблокирован, его отслеживания остановлены.",
        reply_markup=admin_keyboard(),
    )


@dp.message(F.text == "✅ Разбан")
async def admin_unban_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(AdminForm.unban_id)
    await message.answer(
        "Введи Telegram ID пользователя для разбана.",
        reply_markup=cancel_keyboard(),
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
        reply_markup=cancel_keyboard(),
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
    init_db()

    print("Price Tracker started.")
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

        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
