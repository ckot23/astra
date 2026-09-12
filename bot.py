#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Бот «Наклейки на заказ» — один файл для PyCharm (aiogram 3).

1. pip install -r requirements.txt
2. Вставьте BOT_TOKEN, ADMIN_IDS и WEBAPP_URL ниже.
3. Откройте этот файл → ▶ Run.
"""

from __future__ import annotations

import asyncio
import csv
import html as html_lib
import io
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
    BotCommand,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ---------------------------------------------------------------------------
# НАСТРОЙКИ — заполните перед запуском
# ---------------------------------------------------------------------------

BOT_TOKEN = ""  # токен от @BotFather
ADMIN_IDS: list[int] = []  # свой id от @userinfobot, например [123456789]
WEBAPP_URL = ""  # HTTPS-адрес папки webapp/, например https://логин.github.io/sticker-shop/

# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "orders.sqlite3"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv(BASE_DIR / ".env")
BOT_TOKEN = os.getenv("BOT_TOKEN", BOT_TOKEN).strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", WEBAPP_URL).strip()
if os.getenv("ADMIN_IDS"):
    ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sticker-bot")

FILMS = {
    "matte": {"title": "Матовая", "rate": 2.2},
    "gloss": {"title": "Глянцевая", "rate": 2.4},
    "transparent": {"title": "Прозрачная", "rate": 2.8},
    "metallic": {"title": "Металлик / зеркальная", "rate": 3.5},
    "reflective": {"title": "Светоотражающая", "rate": 4.2},
}
DESIGNS = {
    "own": {"title": "Свой файл", "mult": 1.0, "setup": 0},
    "text": {"title": "Надпись / текст", "mult": 1.1, "setup": 0},
    "logo": {"title": "Макет под ключ", "mult": 1.25, "setup": 1500},
    "catalog": {"title": "Из каталога", "mult": 1.0, "setup": 0},
}
COLORS = {
    "black": "Чёрный",
    "white": "Белый",
    "red": "Красный",
    "blue": "Синий",
    "green": "Зелёный",
    "yellow": "Жёлтый",
    "orange": "Оранжевый",
    "silver": "Серебро",
    "gold": "Золото",
    "fullcolor": "Полноцветная печать",
}
COLOR_MULT = {"fullcolor": 1.3, "gold": 1.15, "silver": 1.15}
QUANTITY_TIERS = ((100, 0.60), (50, 0.68), (25, 0.75), (10, 0.82), (5, 0.90), (1, 1.00))
MIN_ITEM_PRICE = 300
MIN_SIDE_CM, MAX_SIDE_CM, MAX_QUANTITY = 1, 500, 10_000
STATUSES = ("new", "confirmed", "production", "done", "canceled")
STATUS_LABELS = {
    "new": "🕒 Новый",
    "confirmed": "🤝 Подтверждён",
    "production": "🖨 В печати",
    "done": "✅ Выдан",
    "canceled": "🗑 Отменён",
}


@dataclass(frozen=True)
class Quote:
    film: str
    design: str
    color: str
    width_cm: float
    height_cm: float
    quantity: int
    unit_price: int
    setup_fee: int
    discount_percent: int
    total: int

    @property
    def film_title(self) -> str:
        return str(FILMS[self.film]["title"])

    @property
    def design_title(self) -> str:
        return str(DESIGNS[self.design]["title"])

    @property
    def color_title(self) -> str:
        return COLORS[self.color]


def quantity_multiplier(quantity: int) -> float:
    for threshold, multiplier in QUANTITY_TIERS:
        if quantity >= threshold:
            return multiplier
    return 1.0


def calculate(
    film: str, design: str, color: str, width_cm: float, height_cm: float, quantity: int
) -> Quote:
    if film not in FILMS:
        raise ValueError("Неизвестный тип плёнки")
    if design not in DESIGNS:
        raise ValueError("Неизвестный вариант дизайна")
    if color not in COLORS:
        raise ValueError("Неизвестный цвет")
    for side in (width_cm, height_cm):
        if not MIN_SIDE_CM <= side <= MAX_SIDE_CM:
            raise ValueError("Сторона должна быть от 1 до 500 см")
    if not 1 <= quantity <= MAX_QUANTITY:
        raise ValueError("Тираж должен быть от 1 до 10000 шт")
    unit = max(
        MIN_ITEM_PRICE,
        int(
            round(
                width_cm
                * height_cm
                * float(FILMS[film]["rate"])
                * float(DESIGNS[design]["mult"])
                * COLOR_MULT.get(color, 1.0)
                / 10.0
            )
            * 10
        ),
    )
    discount = quantity_multiplier(quantity)
    setup = int(DESIGNS[design]["setup"])
    total = int(round(unit * quantity * discount / 10.0) * 10) + setup
    return Quote(
        film=film,
        design=design,
        color=color,
        width_cm=width_cm,
        height_cm=height_cm,
        quantity=quantity,
        unit_price=unit,
        setup_fee=setup,
        discount_percent=int(round((1 - discount) * 100)),
        total=total,
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    film TEXT NOT NULL,
    design TEXT NOT NULL,
    color TEXT NOT NULL,
    width_cm REAL NOT NULL,
    height_cm REAL NOT NULL,
    quantity INTEGER NOT NULL,
    unit_price INTEGER NOT NULL,
    setup_fee INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL,
    contact TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def upsert_customer(user_id: int, username: str | None, full_name: str) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO customers (user_id, username, full_name) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
            (user_id, username, full_name),
        )
        conn.commit()


def create_order(user_id: int, quote: Quote, contact: str, comment: str) -> int:
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO orders (user_id, film, design, color, width_cm, height_cm,
                                   quantity, unit_price, setup_fee, total, contact, comment)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id, quote.film, quote.design, quote.color, quote.width_cm, quote.height_cm,
                quote.quantity, quote.unit_price, quote.setup_fee, quote.total, contact, comment,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def get_order(order_id: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None


def list_orders(status: str | None = None, user_id: int | None = None, limit: int = 10) -> list[dict[str, Any]]:
    query = "SELECT * FROM orders WHERE 1=1"
    params: list[Any] = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if user_id is not None:
        query += " AND user_id = ?"
        params.append(user_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def set_status(order_id: int, status: str) -> bool:
    if status not in STATUSES:
        raise ValueError("Недопустимый статус")
    with db() as conn:
        cur = conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        conn.commit()
        return cur.rowcount > 0


def stats() -> dict[str, int]:
    with db() as conn:
        def one(sql: str) -> int:
            row = conn.execute(sql).fetchone()
            return int(row[0] if row else 0)

        return {
            "customers": one("SELECT COUNT(*) FROM customers"),
            "orders": one("SELECT COUNT(*) FROM orders"),
            "new": one("SELECT COUNT(*) FROM orders WHERE status = 'new'"),
            "today": one("SELECT COUNT(*) FROM orders WHERE date(created_at) = date('now')"),
            "revenue": one(
                "SELECT COALESCE(SUM(total), 0) FROM orders WHERE status IN ('confirmed','production','done')"
            ),
            "avg": one("SELECT COALESCE(AVG(total), 0) FROM orders"),
        }


def parse_payload(payload: dict[str, Any]) -> tuple[Quote, str, str]:
    try:
        quote = calculate(
            film=str(payload["film"]),
            design=str(payload["design"]),
            color=str(payload["color"]),
            width_cm=float(payload["w"]),
            height_cm=float(payload["h"]),
            quantity=int(payload["qty"]),
        )
    except (KeyError, TypeError) as error:
        raise ValueError("Неполные данные заказа") from error
    contact = str(payload.get("contact", "")).strip()[:120]
    if len(contact) < 4:
        raise ValueError("Укажите контакт для связи")
    comment = str(payload.get("comment", "")).strip()[:600]
    return quote, contact, comment


def format_order(order_id: int, quote: Quote, contact: str, comment: str, user: Any) -> str:
    username = "@" + user.username if getattr(user, "username", None) else "—"
    lines = [
        "🔔 <b>Заказ #" + str(order_id) + "</b>",
        "",
        "Материал: " + quote.film_title,
        "Дизайн: " + quote.design_title,
        "Цвет: " + quote.color_title,
        "Размер: " + str(quote.width_cm) + " × " + str(quote.height_cm) + " см",
        "Тираж: " + str(quote.quantity) + " шт × " + str(quote.unit_price) + " ₽",
    ]
    if quote.discount_percent:
        lines.append("Скидка: −" + str(quote.discount_percent) + "%")
    if quote.setup_fee:
        lines.append("Макет: " + str(quote.setup_fee) + " ₽")
    lines += [
        "<b>Итого: " + str(quote.total) + " ₽</b>",
        "",
        "Контакт: " + html_lib.escape(contact),
        "Клиент: " + html_lib.escape(user.full_name or "") + " " + username
        + " (<code>" + str(user.id) + "</code>)",
    ]
    if comment:
        lines += ["", "💬 " + html_lib.escape(comment)]
    return "\n".join(lines)


def order_line(row: dict[str, Any]) -> str:
    film = str(FILMS.get(row["film"], {}).get("title", row["film"]))
    color = COLORS.get(row["color"], row["color"])
    design = str(DESIGNS.get(row["design"], {}).get("title", row["design"]))
    return (
        "<b>#" + str(row["id"]) + "</b> "
        + STATUS_LABELS.get(row["status"], row["status"])
        + " · " + str(row["total"]) + " ₽\n"
        + film + " · " + color + " · " + design + "\n"
        + str(row["width_cm"]) + "×" + str(row["height_cm"]) + " см · "
        + str(row["quantity"]) + " шт\nКонтакт: " + html_lib.escape(str(row["contact"]))
    )


def main_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    if WEBAPP_URL.startswith("https://"):
        rows.append([KeyboardButton(text="🎨 Собрать наклейку", web_app=WebAppInfo(url=WEBAPP_URL))])
    rows.append([KeyboardButton(text="📦 Мои заказы"), KeyboardButton(text="ℹ️ О плёнке")])
    if is_admin:
        rows.append([KeyboardButton(text="🔐 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def order_actions(order_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🤝 Подтвердить", callback_data="adm:set:confirmed:" + str(order_id))
    b.button(text="🖨 В печать", callback_data="adm:set:production:" + str(order_id))
    b.button(text="✅ Выдан", callback_data="adm:set:done:" + str(order_id))
    b.button(text="🗑 Отменить", callback_data="adm:set:canceled:" + str(order_id))
    b.adjust(2)
    return b.as_markup()


def admin_panel() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🕒 Новые", callback_data="adm:list:new")
    b.button(text="📋 Все", callback_data="adm:list:all")
    b.button(text="🖨 В печати", callback_data="adm:list:production")
    b.button(text="📊 Статистика", callback_data="adm:stats")
    b.adjust(2)
    return b.as_markup()


async def accept_order(bot: Bot, user: Any, payload: dict[str, Any]) -> tuple[int, Quote]:
    quote, contact, comment = parse_payload(payload)
    upsert_customer(user.id, user.username, user.full_name or "")
    order_id = create_order(user.id, quote, contact, comment)
    text = format_order(order_id, quote, contact, comment, user)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=order_actions(order_id))
        except Exception as error:  # noqa: BLE001
            log.warning("Не удалось уведомить админа %s: %s", admin_id, error)
    return order_id, quote


WELCOME = (
    "<b>Наклейки на заказ из немецкой плёнки</b>\n\n"
    "Печатаем на профессиональной виниловой плёнке Oracal:\n"
    "💧 не боится воды, мороза и мойки\n"
    "☀️ до 7 лет службы на улице\n"
    "📏 любой размер — от 1 см до 5 м\n\n"
    "От стикера на чехол телефона до логотипа на окне многоэтажки.\n\n"
    "Нажмите «🎨 Собрать наклейку» — откроется конструктор."
)

client = Router(name="client")
admin = Router(name="admin")
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


@client.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = message.from_user
    upsert_customer(user.id, user.username, user.full_name)
    await message.answer(WELCOME, reply_markup=main_menu(user.id in ADMIN_IDS))
    if not WEBAPP_URL.startswith("https://"):
        await message.answer(
            "⚠️ WEBAPP_URL не задан. Выложите папку webapp/ на GitHub Pages и вставьте HTTPS-адрес в bot.py."
        )


@client.message(Command("app"))
async def cmd_app(message: Message) -> None:
    if not WEBAPP_URL.startswith("https://"):
        await message.answer("Сначала укажите WEBAPP_URL (HTTPS) в bot.py — см. README.")
        return
    await message.answer(
        "Открываем конструктор 👇",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🎨 Открыть", web_app=WebAppInfo(url=WEBAPP_URL))]]
        ),
    )


@client.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Команды</b>\n/start — меню\n/app — конструктор\n/myorders — мои заказы\n/help — справка",
        reply_markup=main_menu(message.from_user.id in ADMIN_IDS),
    )


@client.message(F.text == "ℹ️ О плёнке")
async def about_film(message: Message) -> None:
    await message.answer(
        "<b>Почему немецкая плёнка</b>\n\n"
        "• Каландрированный винил — не даёт усадки\n"
        "• Не боится воды, снега и мойки\n"
        "• Цвет не выгорает до 7 лет на улице\n"
        "• От −40°C до +90°C\n"
        "• Клей снимается без следов"
    )


@client.message(Command("myorders"))
@client.message(F.text == "📦 Мои заказы")
async def my_orders(message: Message) -> None:
    rows = list_orders(user_id=message.from_user.id)
    if not rows:
        await message.answer("Заказов пока нет. Нажмите «🎨 Собрать наклейку».")
        return
    parts = ["<b>Ваши заказы</b>"]
    for row in rows:
        parts.append(
            "#" + str(row["id"]) + " — " + STATUS_LABELS.get(row["status"], row["status"])
            + "\n" + str(row["width_cm"]) + "×" + str(row["height_cm"])
            + " см · " + str(row["quantity"]) + " шт · " + str(row["total"]) + " ₽"
        )
    await message.answer("\n\n".join(parts))


@client.message(F.web_app_data)
async def on_webapp_data(message: Message, bot: Bot) -> None:
    try:
        payload = json.loads(message.web_app_data.data)
        if not isinstance(payload, dict):
            raise ValueError("Ожидался JSON-объект")
        order_id, quote = await accept_order(bot, message.from_user, payload)
    except ValueError as error:
        await message.answer("⚠️ " + str(error))
        return
    except Exception:
        log.exception("заказ")
        await message.answer("Не удалось принять заказ. Попробуйте ещё раз.")
        return
    await message.answer(
        "✅ <b>Заказ #" + str(order_id) + " принят</b>\n\n"
        + quote.film_title + " · " + quote.color_title + "\n"
        + str(quote.width_cm) + " × " + str(quote.height_cm) + " см · "
        + str(quote.quantity) + " шт\n"
        + "Сумма: <b>" + str(quote.total) + " ₽</b>\n\n"
        + "Менеджер свяжется с вами. Макет можно прислать файлом в этот чат.",
        reply_markup=main_menu(message.from_user.id in ADMIN_IDS),
    )


@client.message(F.document | F.photo)
async def on_layout_file(message: Message) -> None:
    await message.answer("Файл получен, переслал администратору 👌")
    for admin_id in ADMIN_IDS:
        try:
            await message.forward(admin_id)
        except Exception as error:  # noqa: BLE001
            log.warning("Не удалось переслать файл %s: %s", admin_id, error)


@admin.message(Command("admin"))
@admin.message(F.text == "🔐 Админ-панель")
async def admin_home(message: Message) -> None:
    data = stats()
    await message.answer(
        "<b>Админ-панель</b>\nНовых: " + str(data["new"]) + " · всего: " + str(data["orders"])
        + "\n\n/orders  /order 12  /export",
        reply_markup=admin_panel(),
    )


@admin.callback_query(F.data == "adm:stats")
async def cb_stats(callback: CallbackQuery) -> None:
    data = stats()
    await callback.message.answer(
        "<b>Статистика</b>\nКлиентов: " + str(data["customers"])
        + "\nЗаказов: " + str(data["orders"]) + " (новых " + str(data["new"])
        + ", сегодня " + str(data["today"]) + ")\nВыручка: " + str(data["revenue"])
        + " ₽\nСредний чек: " + str(data["avg"]) + " ₽"
    )
    await callback.answer()


@admin.callback_query(F.data.startswith("adm:list:"))
async def cb_list(callback: CallbackQuery) -> None:
    key = callback.data.split(":")[2]
    status = None if key == "all" else key
    rows = list_orders(status=status, limit=10)
    if not rows:
        await callback.answer("Пусто", show_alert=True)
        return
    for row in rows:
        await callback.message.answer(order_line(row), reply_markup=order_actions(row["id"]))
    await callback.answer()


@admin.message(Command("orders"))
async def cmd_orders(message: Message, command: CommandObject) -> None:
    status = (command.args or "").strip() or None
    if status and status not in STATUS_LABELS:
        await message.answer("Статусы: " + ", ".join(STATUS_LABELS))
        return
    rows = list_orders(status=status, limit=10)
    if not rows:
        await message.answer("Заказов нет.")
        return
    for row in rows:
        await message.answer(order_line(row), reply_markup=order_actions(row["id"]))


@admin.message(Command("order"))
async def cmd_order(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer("Формат: /order 12")
        return
    row = get_order(int(raw))
    if not row:
        await message.answer("Заказ не найден.")
        return
    text = order_line(row)
    if row["comment"]:
        text += "\n\n💬 " + html_lib.escape(str(row["comment"]))
    await message.answer(text, reply_markup=order_actions(row["id"]))


@admin.callback_query(F.data.startswith("adm:set:"))
async def cb_set_status(callback: CallbackQuery, bot: Bot) -> None:
    _, _, status, raw_id = callback.data.split(":", 3)
    order_id = int(raw_id)
    if not set_status(order_id, status):
        await callback.answer("Заказ не найден", show_alert=True)
        return
    label = STATUS_LABELS.get(status, status)
    await callback.answer("Статус: " + label)
    await callback.message.answer("Заказ #" + str(order_id) + " → " + label)
    row = get_order(order_id)
    if row and row["user_id"]:
        try:
            await bot.send_message(int(row["user_id"]), "Статус заказа #" + str(order_id) + ": " + label)
        except Exception as error:  # noqa: BLE001
            log.info("Клиенту не доставлено: %s", error)


@admin.message(Command("export"))
async def cmd_export(message: Message) -> None:
    rows = list_orders(limit=10_000)
    if not rows:
        await message.answer("Нечего выгружать.")
        return
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), delimiter=";")
    writer.writeheader()
    writer.writerows(rows)
    await message.answer_document(
        BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename="orders.csv"),
        caption="Заказов: " + str(len(rows)),
    )


async def run() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN пустой.\n"
            "1) Напишите @BotFather → /newbot\n"
            "2) Вставьте токен в начало bot.py в строку BOT_TOKEN = \"...\"\n"
            "3) Запустите файл снова."
        )
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin)
    dp.include_router(client)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="app", description="Конструктор наклеек"),
            BotCommand(command="myorders", description="Мои заказы"),
            BotCommand(command="help", description="Справка"),
        ]
    )
    log.info("Бот запущен. Админы: %s", ADMIN_IDS or "не заданы")
    if not WEBAPP_URL.startswith("https://"):
        log.warning("WEBAPP_URL без HTTPS — кнопка мини-аппа скрыта")
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit) as error:
        print(error if str(error) else "\nОстановлено")


if __name__ == "__main__":
    main()
