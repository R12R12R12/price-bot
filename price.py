
# ============================================================
# PRICE TRACKER BOT
# Python 3.11+
# Telegram bot + SQLite + subscriptions + admin panel
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

from aiogram import Bot, Dispatcher, F
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
# 1. SETTINGS — EDIT THIS SECTION
# ============================================================

BOT_TOKEN = "8460061736:AAFdOGa74cPArtA1OwnvR7yNqYsd4cpS-mo"

# Put YOUR numeric Telegram ID here.
# Example: ADMIN_IDS = {123456789}
ADMIN_IDS = {123456789}

# Admin login password. Change before deploying.
ADMIN_PASSWORD = "2525"

DB_NAME = "price_tracker.db"

# Free accounts can track up to this many active products.
FREE_TRACK_LIMIT = 3

# Maximum tracking period: 30 days.
MAX_TRACK_DAYS = 30

# Default subscription length, in days.
DEFAULT_SUBSCRIPTION_DAYS = 30

# Price check interval options, in seconds.
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

# Supported shops and their expected domains.
SHOPS = {
    "Wildberries": ("wildberries.ru",),
    "Ozon": ("ozon.ru",),
    "Яндекс Маркет": ("market.yandex.ru",),
    "Мегамаркет": ("megamarket.ru",),
    "AliExpress": ("aliexpress.ru", "aliexpress.com"),
}


# ============================================================
# 2. BOT AND DATABASE
# ============================================================

if (
    not BOT_TOKEN
    or BOT_TOKEN == "PASTE_YOUR_TELEGRAM_BOT_TOKEN_HERE"
):
    raise RuntimeError("Вставь токен бота в BOT_TOKEN вверху файла.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

admin_sessions = set()


def db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as conn:
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
            target_price REAL,
            base_price REAL,
            last_price REAL,
            last_notified_price REAL,
            interval_seconds INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            next_check INTEGER NOT NULL,
            notified_target INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_tracks_due
        ON tracks(active, next_check);

        CREATE INDEX IF NOT EXISTS idx_tracks_user
        ON tracks(user_id);
        """)


def execute(sql, params=()):
    with db() as conn:
        return conn.execute(sql, params)


def get_user(user_id):
    return execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()


def register_user(user):
    execute("""
        INSERT INTO users(user_id, username, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET username = excluded.username
    """, (
        user.id,
        user.username,
        int(time.time())
    ))


def is_banned(user_id):
    row = get_user(user_id)
    return bool(row and row["banned"])


def subscription_active(user_id):
    row = get_user(user_id)
    return bool(
        row and row["subscription_until"] > int(time.time())
    )


def free_track_count(user_id):
    row = execute("""
        SELECT COUNT(*) AS total
        FROM tracks
        WHERE user_id = ? AND active = 1
    """, (user_id,)).fetchone()

    return row["total"]


def fmt_date(timestamp):
    if not timestamp:
        return "нет"

    return datetime.fromtimestamp(timestamp).strftime(
        "%d.%m.%Y %H:%M"
    )


# ============================================================
# 3. KEYBOARDS
# ============================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="➕ Добавить товар"),
                KeyboardButton(text="📦 Мои товары"),
            ],
            [
                KeyboardButton(text="💳 Подписка"),
                KeyboardButton(text="ℹ️ Помощь"),
            ],
        ],
        resize_keyboard=True
    )


def shops_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Wildberries"),
                KeyboardButton(text="Ozon"),
            ],
            [
                KeyboardButton(text="Яндекс Маркет"),
                KeyboardButton(text="Мегамаркет"),
            ],
            [
                KeyboardButton(text="AliExpress"),
                KeyboardButton(text="❌ Отмена"),
            ],
        ],
        resize_keyboard=True
    )


def interval_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="1 час"),
                KeyboardButton(text="3 часа"),
            ],
            [
                KeyboardButton(text="6 часов"),
                KeyboardButton(text="12 часов"),
            ],
            [
                KeyboardButton(text="1 день"),
                KeyboardButton(text="3 дня"),
            ],
            [
                KeyboardButton(text="7 дней"),
                KeyboardButton(text="30 дней"),
            ],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True
    )


def duration_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="1 день"),
                KeyboardButton(text="7 дней"),
            ],
            [
                KeyboardButton(text="14 дней"),
                KeyboardButton(text="30 дней"),
            ],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True
    )


def admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="👥 Пользователи"),
                KeyboardButton(text="📦 Все отслеживания"),
            ],
            [
                KeyboardButton(text="📣 Рассылка"),
                KeyboardButton(text="🚫 Бан"),
            ],
            [
                KeyboardButton(text="✅ Разбан"),
                KeyboardButton(text="⏳ Продлить подписку"),
            ],
            [
                KeyboardButton(text="📊 Статистика"),
                KeyboardButton(text="🏠 Главное меню"),
            ],
        ],
        resize_keyboard=True
    )


# ============================================================
# 4. FSM STATES
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
# 5. URL VALIDATION
# ============================================================

def valid_shop_url(shop, url):
    try:
        parsed = urlparse(url.strip())

        if parsed.scheme not in ("http", "https"):
            return False

        hostname = (parsed.hostname or "").lower()

        if not hostname:
            return False

        allowed = SHOPS.get(shop, ())

        return any(
            hostname == domain
            or hostname.endswith("." + domain)
            for domain in allowed
        )

    except Exception:
        return False


# ============================================================
# 6. PRICE EXTRACTION
#
# Best-effort generic parser.
# Does not bypass CAPTCHA, login, or anti-bot protection.
# Shop-specific adapters can replace this function later.
# ============================================================

def find_prices(obj):
    """Recursively extract price values from JSON-LD."""

    found = []

    if isinstance(obj, dict):
        offers = obj.get("offers")

        if offers is not None:
            found.extend(find_prices(offers))

        for key in ("price", "lowPrice"):
            value = obj.get(key)

            if value is not None:
                found.extend(find_prices({key: value}))

    elif isinstance(obj, list):
        for item in obj:
            found.extend(find_prices(item))

    else:
        try:
            # Ignore unrelated objects and invalid numbers.
            value = float(str(obj).replace(",", "."))
            if 0 < value < 100_000_000:
                found.append(value)
        except (ValueError, TypeError):
            pass

    return found


def parse_product_price(html):
    soup = BeautifulSoup(html, "html.parser")

    # JSON-LD structured product data.
    for script in soup.find_all(
        "script",
        attrs={"type": "application/ld+json"}
    ):
        try:
            data = json.loads(script.string or script.get_text())
            prices = find_prices(data)

            if prices:
                return min(prices)
        except (ValueError, TypeError):
            continue

    # Common product price metadata.
    selectors = [
        ("meta", {"property": "product:price:amount"}),
        ("meta", {"property": "og:price:amount"}),
        ("meta", {"itemprop": "price"}),
        ("meta", {"name": "price"}),
    ]

    for tag, attrs in selectors:
        element = soup.find(tag, attrs=attrs)

        if element:
            value = element.get("content")

            if value:
                try:
                    price = float(value.replace(",", "."))
                    if 0 < price < 100_000_000:
                        return price
                except ValueError:
                    pass

    return None


async def fetch_price(shop, url):
    """
    Returns a numeric price or None if the page does not expose
    a reliably parsable price to this basic implementation.
    """

    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
            }
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

            # Don't trust redirects to unrelated domains.
            final_host = (urlparse(str(response.url)).hostname or "").lower()
            allowed = SHOPS.get(shop, ())

            if not any(
                final_host == domain
                or final_host.endswith("." + domain)
                for domain in allowed
            ):
                return None

            return parse_product_price(response.text)

    except Exception as exc:
        print(f"[PRICE ERROR] {shop}: {type(exc).__name__}: {exc}")
        return None


# ============================================================
# 7. START AND MAIN MENU
# ============================================================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    register_user(message.from_user)

    if is_banned(message.from_user.id):
        await message.answer("Доступ к боту ограничен.")
        return

    await message.answer(
        "👋 Привет! Это Price Tracker.\n\n"
        "Отслеживай цены товаров и получай уведомления "
        "при снижении цены.\n\n"
        "Нажми «Добавить товар», чтобы начать.",
        reply_markup=main_keyboard()
    )


@dp.message(Command("cancel"))
@dp.message(F.text == "❌ Отмена")
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Действие отменено.",
        reply_markup=main_keyboard()
    )


@dp.message(F.text == "ℹ️ Помощь")
async def help_message(message: Message):
    await message.answer(
        "ℹ️ Как пользоваться:\n\n"
        "1. Нажми «Добавить товар».\n"
        "2. Выбери магазин.\n"
        "3. Отправь ссылку на товар.\n"
        "4. При желании укажи название и целевую цену.\n"
        "5. Выбери интервал проверки и срок отслеживания.\n\n"
        "Если целевая цена не указана, бот попробует "
        "уведомлять о снижении цены минимум на 5% "
        "от последней зафиксированной цены.\n\n"
        "Важно: если сайт не предоставляет цену в "
        "доступном формате, бот не сможет гарантировать "
        "автоматическое отслеживание.",
        reply_markup=main_keyboard()
    )


# ============================================================
# 8. SUBSCRIPTION
# ============================================================

@dp.message(F.text == "💳 Подписка")
async def subscription(message: Message):
    register_user(message.from_user)

    row = get_user(message.from_user.id)
    until = row["subscription_until"] if row else 0
    active = until > int(time.time())

    if active:
        status = f"Активна до: {fmt_date(until)}"
    else:
        status = "Активной подписки нет."

    await message.answer(
        "💳 Твоя подписка\n\n"
        f"Статус: {status}\n\n"
        f"Бесплатный лимит: {FREE_TRACK_LIMIT} активных товаров.\n"
        "Платная подписка в этой версии выдаётся "
        "администратором вручную.\n\n"
        "Автоматическая оплата пока не подключена.",
        reply_markup=main_keyboard()
    )


# ============================================================
# 9. ADD TRACKING — WIZARD
# ============================================================

@dp.message(F.text == "➕ Добавить товар")
async def add_track(message: Message, state: FSMContext):
    register_user(message.from_user)

    if is_banned(message.from_user.id):
        await message.answer("Доступ ограничен.")
        return

    if (
        not subscription_active(message.from_user.id)
        and free_track_count(message.from_user.id) >= FREE_TRACK_LIMIT
    ):
        await message.answer(
            "Ты достиг бесплатного лимита.\n"
            "Открой «Подписка» или обратись к администратору."
        )
        return

    await state.clear()
    await state.set_state(TrackForm.shop)

    await message.answer(
        "Выбери магазин:",
        reply_markup=shops_keyboard()
    )


@dp.message(TrackForm.shop)
async def select_shop(message: Message, state: FSMContext):
    if message.text not in SHOPS:
        await message.answer("Выбери магазин кнопкой ниже.")
        return

    await state.update_data(shop=message.text)
    await state.set_state(TrackForm.url)

    await message.answer(
        "Отправь полную ссылку на товар.\n"
        "Например: https://www.wildberries.ru/catalog/...\n\n"
        "Ссылка должна вести на выбранный магазин."
    )


@dp.message(TrackForm.url)
async def receive_url(message: Message, state: FSMContext):
    data = await state.get_data()
    shop = data["shop"]
    url = (message.text or "").strip()

    if not valid_shop_url(shop, url):
        await message.answer(
            "❌ Ссылка не подходит выбранному магазину.\n"
            "Отправь правильную ссылку или нажми «Отмена»."
        )
        return

    await state.update_data(url=url)
    await state.set_state(TrackForm.name)

    await message.answer(
        "Введи название товара для удобства "
        "или напиши «Пропустить»."
    )


@dp.message(TrackForm.name)
async def receive_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()

    if name.casefold() == "пропустить":
        name = ""

    await state.update_data(name=name[:150])
    await state.set_state(TrackForm.target)

    await message.answer(
        "🎯 Введи желаемую цену в рублях.\n\n"
        "Например: 5000\n"
        "Когда цена станет не выше указанной, придёт уведомление.\n\n"
        "Если целевая цена не нужна, напиши «Пропустить»."
    )


@dp.message(TrackForm.target)
async def receive_target(message: Message, state: FSMContext):
    raw = (message.text or "").strip()

    if raw.casefold() == "пропустить":
        target = None
    else:
        try:
            target = float(
                raw.replace(" ", "").replace(",", ".").replace("₽", "")
            )

            if not 1 <= target <= 100_000_000:
                raise ValueError

        except ValueError:
            await message.answer(
                "Введи положительную цену числом "
                "или напиши «Пропустить»."
            )
            return

    await state.update_data(target=target)
    await state.set_state(TrackForm.interval)

    await message.answer(
        "⏱ Как часто проверять цену?\n"
        "Минимум — каждый час, максимум — раз в 30 дней.",
        reply_markup=interval_keyboard()
    )


@dp.message(TrackForm.interval)
async def receive_interval(message: Message, state: FSMContext):
    interval = INTERVALS.get(message.text)

    if not interval:
        await message.answer("Выбери интервал кнопкой.")
        return

    await state.update_data(interval=interval)
    await state.set_state(TrackForm.duration)

    await message.answer(
        "📅 Как долго отслеживать товар?\n"
        "Максимум — 30 дней.",
        reply_markup=duration_keyboard()
    )


@dp.message(TrackForm.duration)
async def receive_duration(message: Message, state: FSMContext):
    duration = DURATIONS.get(message.text)

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

    now = int(time.time())

    await message.answer(
        "⏳ Проверяю, удастся ли прочитать цену товара..."
    )

    initial_price = await fetch_price(data["shop"], data["url"])

    execute("""
        INSERT INTO tracks (
            user_id, shop, url, product_name, target_price,
            base_price, last_price, last_notified_price,
            interval_seconds, expires_at, next_check
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        message.from_user.id,
        data["shop"],
        data["url"],
        data["name"],
        data["target"],
        initial_price,
        initial_price,
        initial_price,
        interval,
        now + duration,
        now + interval
    ))

    await state.clear()

    if initial_price is None:
        price_status = (
            "⚠️ Не удалось автоматически прочитать цену сейчас. "
            "Бот попробует проверить её позднее. "
            "Если магазин постоянно блокирует доступ или "
            "не предоставляет цену в HTML, потребуется "
            "отдельный адаптер."
        )
    else:
        price_status = f"Текущая цена: {initial_price:.2f} ₽"

    await message.answer(
        "✅ Товар добавлен!\n\n"
        f"Магазин: {data['shop']}\n"
        f"Название: {data['name'] or 'Не указано'}\n"
        f"{price_status}\n"
        f"Целевая цена: "
        f"{f'{data['target']:.2f} ₽' if data['target'] else 'не задана'}\n"
        f"Проверка: каждые {message_interval_text(interval)}\n"
        f"Отслеживание до: {fmt_date(now + duration)}",
        reply_markup=main_keyboard()
    )


def message_interval_text(seconds):
    for label, value in INTERVALS.items():
        if value == seconds:
            return label
    return f"{seconds // 3600} ч."


# ============================================================
# 10. USER TRACKINGS
# ============================================================

@dp.message(F.text == "📦 Мои товары")
async def my_tracks(message: Message):
    user_id = message.from_user.id

    rows = execute("""
        SELECT * FROM tracks
        WHERE user_id = ? AND active = 1
        ORDER BY id DESC
    """, (user_id,)).fetchall()

    if not rows:
        await message.answer(
            "У тебя пока нет активных товаров.",
            reply_markup=main_keyboard()
        )
        return

    for row in rows:
        price = (
            f"{row['last_price']:.2f} ₽"
            if row["last_price"] is not None
            else "пока неизвестна"
        )

        target = (
            f"{row['target_price']:.2f} ₽"
            if row["target_price"] is not None
            else "снижение на 5%"
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="🗑 Удалить отслеживание",
                    callback_data=f"deltrack:{row['id']}"
                )
            ]]
        )

        await message.answer(
            f"📦 Товар №{row['id']}\n"
            f"Магазин: {row['shop']}\n"
            f"Название: {row['product_name'] or 'Не указано'}\n"
            f"Цена: {price}\n"
            f"Целевая цена: {target}\n"
            f"Следующая проверка: {fmt_date(row['next_check'])}\n"
            f"Отслеживание до: {fmt_date(row['expires_at'])}\n"
            f"Ссылка: {row['url']}",
            reply_markup=kb
        )


@dp.callback_query(F.data.startswith("deltrack:"))
async def delete_track(callback: CallbackQuery):
    try:
        track_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный номер.")
        return

    row = execute(
        "SELECT user_id FROM tracks WHERE id = ?",
        (track_id,)
    ).fetchone()

    if not row or row["user_id"] != callback.from_user.id:
        await callback.answer("Товар не найден.")
        return

    execute(
        "UPDATE tracks SET active = 0 WHERE id = ?",
        (track_id,)
    )

    await callback.message.edit_text("Отслеживание отключено.")
    await callback.answer("Удалено.")


# ============================================================
# 11. PRICE MONITOR
# ============================================================

async def check_prices_loop():
    while True:
        try:
            now = int(time.time())

            # Disable expired tracking jobs.
            execute("""
                UPDATE tracks
                SET active = 0
                WHERE active = 1 AND expires_at <= ?
            """, (now,))

            due = execute("""
                SELECT * FROM tracks
                WHERE active = 1 AND next_check <= ?
                ORDER BY next_check
                LIMIT 25
            """, (now,)).fetchall()

            for row in due:
                # Schedule the next check before network requests.
                execute("""
                    UPDATE tracks
                    SET next_check = ?
                    WHERE id = ?
                """, (
                    now + row["interval_seconds"],
                    row["id"]
                ))

                price = await fetch_price(row["shop"], row["url"])

                if price is None:
                    continue

                old_price = row["last_price"]
                target = row["target_price"]
                notify = False
                reason = ""

                if target is not None and price <= target:
                    if not row["notified_target"]:
                        notify = True
                        reason = (
                            f"Цена достигла целевого значения "
                            f"{target:.2f} ₽."
                        )

                        execute("""
                            UPDATE tracks
                            SET notified_target = 1
                            WHERE id = ?
                        """, (row["id"],))

                elif (
                    target is None
                    and old_price is not None
                    and price <= old_price * 0.95
                    and (
                        row["last_notified_price"] is None
                        or price <= row["last_notified_price"] * 0.95
                    )
                ):
                    notify = True
                    reason = "Цена снизилась минимум на 5%."

                    execute("""
                        UPDATE tracks
                        SET last_notified_price = ?
                        WHERE id = ?
                    """, (price, row["id"]))

                execute("""
                    UPDATE tracks
                    SET last_price = ?,
                        base_price = COALESCE(base_price, ?)
                    WHERE id = ?
                """, (price, price, row["id"]))

                if notify:
                    try:
                        await bot.send_message(
                            row["user_id"],
                            "🔔 Изменение цены!\n\n"
                            f"Товар: {row['product_name'] or row['shop']}\n"
                            f"Цена: {price:.2f} ₽\n"
                            f"{reason}\n\n"
                            f"{row['url']}"
                        )
                    except TelegramAPIError as exc:
                        print(
                            f"[NOTIFICATION ERROR] "
                            f"{row['user_id']}: {exc}"
                        )

                # Small pause between requests.
                await asyncio.sleep(1)

        except Exception as exc:
            print(f"[MONITOR ERROR] {type(exc).__name__}: {exc}")

        await asyncio.sleep(60)


# ============================================================
# 12. ADMIN AUTHENTICATION
# ============================================================

async def begin_admin(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Доступ запрещён.")
        return

    await state.clear()
    await state.set_state(AdminForm.password)

    await message.answer("Введи пароль администратора:")


@dp.message(Command("admin"))
async def admin_command(message: Message, state: FSMContext):
    await begin_admin(message, state)


@dp.message(F.text.casefold() == "/админ")
async def admin_cyrillic_command(
    message: Message,
    state: FSMContext
):
    await begin_admin(message, state)


@dp.message(AdminForm.password)
async def admin_password(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        await message.answer("Доступ запрещён.")
        return

    if (message.text or "").strip() != ADMIN_PASSWORD:
        await message.answer("Неверный пароль.")
        return

    admin_sessions.add(message.from_user.id)
    await state.clear()

    await message.answer(
        "🔐 Админ-панель открыта.",
        reply_markup=admin_keyboard()
    )


def is_admin(user_id):
    return (
        user_id in ADMIN_IDS
        and user_id in admin_sessions
    )


# ============================================================
# 13. ADMIN PANEL
# ============================================================

@dp.message(F.text == "🏠 Главное меню")
async def back_to_main(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer(
            "Главное меню.",
            reply_markup=main_keyboard()
        )
        return

    await message.answer(
        "Главное меню.",
        reply_markup=main_keyboard()
    )


@dp.message(F.text == "👥 Пользователи")
async def admin_users(message: Message):
    if not is_admin(message.from_user.id):
        return

    rows = execute("""
        SELECT user_id, username, banned, subscription_until
        FROM users
        ORDER BY created_at DESC
    """).fetchall()

    if not rows:
        await message.answer("Пользователей пока нет.")
        return

    lines = ["👥 Пользователи:"]

    for row in rows:
        username = (
            f"@{row['username']}"
            if row["username"]
            else "без username"
        )

        status = "БАН" if row["banned"] else "активен"

        lines.append(
            f"ID: {row['user_id']} | {username} | {status} | "
            f"подписка до {fmt_date(row['subscription_until'])}"
        )

    # Send multiple messages to avoid Telegram's message limit.
    chunk = ""

    for line in lines:
        if len(chunk) + len(line) > 3500:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"

    if chunk:
        await message.answer(chunk)


@dp.message(F.text == "📦 Все отслеживания")
async def admin_tracks(message: Message):
    if not is_admin(message.from_user.id):
        return

    rows = execute("""
        SELECT id, user_id, shop, product_name, active, url
        FROM tracks
        ORDER BY id DESC
        LIMIT 100
    """).fetchall()

    if not rows:
        await message.answer("Отслеживаний пока нет.")
        return

    lines = ["📦 Последние 100 отслеживаний:"]

    for row in rows:
        lines.append(
            f"#{row['id']} | user={row['user_id']} | "
            f"{row['shop']} | {row['product_name'] or 'без названия'} | "
            f"active={row['active']}\n{row['url']}"
        )

    chunk = ""

    for line in lines:
        if len(chunk) + len(line) > 3500:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"

    if chunk:
        await message.answer(chunk)


@dp.message(F.text == "📊 Статистика")
async def admin_stats(message: Message):
    if not is_admin(message.from_user.id):
        return

    users = execute(
        "SELECT COUNT(*) AS n FROM users"
    ).fetchone()["n"]

    banned = execute(
        "SELECT COUNT(*) AS n FROM users WHERE banned = 1"
    ).fetchone()["n"]

    subscribers = execute(
        "SELECT COUNT(*) AS n FROM users WHERE subscription_until > ?",
        (int(time.time()),)
    ).fetchone()["n"]

    active_tracks = execute(
        "SELECT COUNT(*) AS n FROM tracks WHERE active = 1"
    ).fetchone()["n"]

    await message.answer(
        "📊 Статистика\n\n"
        f"Пользователей: {users}\n"
        f"Заблокировано: {banned}\n"
        f"Активных подписок: {subscribers}\n"
        f"Активных отслеживаний: {active_tracks}"
    )


# ============================================================
# 14. ADMIN BROADCAST
# ============================================================

@dp.message(F.text == "📣 Рассылка")
async def admin_broadcast_start(
    message: Message,
    state: FSMContext
):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(AdminForm.broadcast)

    await message.answer(
        "Отправь текст рассылки.\n"
        "Сообщение будет отправлено всем незаблокированным "
        "пользователям.\n\n"
        "Для отмены введи /cancel."
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

    rows = execute(
        "SELECT user_id FROM users WHERE banned = 0"
    ).fetchall()

    sent = 0
    failed = 0

    await message.answer(
        f"Начинаю рассылку для {len(rows)} пользователей..."
    )

    for row in rows:
        try:
            await bot.send_message(row["user_id"], text)
            sent += 1
        except TelegramAPIError:
            failed += 1

        # Avoid sending too quickly.
        await asyncio.sleep(0.05)

    await message.answer(
        "📣 Рассылка завершена.\n"
        f"Успешно: {sent}\n"
        f"Ошибок: {failed}",
        reply_markup=admin_keyboard()
    )


# ============================================================
# 15. ADMIN BAN / UNBAN
# ============================================================

@dp.message(F.text == "🚫 Бан")
async def admin_ban_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(AdminForm.ban_id)
    await message.answer("Введи Telegram ID пользователя для бана.")


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

    execute(
        "UPDATE users SET banned = 1 WHERE user_id = ?",
        (user_id,)
    )

    await state.clear()
    await message.answer(
        f"Пользователь {user_id} заблокирован, если он есть в базе.",
        reply_markup=admin_keyboard()
    )


@dp.message(F.text == "✅ Разбан")
async def admin_unban_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(AdminForm.unban_id)
    await message.answer("Введи Telegram ID пользователя для разбана.")


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

    execute(
        "UPDATE users SET banned = 0 WHERE user_id = ?",
        (user_id,)
    )

    await state.clear()
    await message.answer(
        f"Пользователь {user_id} разблокирован, если он есть в базе.",
        reply_markup=admin_keyboard()
    )


# ============================================================
# 16. ADMIN SUBSCRIPTION EXTENSION
# ============================================================

@dp.message(F.text == "⏳ Продлить подписку")
async def admin_extend_start(
    message: Message,
    state: FSMContext
):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(AdminForm.extend_id)
    await message.answer(
        "Введи Telegram ID пользователя, которому "
        "нужно продлить подписку."
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
        await message.answer(
            "Пользователь не найден в базе. "
            "Он должен сначала запустить бота."
        )
        await state.clear()
        return

    await state.update_data(extend_user_id=user_id)
    await state.set_state(AdminForm.extend_days)

    await message.answer(
        "На сколько дней продлить подписку?\n"
        "Например: 30"
    )


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
        await message.answer(
            "Введи число дней от 1 до 3650."
        )
        return

    data = await state.get_data()
    user_id = data["extend_user_id"]

    row = get_user(user_id)
    current_until = row["subscription_until"]
    base = max(current_until, int(time.time()))
    new_until = base + days * 86400

    execute("""
        UPDATE users
        SET subscription_until = ?
        WHERE user_id = ?
    """, (new_until, user_id))

    await state.clear()

    await message.answer(
        f"✅ Подписка пользователя {user_id} продлена на {days} дней.\n"
        f"Новый срок: {fmt_date(new_until)}",
        reply_markup=admin_keyboard()
    )

    try:
        await bot.send_message(
            user_id,
            "💳 Твоя подписка продлена!\n"
            f"Действует до: {fmt_date(new_until)}"
        )
    except TelegramAPIError:
        pass


# ============================================================
# 17. COMMON ACCESS GUARD
# ============================================================

@dp.message()
async def unknown_message(message: Message):
    if not message.from_user:
        return

    register_user(message.from_user)

    if is_banned(message.from_user.id):
        await message.answer("Доступ ограничен.")
        return

    await message.answer(
        "Выбери действие кнопкой внизу.",
        reply_markup=main_keyboard()
    )


# ============================================================
# 18. STARTUP
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
