import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
try:
    from telegram import CopyTextButton
except ImportError:
    CopyTextButton = None
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# 📌 Настройки (основные параметры бота)
BOT_TOKEN = "8455815258:AAEVdMN6mEW4LzEqzWEnGqgZPGZ4hSZeQew"
ALLOWED_USERS = [406861269, 5960067612]  # Добавь нужные Telegram
SHEET_NAME = "лиды от бота"
SHEET_TAB = "общая"
CREDENTIALS_FILE = "fluid-kiln-485023-d5-c5bf3a6eb7be.json"

POLL_INTERVAL_SECONDS = 60
LEAD_RESEND_INTERVAL_SECONDS = 60 * 60
REPEAT_ASSIGNED_LEAD_SECONDS = 60 * 60
PENDING_PROCESSING_REMIND_SECONDS = 30 * 60
BUSY_REMINDER_SECONDS = 15 * 60
POLAND_TZ = ZoneInfo("Europe/Warsaw")
QUIET_HOURS_START = 20  # 20:00 по Польше
QUIET_HOURS_END = 8  # 08:00 по Польше
ADMIN_ALERT_CHAT_ID = ALLOWED_USERS[0] if ALLOWED_USERS else None
ERROR_ALERT_DEBOUNCE_SECONDS = 300
MAX_SCHEDULE_YEARS_AHEAD = 1

# 🔧 Логирование (помогает видеть ошибки в консоли)
logging.basicConfig(level=logging.INFO)

# 📄 Подключение к Google Sheets (чтение/запись в таблицу)
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]
creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
client = gspread.authorize(creds)
sheet = client.open(SHEET_NAME).worksheet(SHEET_TAB)

# ✅ Кэшируем заголовки, чтобы не читать их каждый раз
SHEET_HEADERS = sheet.row_values(1)

# ✅ Отслеживаем, что уже отправлено (чтобы не слать одинаковые лиды слишком часто)
sent_rows: Dict[int, dict] = {}

# ✅ Текущие лиды в заполнении (ключ = row_id, значение = состояние заполнения)
pending_leads: Dict[int, dict] = {}

# ✅ Запланированные лиды на обработку (row_id -> когда и кому показать)
scheduled_leads: Dict[int, dict] = {}

# ✅ Активный лид в обработке по пользователю (user_id -> row_id)
active_processing: Dict[int, int] = {}

# ✅ Отслеживаем повторные уведомления закреплённых лидов
assigned_repeat: Dict[int, dict] = {}

# ✅ Пользователь взял лид и должен завершить обработку (блок новых лидов/напоминаний)
user_busy_state: Dict[int, dict] = {}

# ✅ Режим пользователя: process/edit_history
user_mode: Dict[int, str] = {}
user_history_messages: Dict[int, List[int]] = {}

# ✅ Пользователь выбрал "обработать потом" и должен ввести дату/время
pending_schedule_input: Dict[int, int] = {}

last_error_alert_at = 0.0


def _find_phone_column() -> Optional[int]:
    """Ищем колонку с телефоном по заголовкам."""
    headers_norm = [str(h).strip().lower() for h in SHEET_HEADERS]

    if "phone number" in headers_norm:
        return headers_norm.index("phone number") + 1

    for i, header in enumerate(headers_norm):
        if "phone" in header or "тел" in header:
            return i + 1

    return None


def _find_column(names: List[str]) -> Optional[int]:
    """Ищем колонку по возможным названиям заголовка."""
    headers_norm = [str(h).strip().lower() for h in SHEET_HEADERS]
    for name in names:
        if name in headers_norm:
            return headers_norm.index(name) + 1
    return None


def _row_value(row: List[str], col_index: Optional[int]) -> str:
    """Безопасно получаем значение из строки по индексу колонки."""
    if not col_index or len(row) < col_index:
        return ""
    return str(row[col_index - 1]).strip()


def _phone_html(phone: str) -> str:
    """Готовим кликабельный телефон для Telegram (iOS/Android)."""
    clean_phone = re.sub(r"[^0-9+]", "", phone)
    if not clean_phone:
        return phone or "—"
    return f'<a href="tel:{clean_phone}">{phone}</a>'


def _copy_keyboard_for_row(row: List[str]) -> Optional[InlineKeyboardMarkup]:
    phone = _row_value(row, _find_phone_column())
    if not phone or CopyTextButton is None:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Скопировать номер", copy_text=CopyTextButton(text=phone[:256]))]
    ])


def _build_lead_text(row: List[str], phone: str, include_extra: bool = False) -> str:
    """Формируем текст лида для отправки в Telegram."""
    name_col = _find_column(["name"])
    lead_info_col = _find_column(["lead info"])
    ads_col = _find_column(["ads"])
    date_call_col = _find_column(["date and time to call"])
    gender_col = _find_column(["gender"])
    status_col = _find_column(["status"])
    commoners_col = _find_column(["commoners"])
    comment_col = _find_column(["recruiter's comment", "recruiter comment"])
    age_col = _find_column(["age", "возраст"])
    nationality_col = _find_column(["nationality", "национальность"])
    date_call_value = _row_value(row, date_call_col)
    header = "🗂 <b>Новый лид</b>"
    if date_call_value:
        header = "🗂 <b>Лид закреплен за тобой, кандидат ждет</b>"
    text = (
        f"{header}\n"
        f"👤 Имя: {_row_value(row, name_col) or '—'}\n"
        f"📞 Телефон: {_phone_html(phone)}\n"
        f"ℹ️ Инфо: {_row_value(row, lead_info_col) or '—'}\n"
        f"📢 Реклама: {_row_value(row, ads_col) or '—'}\n"
        f"📅 Когда звонить: {date_call_value or '—'}"
    )
    if include_extra:
        text += (
            f"\n👤 Gender: {_row_value(row, gender_col) or '—'}"
            f"\n📌 Status: {_row_value(row, status_col) or '—'}"
            f"\n📝 Commoners: {_row_value(row, commoners_col) or '—'}"
            f"\n💬 Комментарий: {_row_value(row, comment_col) or '—'}"
            f"\n🎂 Возраст: {_row_value(row, age_col) or '—'}"
            f"\n🌍 Национальность: {_row_value(row, nationality_col) or '—'}"
        )
    return text


def _parse_datetime(value: str) -> Optional[datetime]:
    """Парсим дату/время из таблицы в формате ддммгггг ччмм."""
    if not value:
        return None
    raw = value.strip()
    for fmt in ("%d%m%Y %H%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _format_datetime(value: datetime) -> str:
    """Форматируем дату/время для записи в таблицу (дд.мм.гггг чч:мм)."""
    return value.strftime("%d.%m.%Y %H:%M")




def _validate_datetime_input(raw: str) -> tuple[Optional[datetime], Optional[str]]:
    value = (raw or "").strip()
    parsed = _parse_datetime(value)
    if not parsed:
        return None, "❗ Неверный формат. Введите дату и время в формате ддммгггг ччмм."
    now = datetime.now(POLAND_TZ).replace(tzinfo=None)
    max_dt = now + timedelta(days=365 * MAX_SCHEDULE_YEARS_AHEAD)
    if parsed <= now:
        return None, "❗ Нельзя указывать дату/время в прошлом. Введите будущее время."
    if parsed > max_dt:
        return None, "❗ Нельзя ставить дату дальше чем на 1 год вперёд."
    return parsed, None

def _load_sheet_data() -> List[List[str]]:
    """Загружаем все данные таблицы одним запросом."""
    return sheet.get_all_values()


def _is_quiet_hours() -> bool:
    """Проверяем, что сейчас тихие часы (не отправляем уведомления ночью по Польше)."""
    poland_hour = datetime.now(POLAND_TZ).hour
    return poland_hour >= QUIET_HOURS_START or poland_hour < QUIET_HOURS_END


def _mark_user_busy(user_id: int, row_id: Optional[int] = None) -> None:
    """Фиксируем, что пользователь взял лид и занят его обработкой."""
    now = time.time()
    busy = user_busy_state.get(user_id, {})
    user_busy_state[user_id] = {
        "row_id": row_id if row_id is not None else busy.get("row_id"),
        "taken_at": busy.get("taken_at", now),
        "last_notice": busy.get("last_notice", now),
    }


def _is_user_busy(user_id: int) -> bool:
    """Проверяем, что пользователь занят взятым лидом."""
    return user_id in user_busy_state


def _busy_row_id(user_id: int) -> Optional[int]:
    """Текущий row_id занятого лида пользователя (если известен)."""
    busy_data = user_busy_state.get(user_id)
    if busy_data and busy_data.get("row_id") is not None:
        try:
            return int(busy_data["row_id"])
        except (TypeError, ValueError):
            pass
    if user_id in active_processing:
        return active_processing[user_id]
    if user_id in pending_schedule_input:
        return pending_schedule_input[user_id]
    for row_id, lead_data in pending_leads.items():
        if lead_data.get("user_id") == user_id:
            return row_id
    return None


def _callback_row_id(data: str) -> Optional[int]:
    for prefix in ("schedule_", "gender_", "status_", "nationality_"):
        if data.startswith(prefix):
            parts = data.split("_", 2)
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return None


def _busy_callback_allowed(user_id: int, data: str) -> bool:
    busy_row = _busy_row_id(user_id)
    if busy_row is None:
        return False
    cb_row = _callback_row_id(data)
    return cb_row == busy_row


def _user_in_history_mode(user_id: int) -> bool:
    return user_mode.get(user_id) == "edit_history"


def _is_followup_status(value: str) -> bool:
    return (value or "").strip().lower() in {"думает", "нет ответа"}


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Обработка новых лидов", callback_data="main_process")],
        [InlineKeyboardButton("История", callback_data="main_history")],
    ])


async def _clear_user_new_lead_cards(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    for row_id, data in list(sent_rows.items()):
        msg_id = data.get("message_ids", {}).pop(user_id, None)
        if msg_id:
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=msg_id)
            except Exception:
                pass
        if not data.get("message_ids"):
            sent_rows.pop(row_id, None)


def _track_history_message(user_id: int, message_id: Optional[int]) -> None:
    if not message_id:
        return
    user_history_messages.setdefault(user_id, [])
    if message_id not in user_history_messages[user_id]:
        user_history_messages[user_id].append(message_id)


async def _clear_history_messages(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    ids = user_history_messages.pop(user_id, [])
    for msg_id in ids:
        try:
            await context.bot.delete_message(chat_id=user_id, message_id=msg_id)
        except Exception:
            pass


async def _cleanup_lead_messages(context: ContextTypes.DEFAULT_TYPE, row_id: int) -> None:
    """Удаляем сообщения по лиду, чтобы не засорять чат."""
    lead_data = pending_leads.get(row_id)
    if not lead_data:
        return
    user_id = lead_data.get("user_id")
    message_ids = lead_data.get("message_ids", [])
    for message_id in message_ids:
        try:
            await context.bot.delete_message(
                chat_id=user_id,
                message_id=message_id,
            )
        except Exception:
            logging.exception("Не удалось удалить сообщение %s для лида %s", message_id, row_id)


async def _send_lead_to_users(
    application: Application,
    text: str,
    row_id: int,
    user_ids: List[int],
) -> None:
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📋 Взять лид", callback_data=f"take_{row_id}")]]
    )

    for user_id in user_ids:
        try:
            await application.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception:
            logging.exception("Не удалось отправить лид пользователю %s", user_id)


async def _send_schedule_prompt(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    row_id: int,
    row: List[str],
    header: str,
):
    """Отправляем лид с кнопками планирования повторной обработки."""
    phone_col = _find_phone_column()
    phone = _row_value(row, phone_col)
    lead_text = _build_lead_text(row, phone, include_extra=True)
    rows = []
    phone_val = _row_value(row, _find_phone_column())
    if phone_val and CopyTextButton is not None:
        rows.append([InlineKeyboardButton("📋 Скопировать номер", copy_text=CopyTextButton(text=phone_val[:256]))])
    rows.append([InlineKeyboardButton("Взять сейчас", callback_data=f"schedule_{row_id}_now")])
    rows.append([InlineKeyboardButton("Обработать потом", callback_data=f"schedule_{row_id}_later")])
    schedule_keyboard = InlineKeyboardMarkup(rows)
    return await context.bot.send_message(
        chat_id=user_id,
        text=header + "📋 Информация по лиду:\n\n" + lead_text + "\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=schedule_keyboard,
    )

async def _send_missed_for_user(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """После завершения обработки досылаем пользователю пропущенные лиды и напоминания."""
    phone_col = _find_phone_column()
    if phone_col is None:
        return
    all_values = _load_sheet_data()
    if len(all_values) <= 1:
        return
    all_rows = all_values[1:]
    now = time.time()

    for row_id, row in enumerate(all_rows, start=2):
        phone = _row_value(row, phone_col)
        rekruter = _row_value(row, 2)
        if not phone or rekruter:
            continue
        row_track = sent_rows.get(row_id)
        old_message_id = None
        if row_track:
            old_message_id = row_track.get("message_ids", {}).get(user_id)
        if old_message_id:
            try:
                await context.bot.delete_message(
                    chat_id=user_id,
                    message_id=old_message_id,
                )
            except Exception:
                logging.exception(
                    "Не удалось удалить старое сообщение нового лида для пользователя %s",
                    user_id,
                )
        try:
            message = await context.bot.send_message(
                chat_id=user_id,
                text=_build_lead_text(row, phone),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("📋 Взять лид", callback_data=f"take_{row_id}")]]
                ),
            )
            row_track = sent_rows.get(row_id, {"last_sent": now, "message_ids": {}})
            row_track["last_sent"] = now
            row_track.setdefault("message_ids", {})[user_id] = message.message_id
            sent_rows[row_id] = row_track
        except Exception:
            logging.exception("Не удалось дослать новый лид пользователю %s", user_id)

    date_col = _find_column(["date and time to call"])
    if not date_col:
        return
    for row_id, row in enumerate(all_rows, start=2):
        if not _row_assigned_to_user(row, user_id):
            assigned_repeat.pop(row_id, None)
            scheduled_leads.pop(row_id, None)
            continue
        status_col = _find_column(["status"])
        status_value = _row_value(row, status_col)
        if not _is_followup_status(status_value):
            assigned_repeat.pop(row_id, None)
            scheduled_leads.pop(row_id, None)
            continue
        due_dt = _parse_datetime(_row_value(row, date_col))
        if not due_dt or due_dt.timestamp() > now:
            continue
        try:
            repeat_data = assigned_repeat.get(row_id)
            if repeat_data and repeat_data.get("message_id"):
                try:
                    await context.bot.delete_message(
                        chat_id=user_id,
                        message_id=repeat_data["message_id"],
                    )
                except Exception:
                    logging.exception(
                        "Не удалось удалить старое сообщение повтора для пользователя %s",
                        user_id,
                    )
            message = await _send_schedule_prompt(
                context,
                user_id,
                row_id,
                row,
                header="✅ Вы взяли этот лид и его нужно обработать.\n\n",
            )
            assigned_repeat[row_id] = {"last_sent": now, "message_id": message.message_id}
        except Exception:
            logging.exception("Не удалось дослать повторный лид пользователю %s", user_id)




async def check_new_leads(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверяем новые лиды, пересылаем непринятые и открываем запланированные."""
    if _is_quiet_hours():
        return

    phone_col = _find_phone_column()
    if phone_col is None:
        logging.error("Не нашёл колонку с телефоном в шапке таблицы (1-я строка).")
        return

    all_values = _load_sheet_data()
    if len(all_values) <= 1:
        return
    all_rows = all_values[1:]
    now = time.time()

    # Напоминание занятому пользователю каждые 15 минут: сначала завершить взятый лид.
    for busy_user_id, busy_data in list(user_busy_state.items()):
        last_notice = busy_data.get("last_notice", busy_data.get("taken_at", now))
        if now - last_notice < BUSY_REMINDER_SECONDS:
            continue
        try:
            busy_row_id = _busy_row_id(busy_user_id)
            if busy_row_id and 0 <= busy_row_id - 2 < len(all_rows):
                busy_row = all_rows[busy_row_id - 2]
                if _row_assigned_to_user(busy_row, busy_user_id):
                    await _cleanup_lead_messages(context, busy_row_id)
                    await _resume_processing_for_user(context, busy_user_id, busy_row_id, busy_row)
                    busy_data["last_notice"] = now
                    continue
            await context.bot.send_message(
                chat_id=busy_user_id,
                text=(
                    "⏳ Вы взяли лид. Сначала завершите его обработку, "
                    "чтобы получать новые заявки."
                ),
            )
            busy_data["last_notice"] = now
        except Exception:
            logging.exception("Не удалось отправить напоминание занятому пользователю %s", busy_user_id)
    for idx, row in enumerate(all_rows, start=2):
        row_id = idx

        phone = str(row[phone_col - 1]).strip() if phone_col and len(row) >= phone_col else ""
        rekruter = str(row[1]).strip() if len(row) >= 2 else ""

        if not phone or rekruter:
            continue

        # Если лид уже отправляли недавно — пропускаем до истечения интервала.
        last_sent_data = sent_rows.get(row_id)
        last_sent_time = last_sent_data["last_sent"] if last_sent_data else None
        if last_sent_time and now - last_sent_time < LEAD_RESEND_INTERVAL_SECONDS:
            continue

        text = _build_lead_text(row, phone)
        # Удаляем старые сообщения с этим лидом, чтобы оставить только последнее.
        message_ids: Dict[int, int] = {}
        if last_sent_data and last_sent_data.get("message_ids"):
            for user_id, message_id in last_sent_data["message_ids"].items():
                try:
                    await context.bot.delete_message(
                        chat_id=user_id,
                        message_id=message_id,
                    )
                except Exception:
                    logging.exception(
                        "Не удалось удалить сообщение лида у пользователя %s", user_id
                    )
        # Отправляем лид всем разрешённым пользователям и запоминаем ID сообщений.
        for user_id in ALLOWED_USERS:
            if _is_user_busy(user_id) or _user_in_history_mode(user_id):
                continue
            try:
                message = await context.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("📋 Взять лид", callback_data=f"take_{row_id}")]]
                    ),
                )
                message_ids[user_id] = message.message_id
            except Exception:
                logging.exception("Не удалось отправить лид пользователю %s", user_id)
        if message_ids:
            sent_rows[row_id] = {"last_sent": now, "message_ids": message_ids}

    # Проверяем, пришло ли время повторной обработки по запланированным лидам.
    due_rows = []
    for row_id, data in scheduled_leads.items():
        if now >= data["due_at"]:
            due_rows.append(row_id)

    handled_rows = set()
    for row_id in due_rows:
        data = scheduled_leads.get(row_id)
        if not data:
            continue
        user_id = data["user_id"]
        if _user_in_history_mode(user_id):
            continue
        if _is_user_busy(user_id):
            if _busy_row_id(user_id) == row_id and 0 <= row_id - 2 < len(all_rows):
                busy_data = user_busy_state.get(user_id, {})
                last_notice = busy_data.get("last_notice", busy_data.get("taken_at", now))
                if now - last_notice >= BUSY_REMINDER_SECONDS:
                    await _cleanup_lead_messages(context, row_id)
                    await _resume_processing_for_user(context, user_id, row_id, all_rows[row_id - 2])
                    busy_data["last_notice"] = now
            continue
        row = all_rows[row_id - 2]
        rekruter_value = str(row[1]).strip() if len(row) >= 2 else ""
        if rekruter_value != str(user_id):
            continue
        status_col = _find_column(["status"])
        if not _is_followup_status(_row_value(row, status_col)):
            scheduled_leads.pop(row_id, None)
            assigned_repeat.pop(row_id, None)
            continue
        repeat_data = assigned_repeat.get(row_id)
        if repeat_data and repeat_data.get("message_id"):
            try:
                await context.bot.delete_message(
                    chat_id=user_id,
                    message_id=repeat_data["message_id"],
                )
            except Exception:
                logging.exception(
                    "Не удалось удалить старое сообщение повтора для пользователя %s",
                    user_id,
                )
        message = await _send_schedule_prompt(
            context,
            user_id,
            row_id,
            row,
            header="✅ Вы взяли этот лид и его нужно обработать.\n\n",
        )
        scheduled_leads.pop(row_id, None)
        assigned_repeat[row_id] = {"last_sent": now, "message_id": message.message_id}
        handled_rows.add(row_id)

    # Проверяем лиды, закреплённые за пользователем, по дате/времени в таблице.
    date_col = _find_column(["date and time to call"])
    if date_col:
        for row_id, row in enumerate(all_rows, start=2):
            if row_id in handled_rows:
                continue
            rekruter = str(row[1]).strip() if len(row) >= 2 else ""
            if not rekruter:
                continue
            try:
                assigned_user_id = int(rekruter)
            except ValueError:
                continue
            if _user_in_history_mode(assigned_user_id):
                continue
            if _is_user_busy(assigned_user_id):
                if _busy_row_id(assigned_user_id) == row_id:
                    busy_data = user_busy_state.get(assigned_user_id, {})
                    last_notice = busy_data.get("last_notice", busy_data.get("taken_at", now))
                    if now - last_notice >= BUSY_REMINDER_SECONDS:
                        await _cleanup_lead_messages(context, row_id)
                        await _resume_processing_for_user(context, assigned_user_id, row_id, row)
                        busy_data["last_notice"] = now
                continue
            status_col = _find_column(["status"])
            if not _is_followup_status(_row_value(row, status_col)):
                assigned_repeat.pop(row_id, None)
                scheduled_leads.pop(row_id, None)
                continue
            raw_datetime = str(row[date_col - 1]).strip() if len(row) >= date_col else ""
            due_dt = _parse_datetime(raw_datetime)
            if not due_dt:
                continue
            due_ts = due_dt.timestamp()
            if now < due_ts:
                continue
            # Повторная отправка для закреплённого пользователя каждый час,
            # пока он не начнёт обработку.
            repeat_data = assigned_repeat.get(row_id)
            last_sent = repeat_data["last_sent"] if repeat_data else None
            remind_interval = (
                PENDING_PROCESSING_REMIND_SECONDS
                if row_id in pending_leads
                else REPEAT_ASSIGNED_LEAD_SECONDS
            )
            if last_sent and now - last_sent < remind_interval:
                continue
            if repeat_data and repeat_data.get("message_id"):
                try:
                    await context.bot.delete_message(
                        chat_id=assigned_user_id,
                        message_id=repeat_data["message_id"],
                    )
                except Exception:
                    logging.exception(
                        "Не удалось удалить старое сообщение повтора для пользователя %s",
                        assigned_user_id,
                    )
            try:
                message = await _send_schedule_prompt(
                    context,
                    assigned_user_id,
                    row_id,
                    row,
                    header="✅ Вы взяли этот лид и его нужно обработать.\n\n",
                )
                assigned_repeat[row_id] = {"last_sent": now, "message_id": message.message_id}
            except Exception:
                logging.exception(
                    "Не удалось отправить повторный лид пользователю %s",
                    assigned_user_id,
                )




async def _notify_admin(application: Application, text: str) -> None:
    """Отправляет сервисные уведомления владельцу бота."""
    if not ADMIN_ALERT_CHAT_ID:
        return
    try:
        await application.bot.send_message(
            chat_id=ADMIN_ALERT_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logging.exception("Не удалось отправить сервисное уведомление админу")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик ошибок PTB."""
    global last_error_alert_at
    logging.exception("Глобальная ошибка PTB", exc_info=context.error)
    now = time.time()
    if now - last_error_alert_at < ERROR_ALERT_DEBOUNCE_SECONDS:
        return
    last_error_alert_at = now
    await _notify_admin(
        context.application,
        "⚠️ <b>Бот поймал ошибку</b>\nПроверьте процесс/логи на устройстве запуска.",
    )

async def poll_leads_forever(application: Application) -> None:
    """Фоновый цикл, который регулярно проверяет лиды."""
    context = ContextTypes.DEFAULT_TYPE(application=application)
    while True:
        try:
            await check_new_leads(context)
        except Exception:
            logging.exception("Ошибка при проверке лидов")
            await _notify_admin(
                application,
                "⚠️ <b>Ошибка фоновой проверки лидов</b>\nПроверьте интернет/Google Sheets/логи.",
            )
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start."""
    await update.message.reply_text(
        "👋 Привет! Выберите режим работы:",
        reply_markup=_main_menu_keyboard(),
    )


# /leads
async def leads(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /leads для ручной проверки."""
    await update.message.reply_text("🔎 Проверяю новые лиды…")
    await check_new_leads(context)


async def process_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text("❌ У вас нет доступа к этому боту.")
        return
    if _is_user_busy(user_id):
        await update.message.reply_text("⛔ Сначала завершите текущий лид.")
        return
    user_mode[user_id] = "process"
    await _clear_history_messages(context, user_id)
    await update.message.reply_text("✅ Режим: обработка новых лидов включен.")
    await check_new_leads(context)
    await _send_missed_for_user(context, user_id)


async def history_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text("❌ У вас нет доступа к этому боту.")
        return
    if _is_user_busy(user_id):
        await update.message.reply_text("⛔ Сначала завершите текущий лид.")
        return
    user_mode[user_id] = "edit_history"
    await _clear_user_new_lead_cards(context, user_id)
    await update.message.reply_text(
        "📚 История: выберите режим:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("За сегодня", callback_data="hist_today")],
            [InlineKeyboardButton("За вчера", callback_data="hist_yesterday")],
            [InlineKeyboardButton("Все мои", callback_data="hist_all")],
            [InlineKeyboardButton("По телефону", callback_data="hist_phone")],
        ]),
    )


# Обработка кнопки "Взять лид"
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик всех кнопок (callback_data)."""
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        logging.warning("CallbackQuery устарел или неверен, пропускаем ответ.")
    user_id = query.from_user.id

    if not str(user_id) in map(str, ALLOWED_USERS):
        await query.edit_message_text("❌ У вас нет доступа к этой функции.")
        return

    data = query.data

    if _is_user_busy(user_id) and not _busy_callback_allowed(user_id, data):
        await context.bot.send_message(
            chat_id=user_id,
            text="⛔ Сначала завершите текущий лид. После сохранения сможете переключить режим или взять другой лид.",
        )
        return

    if data == "main_process":
        user_mode[user_id] = "process"
        await _clear_history_messages(context, user_id)
        await query.edit_message_text("✅ Режим: обработка новых лидов включен.")
        await check_new_leads(context)
        await _send_missed_for_user(context, user_id)
        return

    if data == "main_history":
        user_mode[user_id] = "edit_history"
        await _clear_user_new_lead_cards(context, user_id)
        await query.edit_message_text(
            "📚 История: выберите режим:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("За сегодня", callback_data="hist_today")],
                [InlineKeyboardButton("За вчера", callback_data="hist_yesterday")],
                [InlineKeyboardButton("Все мои", callback_data="hist_all")],
                [InlineKeyboardButton("По телефону", callback_data="hist_phone")],
            ]),
        )
        return

    if data in {"hist_today", "hist_yesterday", "hist_all"}:
        target_date = None
        if data in {"hist_today", "hist_yesterday"}:
            days_back = 0 if data == "hist_today" else 1
            target_date = (datetime.now(POLAND_TZ).date() - timedelta(days=days_back)).strftime("%d.%m.%Y")
        all_values = _load_sheet_data()
        if len(all_values) <= 1:
            await query.edit_message_text("ℹ️ История пуста.")
            return
        rows = all_values[1:]
        date_col = _find_column(["processed at", "processed_at", "дата обработки"])
        if not date_col:
            await query.edit_message_text("❗ Для истории добавьте колонку 'processed at' в таблицу.")
            return
        found = False
        for row_id, row in enumerate(rows, start=2):
            if _row_value(row, 2) != str(user_id):
                continue
            processed_at = _row_value(row, date_col)
            if target_date and not processed_at.startswith(target_date):
                continue
            found = True
            phone = _row_value(row, _find_phone_column())
            rows_kb = []
            if phone and CopyTextButton is not None:
                rows_kb.append([InlineKeyboardButton("📋 Скопировать номер", copy_text=CopyTextButton(text=phone[:256]))])
            rows_kb.append([InlineKeyboardButton("Изменить", callback_data=f"edit_{row_id}")])
            hist_msg = await context.bot.send_message(
                chat_id=user_id,
                text=_build_lead_text(row, phone, include_extra=True),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(rows_kb),
            )
            _track_history_message(user_id, hist_msg.message_id)
        if not found:
            await query.edit_message_text("ℹ️ В истории лидов не найдено.")
        else:
            await query.edit_message_text("✅ Лиды из истории отправлены.")
        return

    if data == "hist_phone":
        user_mode[user_id] = "edit_history_phone"
        await query.edit_message_text("📞 Введите телефон (полностью или часть номера) для поиска в истории:")
        return
    if data.startswith("edit_"):
        row_id = int(data.split("_")[1])
        all_values = _load_sheet_data()
        if len(all_values) <= row_id - 1:
            await query.edit_message_text("❗ Не удалось найти лид в таблице.")
            return
        row = all_values[row_id - 1]
        user_mode[user_id] = "edit_history"
        _mark_user_busy(user_id, row_id)
        await query.edit_message_text("✏️ Редактирование лида запущено.")
        await start_lead_processing(context, user_id, row_id, row, note=False)
        pending_leads[row_id]["edit_mode"] = True
        return

    if data == "postedit_continue":
        await query.edit_message_text(
            "📚 История: выберите режим:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("За сегодня", callback_data="hist_today")],
                [InlineKeyboardButton("За вчера", callback_data="hist_yesterday")],
                [InlineKeyboardButton("Все мои", callback_data="hist_all")],
                [InlineKeyboardButton("По телефону", callback_data="hist_phone")],
            ]),
        )
        return

    if data == "postedit_back":
        user_mode[user_id] = "process"
        await _clear_history_messages(context, user_id)
        await query.edit_message_text("✅ Возвращаю в обработку новых лидов.")
        await check_new_leads(context)
        await _send_missed_for_user(context, user_id)
        return
    # Кнопка "Взять лид"
    if data.startswith("take_"):
        row_id = int(data.split("_")[1])
        all_values = _load_sheet_data()
        if len(all_values) <= row_id - 1:
            await query.edit_message_text("❗ Не удалось найти лид в таблице.")
            return
        row = all_values[row_id - 1]  # сдвиг из-за заголовков
        rekruter = _row_value(row, 2)

        # Если лид уже закреплён — сообщаем об этом.
        if rekruter and rekruter != str(user_id):
            await query.edit_message_text("❗ Этот лид уже взят другим рекрутером.")
            return

        if rekruter == str(user_id):
            await query.edit_message_text("ℹ️ Этот лид уже закреплён за вами. Возобновляю обработку.")
            await _resume_processing_for_user(context, user_id, row_id, row, restart_if_missing=True)
            return

        # Закрепляем лид за пользователем в таблице.
        sheet.update_cell(row_id, 2, str(user_id))  # колонка B (rekuter)
        # Удаляем отправленные ранее сообщения с этим лидом у других пользователей.
        sent_rows_data = sent_rows.pop(row_id, None)
        if sent_rows_data and sent_rows_data.get("message_ids"):
            for other_user, message_id in sent_rows_data["message_ids"].items():
                if other_user == user_id:
                    continue
                try:
                    await context.bot.delete_message(
                        chat_id=other_user,
                        message_id=message_id,
                    )
                except Exception:
                    logging.exception(
                        "Не удалось удалить сообщение лида у пользователя %s", other_user
                    )
        await query.edit_message_text(
            "✅ Лид взят. Этот лид закреплён за вами.",
        )
        _mark_user_busy(user_id, row_id)
        phone_col = _find_phone_column()
        phone = _row_value(row, phone_col)
        lead_text = _build_lead_text(row, phone)
        # Предлагаем, когда начать обработку.
        schedule_rows = []
        if phone and CopyTextButton is not None:
            schedule_rows.append([InlineKeyboardButton("📋 Скопировать номер", copy_text=CopyTextButton(text=phone[:256]))])
        schedule_rows.append([InlineKeyboardButton("Взять сейчас", callback_data=f"schedule_{row_id}_now")])
        schedule_rows.append([InlineKeyboardButton("Обработать потом", callback_data=f"schedule_{row_id}_later")])
        schedule_keyboard = InlineKeyboardMarkup(schedule_rows)
        await query.message.reply_text(
            "✅ Вы взяли лид.\n\n"
            "📋 Информация по лиду:\n\n"
            + lead_text
            + "\n\nВыберите действие:",
            parse_mode="HTML",
            reply_markup=schedule_keyboard,
        )
        return

    # Выбор времени начала обработки
    if data.startswith("schedule_"):
        parts = data.split("_", 2)
        row_id = int(parts[1])
        action = parts[2]
        if action == "now":
            all_values = _load_sheet_data()
            if len(all_values) <= row_id - 1:
                await query.edit_message_text("❗ Не удалось найти лид в таблице.")
                return
            row = all_values[row_id - 1]
            if not _row_assigned_to_user(row, user_id):
                pending_schedule_input.pop(user_id, None)
                user_busy_state.pop(user_id, None)
                await query.edit_message_text("❗ Лид больше не закреплён за вами. Обновляю доступные лиды.")
                await _send_missed_for_user(context, user_id)
                return
            try:
                await query.message.delete()
            except BadRequest:
                logging.warning("Не удалось удалить сообщение с кнопками для лида %s", row_id)
            pending_schedule_input.pop(user_id, None)
            _mark_user_busy(user_id, row_id)
            assigned_repeat[row_id] = {"last_sent": time.time(), "message_id": None}
            await start_lead_processing(context, user_id, row_id, row, note=False)
            return

        if action == "later":
            pending_schedule_input[user_id] = row_id
            assigned_repeat.pop(row_id, None)
            await query.edit_message_text(
                "🗓 Введите дату и время обработки в формате ддммгггг ччмм"
            )
            return

        await query.edit_message_text("❗ Неизвестное действие.")
        return

    # Убрали автоперенос по кнопкам — дату/время вводит пользователь вручную.

    # Выбор пола (gender)
    if data.startswith("gender_"):
        parts = data.split("_", 2)
        row_id = int(parts[1])
        gender_value = parts[2]
        gender_map = {
            "male": "мужчина",
            "female": "женщина",
            "couple": "пара",
        }
        if row_id not in pending_leads:
            await query.edit_message_text("❗ Нет активного лида для заполнения.")
            return
        pending_leads[row_id]["gender"] = gender_map.get(gender_value, gender_value)
        await query.edit_message_text(
            f"✅ Gender выбран: {pending_leads[row_id]['gender']}."
        )
        return

    # Выбор национальности
    if data.startswith("nationality_"):
        parts = data.split("_", 2)
        row_id = int(parts[1])
        nationality_value = parts[2]
        if row_id not in pending_leads:
            await query.edit_message_text("❗ Нет активного лида для заполнения.")
            return
        nationality_map = {
            "ua": "украинец",
            "by": "белорус",
            "md": "молдован",
            "kz": "казах",
            "ge": "грузин",
            "am": "армяшка",
        }
        pending_leads[row_id]["nationality"] = nationality_map.get(
            nationality_value, nationality_value
        )
        pending_leads[row_id]["awaiting_comment"] = True
        await query.edit_message_text(
            f"✅ Национальность выбрана: {pending_leads[row_id]['nationality']}."
        )
        await query.message.reply_text("Введите комментарий рекрутера:")
        return

    # Выбор статуса лида
    if data.startswith("status_"):
        parts = data.split("_", 2)
        row_id = int(parts[1])
        status_value = parts[2]
        if row_id not in pending_leads:
            await query.edit_message_text("❗ Нет активного лида для заполнения.")
            return
        status_map = {
            "no_answer": "нет ответа",
            "declined": "отказался",
            "agreed": "согласился",
            "not_fit": "не подходит",
            "thinking": "думает",
        }
        status_label = status_map.get(status_value, status_value)
        pending_leads[row_id]["status"] = status_label
        active_processing[user_id] = row_id
        await query.edit_message_text(f"✅ Status выбран: {status_label}.")

        # Для "отказался/согласился/не подходит" нужен только комментарий.
        if status_value in {"declined", "agreed", "not_fit"}:
            pending_leads[row_id]["awaiting_date_input"] = False
            pending_leads[row_id]["followup_datetime"] = None
            scheduled_leads.pop(row_id, None)
            if pending_leads[row_id].get("age"):
                if pending_leads[row_id].get("nationality"):
                    pending_leads[row_id]["awaiting_comment"] = True
                    await query.message.reply_text("Введите комментарий рекрутера:")
                    return
                nationality_keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Украинец", callback_data=f"nationality_{row_id}_ua"
                            ),
                            InlineKeyboardButton(
                                "Белорус", callback_data=f"nationality_{row_id}_by"
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                "Молдован", callback_data=f"nationality_{row_id}_md"
                            ),
                            InlineKeyboardButton(
                                "Казах", callback_data=f"nationality_{row_id}_kz"
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                "Грузин", callback_data=f"nationality_{row_id}_ge"
                            ),
                            InlineKeyboardButton(
                                "Армяшка", callback_data=f"nationality_{row_id}_am"
                            ),
                        ],
                    ]
                )
                await query.message.reply_text(
                    "Выберите национальность:",
                    reply_markup=nationality_keyboard,
                )
                return
            pending_leads[row_id]["awaiting_age"] = True
            await query.message.reply_text("Введите возраст кандидата:")
            return

        # Для "нет ответа/думает" нужен выбор времени повторной обработки.
        if status_value in {"no_answer", "thinking"}:
            pending_leads[row_id]["awaiting_date_input"] = True
            pending_leads[row_id]["awaiting_comment"] = False
            await query.edit_message_text(
                f"✅ Status выбран: {status_label}.\n\n"
                "Введите дату и время повторной обработки в формате "
                "ддммгггг ччмм:"
            )
            return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик обычного текста (для комментария рекрутера)."""
    if not update.message:
        return

    user_id = update.message.from_user.id

    if user_mode.get(user_id) == "edit_history_phone":
        needle_raw = update.message.text.strip()
        if not needle_raw:
            await update.message.reply_text("❗ Введите телефон или его часть.")
            return
        needle = re.sub(r"\D", "", needle_raw)
        phone_col = _find_phone_column()
        if phone_col is None:
            await update.message.reply_text("❗ Не найдена колонка phone в таблице.")
            return
        all_values = _load_sheet_data()
        rows = all_values[1:] if len(all_values) > 1 else []
        found = False
        for row_id, row in enumerate(rows, start=2):
            if _row_value(row, 2) != str(user_id):
                continue
            phone_val = _row_value(row, phone_col)
            phone_digits = re.sub(r"\D", "", phone_val)
            if needle and needle not in phone_digits:
                continue
            found = True
            rows_kb = []
            if phone_val and CopyTextButton is not None:
                rows_kb.append([InlineKeyboardButton("📋 Скопировать номер", copy_text=CopyTextButton(text=phone_val[:256]))])
            rows_kb.append([InlineKeyboardButton("Изменить", callback_data=f"edit_{row_id}")])
            hist_msg = await context.bot.send_message(
                chat_id=user_id,
                text=_build_lead_text(row, phone_val, include_extra=True),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(rows_kb),
            )
            _track_history_message(user_id, hist_msg.message_id)
        if not found:
            await update.message.reply_text("ℹ️ По указанному телефону лидов не найдено.")
        else:
            await update.message.reply_text("✅ Найденные лиды отправлены.")
        user_mode[user_id] = "edit_history"
        return

    schedule_row_id = pending_schedule_input.get(user_id)
    if schedule_row_id:
        raw_datetime = update.message.text.strip()
        due_dt, dt_error = _validate_datetime_input(raw_datetime)
        if dt_error:
            await update.message.reply_text(dt_error)
            return
        date_col = _find_column(["date and time to call"])
        if date_col is None:
            await update.message.reply_text(
                "❌ Не нашёл колонку date and time to call в таблице."
            )
            return
        normalized_datetime = _format_datetime(due_dt)
        sheet.update_cell(schedule_row_id, date_col, normalized_datetime)
        status_col = _find_column(["status"])
        if status_col:
            sheet.update_cell(schedule_row_id, status_col, "нет ответа")
        processed_col = _find_column(["processed at", "processed_at", "дата обработки"])
        if processed_col:
            sheet.update_cell(schedule_row_id, processed_col, datetime.now(POLAND_TZ).strftime("%d.%m.%Y %H:%M"))
        scheduled_leads[schedule_row_id] = {"user_id": user_id, "due_at": due_dt.timestamp()}
        pending_schedule_input.pop(user_id, None)
        user_busy_state.pop(user_id, None)
        await update.message.reply_text(
            f"✅ Лид запланирован на обработку в {normalized_datetime}."
        )
        await _send_missed_for_user(context, user_id)
        return

    row_id = active_processing.get(user_id)
    if not row_id:
        return
    lead_data = pending_leads.get(row_id)
    if not lead_data:
        return

    # Ожидаем возраст кандидата.
    if lead_data.get("awaiting_age"):
        age_value = update.message.text.strip()
        lead_data["age"] = age_value
        lead_data["awaiting_age"] = False
        nationality_keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Украинец", callback_data=f"nationality_{row_id}_ua"),
                    InlineKeyboardButton("Белорус", callback_data=f"nationality_{row_id}_by"),
                ],
                [
                    InlineKeyboardButton("Молдован", callback_data=f"nationality_{row_id}_md"),
                    InlineKeyboardButton("Казах", callback_data=f"nationality_{row_id}_kz"),
                ],
                [
                    InlineKeyboardButton("Грузин", callback_data=f"nationality_{row_id}_ge"),
                    InlineKeyboardButton("Армяшка", callback_data=f"nationality_{row_id}_am"),
                ],
            ]
        )
        await update.message.reply_text(
            "Выберите национальность:",
            reply_markup=nationality_keyboard,
        )
        return

    # Ожидаем комментарий рекрутера и сохраняем его в таблицу.
    if lead_data.get("awaiting_comment"):
        comment = update.message.text.strip()
        lead_data["awaiting_comment"] = False

        gender_col = _find_column(["gender"])
        status_col = _find_column(["status"])
        age_col = _find_column(["age", "возраст"])
        nationality_col = _find_column(["nationality", "национальность"])
        date_col = _find_column(["date and time to call"])
        comment_col = _find_column(["recruiter's comment", "recruiter comment"])

        missing = []
        if gender_col is None:
            missing.append("gender")
        if status_col is None:
            missing.append("status")
        if comment_col is None:
            missing.append("Recruiter's comment")
        status_value = (lead_data.get("status") or "").lower()
        if status_value in {"думает", "отказался", "согласился", "не подходит"}:
            if age_col is None:
                missing.append("age/возраст")
            if nationality_col is None:
                missing.append("nationality/национальность")

        if missing:
            await update.message.reply_text(
                "❌ Не нашёл колонку(и) в таблице: " + ", ".join(missing)
            )
            return

        # Записываем итоговые поля в таблицу.
        sheet.update_cell(row_id, gender_col, lead_data.get("gender", ""))
        sheet.update_cell(row_id, status_col, lead_data.get("status", ""))
        if age_col and lead_data.get("age"):
            sheet.update_cell(row_id, age_col, lead_data.get("age", ""))
        if nationality_col and lead_data.get("nationality"):
            sheet.update_cell(row_id, nationality_col, lead_data.get("nationality", ""))
        followup_datetime = lead_data.get("followup_datetime")
        if date_col and status_value in {"думает", "нет ответа"} and followup_datetime:
            sheet.update_cell(row_id, date_col, followup_datetime)
        if date_col and status_value in {"отказался", "согласился", "не подходит"}:
            sheet.update_cell(row_id, date_col, "")
        prev_comment = _row_value(_load_sheet_data()[row_id - 1], comment_col)
        if prev_comment:
            merged_comment = f"{prev_comment}\n---\n{comment}"
        else:
            merged_comment = comment
        if lead_data.get("edit_mode"):
            sheet.update_cell(row_id, comment_col, comment)
        else:
            sheet.update_cell(row_id, comment_col, merged_comment)

        processed_col = _find_column(["processed at", "processed_at", "дата обработки"])
        if processed_col:
            sheet.update_cell(row_id, processed_col, datetime.now(POLAND_TZ).strftime("%d.%m.%Y %H:%M"))

        await update.message.reply_text("✅ Данные сохранены в таблице.")
        await _cleanup_lead_messages(context, row_id)
        was_edit = bool(lead_data.get("edit_mode"))
        pending_leads.pop(row_id, None)
        active_processing.pop(user_id, None)
        user_busy_state.pop(user_id, None)
        if was_edit:
            await update.message.reply_text(
                "Выберите действие:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Продолжить редактировать", callback_data="postedit_continue")],
                    [InlineKeyboardButton("Вернуться в обработку лидов", callback_data="postedit_back")],
                ]),
            )
            return
        await _send_missed_for_user(context, user_id)
        return

    if lead_data.get("awaiting_date_input"):
        raw_datetime = update.message.text.strip()
        due_dt, dt_error = _validate_datetime_input(raw_datetime)
        if dt_error:
            await update.message.reply_text(dt_error)
            return
        date_col = _find_column(["date and time to call"])
        if date_col is None:
            await update.message.reply_text(
                "❌ Не нашёл колонку date and time to call в таблице."
            )
            return
        normalized_datetime = _format_datetime(due_dt)
        sheet.update_cell(row_id, date_col, normalized_datetime)
        processed_col = _find_column(["processed at", "processed_at", "дата обработки"])
        if processed_col:
            sheet.update_cell(row_id, processed_col, datetime.now(POLAND_TZ).strftime("%d.%m.%Y %H:%M"))
        assigned_repeat.pop(row_id, None)
        scheduled_leads[row_id] = {"user_id": user_id, "due_at": due_dt.timestamp()}
        lead_data["awaiting_date_input"] = False
        lead_data["followup_datetime"] = normalized_datetime
        status_value = (lead_data.get("status") or "").lower()
        if status_value == "думает":
            if lead_data.get("age"):
                if lead_data.get("nationality"):
                    lead_data["awaiting_comment"] = True
                    await update.message.reply_text(
                        f"✅ Лид запланирован на повторную обработку в {normalized_datetime}.\n"
                        "Теперь введите комментарий рекрутера:"
                    )
                    return
                nationality_keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Украинец", callback_data=f"nationality_{row_id}_ua"
                            ),
                            InlineKeyboardButton(
                                "Белорус", callback_data=f"nationality_{row_id}_by"
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                "Молдован", callback_data=f"nationality_{row_id}_md"
                            ),
                            InlineKeyboardButton(
                                "Казах", callback_data=f"nationality_{row_id}_kz"
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                "Грузин", callback_data=f"nationality_{row_id}_ge"
                            ),
                            InlineKeyboardButton(
                                "Армяшка", callback_data=f"nationality_{row_id}_am"
                            ),
                        ],
                    ]
                )
                await update.message.reply_text(
                    "Выберите национальность:",
                    reply_markup=nationality_keyboard,
                )
                return
            lead_data["awaiting_age"] = True
            await update.message.reply_text(
                f"✅ Лид запланирован на повторную обработку в {normalized_datetime}.\n"
                "Теперь введите возраст кандидата:"
            )
        else:
            lead_data["awaiting_comment"] = True
            await update.message.reply_text(
                f"✅ Лид запланирован на повторную обработку в {normalized_datetime}.\n"
                "Теперь введите комментарий рекрутера:"
            )
        return


async def start_lead_processing(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    row_id: int,
    row: List[str],
    note: bool,
) -> None:
    # Инициализируем состояние заполнения лида.
    pending_leads[row_id] = {
        "lead_row_id": row_id,
        "user_id": user_id,
        "gender": None,
        "status": None,
        "age": None,
        "nationality": None,
        "awaiting_comment": False,
        "awaiting_age": False,
        "awaiting_reschedule": False,
        "awaiting_date_input": False,
        "message_ids": [],
    }
    age_col = _find_column(["age", "возраст"])
    nationality_col = _find_column(["nationality", "национальность"])
    pending_leads[row_id]["age"] = _row_value(row, age_col) or None
    pending_leads[row_id]["nationality"] = _row_value(row, nationality_col) or None
    phone_col = _find_phone_column()
    phone = _row_value(row, phone_col)
    lead_text = _build_lead_text(row, phone, include_extra=True)
    # Если лид пришёл повторно по расписанию — показываем напоминание.
    header = "✅ Вы взяли этот лид и его нужно обработать.\n\n" if note else ""
    lead_message = await context.bot.send_message(
        chat_id=user_id,
        text=header + "📋 Информация по лиду:\n\n" + lead_text,
        parse_mode="HTML",
    )
    pending_leads[row_id]["message_ids"].append(lead_message.message_id)
    gender_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Мужчина", callback_data=f"gender_{row_id}_male"),
                InlineKeyboardButton("Женщина", callback_data=f"gender_{row_id}_female"),
                InlineKeyboardButton("Пара", callback_data=f"gender_{row_id}_couple"),
            ]
        ]
    )
    gender_message = await context.bot.send_message(
        chat_id=user_id,
        text="Выберите gender:",
        reply_markup=gender_keyboard,
    )
    pending_leads[row_id]["message_ids"].append(gender_message.message_id)
    status_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Нет ответа", callback_data=f"status_{row_id}_no_answer"),
                InlineKeyboardButton("Отказался", callback_data=f"status_{row_id}_declined"),
            ],
            [
                InlineKeyboardButton("Согласился", callback_data=f"status_{row_id}_agreed"),
                InlineKeyboardButton("Не подходит", callback_data=f"status_{row_id}_not_fit"),
            ],
            [
                InlineKeyboardButton("Думает", callback_data=f"status_{row_id}_thinking"),
            ],
        ]
    )
    status_message = await context.bot.send_message(
        chat_id=user_id,
        text="Выберите status:",
        reply_markup=status_keyboard,
    )
    pending_leads[row_id]["message_ids"].append(status_message.message_id)


async def on_startup(application: Application) -> None:
    """Запуск фоновой проверки при старте бота."""
    await check_new_leads(ContextTypes.DEFAULT_TYPE(application=application))
    await _notify_admin(application, "✅ <b>Бот запущен</b>")
    application.create_task(poll_leads_forever(application))


def _row_assigned_to_user(row: List[str], user_id: int) -> bool:
    return _row_value(row, 2) == str(user_id)


async def _resume_processing_for_user(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    row_id: int,
    row: List[str],
    restart_if_missing: bool = False,
) -> None:
    """Возвращаем пользователю текущий шаг обработки, если он уже начал лид."""
    lead_data = pending_leads.get(row_id)
    if not lead_data or lead_data.get("user_id") != user_id:
        if restart_if_missing:
            await start_lead_processing(context, user_id, row_id, row, note=True)
            return
        if pending_schedule_input.get(user_id) == row_id:
            await context.bot.send_message(
                chat_id=user_id,
                text="⏳ Возобновляем ваш этап: введите дату и время обработки в формате ддммгггг ччмм.",
            )
            return
        await context.bot.send_message(
            chat_id=user_id,
            text="⏳ Вы уже взяли этот лид. Продолжите заполнение с текущего шага.",
        )
        return

    _mark_user_busy(user_id, row_id)
    active_processing[user_id] = row_id
    phone = _row_value(row, _find_phone_column())
    msg = await context.bot.send_message(
        chat_id=user_id,
        text="⏳ Возобновляем обработку вашего лида.\n\n" + _build_lead_text(row, phone, include_extra=True),
        parse_mode="HTML",
    )
    lead_data.setdefault("message_ids", []).append(msg.message_id)

    if lead_data.get("awaiting_date_input"):
        step = await context.bot.send_message(
            chat_id=user_id,
            text="Введите дату и время повторной обработки в формате ддммгггг ччмм:\nНапример: 05052026 1430",
        )
        lead_data["message_ids"].append(step.message_id)
        return
    if lead_data.get("awaiting_age"):
        step = await context.bot.send_message(chat_id=user_id, text="Введите возраст кандидата:")
        lead_data["message_ids"].append(step.message_id)
        return
    if lead_data.get("awaiting_comment"):
        step = await context.bot.send_message(chat_id=user_id, text="Введите комментарий рекрутера:")
        lead_data["message_ids"].append(step.message_id)
        return

    step = await context.bot.send_message(
        chat_id=user_id,
        text="Продолжите заполнение: выберите gender и status.",
    )
    lead_data["message_ids"].append(step.message_id)

    gender_keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Мужчина", callback_data=f"gender_{row_id}_male"),
            InlineKeyboardButton("Женщина", callback_data=f"gender_{row_id}_female"),
            InlineKeyboardButton("Пара", callback_data=f"gender_{row_id}_couple"),
        ]]
    )
    gender_message = await context.bot.send_message(
        chat_id=user_id,
        text="Выберите gender:",
        reply_markup=gender_keyboard,
    )
    lead_data["message_ids"].append(gender_message.message_id)

    status_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Нет ответа", callback_data=f"status_{row_id}_no_answer"),
                InlineKeyboardButton("Отказался", callback_data=f"status_{row_id}_declined"),
            ],
            [
                InlineKeyboardButton("Согласился", callback_data=f"status_{row_id}_agreed"),
                InlineKeyboardButton("Не подходит", callback_data=f"status_{row_id}_not_fit"),
            ],
            [InlineKeyboardButton("Думает", callback_data=f"status_{row_id}_thinking")],
        ]
    )
    status_message = await context.bot.send_message(
        chat_id=user_id,
        text="Выберите status:",
        reply_markup=status_keyboard,
    )
    lead_data["message_ids"].append(status_message.message_id)


# Запуск
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("leads", leads))
    app.add_handler(CommandHandler("process", process_mode))
    app.add_handler(CommandHandler("history", history_mode))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)
    print("✅ Бот запущен")
    app.run_polling()

