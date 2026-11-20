import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, MessageEntityType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    MessageEntity,
)
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("cottaven_bot")


# ==========================================
# КОНФИГУРАЦИЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# ==========================================
def get_env_int(name: str, default: int = 0) -> int:
    val = os.getenv(name, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning(f"Переменная {name} имеет некорректное числовое значение: {val}")
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = get_env_int("OWNER_ID")
CHANNEL_ID = get_env_int("CHANNEL_ID")
RP_CHANNEL_ID = get_env_int("RP_CHANNEL_ID")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/").strip()

ENV_MOD_CHAT_ID = get_env_int("MOD_CHAT_ID")
ENV_MOD_TOPIC_ID = get_env_int("MOD_TOPIC_ID")

DB_PATH = os.getenv("DB_PATH", "cottaven_bot.db")

if not BOT_TOKEN:
    raise RuntimeError("КРИТИЧЕСКАЯ ОШИБКА: Не задана переменная окружения BOT_TOKEN!")


# ==========================================
# БАЗА ДАННЫХ (SQLite)
# ==========================================
async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_id INTEGER NOT NULL,
                author_username TEXT,
                author_name TEXT,
                target_channel TEXT NOT NULL,
                text_content TEXT,
                entities_json TEXT,
                photos_json TEXT,
                buttons_json TEXT,
                status TEXT DEFAULT 'pending',
                mod_chat_id INTEGER,
                mod_topic_id INTEGER,
                mod_control_msg_id INTEGER,
                reviewer_id INTEGER,
                reviewer_name TEXT,
                reject_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.commit()


async def is_admin(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM admins WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None


async def add_admin_db(user_id: int, username: str, full_name: str, added_by: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO admins (user_id, username, full_name, added_by)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, username, full_name, added_by),
        )
        await db.commit()


async def remove_admin_db(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        await db.commit()
        return cursor.rowcount > 0


async def get_all_admins() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM admins ORDER BY added_at ASC") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()


async def get_setting(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def get_mod_destination() -> tuple[int, Optional[int]]:
    chat_str = await get_setting("mod_chat_id")
    topic_str = await get_setting("mod_topic_id")

    chat_id = int(chat_str) if chat_str else ENV_MOD_CHAT_ID
    topic_id: Optional[int] = None

    if topic_str is not None and topic_str != "":
        t_val = int(topic_str)
        topic_id = t_val if t_val > 0 else None
    elif ENV_MOD_TOPIC_ID > 0:
        topic_id = ENV_MOD_TOPIC_ID

    return chat_id, topic_id


# ==========================================
# РАБОТА С ENTITIES, ПОДПИСЬЮ И КНОПКАМИ
# ==========================================
def serialize_entities(entities: Optional[List[MessageEntity]]) -> str:
    if not entities:
        return "[]"
    return json.dumps([e.model_dump(exclude_none=True) for e in entities], ensure_ascii=False)


def deserialize_entities(entities_json: Optional[str]) -> List[MessageEntity]:
    if not entities_json:
        return []
    raw = json.loads(entities_json)
    return [MessageEntity(**item) for item in raw]


def utf16_len(text: str) -> int:
    """Telegram считает offset и length в UTF-16 code units (критично для эмодзи и премиум-эмодзи)."""
    return len(text.encode("utf-16-le")) // 2


def build_post_with_footer(
    raw_text: Optional[str],
    raw_entities: List[MessageEntity],
) -> tuple[str, List[MessageEntity]]:
    """
    Добавляет в конец поста обязательную подпись:
    COTTAVEN RP • Подписаться (где 'Подписаться' — ссылка на CHANNEL_LINK),
    сохраняя все оригинальные entities (включая custom_emoji / Telegram Premium).
    """
    base_text = raw_text or ""
    entities = list(raw_entities)

    separator = "\n\n" if base_text.strip() else ""
    prefix = "COTTAVEN RP • "
    link_word = "Подписаться"

    full_text = f"{base_text}{separator}{prefix}{link_word}"

    link_offset = utf16_len(f"{base_text}{separator}{prefix}")
    link_length = utf16_len(link_word)

    footer_entity = MessageEntity(
        type=MessageEntityType.TEXT_LINK,
        offset=link_offset,
        length=link_length,
        url=CHANNEL_LINK,
    )
    entities.append(footer_entity)
    return full_text, entities


BUTTON_STYLES = {
    "default": ("⚪ Стандартная", None),
    "primary": ("🔵 Синяя (Primary)", "primary"),
    "success": ("🟢 Зелёная (Success)", "success"),
    "danger": ("🔴 Красная (Danger)", "danger"),
}


def build_custom_keyboard(
    buttons_data: List[Dict[str, str]],
    extra_rows: Optional[List[List[InlineKeyboardButton]]] = None,
) -> Optional[InlineKeyboardMarkup]:
    """
    Собирает клавиатуру из пользовательских кнопок с поддержкой цветов (style)
    и при необходимости добавляет управляющие кнопки снизу.
    """
    rows: List[List[InlineKeyboardButton]] = []
    for btn in buttons_data:
        style_val = btn.get("style")
        kwargs: Dict[str, Any] = {
            "text": btn["text"],
            "url": btn["url"],
        }
        if style_val in ("primary", "success", "danger"):
            kwargs["style"] = style_val
        rows.append([InlineKeyboardButton(**kwargs)])

    if extra_rows:
        rows.extend(extra_rows)

    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==========================================
# FSM СОСТОЯНИЯ
# ==========================================
class PostCreation(StatesGroup):
    choosing_channel = State()
    entering_content = State()
    configuring_buttons = State()
    entering_btn_text = State()
    entering_btn_url = State()
    choosing_btn_style = State()
    confirming_post = State()


class ModRejectState(StatesGroup):
    waiting_reason = State()


# ==========================================
# БУФЕР ДЛЯ АЛЬБОМОВ (ДО 10 ФОТО)
# ==========================================
@dataclass
class AlbumBuffer:
    photos: List[str] = field(default_factory=list)
    caption: Optional[str] = None
    caption_entities: List[MessageEntity] = field(default_factory=list)
    last_message: Optional[Message] = None
    task: Optional[asyncio.Task] = None


album_buffers: Dict[str, AlbumBuffer] = {}

router = Router()


# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ КЛАВИАТУРЫ
# ==========================================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Предложить пост",
                    callback_data="create_post",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📢 Наш основной канал",
                    url=CHANNEL_LINK,
                )
            ],
        ]
    )


def channel_choice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏛 Основной ТГК",
                    callback_data="target:main",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📰 ТГК для РП новостей",
                    callback_data="target:rp",
                    style="success",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="cancel_creation",
                    style="danger",
                )
            ],
        ]
    )


def button_builder_kb(buttons: List[Dict[str, str]]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for idx, b in enumerate(buttons):
        style_label = BUTTON_STYLES.get(b.get("style") or "default", ("⚪", None))[0].split()[0]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{style_label} {b['text']} (Удалить)",
                    callback_data=f"del_btn:{idx}",
                )
            ]
        )

    if len(buttons) < 8:
        rows.append(
            [
                InlineKeyboardButton(
                    text="➕ Добавить кнопку",
                    callback_data="add_btn",
                    style="primary",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="👁 Предпросмотр и отправка",
                callback_data="to_preview",
                style="success",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="❌ Отмена",
                callback_data="cancel_creation",
                style="danger",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn_style_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔵 Синяя кнопка",
                    callback_data="btn_style:primary",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="🟢 Зелёная кнопка",
                    callback_data="btn_style:success",
                    style="success",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔴 Красная кнопка",
                    callback_data="btn_style:danger",
                    style="danger",
                ),
                InlineKeyboardButton(
                    text="⚪ Стандартная кнопка",
                    callback_data="btn_style:default",
                ),
            ],
        ]
    )


def confirm_post_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Отправить на проверку",
                    callback_data="submit_post",
                    style="success",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎛 Изменить кнопки",
                    callback_data="back_to_buttons",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Отменить пост",
                    callback_data="cancel_creation",
                    style="danger",
                )
            ],
        ]
    )


def moderation_kb(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Опубликовать",
                    callback_data=f"mod_approve:{sub_id}",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"mod_reject:{sub_id}",
                    style="danger",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💬 Отклонить с причиной",
                    callback_data=f"mod_reject_reason:{sub_id}",
                )
            ],
        ]
    )


# ==========================================
# КОМАНДЫ ВЛАДЕЛЬЦА И АДМИНИСТРАТОРОВ
# ==========================================
@router.message(Command("setmod"))
async def cmd_setmod(message: Message) -> None:
    if not message.from_user or message.from_user.id != OWNER_ID:
        return

    if message.chat.type == ChatType.PRIVATE:
        await message.answer(
            "⚠️ Эту команду нужно вызвать в рабочем чате модерации (в нужном топике)."
        )
        return

    chat_id = message.chat.id
    topic_id = message.message_thread_id or 0

    await set_setting("mod_chat_id", str(chat_id))
    await set_setting("mod_topic_id", str(topic_id))

    topic_info = f"Топик ID: <code>{topic_id}</code>" if topic_id else "Общий чат (без топика)"
    await message.answer(
        f"✅ <b>Чат модерации успешно привязан!</b>\n\n"
        f"• Чат ID: <code>{chat_id}</code>\n"
        f"• {topic_info}\n\n"
        f"Теперь все новые посты от участников будут поступать сюда."
    )


@router.message(Command("addadmin"))
async def cmd_addadmin(message: Message, bot: Bot) -> None:
    if not message.from_user or message.from_user.id != OWNER_ID:
        await message.answer("⛔ Эта команда доступна только главному владельцу бота.")
        return

    target_id: Optional[int] = None
    target_username: str = ""
    target_name: str = "Администратор"

    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        target_id = u.id
        target_username = u.username or ""
        target_name = u.full_name
    else:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().lstrip("-").isdigit():
            await message.answer(
                "ℹ️ <b>Использование:</b>\n"
                "<code>/addadmin 123456789</code> или ответьте этой командой на сообщение пользователя."
            )
            return
        target_id = int(parts[1].strip())
        try:
            chat_info = await bot.get_chat(target_id)
            target_username = chat_info.username or ""
            target_name = chat_info.full_name or f"ID {target_id}"
        except Exception:
            target_name = f"ID {target_id}"

    if target_id == OWNER_ID:
        await message.answer("👑 Вы уже являетесь главным владельцем бота.")
        return

    await add_admin_db(target_id, target_username, target_name, message.from_user.id)
    uname_str = f" (@{target_username})" if target_username else ""
    await message.answer(
        f"✅ <b>Новый администратор назначен!</b>\n"
        f"👤 {target_name}{uname_str}\n"
        f"🆔 <code>{target_id}</code>"
    )


@router.message(Command("deladmin"))
async def cmd_deladmin(message: Message) -> None:
    if not message.from_user or message.from_user.id != OWNER_ID:
        await message.answer("⛔ Эта команда доступна только главному владельцу бота.")
        return

    target_id: Optional[int] = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
    else:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().lstrip("-").isdigit():
            await message.answer(
                "ℹ️ <b>Использование:</b>\n"
                "<code>/deladmin 123456789</code> или ответьте этой командой на сообщение пользователя."
            )
            return
        target_id = int(parts[1].strip())

    removed = await remove_admin_db(target_id)
    if removed:
        await message.answer(f"🗑 Администратор <code>{target_id}</code> успешно снят.")
    else:
        await message.answer(f"⚠️ Пользователь <code>{target_id}</code> не найден в списке администраторов.")


@router.message(Command("admins"))
async def cmd_admins(message: Message) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        return

    admins = await get_all_admins()
    lines = [f"👑 <b>Владелец:</b> <code>{OWNER_ID}</code>\n"]
    if not admins:
        lines.append("📋 Назначенных администраторов пока нет.")
    else:
        lines.append("🛡 <b>Список администраторов:</b>")
        for i, adm in enumerate(admins, start=1):
            uname = f" (@{adm['username']})" if adm.get("username") else ""
            lines.append(f"{i}. {adm['full_name']}{uname} — <code>{adm['user_id']}</code>")

    await message.answer("\n".join(lines))


# ==========================================
# ПОЛЬЗОВАТЕЛЬСКИЙ ФЛОУ ПРЕДЛОЖКИ (ЛИЧКА)
# ==========================================
@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "👋 <b>Добро пожаловать в предложку COTTAVEN RP!</b>\n\n"
        "Здесь вы можете отправить новость, скриншоты или РП-событие на публикацию в наши Telegram-каналы.\n"
        "• Поддерживается любое форматирование и <b>Telegram Premium эмодзи</b>\n"
        "• Можно прикрепить <b>до 10 фотографий</b>\n"
        "• Можно добавить красивые <b>цветные кнопки-ссылки</b> к посту",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("cancel"), F.chat.type == ChatType.PRIVATE)
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Создание поста отменено.", reply_markup=main_menu_kb())


@router.callback_query(F.data == "cancel_creation")
async def cb_cancel_creation(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "❌ Создание поста отменено.",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "create_post")
async def cb_create_post(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(PostCreation.choosing_channel)
    await callback.message.edit_text(
        "📌 <b>Шаг 1 из 3 — Выбор канала</b>\n\n"
        "В какой Telegram-канал вы хотите предложить публикацию?",
        reply_markup=channel_choice_kb(),
    )
    await callback.answer()


@router.callback_query(PostCreation.choosing_channel, F.data.startswith("target:"))
async def cb_choose_channel(callback: CallbackQuery, state: FSMContext) -> None:
    target = callback.data.split(":")[1]
    channel_label = "🏛 Основной ТГК" if target == "main" else "📰 ТГК для РП новостей"

    await state.update_data(
        target_channel=target,
        channel_label=channel_label,
        photos=[],
        text_content="",
        entities_json="[]",
        buttons=[],
    )
    await state.set_state(PostCreation.entering_content)

    await callback.message.edit_text(
        f"✅ Выбран канал: <b>{channel_label}</b>\n\n"
        "📝 <b>Шаг 2 из 3 — Контент поста</b>\n\n"
        "Отправьте текст поста, одну фотографию или альбом <b>до 10 фотографий</b> с подписью.\n"
        "<i>Все стили текста, цитаты, спойлеры и Telegram Premium эмодзи сохранятся в точности как в вашем сообщении.</i>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data="cancel_creation",
                        style="danger",
                    )
                ]
            ]
        ),
    )
    await callback.answer()


async def proceed_to_buttons_step(message: Message, state: FSMContext) -> None:
    await state.set_state(PostCreation.configuring_buttons)
    data = await state.get_data()
    buttons = data.get("buttons", [])
    photos_count = len(data.get("photos", []))

    await message.answer(
        f"✅ Контент принят! (Фотографий: <b>{photos_count}</b>)\n\n"
        "🎛 <b>Шаг 3 из 3 — Настройка кнопок</b>\n\n"
        "Вы можете добавить к посту интерактивные кнопки-ссылки с выбором цвета (синяя, зелёная, красная или стандартная) или сразу перейти к предпросмотру.",
        reply_markup=button_builder_kb(buttons),
    )


async def finalize_album(media_group_id: str, state: FSMContext) -> None:
    await asyncio.sleep(1.2)
    buf = album_buffers.pop(media_group_id, None)
    if not buf or not buf.last_message:
        return

    photos = buf.photos[:10]
    await state.update_data(
        photos=photos,
        text_content=buf.caption or "",
        entities_json=serialize_entities(buf.caption_entities),
    )
    if len(buf.photos) > 10:
        await buf.last_message.answer("⚠️ Вы отправили больше 10 фото. Сохранены первые 10 фотографий.")
    await proceed_to_buttons_step(buf.last_message, state)


@router.message(PostCreation.entering_content, F.chat.type == ChatType.PRIVATE)
async def handle_post_content(message: Message, state: FSMContext) -> None:
    # Если это медиагруппа (альбом до 10 фото)
    if message.media_group_id:
        if not message.photo:
            return
        mg_id = message.media_group_id
        if mg_id not in album_buffers:
            album_buffers[mg_id] = AlbumBuffer()
        buf = album_buffers[mg_id]
        buf.photos.append(message.photo[-1].file_id)
        buf.last_message = message
        if message.caption:
            buf.caption = message.caption
            buf.caption_entities = list(message.caption_entities or [])

        if buf.task:
            buf.task.cancel()
        buf.task = asyncio.create_task(finalize_album(mg_id, state))
        return

    # Одиночное фото с текстом или без
    if message.photo:
        await state.update_data(
            photos=[message.photo[-1].file_id],
            text_content=message.caption or "",
            entities_json=serialize_entities(message.caption_entities),
        )
        await proceed_to_buttons_step(message, state)
        return

    # Чистый текстовый пост
    if message.text:
        await state.update_data(
            photos=[],
            text_content=message.text,
            entities_json=serialize_entities(message.entities),
        )
        await proceed_to_buttons_step(message, state)
        return

    await message.answer("⚠️ Пожалуйста, отправьте текст или фотографии (до 10 штук).")


# ==========================================
# КОНСТРУКТОР ЦВЕТНЫХ КНОПОК
# ==========================================
@router.callback_query(PostCreation.configuring_buttons, F.data == "add_btn")
async def cb_add_btn(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PostCreation.entering_btn_text)
    await callback.message.edit_text(
        "✏️ <b>Введите текст для кнопки</b> (например: <code>Подать заявку</code> или <code>Подробнее</code>):"
    )
    await callback.answer()


@router.message(PostCreation.entering_btn_text, F.text)
async def handle_btn_text(message: Message, state: FSMContext) -> None:
    btn_text = message.text.strip()
    if len(btn_text) > 40:
        await message.answer("⚠️ Текст кнопки слишком длинный (максимум 40 символов). Введите короче:")
        return

    await state.update_data(temp_btn_text=btn_text)
    await state.set_state(PostCreation.entering_btn_url)
    await message.answer(
        f"🔗 Теперь отправьте <b>ссылку (URL)</b> для кнопки «<b>{btn_text}</b>»:\n"
        "<i>Ссылка должна начинаться с https:// или http://</i>"
    )


@router.message(PostCreation.entering_btn_url, F.text)
async def handle_btn_url(message: Message, state: FSMContext) -> None:
    url = message.text.strip()
    if not re.match(r"^https?://[^\s]+$", url):
        await message.answer("⚠️ Некорректная ссылка! Она должна начинаться с <code>https://</code> или <code>http://</code>. Попробуйте снова:")
        return

    await state.update_data(temp_btn_url=url)
    await state.set_state(PostCreation.choosing_btn_style)
    await message.answer(
        "🎨 <b>Выберите цвет кнопки:</b>",
        reply_markup=btn_style_kb(),
    )


@router.callback_query(PostCreation.choosing_btn_style, F.data.startswith("btn_style:"))
async def cb_choose_btn_style(callback: CallbackQuery, state: FSMContext) -> None:
    style_key = callback.data.split(":")[1]
    data = await state.get_data()
    buttons: List[Dict[str, str]] = data.get("buttons", [])
    buttons.append(
        {
            "text": data["temp_btn_text"],
            "url": data["temp_btn_url"],
            "style": style_key,
        }
    )
    await state.update_data(buttons=buttons)
    await state.set_state(PostCreation.configuring_buttons)

    await callback.message.edit_text(
        f"✅ Кнопка «<b>{data['temp_btn_text']}</b>» добавлена! Всего кнопок: <b>{len(buttons)}</b>.",
        reply_markup=button_builder_kb(buttons),
    )
    await callback.answer()


@router.callback_query(PostCreation.configuring_buttons, F.data.startswith("del_btn:"))
async def cb_del_btn(callback: CallbackQuery, state: FSMContext) -> None:
    idx = int(callback.data.split(":")[1])
    data = await state.get_data()
    buttons: List[Dict[str, str]] = data.get("buttons", [])
    if 0 <= idx < len(buttons):
        buttons.pop(idx)
    await state.update_data(buttons=buttons)
    await callback.message.edit_reply_markup(reply_markup=button_builder_kb(buttons))
    await callback.answer("Кнопка удалена")


@router.callback_query(PostCreation.confirming_post, F.data == "back_to_buttons")
async def cb_back_to_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    buttons = data.get("buttons", [])
    await state.set_state(PostCreation.configuring_buttons)
    await callback.message.answer(
        "🎛 <b>Редактирование кнопок поста:</b>",
        reply_markup=button_builder_kb(buttons),
    )
    await callback.answer()


# ==========================================
# ОТПРАВКА ПОСТА (УНИВЕРСАЛЬНАЯ ФУНКЦИЯ)
# ==========================================
async def deliver_post_payload(
    bot: Bot,
    chat_id: int,
    text_content: Optional[str],
    entities_json: Optional[str],
    photos: List[str],
    buttons: List[Dict[str, str]],
    message_thread_id: Optional[int] = None,
    extra_keyboard_rows: Optional[List[List[InlineKeyboardButton]]] = None,
) -> Message:
    """
    Отправляет готовый пост с подписью COTTAVEN RP • Подписаться,
    сохраняя entities (вкл. Premium Emoji), до 10 фото и цветные кнопки.
    Примечание по Telegram API: к альбому (sendMediaGroup) прикрепить inline-кнопки напрямую нельзя,
    поэтому если у альбома есть кнопки — они прикрепляются аккуратным блоком сразу под альбомом.
    """
    raw_entities = deserialize_entities(entities_json)
    final_text, final_entities = build_post_with_footer(text_content, raw_entities)
    kb = build_custom_keyboard(buttons, extra_rows=extra_keyboard_rows)

    thread_kwargs: Dict[str, Any] = {}
    if message_thread_id:
        thread_kwargs["message_thread_id"] = message_thread_id

    # Вариант 1: Без фотографий (только текст)
    if not photos:
        return await bot.send_message(
            chat_id=chat_id,
            text=final_text,
            entities=final_entities,
            parse_mode=None,
            reply_markup=kb,
            disable_web_page_preview=True,
            **thread_kwargs,
        )

    # Вариант 2: 1 фотография
    if len(photos) == 1:
        if len(final_text) <= 1024:
            return await bot.send_photo(
                chat_id=chat_id,
                photo=photos[0],
                caption=final_text,
                caption_entities=final_entities,
                parse_mode=None,
                reply_markup=kb,
                **thread_kwargs,
            )
        else:
            # Если текст длиннее 1024 символов (лимит подписи к фото)
            await bot.send_photo(chat_id=chat_id, photo=photos[0], **thread_kwargs)
            return await bot.send_message(
                chat_id=chat_id,
                text=final_text,
                entities=final_entities,
                parse_mode=None,
                reply_markup=kb,
                disable_web_page_preview=True,
                **thread_kwargs,
            )

    # Вариант 3: Альбом от 2 до 10 фотографий
    media: List[InputMediaPhoto] = []
    caption_fits = len(final_text) <= 1024

    for idx, file_id in enumerate(photos[:10]):
        if idx == 0 and caption_fits:
            media.append(
                InputMediaPhoto(
                    media=file_id,
                    caption=final_text,
                    caption_entities=final_entities,
                    parse_mode=None,
                )
            )
        else:
            media.append(InputMediaPhoto(media=file_id))

    sent_group = await bot.send_media_group(
        chat_id=chat_id,
        media=media,
        **thread_kwargs,
    )

    if not caption_fits:
        return await bot.send_message(
            chat_id=chat_id,
            text=final_text,
            entities=final_entities,
            parse_mode=None,
            reply_markup=kb,
            disable_web_page_preview=True,
            **thread_kwargs,
        )

    if kb:
        return await bot.send_message(
            chat_id=chat_id,
            text="⬇️ <b>Навигация и ссылки к посту:</b>",
            reply_markup=kb,
            **thread_kwargs,
        )

    return sent_group[-1]


# ==========================================
# ПРЕДПРОСМОТР И ОТПРАВКА НА МОДЕРАЦИЮ
# ==========================================
@router.callback_query(PostCreation.configuring_buttons, F.data == "to_preview")
async def cb_to_preview(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.set_state(PostCreation.confirming_post)

    await callback.message.answer(
        f"👁 <b>Предпросмотр вашего поста</b> (Канал: <b>{data['channel_label']}</b>):"
    )

    await deliver_post_payload(
        bot=bot,
        chat_id=callback.message.chat.id,
        text_content=data.get("text_content"),
        entities_json=data.get("entities_json"),
        photos=data.get("photos", []),
        buttons=data.get("buttons", []),
    )

    await callback.message.answer(
        "Если всё выглядит отлично — отправляйте пост на проверку администраторам!",
        reply_markup=confirm_post_kb(),
    )
    await callback.answer()


@router.callback_query(PostCreation.confirming_post, F.data == "submit_post")
async def cb_submit_post(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    mod_chat_id, mod_topic_id = await get_mod_destination()
    if not mod_chat_id:
        await callback.answer(
            "❌ Чат модерации не настроен! Владелец должен прописать /setmod в чате модерации.",
            show_alert=True,
        )
        return

    data = await state.get_data()
    user = callback.from_user

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO submissions (
                author_id, author_username, author_name,
                target_channel, text_content, entities_json,
                photos_json, buttons_json, mod_chat_id, mod_topic_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user.id,
                user.username or "",
                user.full_name,
                data["target_channel"],
                data.get("text_content", ""),
                data.get("entities_json", "[]"),
                json.dumps(data.get("photos", [])),
                json.dumps(data.get("buttons", []), ensure_ascii=False),
                mod_chat_id,
                mod_topic_id or 0,
            ),
        )
        sub_id = cursor.lastrowid
        await db.commit()

    target_name = "🏛 Основной ТГК" if data["target_channel"] == "main" else "📰 ТГК для РП новостей"
    uname = f"@{user.username}" if user.username else "без username"

    header_kwargs: Dict[str, Any] = {}
    if mod_topic_id:
        header_kwargs["message_thread_id"] = mod_topic_id

    # 1. Отправляем сам пост в чат модерации (в нужный топик)
    await deliver_post_payload(
        bot=bot,
        chat_id=mod_chat_id,
        text_content=data.get("text_content"),
        entities_json=data.get("entities_json"),
        photos=data.get("photos", []),
        buttons=data.get("buttons", []),
        message_thread_id=mod_topic_id,
    )

    # 2. Отправляем карточку управления для модераторов
    ctrl_msg = await bot.send_message(
        chat_id=mod_chat_id,
        text=(
            f"📥 <b>НОВАЯ ЗАЯВКА НА ПУБЛИКАЦИЮ #{sub_id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>Куда:</b> {target_name}\n"
            f"👤 <b>Автор:</b> {user.full_name} ({uname}) [<code>{user.id}</code>]\n"
            f"🖼 <b>Фото:</b> {len(data.get('photos', []))} шт. | "
            f"🔘 <b>Кнопок:</b> {len(data.get('buttons', []))} шт.\n"
            f"⏳ <b>Статус:</b> Ожидает решения администратора"
        ),
        reply_markup=moderation_kb(sub_id),
        **header_kwargs,
    )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE submissions SET mod_control_msg_id = ? WHERE id = ?",
            (ctrl_msg.message_id, sub_id),
        )
        await db.commit()

    await state.clear()
    await callback.message.edit_text(
        f"🎉 <b>Ваш пост #{sub_id} отправлен на модерацию!</b>\n\n"
        "Как только администраторы проверят публикацию, бот пришлёт вам уведомление.",
        reply_markup=main_menu_kb(),
    )
    await callback.answer("Отправлено!")


# ==========================================
# МОДЕРАЦИЯ ПОСТОВ В ЧАТЕ С ТОПИКАМИ
# ==========================================
async def get_submission(sub_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM submissions WHERE id = ?", (sub_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


@router.callback_query(F.data.startswith("mod_approve:"))
async def cb_mod_approve(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("⛔ У вас нет прав администратора бота.", show_alert=True)
        return

    sub_id = int(callback.data.split(":")[1])
    sub = await get_submission(sub_id)
    if not sub:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    if sub["status"] != "pending":
        await callback.answer(f"Эта заявка уже обработана ({sub['status']}).", show_alert=True)
        return

    dest_channel_id = CHANNEL_ID if sub["target_channel"] == "main" else RP_CHANNEL_ID
    target_name = "🏛 Основной ТГК" if sub["target_channel"] == "main" else "📰 ТГК для РП новостей"

    if not dest_channel_id:
        await callback.answer(
            "❌ ID целевого канала не задан в переменных окружения!",
            show_alert=True,
        )
        return

    try:
        await deliver_post_payload(
            bot=bot,
            chat_id=dest_channel_id,
            text_content=sub["text_content"],
            entities_json=sub["entities_json"],
            photos=json.loads(sub["photos_json"] or "[]"),
            buttons=json.loads(sub["buttons_json"] or "[]"),
        )
    except TelegramBadRequest as e:
        await callback.answer(f"Ошибка публикации в канал: {e.message}", show_alert=True)
        return

    reviewer = callback.from_user
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE submissions
            SET status = 'approved', reviewer_id = ?, reviewer_name = ?
            WHERE id = ?
            """,
            (reviewer.id, reviewer.full_name, sub_id),
        )
        await db.commit()

    uname = f"@{sub['author_username']}" if sub.get("author_username") else "без username"
    await callback.message.edit_text(
        f"✅ <b>ПОСТ #{sub_id} ОПУБЛИКОВАН</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Канал:</b> {target_name}\n"
        f"👤 <b>Автор:</b> {sub['author_name']} ({uname})\n"
        f"🛡 <b>Одобрил:</b> {reviewer.full_name} [<code>{reviewer.id}</code>]",
        reply_markup=None,
    )

    # Уведомляем автора поста
    try:
        await bot.send_message(
            chat_id=sub["author_id"],
            text=(
                f"🎉 <b>Ваш пост #{sub_id} одобрен и опубликован!</b>\n"
                f"📢 Канал: <b>{target_name}</b>"
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Перейти в канал", url=CHANNEL_LINK, style="success")]
                ]
            ),
        )
    except Exception:
        pass

    await callback.answer("✅ Пост успешно опубликован!")


async def execute_reject(
    bot: Bot,
    sub: Dict[str, Any],
    reviewer_id: int,
    reviewer_name: str,
    reason: Optional[str],
    control_message: Optional[Message] = None,
) -> None:
    sub_id = sub["id"]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE submissions
            SET status = 'rejected', reviewer_id = ?, reviewer_name = ?, reject_reason = ?
            WHERE id = ?
            """,
            (reviewer_id, reviewer_name, reason or "", sub_id),
        )
        await db.commit()

    target_name = "🏛 Основной ТГК" if sub["target_channel"] == "main" else "📰 ТГК для РП новостей"
    uname = f"@{sub['author_username']}" if sub.get("author_username") else "без username"
    reason_block = f"\n📝 <b>Причина:</b> {reason}" if reason else ""

    new_text = (
        f"❌ <b>ПОСТ #{sub_id} ОТКЛОНЁН</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Канал:</b> {target_name}\n"
        f"👤 <b>Автор:</b> {sub['author_name']} ({uname})\n"
        f"🛡 <b>Отклонил:</b> {reviewer_name} [<code>{reviewer_id}</code>]"
        f"{reason_block}"
    )

    if control_message:
        await control_message.edit_text(new_text, reply_markup=None)
    elif sub.get("mod_chat_id") and sub.get("mod_control_msg_id"):
        try:
            await bot.edit_message_text(
                chat_id=sub["mod_chat_id"],
                message_id=sub["mod_control_msg_id"],
                text=new_text,
                reply_markup=None,
            )
        except Exception:
            pass

    try:
        user_reason = f"\n💬 <b>Комментарий модератора:</b> {reason}" if reason else ""
        await bot.send_message(
            chat_id=sub["author_id"],
            text=(
                f"😔 <b>Ваш пост #{sub_id} был отклонён модерацией.</b>"
                f"{user_reason}"
            ),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("mod_reject:"))
async def cb_mod_reject(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("⛔ У вас нет прав администратора бота.", show_alert=True)
        return

    sub_id = int(callback.data.split(":")[1])
    sub = await get_submission(sub_id)
    if not sub or sub["status"] != "pending":
        await callback.answer("Заявка уже обработана или не найдена.", show_alert=True)
        return

    await execute_reject(
        bot=bot,
        sub=sub,
        reviewer_id=callback.from_user.id,
        reviewer_name=callback.from_user.full_name,
        reason=None,
        control_message=callback.message,
    )
    await callback.answer("❌ Пост отклонён.")


@router.callback_query(F.data.startswith("mod_reject_reason:"))
async def cb_mod_reject_reason(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not await is_admin(callback.from_user.id):
        await callback.answer("⛔ У вас нет прав администратора бота.", show_alert=True)
        return

    sub_id = int(callback.data.split(":")[1])
    sub = await get_submission(sub_id)
    if not sub or sub["status"] != "pending":
        await callback.answer("Заявка уже обработана или не найдена.", show_alert=True)
        return

    await state.set_state(ModRejectState.waiting_reason)
    await state.update_data(reject_sub_id=sub_id)

    thread_kwargs: Dict[str, Any] = {}
    if callback.message.message_thread_id:
        thread_kwargs["message_thread_id"] = callback.message.message_thread_id

    await callback.message.reply(
        f"✍️ <b>Введите причину отказа для поста #{sub_id}</b> (следующим сообщением в этом чате):",
        **thread_kwargs,
    )
    await callback.answer()


@router.message(ModRejectState.waiting_reason, F.text)
async def handle_reject_reason(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        return

    data = await state.get_data()
    sub_id = data.get("reject_sub_id")
    await state.clear()

    if not sub_id:
        return

    sub = await get_submission(sub_id)
    if not sub or sub["status"] != "pending":
        await message.reply("⚠️ Эта заявка уже была обработана.")
        return

    await execute_reject(
        bot=bot,
        sub=sub,
        reviewer_id=message.from_user.id,
        reviewer_name=message.from_user.full_name,
        reason=message.text.strip(),
    )
    await message.reply(f"✅ Пост #{sub_id} отклонён с указанием причины.")


# ==========================================
# ТОЧКА ВХОДА
# ==========================================
async def main() -> None:
    await init_db()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Бот COTTAVEN RP успешно запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
