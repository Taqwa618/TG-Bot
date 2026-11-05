"""
Telegram-бот обратной связи (библиотека aiogram 3.x).

Как работает:
1. Пользователь нажимает /start и кнопку «📝 Написать обращение».
2. Отправляет сообщение: текст, фото, видео, видео-заметку, голосовое,
   аудио, документ или стикер.
3. Бот сохраняет обращение в SQLite и копирует его всем администраторам
   (личка и/или группа, указанные в ADMIN_IDS).
4. Администратор отвечает одним из способов:
   • кнопкой «✉️ Ответить» под обращением — затем пишет ответ;
   • Reply-сообщением на копию обращения — любой текст или медиа;
   • командой /reply <номер обращения> <текст>.
5. Ответ доставляется пользователю в чат с ботом.

Команды администратора: /admin, /stats, /users, /find <user_id>,
/ban <user_id>, /unban <user_id>, /reply <ticket_id> <текст>.

ВАЖНО про порядок хендлеров: aiogram проверяет их сверху вниз и вызывает
первый подходящий. Поэтому команды зарегистрированы раньше общего
обработчика обращения, а fallback (подсказка) — в самом конце.
"""

import asyncio
import html
import logging
import time
from pathlib import Path

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

import config
from storage import Database

# --------------------------------------------------------------------------- #
#  Объекты и глобальные переменные
# --------------------------------------------------------------------------- #

router = Router()
db = Database(config.DB_PATH)

# Время последнего обращения каждого пользователя (для антифлуда).
# Хранится просто в памяти — после перезапуска бота обнуляется.
last_feedback_at: dict[int, float] = {}

# --------------------------------------------------------------------------- #
#  Кнопки и тексты бота (всё, что видит пользователь — в одном месте)
# --------------------------------------------------------------------------- #

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

# Подпись, с которой приходит ответ пользователю
ANSWER_PREFIX = "✉️ <b>Ответ поддержки:</b>\n"

# Красивые подписи для типов контента и статусов
TYPE_LABELS = {
    "text": "текст",
    "photo": "фото",
    "video": "видео",
    "video_note": "видео-заметка",
    "voice": "голосовое",
    "audio": "аудио",
    "document": "документ",
    "sticker": "стикер",
    "other": "другое",
}
STATUS_LABELS = {"new": "🆕 новое", "answered": "✅ отвечено"}


# --------------------------------------------------------------------------- #
#  Вспомогательные конструкции
# --------------------------------------------------------------------------- #

class IsAdmin(BaseFilter):
    """Пропускает события только от администраторов из config.ADMIN_IDS."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user is not None and event.from_user.id in config.ADMIN_IDS


class UserStates(StatesGroup):
    """Состояние «пользователь пишет обращение»."""
    waiting_for_message = State()


class AdminStates(StatesGroup):
    """Состояние «администратор пишет ответ на выбранное обращение»."""
    waiting_for_reply = State()


def user_main_keyboard() -> ReplyKeyboardMarkup:
    """Обычная (reply) клавиатура пользователя."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_NEW)],
            [KeyboardButton(text=BTN_HELP), KeyboardButton(text=BTN_MY_ID)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие или напишите команду",
    )


def admin_ticket_keyboard(ticket_id: int, user_id: int, banned: bool) -> InlineKeyboardMarkup:
    """Инлайн-кнопки под копией обращения в чате администратора."""
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
    """Определяет тип сообщения.

    Возвращает кортеж (тип, file_id, текст). Для медиа текстом считается
    подпись (caption), а file_id нужен, если захотите докачать файл.
    """
    if message.text:
        return "text", None, message.text
    if message.photo:  # фото приходит списком размеров — берём самый большой
        return "photo", message.photo[-1].file_id, message.caption
    if message.video:
        return "video", message.video.file_id, message.caption
    if message.video_note:
        return "video_note", message.video_note.file_id, None
    if message.voice:
        return "voice", message.voice.file_id, message.caption
    if message.audio:
        return "audio", message.audio.file_id, message.caption
    if message.document:
        return "document", message.document.file_id, message.caption
    if message.sticker:
        return "sticker", message.sticker.file_id, None
    return "other", None, None


# --------------------------------------------------------------------------- #
#  Доставка ответа пользователю
# --------------------------------------------------------------------------- #

async def deliver_answer(
    bot: Bot, user_id: int, text: str | None = None, source: Message | None = None
) -> bool:
    """Отправляет ответ администратора пользователю.

    text — готовый текст (например, из команды /reply);
    source — сообщение администратора целиком (текст или медиа).
    Возвращает True, если доставлено.
    """
    try:
        if text is not None:
            await bot.send_message(user_id, ANSWER_PREFIX + html.escape(text))
            return True
        if source is None:
            return False
        if source.text:
            await bot.send_message(user_id, ANSWER_PREFIX + html.escape(source.text))
        elif source.caption is not None:
            # Медиа с подписью: добавляем свой префикс к подписи
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source.chat.id,
                message_id=source.message_id,
                caption=ANSWER_PREFIX + html.escape(source.caption),
            )
        else:
            # Медиа без подписи просто копируем как есть
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source.chat.id,
                message_id=source.message_id,
            )
        return True
    except TelegramForbiddenError:
        logging.warning("Пользователь %s заблокировал бота — ответ не доставлен.", user_id)
    except TelegramAPIError as exc:
        logging.warning("Не удалось доставить ответ пользователю %s: %s", user_id, exc)
    return False


async def finish_admin_answer(
    message: Message, ticket_id: int, text: str | None = None
) -> None:
    """Общий финал для ответа админа: найти обращение, доставить, сохранить статус.

    text — готовый текст ответа (для команды /reply);
    если не задан, берётся всё сообщение администратора (текст или медиа).
    """
    ticket = db.get_ticket(ticket_id)
    if ticket is None:
        await message.answer(f"🤷 Обращение №{ticket_id} не найдено.")
        return
    delivered = await deliver_answer(
        message.bot,
        ticket["user_id"],
        text=text if text is not None else message.text,
        source=message if text is None else None,
    )
    if delivered:
        db.set_ticket_status(ticket_id, "answered")
        await message.answer(
            f"✅ Ответ отправлен пользователю ID "
            f"<code>{ticket['user_id']}</code> (обращение №{ticket_id})."
        )
    else:
        await message.answer(
            "⚠️ Ответ не доставлен: возможно, пользователь заблокировал бота "
            "или ещё не нажимал Start."
        )


# --------------------------------------------------------------------------- #
#  Форматирование карточек
# --------------------------------------------------------------------------- #

def format_user_card(row) -> str:
    """Красивая карточка пользователя для команд /find, /users."""
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
    """Карточка обращения для кнопки «ℹ️ Инфо»."""
    user_part = "неизвестен"
    if user_row is not None:
        user_part = (
            f"{html.escape(user_row['full_name'])} "
            f"(ID <code>{user_row['user_id']}</code>)"
        )
    body = html.escape(ticket["text"]) if ticket["text"] else "(без текста)"
    status = STATUS_LABELS.get(ticket["status"], ticket["status"])
    content_type = TYPE_LABELS.get(ticket["content_type"], ticket["content_type"])
    return (
        f"🎫 <b>Обращение №{ticket['id']}</b>\n"
        f"Статус: {status}\n"
        f"От: {user_part}\n"
        f"Тип: {content_type}\n"
        f"Дата: {ticket['created_at']}\n"
        f"Содержимое: {body}"
    )


# --------------------------------------------------------------------------- #
#  Общие хендлеры (для всех)
# --------------------------------------------------------------------------- #

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Приветствие. Сбрасывает любое незавершённое действие."""
    if message.from_user is None:
        return
    db.upsert_user(message.from_user)
    await state.clear()
    greeting = f"👋 Здравствуйте, {html.escape(message.from_user.first_name or 'друг')}!\n\n"
    if message.from_user.id in config.ADMIN_IDS:
        greeting += "🛠 Вы администратор — панель управления: /admin\n\n"
    await message.answer(greeting + START_TEXT, reply_markup=user_main_keyboard())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Отмена любого активного действия (отправка обращения или ответа)."""
    await state.clear()
    await message.answer(CANCEL_TEXT, reply_markup=user_main_keyboard())


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    """Показывает ID пользователя и чата (нужно для ADMIN_IDS)."""
    parts = []
    if message.from_user is not None:
        parts.append(f"Ваш ID: <code>{message.from_user.id}</code>")
    parts.append(f"ID этого чата: <code>{message.chat.id}</code>")
    if message.from_user is None or message.from_user.id not in config.ADMIN_IDS:
        parts.append(
            "\nЧтобы стать администратором, добавьте свой ID в ADMIN_IDS в файле .env"
        )
    await message.answer("\n".join(parts))


@router.message(F.text == BTN_HELP)
async def btn_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(F.text == BTN_MY_ID)
async def btn_my_id(message: Message) -> None:
    if message.from_user is None:
        return
    await message.answer(
        f"🆔 Ваш ID: <code>{message.from_user.id}</code>\n\n"
        "Передайте этот ID администратору, если вас нужно добавить в админы."
    )


# --------------------------------------------------------------------------- #
#  Хендлеры администратора
# --------------------------------------------------------------------------- #

@router.message(IsAdmin(), Command("admin"))
async def cmd_admin(message: Message) -> None:
    """Справка для администратора + краткая статистика."""
    stats = db.stats()
    await message.answer(
        ADMIN_HELP_TEXT + (
            f"\n📊 <b>Сейчас:</b> пользователей {stats['users_total']} "
            f"(забанено {stats['users_banned']}), обращений {stats['tickets_total']}, "
            f"без ответа {stats['tickets_open']}."
        )
    )


@router.message(IsAdmin(), Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Только статистика."""
    stats = db.stats()
    await message.answer(
        "📊 <b>Статистика</b>\n"
        f"Пользователей всего: {stats['users_total']}\n"
        f"Забанено: {stats['users_banned']}\n"
        f"Обращений всего: {stats['tickets_total']}\n"
        f"Без ответа: {stats['tickets_open']}"
    )


@router.message(IsAdmin(), Command("users"))
async def cmd_users(message: Message) -> None:
    """Список последних активных пользователей."""
    rows = db.recent_users(limit=10)
    if not rows:
        await message.answer("Пользователей пока нет.")
        return
    lines = ["👥 <b>Последние пользователи:</b>\n"]
    for index, row in enumerate(rows, start=1):
        ban_mark = " 🚫" if row["banned"] else ""
        lines.append(
            f"{index}. {html.escape(row['full_name'])} "
            f"(ID <code>{row['user_id']}</code>) — обращений: {row['tickets_count']}{ban_mark}"
        )
    await message.answer("\n".join(lines))


@router.message(IsAdmin(), Command("find"))
async def cmd_find(message: Message, command: CommandObject) -> None:
    """Карточка пользователя + его последние обращения: /find <user_id>."""
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
            lines.append(
                f"№{ticket['id']} — {TYPE_LABELS.get(ticket['content_type'], ticket['content_type'])}, "
                f"{ticket['created_at']}, {STATUS_LABELS.get(ticket['status'], ticket['status'])}"
            )
    else:
        lines.append("обращений нет.")
    await message.answer("\n".join(lines))


async def _ban_by_args(message: Message, command: CommandObject, banned: bool) -> None:
    """Общая часть для /ban и /unban."""
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
    """Банит пользователя: его обращения перестают приниматься."""
    await _ban_by_args(message, command, banned=True)


@router.message(IsAdmin(), Command("unban"))
async def cmd_unban(message: Message, command: CommandObject) -> None:
    """Снимает бан."""
    await _ban_by_args(message, command, banned=False)


@router.message(IsAdmin(), Command("reply"))
async def cmd_reply(message: Message, command: CommandObject) -> None:
    """Ответ по номеру обращения: /reply 3 Ваш вопрос рассмотрен."""
    args = (command.args or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Использование: /reply <номер обращения> <текст>\n"
            "Пример: /reply 3 Здравствуйте! Ваш вопрос решён."
        )
        return
    try:
        ticket_id = int(args[0])
    except ValueError:
        await message.answer("Номер обращения должен быть числом. Пример: /reply 3 Спасибо!")
        return
    await finish_admin_answer(message, ticket_id, text=args[1])


# --------------------------------------------------------------------------- #
#  Ответы администратора (инлайн-кнопки)
# --------------------------------------------------------------------------- #

@router.callback_query(IsAdmin(), F.data.startswith("reply:"))
async def cb_start_reply(call: CallbackQuery, state: FSMContext) -> None:
    """Админ нажал «✉️ Ответить» — бот ждёт следующий ответ как текст ответа."""
    ticket_id = int(str(call.data).split(":", 1)[1])
    await state.set_state(AdminStates.waiting_for_reply)
    await state.update_data(ticket_id=ticket_id)
    if call.message is not None:
        await call.message.answer(
            f"✍️ Напишите ответ на обращение №{ticket_id}.\n"
            "Можно отправить текст, фото или файл. /cancel — отмена."
        )
    await call.answer()


@router.callback_query(IsAdmin(), F.data.startswith("info:"))
async def cb_ticket_info(call: CallbackQuery) -> None:
    """Кнопка «ℹ️ Инфо» — карточка обращения."""
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
    """Кнопки «🚫 Забанить» / «✅ Разбанить» под обращением."""
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


# --------------------------------------------------------------------------- #
#  Ответы администратора (Reply-сообщение на копию обращения)
# --------------------------------------------------------------------------- #

@router.message(IsAdmin(), F.reply_to_message)
async def admin_reply_by_reply(message: Message, state: FSMContext) -> None:
    """Админ ответил на копию обращения — доставляем ответ пользователю."""
    target = message.reply_to_message
    if target.from_user is None or target.from_user.id != message.bot.id:
        return  # ответ не на сообщение бота — игнорируем
    ticket_id = db.find_ticket_by_copy(message.chat.id, target.message_id)
    if ticket_id is None:
        await message.answer(
            "🤷 Это сообщение не является копией обращения.\n"
            "Отвечайте на сообщение с обращением или используйте /reply <номер> <текст>."
        )
        return
    # Сбрасываем FSM: возможно, админ был в режиме ответа после кнопки «✉️ Ответить»
    await state.clear()
    await finish_admin_answer(message, ticket_id)


# --------------------------------------------------------------------------- #
#  Ответы администратора (после кнопки «✉️ Ответить»)
# --------------------------------------------------------------------------- #

@router.message(IsAdmin(), StateFilter(AdminStates.waiting_for_reply))
async def admin_reply_by_state(message: Message, state: FSMContext) -> None:
    """Админ отправляет ответ, начатый кнопкой «✉️ Ответить»."""
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    await state.clear()
    if not isinstance(ticket_id, int):
        await message.answer("🤷 Не понял, на какое обращение отвечать. Нажмите «✉️ Ответить» заново.")
        return
    await finish_admin_answer(message, ticket_id)


# --------------------------------------------------------------------------- #
#  Приём обращения от пользователя
# --------------------------------------------------------------------------- #

@router.message(F.text == BTN_NEW)
async def btn_new_feedback(message: Message, state: FSMContext) -> None:
    """Пользователь нажал «📝 Написать обращение» — включаем режим приёма."""
    if message.from_user is None:
        return
    await state.set_state(UserStates.waiting_for_message)
    await message.answer(WAITING_PROMPT)


@router.message(StateFilter(UserStates.waiting_for_message))
async def receive_feedback(message: Message, state: FSMContext) -> None:
    """Принимает обращение любого типа, сохраняет и рассылает админам."""
    user = message.from_user
    if user is None:
        return

    # 1. Антифлуд: слишком частые обращения игнорируем
    now = time.monotonic()
    seconds_left = config.FLOOD_LIMIT_SECONDS - (now - last_feedback_at.get(user.id, 0.0))
    if seconds_left > 0:
        await message.answer(f"⏳ Слишком часто. Подождите {seconds_left:.0f} сек.")
        return

    # 2. Забаненные не могут писать
    user_row = db.get_user(user.id)
    if user_row is not None and user_row["banned"]:
        await state.clear()
        await message.answer(BANNED_TEXT)
        return

    # 3. Сохраняем обращение
    content_type, file_id, text = extract_content(message)
    ticket_id = db.add_ticket(user.id, content_type, text, file_id)
    db.register_ticket_activity(user.id)
    last_feedback_at[user.id] = now

    # 4. Рассылаем копию всем администраторам с кнопками управления
    keyboard = admin_ticket_keyboard(ticket_id, user.id, banned=bool(user_row and user_row["banned"]))
    delivered = 0
    for admin_id in config.ADMIN_IDS:
        try:
            copy = await message.bot.copy_message(
                chat_id=admin_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=keyboard,
            )
        except TelegramAPIError as exc:
            logging.warning("Не удалось отправить обращение админу %s: %s", admin_id, exc)
            continue
        db.add_copy(admin_id, copy.message_id, ticket_id)
        delivered += 1

    await state.clear()
    if delivered:
        await message.answer(
            f"✅ <b>Обращение №{ticket_id} принято!</b>\n"
            "Администраторы ответят вам в этом чате."
        )
    else:
        await message.answer(
            "⚠️ Обращение сохранено, но администраторы пока недоступны. "
            "Попробуйте позже."
        )


# --------------------------------------------------------------------------- #
#  Fallback: любое другое сообщение
# --------------------------------------------------------------------------- #

@router.message()
async def fallback(message: Message) -> None:
    """Подсказка для сообщений без контекста. Админам не мешаем."""
    if message.from_user is not None and message.from_user.id in config.ADMIN_IDS:
        return
    await message.answer(FALLBACK_TEXT, reply_markup=user_main_keyboard())


# --------------------------------------------------------------------------- #
#  Запуск бота
# --------------------------------------------------------------------------- #

def setup_logging() -> None:
    """Логи одновременно в консоль и в файл logs/bot.log."""
    logs_dir = Path(__file__).resolve().parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=[
            logging.FileHandler(logs_dir / "bot.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


async def main() -> None:
    config.validate()          # проверяем .env и выходим с понятной ошибкой
    setup_logging()
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    logging.info("Бот запускается. Администраторы: %s", config.ADMIN_IDS)
    # Сбрасываем вебхук и не обрабатываем старые отложенные апдейты
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
        # SystemExit — это понятная ошибка конфигурации из config.validate()
        if isinstance(exc, SystemExit) and exc.code:
            print(exc.code)
        elif isinstance(exc, KeyboardInterrupt):
            print("\nБот остановлен по Ctrl+C.")
