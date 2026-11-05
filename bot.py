"""
Telegram-бот обратной связи (библиотека aiogram 3.x).
Полностью автономная версия: не требует файлов config.py и storage.py.
Настройки читаются из переменных окружения (Bothost/.env).
"""

import asyncio
import html
import logging
import os
import sqlite3
import time
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# --------------------------------------------------------------------------- #
#  КОНФИГУРАЦИЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("Ошибка: переменная окружения BOT_TOKEN не задана.")

# ID администраторов (через запятую)
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip()]
if not ADMIN_IDS:
    raise SystemExit("Ошибка: переменная окружения ADMIN_IDS не задана или пуста.")

# ID группы/канала (если нужно куда-то дублировать)
GROUP_ID = int(os.getenv("GROUP_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

# Настройки антифлуда и базы
FLOOD_LIMIT_SECONDS = int(os.getenv("FLOOD_LIMIT_SECONDS", "10"))
DB_PATH = os.getenv("DB_PATH", "feedback.db")

# --------------------------------------------------------------------------- #
#  БАЗА ДАННЫХ (ВСТРОЕННЫЙ КЛАСС ВМЕСТО storage.py)
# --------------------------------------------------------------------------- #

class Database:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._create_tables()

    def _create_tables(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    created_at TEXT,
                    last_active_at TEXT,
                    tickets_count INTEGER DEFAULT 0,
                    banned INTEGER DEFAULT 0
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    content_type TEXT,
                    text TEXT,
                    file_id TEXT,
                    status TEXT DEFAULT 'new',
                    created_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS ticket_copies (
                    admin_id INTEGER,
                    message_id INTEGER,
                    ticket_id INTEGER,
                    PRIMARY KEY (admin_id, message_id)
                )
            """)

    def upsert_user(self, user):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.conn:
            cur = self.conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user.id,))
            if cur.fetchone() is None:
                self.conn.execute(
                    "INSERT INTO users (user_id, username, full_name, created_at, last_active_at) VALUES (?, ?, ?, ?, ?)",
                    (user.id, user.username, user.full_name, now, now)
                )
            else:
                self.conn.execute(
                    "UPDATE users SET username = ?, full_name = ?, last_active_at = ? WHERE user_id = ?",
                    (user.username, user.full_name, now, user.id)
                )

    def get_user(self, user_id):
        cur = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return cur.fetchone()

    def set_banned(self, user_id, banned: bool):
        with self.conn:
            cur = self.conn.execute("UPDATE users SET banned = ? WHERE user_id = ?", (1 if banned else 0, user_id))
            return cur.rowcount > 0

    def add_ticket(self, user_id, content_type, text, file_id):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO tickets (user_id, content_type, text, file_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, content_type, text, file_id, now)
            )
            return cur.lastrowid

    def register_ticket_activity(self, user_id):
        with self.conn:
            self.conn.execute("UPDATE users SET tickets_count = tickets_count + 1 WHERE user_id = ?", (user_id,))

    def get_ticket(self, ticket_id):
        cur = self.conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        return cur.fetchone()

    def set_ticket_status(self, ticket_id, status):
        with self.conn:
            self.conn.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id))

    def add_copy(self, admin_id, message_id, ticket_id):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO ticket_copies (admin_id, message_id, ticket_id) VALUES (?, ?, ?)",
                (admin_id, message_id, ticket_id)
            )

    def find_ticket_by_copy(self, admin_id, message_id):
        cur = self.conn.execute("SELECT ticket_id FROM ticket_copies WHERE admin_id = ? AND message_id = ?", (admin_id, message_id))
        row = cur.fetchone()
        return row["ticket_id"] if row else None

    def recent_users(self, limit=10):
        cur = self.conn.execute("SELECT * FROM users ORDER BY last_active_at DESC LIMIT ?", (limit,))
        return cur.fetchall()

    def user_tickets(self, user_id, limit=5):
        cur = self.conn.execute("SELECT * FROM tickets WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))
        return cur.fetchall()

    def stats(self):
        users_total = self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        users_banned = self.conn.execute("SELECT COUNT(*) FROM users WHERE banned = 1").fetchone()[0]
        tickets_total = self.conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        tickets_open = self.conn.execute("SELECT COUNT(*) FROM tickets WHERE status = 'new'").fetchone()[0]
        return {
            "users_total": users_total,
            "users_banned": users_banned,
            "tickets_total": tickets_total,
            "tickets_open": tickets_open,
        }

    def close(self):
        self.conn.close()

db = Database(DB_PATH)

# --------------------------------------------------------------------------- #
#  ОСТАЛЬНОЙ КОД БОТА (без изменений)
# --------------------------------------------------------------------------- #

router = Router()
last_feedback_at: dict[int, float] = {}

BTN_NEW = "📝 Написать обращение"
BTN_HELP = "ℹ️ Помощь"
BTN_MY_ID = "🆔 Мой ID"

START_TEXT = (
    "Это бот <b>обратной связи</b>. Здесь можно задать вопрос, отправить "
    "предложение или жалобу — администраторы увидят обращение и ответят "
    "вам в этом чате.\n\n"
    "Можно отправить: текст, фото, видео, голосовое сообщение или документ.\n\n"
    "Нажмите кнопку «📝 Написать обращение» и следуйте подсказкам."
)

WAITING_PROMPT = (
    "✍️ <b>Напишите ваше обращение одним сообщением.</b>\n\n"
    "Можно отправить текст, фото, видео, голосовое сообщение или документ.\n"
    "Если передумали — /cancel."
)

HELP_TEXT = (
    "ℹ️ <b>Как работает бот</b>\n"
    "1. Нажмите «📝 Написать обращение».\n"
    "2. Отправьте сообщение — текст, фото, видео, голосовое или документ.\n"
    "3. Дождитесь ответа администратора в этом чате.\n\n"
    "Команды:\n"
    "/cancel — отменить отправку обращения\n"
    "/id — показать ваш ID\n"
    "/start — начать с начала"
)

FALLBACK_TEXT = (
    "🤖 Чтобы отправить обращение, нажмите «📝 Написать обращение» "
    "или введите /start."
)

CANCEL_TEXT = (
    "👌 Готово. Когда будете готовы — нажмите «📝 Написать обращение»."
)

BANNED_TEXT = (
    "🚫 Администратор заблокировал вас в этом боте. "
    "Обращения не принимаются."
)

ADMIN_HELP_TEXT = (
    "🛠 <b>Панель администратора</b>\n\n"
    "Каждое обращение приходит сюда копией. Ответить можно тремя способами:\n"
    "1. Кнопка «✉️ Ответить» под обращением, затем написать ответ.\n"
    "2. Ответить (Reply) на копию обращения — текстом или медиа.\n"
    "3. Команда: /reply <номер обращения> <текст>\n\n"
    "Команды:\n"
    "/stats — статистика\n"
    "/users — последние пользователи\n"
    "/find &lt;user_id&gt; — обращения пользователя\n"
    "/ban &lt;user_id&gt; — заблокировать пользователя\n"
    "/unban &lt;user_id&gt; — разблокировать\n"
    "/cancel — выйти из режима ответа\n\n"
    "Свой ID и ID чата: /id"
)

ANSWER_PREFIX = "✉️ <b>Ответ поддержки:</b>\n"

TYPE_LABELS = {
    "text": "текст", "photo": "фото", "video": "видео", "video_note": "видео-заметка",
    "voice": "голосовое", "audio": "аудио", "document": "документ", "sticker": "стикер", "other": "другое",
}
STATUS_LABELS = {"new": "🆕 новое", "answered": "✅ отвечено"}

class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user is not None and event.from_user.id in ADMIN_IDS

class UserStates(StatesGroup):
    waiting_for_message = State()

class AdminStates(StatesGroup):
    waiting_for_reply = State()

def user_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_NEW)],
            [KeyboardButton(text=BTN_HELP), KeyboardButton(text=BTN_MY_ID)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие или напишите команду",
    )

def admin_ticket_keyboard(ticket_id: int, user_id: int, banned: bool) -> InlineKeyboardMarkup:
    ban_text = "✅ Разбанить" if banned else "🚫 Забанить"
    ban_cb = f"unban:{user_id}" if banned else f"ban:{user_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✉️ Ответить", callback_data=f"reply:{ticket_id}")],
            [
                InlineKeyboardButton(text="ℹ️ Инфо", callback_data=f"info:{ticket_id}"),
                InlineKeyboardButton(text=ban_text, callback_data=ban_cb),
            ],
        ]
    )

def extract_content(message: Message) -> tuple[str, str | None, str | None]:
    if message.text: return "text", None, message.text
    if message.photo: return "photo", message.photo[-1].file_id, message.caption
    if message.video: return "video", message.video.file_id, message.caption
    if message.video_note: return "video_note", message.video_note.file_id, None
    if message.voice: return "voice", message.voice.file_id, message.caption
    if message.audio: return "audio", message.audio.file_id, message.caption
    if message.document: return "document", message.document.file_id, message.caption
    if message.sticker: return "sticker", message.sticker.file_id, None
    return "other", None, None

async def deliver_answer(bot: Bot, user_id: int, text: str | None = None, source: Message | None = None) -> bool:
    try:
        if text is not None:
            await bot.send_message(user_id, ANSWER_PREFIX + html.escape(text))
            return True
        if source is None: return False
        if source.text:
            await bot.send_message(user_id, ANSWER_PREFIX + html.escape(source.text))
        elif source.caption is not None:
            await bot.copy_message(chat_id=user_id, from_chat_id=source.chat.id, message_id=source.message_id, caption=ANSWER_PREFIX + html.escape(source.caption))
        else:
            await bot.copy_message(chat_id=user_id, from_chat_id=source.chat.id, message_id=source.message_id)
        return True
    except TelegramForbiddenError:
        logging.warning("Пользователь %s заблокировал бота — ответ не доставлен.", user_id)
    except TelegramAPIError as exc:
        logging.warning("Не удалось доставить ответ пользователю %s: %s", user_id, exc)
    return False

async def finish_admin_answer(message: Message, ticket_id: int, text: str | None = None) -> None:
    ticket = db.get_ticket(ticket_id)
    if ticket is None:
        await message.answer(f"🤷 Обращение №{ticket_id} не найдено.")
        return
    delivered = await deliver_answer(message.bot, ticket["user_id"], text=text if text is not None else message.text, source=message if text is None else None)
    if delivered:
        db.set_ticket_status(ticket_id, "answered")
        await message.answer(f"✅ Ответ отправлен пользователю ID <code>{ticket['user_id']}</code> (обращение №{ticket_id}).")
    else:
        await message.answer("⚠️ Ответ не доставлен: возможно, пользователь заблокировал бота или ещё не нажимал Start.")

def format_user_card(row) -> str:
    username = f"@{row['username']}" if row["username"] else "нет"
    status = "🚫 забанен" if row["banned"] else "✅ активен"
    return (
        f"👤 <b>{html.escape(row['full_name'])}</b>\n"
        f"ID: <code>{row['user_id']}</code>\n"
        f"Username: {html.escape(username)}\n"
        f"Обращений: {row['tickets_count']}\n"
        f"Статус: {status}\n"
        f"Первое обращение: {row['created_at']}\n"
        f"Последняя активность: {row['last_active_at']}"
    )

def format_ticket_info(ticket, user_row) -> str:
    user_part = "неизвестен"
    if user_row is not None:
        user_part = f"{html.escape(user_row['full_name'])} (ID <code>{user_row['user_id']}</code>)"
    body = html.escape(ticket["text"]) if ticket["text"] else "(без текста)"
    status = STATUS_LABELS.get(ticket["status"], ticket["status"])
    content_type = TYPE_LABELS.get(ticket["content_type"], ticket["content_type"])
    return (
        f"🎫 <b>Обращение №{ticket['id']}</b>\nСтатус: {status}\nОт: {user_part}\n"
        f"Тип: {content_type}\nДата: {ticket['created_at']}\nСодержимое: {body}"
    )

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if message.from_user is None: return
    db.upsert_user(message.from_user)
    await state.clear()
    greeting = f"👋 Здравствуйте, {html.escape(message.from_user.first_name or 'друг')}!\n\n"
    if message.from_user.id in ADMIN_IDS:
        greeting += "🛠 Вы администратор — панель управления: /admin\n\n"
    await message.answer(greeting + START_TEXT, reply_markup=user_main_keyboard())

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(CANCEL_TEXT, reply_markup=user_main_keyboard())

@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    parts = []
    if message.from_user is not None:
        parts.append(f"Ваш ID: <code>{message.from_user.id}</code>")
    parts.append(f"ID этого чата: <code>{message.chat.id}</code>")
    if message.from_user is None or message.from_user.id not in ADMIN_IDS:
        parts.append("\nЧтобы стать администратором, добавьте свой ID в ADMIN_IDS.")
    await message.answer("\n".join(parts))

@router.message(F.text == BTN_HELP)
async def btn_help(message: Message) -> None:
    await message.answer(HELP_TEXT)

@router.message(F.text == BTN_MY_ID)
async def btn_my_id(message: Message) -> None:
    if message.from_user is None: return
    await message.answer(f"🆔 Ваш ID: <code>{message.from_user.id}</code>\n\nПередайте этот ID администратору, если вас нужно добавить в админы.")

@router.message(IsAdmin(), Command("admin"))
async def cmd_admin(message: Message) -> None:
    stats = db.stats()
    await message.answer(ADMIN_HELP_TEXT + (f"\n📊 <b>Сейчас:</b> пользователей {stats['users_total']} (забанено {stats['users_banned']}), обращений {stats['tickets_total']}, без ответа {stats['tickets_open']}."))

@router.message(IsAdmin(), Command("stats"))
async def cmd_stats(message: Message) -> None:
    stats = db.stats()
    await message.answer("📊 <b>Статистика</b>\n" f"Пользователей всего: {stats['users_total']}\n" f"Забанено: {stats['users_banned']}\n" f"Обращений всего: {stats['tickets_total']}\n" f"Без ответа: {stats['tickets_open']}")

@router.message(IsAdmin(), Command("users"))
async def cmd_users(message: Message) -> None:
    rows = db.recent_users(limit=10)
    if not rows:
        await message.answer("Пользователей пока нет.")
        return
    lines = ["👥 <b>Последние пользователи:</b>\n"]
    for index, row in enumerate(rows, start=1):
        ban_mark = " 🚫" if row["banned"] else ""
        lines.append(f"{index}. {html.escape(row['full_name'])} (ID <code>{row['user_id']}</code>) — обращений: {row['tickets_count']}{ban_mark}")
    await message.answer("\n".join(lines))

@router.message(IsAdmin(), Command("find"))
async def cmd_find(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Использование: /find <user_id>\nПример: /find 123456789")
        return
    try:
        user_id = int(command.args.strip())
    except ValueError:
        await message.answer("ID должен быть числом. Пример: /find 123456789")
        return
    user_row = db.get_user(user_id)
    if user_row is None:
        await message.answer(f"🤷 Пользователь ID <code>{user_id}</code> в базе не найден.")
        return
    tickets = db.user_tickets(user_id, limit=5)
    lines = [format_user_card(user_row), "\n<b>Последние обращения:</b>"]
    if tickets:
        for ticket in tickets:
            lines.append(f"№{ticket['id']} — {TYPE_LABELS.get(ticket['content_type'], ticket['content_type'])}, {ticket['created_at']}, {STATUS_LABELS.get(ticket['status'], ticket['status'])}")
    else:
        lines.append("обращений нет.")
    await message.answer("\n".join(lines))

async def _ban_by_args(message: Message, command: CommandObject, banned: bool) -> None:
    if not command.args:
        await message.answer(f"Использование: /{'ban' if banned else 'unban'} <user_id>")
        return
    try:
        user_id = int(command.args.strip())
    except ValueError:
        await message.answer("ID должен быть числом. Пример: /ban 123456789")
        return
    if db.set_banned(user_id, banned):
        verb = "заблокирован" if banned else "разблокирован"
        await message.answer(f"✅ Пользователь ID <code>{user_id}</code> {verb}.")
    else:
        await message.answer(f"🤷 Пользователь ID <code>{user_id}</code> не найден в базе.")

@router.message(IsAdmin(), Command("ban"))
async def cmd_ban(message: Message, command: CommandObject) -> None:
    await _ban_by_args(message, command, banned=True)

@router.message(IsAdmin(), Command("unban"))
async def cmd_unban(message: Message, command: CommandObject) -> None:
    await _ban_by_args(message, command, banned=False)

@router.message(IsAdmin(), Command("reply"))
async def cmd_reply(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Использование: /reply <номер обращения> <текст>\nПример: /reply 3 Здравствуйте! Ваш вопрос решён.")
        return
    try:
        ticket_id = int(args[0])
    except ValueError:
        await message.answer("Номер обращения должен быть числом. Пример: /reply 3 Спасибо!")
        return
    await finish_admin_answer(message, ticket_id, text=args[1])

@router.callback_query(IsAdmin(), F.data.startswith("reply:"))
async def cb_start_reply(call: CallbackQuery, state: FSMContext) -> None:
    ticket_id = int(str(call.data).split(":", 1)[1])
    await state.set_state(AdminStates.waiting_for_reply)
    await state.update_data(ticket_id=ticket_id)
    if call.message is not None:
        await call.message.answer(f"✍️ Напишите ответ на обращение №{ticket_id}.\nМожно отправить текст, фото или файл. /cancel — отмена.")
    await call.answer()

@router.callback_query(IsAdmin(), F.data.startswith("info:"))
async def cb_ticket_info(call: CallbackQuery) -> None:
    ticket_id = int(str(call.data).split(":", 1)[1])
    ticket = db.get_ticket(ticket_id)
    if ticket is None:
        await call.answer("Обращение не найдено", show_alert=True)
        return
    if call.message is not None:
        await call.message.answer(format_ticket_info(ticket, db.get_user(ticket["user_id"])))
    await call.answer()

@router.callback_query(IsAdmin(), F.data.startswith("ban:"))
@router.callback_query(IsAdmin(), F.data.startswith("unban:"))
async def cb_ban_toggle(call: CallbackQuery) -> None:
    action, raw_user_id = str(call.data).split(":", 1)
    banned = action == "ban"
    ok = db.set_banned(int(raw_user_id), banned)
    if call.message is not None:
        if ok:
            verb = "заблокирован" if banned else "разблокирован"
            await call.message.answer(f"✅ Пользователь ID <code>{raw_user_id}</code> {verb}.")
        else:
            await call.message.answer("🤷 Пользователь не найден в базе.")
    await call.answer("Готово" if ok else "Пользователь не найден", show_alert=not ok)

@router.message(IsAdmin(), F.reply_to_message)
async def admin_reply_by_reply(message: Message, state: FSMContext) -> None:
    target = message.reply_to_message
    if target.from_user is None or target.from_user.id != message.bot.id: return
    ticket_id = db.find_ticket_by_copy(message.chat.id, target.message_id)
    if ticket_id is None:
        await message.answer("🤷 Это сообщение не является копией обращения.\nОтвечайте на сообщение с обращением или используйте /reply <номер> <текст>.")
        return
    await state.clear()
    await finish_admin_answer(message, ticket_id)

@router.message(IsAdmin(), StateFilter(AdminStates.waiting_for_reply))
async def admin_reply_by_state(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    await state.clear()
    if not isinstance(ticket_id, int):
        await message.answer("🤷 Не понял, на какое обращение отвечать. Нажмите «✉️ Ответить» заново.")
        return
    await finish_admin_answer(message, ticket_id)

@router.message(F.text == BTN_NEW)
async def btn_new_feedback(message: Message, state: FSMContext) -> None:
    if message.from_user is None: return
    await state.set_state(UserStates.waiting_for_message)
    await message.answer(WAITING_PROMPT)

@router.message(StateFilter(UserStates.waiting_for_message))
async def receive_feedback(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None: return
    now = time.monotonic()
    seconds_left = FLOOD_LIMIT_SECONDS - (now - last_feedback_at.get(user.id, 0.0))
    if seconds_left > 0:
        await message.answer(f"⏳ Слишком часто. Подождите {seconds_left:.0f} сек.")
        return
    user_row = db.get_user(user.id)
    if user_row is not None and user_row["banned"]:
        await state.clear()
        await message.answer(BANNED_TEXT)
        return
    content_type, file_id, text = extract_content(message)
    ticket_id = db.add_ticket(user.id, content_type, text, file_id)
    db.register_ticket_activity(user.id)
    last_feedback_at[user.id] = now
    keyboard = admin_ticket_keyboard(ticket_id, user.id, banned=bool(user_row and user_row["banned"]))
    delivered = 0
    for admin_id in ADMIN_IDS:
        try:
            copy = await message.bot.copy_message(chat_id=admin_id, from_chat_id=message.chat.id, message_id=message.message_id, reply_markup=keyboard)
        except TelegramAPIError as exc:
            logging.warning("Не удалось отправить обращение админу %s: %s", admin_id, exc)
            continue
        db.add_copy(admin_id, copy.message_id, ticket_id)
        delivered += 1
    await state.clear()
    if delivered:
        await message.answer(f"✅ <b>Обращение №{ticket_id} принято!</b>\nАдминистраторы ответят вам в этом чате.")
    else:
        await message.answer("⚠️ Обращение сохранено, но администраторы пока недоступны. Попробуйте позже.")

@router.message()
async def fallback(message: Message) -> None:
    if message.from_user is not None and message.from_user.id in ADMIN_IDS: return
    await message.answer(FALLBACK_TEXT, reply_markup=user_main_keyboard())

def setup_logging() -> None:
    logs_dir = Path(__file__).resolve().parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=[logging.FileHandler(logs_dir / "bot.log", encoding="utf-8"), logging.StreamHandler()],
    )

async def main() -> None:
    setup_logging()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    logging.info("Бот запускается. Администраторы: %s", ADMIN_IDS)
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        db.close()
        logging.info("Бот остановлен.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            print(exc.code)
        elif isinstance(exc, KeyboardInterrupt):
            print("\nБот остановлен по Ctrl+C.")
