"""
Telegram-бот для ростерії: збір клієнтів через /start, сегменти з власним
щотижневим розкладом розсилки, і команда для миттєвої розсилки всім
(зміни, прайси, акції).

Автор: згенеровано Claude для конкретного кейсу ростерії.
"""

import logging
import os
import html
import json
import re
import sqlite3
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
# Можна вказати кілька ID через кому, напр.: ADMIN_IDS=111111,222222,333333
ADMIN_IDS = [
    int(x.strip()) for x in os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "0")).split(",")
    if x.strip().isdigit() and int(x.strip()) != 0
]
DB_PATH = os.getenv("DB_PATH", "roastery.db")
TZ = ZoneInfo("Europe/Kyiv")

WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
}
WEEKDAYS_UA = {
    0: "понеділок", 1: "вівторок", 2: "середа", 3: "четвер",
    4: "п'ятниця", 5: "субота", 6: "неділя",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Тимчасовий стан для інтерактивного вибору отримувачів команди /sendto
# (тримається в пам'яті, скидається при перезапуску бота — це нормально для цього сценарію)
SENDTO_SELECTIONS: dict[int, set[int]] = {}
SENDTO_AWAITING_TEXT: dict[int, list[int]] = {}

# Стан для кроків меню з кнопками (наприклад, очікування тексту після вибору дії)
MENU_PENDING: dict[int, dict] = {}

# Стан майстра планування розсилок (кому / коли / текст) та вибір обраних людей у ньому
BC_PENDING: dict[int, dict] = {}
BC_SELECTIONS: dict[int, set[int]] = {}

# Стан майстра запитань так/ні (кому питати) — окремий від BC, щоб не заважати плануванню
QNA_PENDING: dict[int, dict] = {}
QNA_SELECTIONS: dict[int, set[int]] = {}

# Відстежує, кому з клієнтів адмін відповідає, коли робить Reply на переслане повідомлення
# Ключ: (admin_chat_id, message_id повідомлення з пересилкою) -> chat_id клієнта
FORWARD_MAP: dict[tuple[int, int], int] = {}

# Відстежує повідомлення "новий підписник" у кожного адміна окремо,
# щоб прибрати їх у всіх, щойно хтось один призначив себе відповідальним.
# Ключ: chat_id клієнта -> {admin_chat_id: message_id}
NEW_SUB_MESSAGES: dict[int, dict[int, int]] = {}

# Стан опитувальника замовлення клієнта: chat_id клієнта -> {"step":..., "point_name":..., ...}
ORDER_PENDING: dict[int, dict] = {}

# Стан заповнення "картки клієнта": chat_id клієнта -> {"step":..., дані...}
PROFILE_PENDING: dict[int, dict] = {}

# Стан адміна, коли він редагує картку КОГОСЬ ІНШОГО (не своєму chat_id): admin_chat_id -> {дані...}
ADMIN_EDIT_PROFILE_PENDING: dict[int, dict] = {}

# Стан, коли адмін додає адресу конкретному клієнту: admin_chat_id -> target_chat_id клієнта
ADMIN_ADD_ADDRESS_PENDING: dict[int, dict] = {}

# Стан майстра заповнення картки НОВОГО клієнта одразу після підписки: admin_chat_id -> {дані...}
ADMIN_NEW_PROFILE_PENDING: dict[int, dict] = {}

# Стан редагування адміном конкретного повідомлення в історії листування: admin_chat_id -> {дані...}
ADMIN_MSGEDIT_PENDING: dict[int, dict] = {}

# Вибір кількох запланованих розсилок для одночасного скасування: admin_chat_id -> set(broadcast_id)
BC_CANCEL_SELECTIONS: dict[int, set[int]] = {}

WEEKDAY_LABELS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
BC_HOURS = [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
BC_MINUTES = [0, 15, 30, 45]


# ---------- База даних ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT,
            segment TEXT,
            active INTEGER DEFAULT 1,
            joined_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS segments (
            name TEXT PRIMARY KEY,
            message TEXT DEFAULT '',
            schedule_day INTEGER,
            schedule_time TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            segment TEXT NOT NULL,
            weekday INTEGER NOT NULL,
            hhmm TEXT NOT NULL,
            PRIMARY KEY (segment, weekday, hhmm)
        )
    """)
    sched_cols = [r["name"] for r in conn.execute("PRAGMA table_info(schedules)").fetchall()]
    if "kind" not in sched_cols:
        # 'weekday' зберігає: для kind='weekly'/'biweekly' — день тижня (0-6),
        # для kind='monthly' — день місяця (1-28)
        conn.execute("ALTER TABLE schedules ADD COLUMN kind TEXT DEFAULT 'weekly'")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,          -- 'once' або 'weekly'
            target_type TEXT NOT NULL,   -- 'segment' або 'selected'
            target_value TEXT NOT NULL,  -- назва групи, або chat_id через кому
            message TEXT NOT NULL,
            run_at TEXT,                 -- ISO дата-час, для 'once'
            weekday INTEGER,             -- 0-6, для 'weekly'
            hhmm TEXT,                   -- 'ГГ:ХХ', для 'weekly'
            active INTEGER DEFAULT 1,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            chat_id INTEGER PRIMARY KEY,
            name TEXT,
            username TEXT
        )
    """)
    # Міграція: додаємо колонку "відповідальний адмін" до клієнтів, якщо її ще нема
    existing_cols = [r["name"] for r in conn.execute("PRAGMA table_info(subscribers)").fetchall()]
    if "responsible_admin" not in existing_cols:
        conn.execute("ALTER TABLE subscribers ADD COLUMN responsible_admin INTEGER")
    if "needs_packaging" not in existing_cols:
        conn.execute("ALTER TABLE subscribers ADD COLUMN needs_packaging INTEGER DEFAULT 0")
    bc_cols = [r["name"] for r in conn.execute("PRAGMA table_info(broadcasts)").fetchall()]
    if "is_question" not in bc_cols:
        conn.execute("ALTER TABLE broadcasts ADD COLUMN is_question INTEGER DEFAULT 0")
    if "file_id" not in bc_cols:
        conn.execute("ALTER TABLE broadcasts ADD COLUMN file_id TEXT")
    if "created_by" not in bc_cols:
        conn.execute("ALTER TABLE broadcasts ADD COLUMN created_by INTEGER")
    if "name" not in bc_cols:
        conn.execute("ALTER TABLE broadcasts ADD COLUMN name TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS client_profiles (
            chat_id INTEGER PRIMARY KEY,
            full_name TEXT,
            point_name TEXT,
            address TEXT,
            phone TEXT,
            fop TEXT,
            ipn TEXT,
            payment_method TEXT,
            updated_at TEXT
        )
    """)
    profile_cols = [r["name"] for r in conn.execute("PRAGMA table_info(client_profiles)").fetchall()]
    if "ipn" not in profile_cols:
        conn.execute("ALTER TABLE client_profiles ADD COLUMN ipn TEXT")
    if "payment_method" not in profile_cols:
        conn.execute("ALTER TABLE client_profiles ADD COLUMN payment_method TEXT")
    if "delivery_zone" not in profile_cols:
        conn.execute("ALTER TABLE client_profiles ADD COLUMN delivery_zone TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS client_addresses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            phone TEXT,
            created_at TEXT
        )
    """)
    addr_cols = [r["name"] for r in conn.execute("PRAGMA table_info(client_addresses)").fetchall()]
    if "phone" not in addr_cols:
        conn.execute("ALTER TABLE client_addresses ADD COLUMN phone TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            address TEXT,
            contact_phone TEXT,
            payment_method TEXT,
            items_json TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS message_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            direction TEXT NOT NULL,
            text TEXT,
            telegram_message_id INTEGER,
            created_at TEXT
        )
    """)
    # Міграція: переносимо старий (єдиний) розклад із таблиці segments у нову
    # таблицю schedules, щоб не втратити те, що вже було задано раніше.
    old_rows = conn.execute(
        "SELECT name, schedule_day, schedule_time FROM segments "
        "WHERE schedule_day IS NOT NULL AND schedule_time IS NOT NULL"
    ).fetchall()
    for r in old_rows:
        conn.execute(
            "INSERT OR IGNORE INTO schedules (segment, weekday, hhmm) VALUES (?, ?, ?)",
            (r["name"], r["schedule_day"], r["schedule_time"]),
        )
    conn.commit()
    conn.close()


def is_admin(update: Update) -> bool:
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        return False
    user = update.effective_user
    if user:
        conn = db()
        conn.execute(
            "INSERT INTO admins (chat_id, name, username) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name, username=excluded.username",
            (chat_id, user.full_name, user.username or ""),
        )
        conn.commit()
        conn.close()
    return True


def _admins_with_username() -> list:
    conn = db()
    rows = conn.execute("SELECT * FROM admins WHERE username != '' AND username IS NOT NULL").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _admin_link_button(admin_chat_id: int, label: str) -> InlineKeyboardButton | None:
    conn = db()
    row = conn.execute("SELECT * FROM admins WHERE chat_id=?", (admin_chat_id,)).fetchone()
    conn.close()
    if row and row["username"]:
        return InlineKeyboardButton(label, url=f"https://t.me/{row['username']}")
    return None


def _log_message(chat_id: int, direction: str, text: str, telegram_message_id: int | None) -> None:
    conn = db()
    conn.execute(
        "INSERT INTO message_log (chat_id, direction, text, telegram_message_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (chat_id, direction, text, telegram_message_id, datetime.now(TZ).isoformat()),
    )
    conn.commit()
    conn.close()


def _get_message_log(chat_id: int, limit: int = 15) -> list:
    conn = db()
    rows = conn.execute(
        "SELECT * FROM message_log WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    """Надсилає повідомлення всім адмінам зі списку ADMIN_IDS."""
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception as e:
            logger.warning(f"Не вдалось надіслати адміну {admin_id}: {e}")


# ---------- Команди для клієнтів ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    conn = db()
    conn.execute(
        """INSERT INTO subscribers (chat_id, name, username, segment, active, joined_at)
           VALUES (?, ?, ?, NULL, 1, ?)
           ON CONFLICT(chat_id) DO UPDATE SET active=1""",
        (chat.id, user.full_name, user.username or "", datetime.now(TZ).isoformat()),
    )
    conn.commit()
    conn.close()

    welcome_msg = await update.message.reply_text(
        "Привіт! Дякуємо, що підписались на новини нашої ростерії 🫶\n"
        "Тут ви отримуватимете нагадування, інформацію про кавові новинки, акції та зміни в асортименті😉\n\n"
        "👇👇👇 Кнопки внизу екрана — вони завжди тут: асортимент, доставка, замовлення, картка, менеджер.\n\n"
        "Якщо захочете відписатись — просто напишіть /stop.",
        reply_markup=_keyboard_for_recipient(chat.id),
    )
    try:
        await context.bot.pin_chat_message(chat.id, welcome_msg.message_id, disable_notification=True)
    except Exception as e:
        logger.warning(f"Не вдалось закріпити привітальне повідомлення клієнту {chat.id}: {e}")

    admins_avail = _admins_with_username()
    if len(admins_avail) == 1:
        conn = db()
        conn.execute(
            "UPDATE subscribers SET responsible_admin=? WHERE chat_id=?",
            (admins_avail[0]["chat_id"], chat.id),
        )
        conn.commit()
        conn.close()

    if ADMIN_IDS:
        buttons = [[InlineKeyboardButton("🙋 Я відповідальний", callback_data=f"respme:{chat.id}")]]
        text = (
            f"🆕 Новий підписник: {user.full_name} (@{user.username or '—'})\n"
            f"chat_id: {chat.id}\n\nОберіть, хто відповідальний:"
        )
        sent_ids = {}
        for admin_id in ADMIN_IDS:
            try:
                sent = await context.bot.send_message(
                    admin_id, text, reply_markup=InlineKeyboardMarkup(buttons)
                )
                sent_ids[admin_id] = sent.message_id
            except Exception as e:
                logger.warning(f"Не вдалось надіслати адміну {admin_id}: {e}")
        NEW_SUB_MESSAGES[chat.id] = sent_ids


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    conn.execute("UPDATE subscribers SET active=0 WHERE chat_id=?", (update.effective_chat.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("Ви відписались від розсилки. Щоб повернутись — надішліть /start.")


async def setsegment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Дозволяє будь-коли перепризначити сегмент існуючому клієнту — за юзернеймом або chat_id."""
    if not is_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Використання: /setsegment @юзернейм_або_chat_id новий_сегмент\n"
            "Приклад: /setsegment @ivan_kava gurt_pravyi\n"
            "Щоб прибрати клієнта з усіх сегментів: /setsegment @ivan_kava __none__\n"
            "Список клієнтів і їхні chat_id — команда /clients"
        )
        return
    identifier = context.args[0].lstrip("@")
    new_segment = context.args[1]
    segment_value = None if new_segment == "__none__" else new_segment

    conn = db()
    if identifier.isdigit():
        row = conn.execute("SELECT * FROM subscribers WHERE chat_id=?", (int(identifier),)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM subscribers WHERE LOWER(username)=LOWER(?)", (identifier,)
        ).fetchone()

    if not row:
        conn.close()
        await update.message.reply_text(
            f"Клієнта «{context.args[0]}» не знайдено серед підписників. Перевірте /clients."
        )
        return

    conn.execute("UPDATE subscribers SET segment=? WHERE chat_id=?", (segment_value, row["chat_id"]))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"Готово. {row['name']} (@{row['username'] or '—'}) тепер у сегменті: "
        f"{segment_value or 'без сегмента'}."
    )


# ---------- Callback: призначення сегмента ----------

async def assign_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    _, chat_id, segment = query.data.split(":", 2)
    segment_value = None if segment == "__none__" else segment
    conn = db()
    conn.execute("UPDATE subscribers SET segment=? WHERE chat_id=?", (segment_value, int(chat_id)))
    conn.commit()
    conn.close()
    await query.edit_message_text(query.message.text + f"\n\n✅ Призначено сегмент: {segment_value or 'без сегмента'}")


async def respme_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Адмін одним тапом призначає себе відповідальним за клієнта.
    Після цього повідомлення про нового підписника зникає в усіх інших адмінів,
    і з'являється другий крок — вибір групи."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    _, chat_id_str = query.data.split(":", 1)
    chat_id = int(chat_id_str)

    conn = db()
    conn.execute("UPDATE subscribers SET responsible_admin=? WHERE chat_id=?", (admin_id, chat_id))
    conn.commit()
    admin_row = conn.execute("SELECT name FROM admins WHERE chat_id=?", (admin_id,)).fetchone()
    segments = [r["name"] for r in conn.execute("SELECT name FROM segments")]
    conn.close()
    name = admin_row["name"] if admin_row else "адмін"

    for other_admin_id, msg_id in NEW_SUB_MESSAGES.pop(chat_id, {}).items():
        if other_admin_id == admin_id:
            continue
        try:
            await context.bot.delete_message(other_admin_id, msg_id)
        except Exception as e:
            logger.warning(f"Не вдалось прибрати повідомлення у {other_admin_id}: {e}")

    if not segments:
        await query.edit_message_text(f"✅ {name} — відповідальний за цього клієнта.")
    else:
        buttons = [
            [InlineKeyboardButton(seg, callback_data=f"assign:{chat_id}:{seg}")] for seg in segments
        ]
        buttons.append([InlineKeyboardButton("Без групи", callback_data=f"assign:{chat_id}:__none__")])
        await query.edit_message_text(
            f"✅ {name} — відповідальний за цього клієнта.\n\nТепер оберіть групу:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    await context.bot.send_message(
        admin_id,
        "Бажаєте одразу заповнити картку цього клієнта (ім'я, точка, телефон, адреса тощо)?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Так, заповнити", callback_data=f"adminprofile_newwizard:{chat_id}")],
            [InlineKeyboardButton("⏭ Ні, пізніше", callback_data="adminprofile_newskip")],
        ]),
    )


async def pickresp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Клієнт сам обирає відповідального адміна після /start (не потребує прав адміна)."""
    query = update.callback_query
    await query.answer()
    client_chat_id = update.effective_chat.id
    _, admin_chat_id_str = query.data.split(":", 1)
    admin_chat_id = int(admin_chat_id_str)

    conn = db()
    conn.execute(
        "UPDATE subscribers SET responsible_admin=? WHERE chat_id=?", (admin_chat_id, client_chat_id)
    )
    conn.commit()
    row = conn.execute("SELECT name FROM admins WHERE chat_id=?", (admin_chat_id,)).fetchone()
    conn.close()
    name = row["name"] if row else "менеджера"
    await query.edit_message_text(f"✅ Готово! Ваш відповідальний контакт: {name}.")


# ---------- Адмін-команди: сегменти й розклад ----------

async def addsegment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Використання: /addsegment назва_сегмента")
        return
    name = context.args[0]
    conn = db()
    conn.execute("INSERT OR IGNORE INTO segments (name) VALUES (?)", (name,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Сегмент «{name}» створено. Тепер задайте йому розклад і повідомлення.")


def _schedule_row_label(s) -> str:
    kind = s["kind"] if "kind" in s.keys() and s["kind"] else "weekly"
    if kind == "monthly":
        return f"раз на місяць, {s['weekday']} числа о {s['hhmm']}"
    if kind == "biweekly":
        return f"раз на 2 тижні, {WEEKDAYS_UA[s['weekday']]} о {s['hhmm']}"
    return f"щотижня, {WEEKDAYS_UA[s['weekday']]} о {s['hhmm']}"


def _segments_text() -> str:
    conn = db()
    rows = conn.execute("SELECT * FROM segments").fetchall()
    if not rows:
        conn.close()
        return "Груп ще немає. Створіть: /addsegment назва"
    text = "Групи:\n\n"
    for r in rows:
        count = conn.execute("SELECT COUNT(*) c FROM subscribers WHERE segment=? AND active=1", (r["name"],)).fetchone()["c"]
        sched_rows = conn.execute(
            "SELECT weekday, hhmm, kind FROM schedules WHERE segment=? ORDER BY weekday, hhmm", (r["name"],)
        ).fetchall()
        if sched_rows:
            schedule_lines = "\n".join(f"      • {_schedule_row_label(s)}" for s in sched_rows)
        else:
            schedule_lines = "      (розклад не задано)"
        text += (
            f"📁 {r['name']} ({count} клієнтів)\n"
            f"   Розклад:\n{schedule_lines}\n"
            f"   Повідомлення: {r['message'][:60] + '...' if r['message'] and len(r['message']) > 60 else (r['message'] or '(порожнє)')}\n\n"
        )
    conn.close()
    return text


async def segments_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(_segments_text())


async def setschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Повністю ЗАМІНЮЄ розклад сегмента на один щотижневий запис (видаляє всі попередні)."""
    if not is_admin(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Використання: /setschedule сегмент день ГГ:ХХ\n"
            "⚠️ Ця команда ЗАМІНЮЄ весь розклад сегмента одним щотижневим записом.\n"
            "Якщо потрібно ДОДАТИ ще один час — /addschedule. Раз на 2 тижні — /addschedulebiweekly. "
            "Раз на місяць — /addschedulemonthly.\n"
            "День: mon, tue, wed, thu, fri, sat, sun\n"
            "Приклад: /setschedule wholesale mon 10:00"
        )
        return
    name, day_str, time_str = context.args[0], context.args[1].lower(), context.args[2]
    if day_str not in WEEKDAYS:
        await update.message.reply_text("Невірний день. Використовуйте: mon, tue, wed, thu, fri, sat, sun")
        return
    try:
        hh, mm = map(int, time_str.split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
    except Exception:
        await update.message.reply_text("Невірний формат часу. Приклад: 10:00")
        return

    conn = db()
    conn.execute("INSERT OR IGNORE INTO segments (name) VALUES (?)", (name,))
    conn.execute("DELETE FROM schedules WHERE segment=?", (name,))
    conn.execute(
        "INSERT INTO schedules (segment, weekday, hhmm, kind) VALUES (?, ?, ?, 'weekly')",
        (name, WEEKDAYS[day_str], time_str),
    )
    conn.commit()
    conn.close()

    remove_all_segment_jobs(context.application, name)
    create_segment_job(context.application, name, "weekly", WEEKDAYS[day_str], hh, mm)
    await update.message.reply_text(
        f"Готово. Розклад сегмента «{name}» ЗАМІНЕНО одним записом: "
        f"щотижня, {WEEKDAYS_UA[WEEKDAYS[day_str]]} о {time_str}.\n"
        f"Щоб додати ще один час без видалення цього — /addschedule {name} день ГГ:ХХ"
    )


async def addschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ДОДАЄ ще один щотижневий час розсилки для сегмента, не видаляючи наявні."""
    if not is_admin(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Використання: /addschedule сегмент день ГГ:ХХ\n"
            "Додає ще один ЩОТИЖНЕВИЙ час розсилки для сегмента (наявні розклади лишаються).\n"
            "Приклад: /addschedule wholesale thu 16:00\n\n"
            "Раз на 2 тижні — /addschedulebiweekly. Раз на місяць — /addschedulemonthly."
        )
        return
    name, day_str, time_str = context.args[0], context.args[1].lower(), context.args[2]
    if day_str not in WEEKDAYS:
        await update.message.reply_text("Невірний день. Використовуйте: mon, tue, wed, thu, fri, sat, sun")
        return
    try:
        hh, mm = map(int, time_str.split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
    except Exception:
        await update.message.reply_text("Невірний формат часу. Приклад: 10:00")
        return

    conn = db()
    conn.execute("INSERT OR IGNORE INTO segments (name) VALUES (?)", (name,))
    conn.execute(
        "INSERT OR IGNORE INTO schedules (segment, weekday, hhmm, kind) VALUES (?, ?, ?, 'weekly')",
        (name, WEEKDAYS[day_str], time_str),
    )
    conn.commit()
    count = conn.execute("SELECT COUNT(*) c FROM schedules WHERE segment=?", (name,)).fetchone()["c"]
    conn.close()

    create_segment_job(context.application, name, "weekly", WEEKDAYS[day_str], hh, mm)
    await update.message.reply_text(
        f"Додано. Сегмент «{name}» тепер отримує розсилку щотижня, {WEEKDAYS_UA[WEEKDAYS[day_str]]} о {time_str} "
        f"(додатково до вже наявних). Всього записів розкладу: {count}."
    )


async def addschedulebiweekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Додає розсилку раз на ДВА ТИЖНІ для сегмента."""
    if not is_admin(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Використання: /addschedulebiweekly сегмент день ГГ:ХХ\n"
            "Розсилка раз на 2 тижні у вказаний день.\n"
            "Приклад: /addschedulebiweekly vip mon 10:00"
        )
        return
    name, day_str, time_str = context.args[0], context.args[1].lower(), context.args[2]
    if day_str not in WEEKDAYS:
        await update.message.reply_text("Невірний день. Використовуйте: mon, tue, wed, thu, fri, sat, sun")
        return
    try:
        hh, mm = map(int, time_str.split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
    except Exception:
        await update.message.reply_text("Невірний формат часу. Приклад: 10:00")
        return

    conn = db()
    conn.execute("INSERT OR IGNORE INTO segments (name) VALUES (?)", (name,))
    conn.execute(
        "INSERT OR IGNORE INTO schedules (segment, weekday, hhmm, kind) VALUES (?, ?, ?, 'biweekly')",
        (name, WEEKDAYS[day_str], time_str),
    )
    conn.commit()
    conn.close()

    create_segment_job(context.application, name, "biweekly", WEEKDAYS[day_str], hh, mm)
    await update.message.reply_text(
        f"Додано. Сегмент «{name}» тепер отримує розсилку раз на 2 тижні, "
        f"{WEEKDAYS_UA[WEEKDAYS[day_str]]} о {time_str}."
    )


async def addschedulemonthly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Додає розсилку раз на МІСЯЦЬ для сегмента (конкретний день місяця, 1-28)."""
    if not is_admin(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Використання: /addschedulemonthly сегмент день_місяця ГГ:ХХ\n"
            "День місяця від 1 до 28 (щоб коректно працювало для будь-якого місяця).\n"
            "Приклад: /addschedulemonthly vip 1 10:00 — першого числа кожного місяця"
        )
        return
    name, day_str, time_str = context.args[0], context.args[1], context.args[2]
    try:
        day_of_month = int(day_str)
        assert 1 <= day_of_month <= 28
    except Exception:
        await update.message.reply_text("День місяця має бути числом від 1 до 28.")
        return
    try:
        hh, mm = map(int, time_str.split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
    except Exception:
        await update.message.reply_text("Невірний формат часу. Приклад: 10:00")
        return

    conn = db()
    conn.execute("INSERT OR IGNORE INTO segments (name) VALUES (?)", (name,))
    conn.execute(
        "INSERT OR IGNORE INTO schedules (segment, weekday, hhmm, kind) VALUES (?, ?, ?, 'monthly')",
        (name, day_of_month, time_str),
    )
    conn.commit()
    conn.close()

    create_segment_job(context.application, name, "monthly", day_of_month, hh, mm)
    await update.message.reply_text(
        f"Додано. Сегмент «{name}» тепер отримує розсилку раз на місяць, {day_of_month} числа о {time_str}."
    )


async def removeschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Видаляє один конкретний запис розкладу сегмента (будь-якого типу)."""
    if not is_admin(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Використання: /removeschedule сегмент день ГГ:ХХ\n"
            "Видаляє один конкретний час розсилки (інші лишаються). Для щомісячного розкладу "
            "«день» — це число місяця (1-28).\n"
            "Приклад: /removeschedule wholesale thu 16:00"
        )
        return
    name, day_str, time_str = context.args[0], context.args[1].lower(), context.args[2]
    if day_str in WEEKDAYS:
        weekday_val = WEEKDAYS[day_str]
    else:
        try:
            weekday_val = int(day_str)
        except ValueError:
            await update.message.reply_text(
                "Невірний день. Використовуйте: mon, tue, wed, thu, fri, sat, sun — або число (для щомісячного)."
            )
            return
    try:
        hh, mm = map(int, time_str.split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
    except Exception:
        await update.message.reply_text("Невірний формат часу. Приклад: 10:00")
        return

    conn = db()
    row = conn.execute(
        "SELECT kind FROM schedules WHERE segment=? AND weekday=? AND hhmm=?",
        (name, weekday_val, time_str),
    ).fetchone()
    kind = row["kind"] if row else "weekly"
    conn.execute(
        "DELETE FROM schedules WHERE segment=? AND weekday=? AND hhmm=?",
        (name, weekday_val, time_str),
    )
    conn.commit()
    conn.close()

    job_name = _job_name(name, kind, weekday_val, hh, mm)
    for job in context.application.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    await update.message.reply_text(f"Видалено запис розкладу для «{name}» ({kind}, {day_str} {time_str}).")


async def setmsg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Використання: /setmsg сегмент текст_повідомлення\n"
            "Приклад: /setmsg wholesale Нагадуємо про щотижневе поповнення асортименту!"
        )
        return
    name = context.args[0]
    text = " ".join(context.args[1:])
    conn = db()
    conn.execute("INSERT OR IGNORE INTO segments (name) VALUES (?)", (name,))
    conn.execute("UPDATE segments SET message=? WHERE name=?", (text, name))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Повідомлення для сегмента «{name}» оновлено. Воно надсилатиметься автоматично за розкладом.")


def _client_display_label(chat_id: int, fallback_name: str, fallback_username: str) -> str:
    """Показує назву точки з картки клієнта, якщо вона вже заповнена;
    інакше — звичайне ім'я/юзернейм Telegram."""
    conn = db()
    row = conn.execute("SELECT point_name FROM client_profiles WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    if row and row["point_name"]:
        return row["point_name"]
    return f"{fallback_name} (@{fallback_username or '—'})"


def _clients_text(admin_id: int | None = None) -> str:
    conn = db()
    if admin_id:
        rows = conn.execute(
            "SELECT * FROM subscribers WHERE active=1 AND responsible_admin=? ORDER BY joined_at DESC",
            (admin_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM subscribers WHERE active=1 ORDER BY joined_at DESC").fetchall()
    conn.close()
    if not rows:
        return "У вас поки немає закріплених клієнтів." if admin_id else "Поки немає активних підписників."
    label = "Ваших активних клієнтів" if admin_id else "Активних підписників"
    text = f"{label}: {len(rows)}\n\n"
    for r in rows[:50]:
        display = _client_display_label(r["chat_id"], r["name"], r["username"])
        text += f"• {display} — сегмент: {r['segment'] or 'немає'}\n"
    if len(rows) > 50:
        text += f"\n...і ще {len(rows) - 50}"
    return text


async def clients_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(_clients_text(admin_id=update.effective_chat.id))


def _clients_with_admin_text(max_len: int = 3500) -> str:
    conn = db()
    rows = conn.execute(
        "SELECT s.chat_id, s.name, s.username, s.segment, a.name as admin_name "
        "FROM subscribers s LEFT JOIN admins a ON s.responsible_admin = a.chat_id "
        "WHERE s.active=1 ORDER BY s.joined_at DESC"
    ).fetchall()
    conn.close()
    if not rows:
        return "Поки немає активних підписників."
    header = f"Клієнти та їхні відповідальні ({len(rows)}):\n\n"
    footer = (
        "\n\nШвидко призначити відповідального відразу кільком клієнтам:\n"
        "/bulkresp @юзернейм_адміна id1,id2,id3\n"
        "або одразу всій групі:\n"
        "/bulkresp @юзернейм_адміна segment:назва_групи"
    )
    body = ""
    shown = 0
    for r in rows:
        admin_label = r["admin_name"] if r["admin_name"] else "❓ не призначено"
        display = _client_display_label(r["chat_id"], r["name"], r["username"])
        line = (
            f"• {display} — chat_id: {r['chat_id']}\n"
            f"   Група: {r['segment'] or 'немає'} | Відповідальний: {admin_label}\n"
        )
        if len(header) + len(body) + len(line) + len(footer) > max_len:
            break
        body += line
        shown += 1
    if shown < len(rows):
        body += f"\n...і ще {len(rows) - shown} (список задовгий для одного повідомлення — скасуйте фільтр чи зверніться до /clients для скороченого перегляду)."
    return header + body + footer


async def resplist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    try:
        await update.message.reply_text(_clients_with_admin_text())
    except Exception as e:
        logger.warning(f"Не вдалось показати список клієнтів: {e}")
        await update.message.reply_text("Не вдалось завантажити список (тимчасова помилка). Спробуйте ще раз.")


async def bulkresp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Швидко призначає одного адміна відповідальним одразу для кількох клієнтів або цілої групи.
    Використання: /bulkresp @юзернейм id1,id2,id3
                  /bulkresp @юзернейм segment:назва_групи"""
    if not is_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Використання:\n"
            "/bulkresp @юзернейм_адміна id1,id2,id3 — призначити конкретних клієнтів за chat_id\n"
            "/bulkresp @юзернейм_адміна segment:назва_групи — призначити всіх клієнтів групи одразу\n\n"
            "Chat_id клієнтів можна побачити в «📋 Список клієнтів і відповідальних»."
        )
        return

    admin_username = context.args[0].lstrip("@").lower()
    conn = db()
    admin_row = conn.execute("SELECT chat_id, name FROM admins WHERE LOWER(username)=?", (admin_username,)).fetchone()
    if not admin_row:
        conn.close()
        await update.message.reply_text(
            f"Адміна з юзернеймом @{admin_username} не знайдено. Він має хоч раз написати боту будь-яку команду."
        )
        return
    target_admin_id = admin_row["chat_id"]

    selector = context.args[1]
    if selector.startswith("segment:"):
        seg = selector[len("segment:"):]
        cur = conn.execute(
            "UPDATE subscribers SET responsible_admin=? WHERE segment=? AND active=1", (target_admin_id, seg)
        )
        conn.commit()
        count = cur.rowcount
        conn.close()
        await update.message.reply_text(
            f"✅ Призначено {admin_row['name']} відповідальним для {count} клієнтів групи «{seg}»."
        )
        return

    try:
        ids = [int(x.strip()) for x in selector.split(",") if x.strip()]
    except ValueError:
        conn.close()
        await update.message.reply_text("Не вдалось розпізнати chat_id. Приклад: /bulkresp @admin 111,222,333")
        return

    if not ids:
        conn.close()
        await update.message.reply_text("Не вказано жодного chat_id.")
        return

    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(
        f"UPDATE subscribers SET responsible_admin=? WHERE chat_id IN ({placeholders}) AND active=1",
        (target_admin_id, *ids),
    )
    conn.commit()
    count = cur.rowcount
    conn.close()
    await update.message.reply_text(f"✅ Призначено {admin_row['name']} відповідальним для {count} клієнтів.")


# ---------- Миттєва розсилка всім ----------

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Використання: /broadcast текст повідомлення для ВСІХ клієнтів")
        return
    text = " ".join(context.args)
    conn = db()
    rows = conn.execute("SELECT chat_id FROM subscribers WHERE active=1").fetchall()
    conn.close()

    sent, failed = 0, 0
    for r in rows:
        try:
            await context.bot.send_message(r["chat_id"], text, reply_markup=_keyboard_for_recipient(r["chat_id"]))
            sent += 1
        except Exception as e:
            logger.warning(f"Не вдалось надіслати {r['chat_id']}: {e}")
            failed += 1
    await update.message.reply_text(f"Розсилка завершена. Надіслано: {sent}, помилок: {failed}")


def _resolve_target_admin(client_chat_id: int) -> int | None:
    conn = db()
    row = conn.execute("SELECT responsible_admin FROM subscribers WHERE chat_id=?", (client_chat_id,)).fetchone()
    conn.close()
    resp = row["responsible_admin"] if row else None
    return resp or (ADMIN_IDS[0] if ADMIN_IDS else None)


async def forward_client_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пересилає текстове повідомлення клієнта його відповідальному адміну (або першому адміну)."""
    client = update.effective_user
    chat = update.effective_chat
    target_admin = _resolve_target_admin(chat.id)
    if not target_admin:
        return
    text = update.message.text or ""
    _log_message(chat.id, "in", text, update.message.message_id)
    try:
        sent = await context.bot.send_message(
            target_admin,
            f"✉️ Повідомлення від {client.full_name} (@{client.username or '—'}, chat_id: {chat.id}):\n\n{text}\n\n"
            f"Щоб відповісти клієнту — зробіть Reply на це повідомлення.",
        )
        FORWARD_MAP[(target_admin, sent.message_id)] = chat.id
    except Exception as e:
        logger.warning(f"Не вдалось переслати повідомлення клієнта {chat.id}: {e}")


async def forward_client_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пересилає файл від клієнта його відповідальному адміну (або першому адміну)."""
    client = update.effective_user
    chat = update.effective_chat
    document = update.message.document
    caption = update.message.caption or ""
    target_admin = _resolve_target_admin(chat.id)
    if not target_admin or not document:
        return
    try:
        sent = await context.bot.send_document(
            target_admin,
            document.file_id,
            caption=(
                f"✉️ Файл від {client.full_name} (@{client.username or '—'}, chat_id: {chat.id})"
                + (f":\n{caption}" if caption else "") +
                "\n\nЩоб відповісти клієнту — зробіть Reply на це повідомлення."
            ),
        )
        FORWARD_MAP[(target_admin, sent.message_id)] = chat.id
    except Exception as e:
        logger.warning(f"Не вдалось переслати файл клієнта {chat.id}: {e}")


async def handle_admin_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє файл (PDF тощо), надісланий адміном, з урахуванням поточного контексту:
    очікуваних обраних отримувачів (/sendto), розсилки всім через меню, або прямої команди /broadcastfile."""
    admin_id = update.effective_chat.id
    if not is_admin(update):
        await forward_client_document(update, context)
        return
    document = update.message.document
    if not document:
        return
    caption = update.message.caption or ""

    # 1) Якщо очікуємо отримувачів для /sendto (обрані люди)
    if admin_id in SENDTO_AWAITING_TEXT:
        recipients = SENDTO_AWAITING_TEXT.pop(admin_id)
        sent, failed = 0, 0
        for chat_id in recipients:
            try:
                await context.bot.send_document(
                    chat_id, document.file_id, caption=caption or None, reply_markup=_keyboard_for_recipient(chat_id)
                )
                sent += 1
            except Exception as e:
                logger.warning(f"Не вдалось надіслати файл {chat_id}: {e}")
                failed += 1
        await update.message.reply_text(f"Надіслано файл обраним: {sent}, помилок: {failed}.")
        return

    # 2) Якщо очікуємо текст для розсилки ВСІМ через меню — приймаємо файл замість тексту
    pending = MENU_PENDING.get(admin_id)
    if pending and pending.get("action") == "broadcast_text":
        MENU_PENDING.pop(admin_id, None)
        conn = db()
        rows = conn.execute("SELECT chat_id FROM subscribers WHERE active=1").fetchall()
        conn.close()
        sent, failed = 0, 0
        for r in rows:
            try:
                await context.bot.send_document(
                    r["chat_id"], document.file_id, caption=caption or None, reply_markup=_keyboard_for_recipient(r["chat_id"])
                )
                sent += 1
            except Exception as e:
                logger.warning(f"Не вдалось надіслати файл {r['chat_id']}: {e}")
                failed += 1
        await update.message.reply_text(
            f"✅ Розсилка файлу всім завершена. Надіслано: {sent}, помилок: {failed}.",
            reply_markup=_menu_back_keyboard(),
        )
        return

    # 3) Якщо очікуємо текст для запланованої розсилки/питання — приймаємо файл замість тексту
    if pending and pending.get("action") == "bc_text":
        MENU_PENDING.pop(admin_id, None)
        await _finalize_scheduled_broadcast(update, context, admin_id, caption, file_id=document.file_id)
        return

    # 4) Якщо очікуємо текст для негайного питання так/ні — приймаємо файл замість тексту
    if pending and pending.get("action") == "qna_text":
        MENU_PENDING.pop(admin_id, None)
        await qna_send(context, admin_id, caption, file_id=document.file_id)
        return

    # 5) Пряма команда: файл із підписом «/broadcastfile текст» — усім одразу, без меню
    if caption.lower().startswith("/broadcastfile"):
        await broadcast_file(update, context)
        return

    # 6) Файл прийшов без жодного контексту — підказуємо, як його розіслати
    await update.message.reply_text(
        "Щоб розіслати цей файл, спочатку оберіть дію через меню "
        "(«Написати всім/обраним зараз», «Запланувати розсилку» або «Запланувати запитання так/ні»), "
        "а потім надішліть файл ще раз.\n"
        "Або одразу підпишіть файл текстом «/broadcastfile ваш текст», щоб розіслати його всім негайно."
    )


async def broadcast_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Розсилає прикріплений файл (PDF тощо) усім активним підписникам.
    Спрацьовує, коли адмін надсилає боту документ із підписом, що починається на /broadcastfile."""
    if not is_admin(update):
        return
    document = update.message.document
    if not document:
        return

    caption = update.message.caption or ""
    text = caption
    if text.lower().startswith("/broadcastfile"):
        text = text[len("/broadcastfile"):].strip()

    conn = db()
    rows = conn.execute("SELECT chat_id FROM subscribers WHERE active=1").fetchall()
    conn.close()

    sent, failed = 0, 0
    for r in rows:
        try:
            await context.bot.send_document(
                r["chat_id"], document.file_id, caption=text or None, reply_markup=_keyboard_for_recipient(r["chat_id"])
            )
            sent += 1
        except Exception as e:
            logger.warning(f"Не вдалось надіслати файл {r['chat_id']}: {e}")
            failed += 1
    await update.message.reply_text(f"Розсилка файлу завершена. Надіслано: {sent}, помилок: {failed}")


# ---------- Розсилка обраним вручну зі списку (кнопки-перемикачі) ----------

def _build_sendto_keyboard(admin_id: int, clients: list) -> InlineKeyboardMarkup:
    selected = SENDTO_SELECTIONS.get(admin_id, set())
    buttons = []
    for c in clients:
        mark = "✅" if c["chat_id"] in selected else "⬜"
        label = f"{mark} {_client_display_label(c['chat_id'], c['name'], c['username'])}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"sendto_toggle:{c['chat_id']}")])
    buttons.append([InlineKeyboardButton("➕ Додати ще людину (всі клієнти)", callback_data="sendto_showall")])
    buttons.append([
        InlineKeyboardButton("✅ Надіслати обраним", callback_data="sendto_done"),
        InlineKeyboardButton("❌ Скасувати", callback_data="sendto_cancel"),
    ])
    buttons.append([InlineKeyboardButton("⬅️ Назад до меню", callback_data="menu_back")])
    return InlineKeyboardMarkup(buttons)


def _packaging_toggle_keyboard() -> InlineKeyboardMarkup:
    conn = db()
    rows = conn.execute(
        "SELECT chat_id, name, username, needs_packaging FROM subscribers WHERE active=1 "
        "ORDER BY joined_at DESC LIMIT 60"
    ).fetchall()
    conn.close()
    buttons = []
    for r in rows:
        mark = "✅" if r["needs_packaging"] else "⬜"
        label = f"{mark} {_client_display_label(r['chat_id'], r['name'], r['username'])}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"menu_packagingtoggle:{r['chat_id']}")])
    buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
    return InlineKeyboardMarkup(buttons)


def _client_needs_packaging(chat_id: int) -> bool:
    conn = db()
    row = conn.execute("SELECT needs_packaging FROM subscribers WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return bool(row and row["needs_packaging"])


def _get_admin_clients(admin_id: int, segment: str | None = None) -> list:
    """Повертає активних клієнтів, закріплених саме за цим адміном (responsible_admin)."""
    conn = db()
    if segment:
        rows = conn.execute(
            "SELECT chat_id, name, username FROM subscribers "
            "WHERE active=1 AND responsible_admin=? AND segment=? ORDER BY joined_at DESC LIMIT 100",
            (admin_id, segment),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT chat_id, name, username FROM subscribers "
            "WHERE active=1 AND responsible_admin=? ORDER BY joined_at DESC LIMIT 100",
            (admin_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def _sendto_send_picker(context: ContextTypes.DEFAULT_TYPE, admin_id: int):
    clients = _get_admin_clients(admin_id)
    if not clients:
        await context.bot.send_message(
            admin_id,
            "У вас поки немає закріплених клієнтів 🙈 Спочатку призначте собі клієнтів "
            "(меню → «🙋 Призначити відповідального клієнту» або «📋 Список клієнтів і відповідальних»)."
        )
        return

    SENDTO_SELECTIONS[admin_id] = set()
    context.chat_data["sendto_clients"] = clients
    await context.bot.send_message(
        admin_id,
        "Оберіть, кому надіслати повідомлення (натисніть на людину, щоб додати/прибрати позначку):",
        reply_markup=_build_sendto_keyboard(admin_id, clients),
    )


async def sendto_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await _sendto_send_picker(context, update.effective_chat.id)


async def sendto_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    _, chat_id_str = query.data.split(":", 1)
    chat_id = int(chat_id_str)

    selected = SENDTO_SELECTIONS.setdefault(admin_id, set())
    if chat_id in selected:
        selected.discard(chat_id)
    else:
        selected.add(chat_id)

    clients = context.chat_data.get("sendto_clients", [])
    await query.edit_message_reply_markup(reply_markup=_build_sendto_keyboard(admin_id, clients))


async def sendto_showall_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    conn = db()
    rows = conn.execute(
        "SELECT chat_id, name, username FROM subscribers WHERE active=1 ORDER BY joined_at DESC LIMIT 200"
    ).fetchall()
    conn.close()
    context.chat_data["sendto_clients"] = [dict(r) for r in rows]
    await query.edit_message_reply_markup(reply_markup=_build_sendto_keyboard(admin_id, context.chat_data["sendto_clients"]))


async def sendto_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    selected = SENDTO_SELECTIONS.get(admin_id, set())
    if not selected:
        await query.answer("Спочатку оберіть хоча б одну людину.", show_alert=True)
        return

    SENDTO_AWAITING_TEXT[admin_id] = list(selected)
    SENDTO_SELECTIONS.pop(admin_id, None)
    await query.edit_message_text(
        f"Обрано отримувачів: {len(selected)}.\n\n"
        f"Тепер просто напишіть звичайним повідомленням текст, який хочете їм надіслати."
    )


async def sendto_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    SENDTO_SELECTIONS.pop(admin_id, None)
    SENDTO_AWAITING_TEXT.pop(admin_id, None)
    await query.edit_message_text("Скасовано.")


async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # Пріоритет: якщо цей chat_id саме зараз щось заповнює як клієнт (картка/замовлення) —
    # обробляємо це незалежно від того, чи цей самий chat_id є ще й адміном
    # (адмін міг тестувати ці кроки на своєму ж акаунті).
    if chat_id in PROFILE_PENDING:
        await handle_profile_text_step(update, context)
        return
    if chat_id in ORDER_PENDING:
        await handle_order_text_step(update, context)
        return

    admin_id = chat_id
    if not is_admin(update):
        await forward_client_text(update, context)
        return

    if admin_id in ADMIN_ADD_ADDRESS_PENDING:
        await handle_admin_add_address_text(update, context)
        return

    if admin_id in ADMIN_EDIT_PROFILE_PENDING:
        await handle_admin_edit_profile_text_step(update, context)
        return

    if admin_id in ADMIN_NEW_PROFILE_PENDING:
        await handle_admin_new_profile_text_step(update, context)
        return

    if admin_id in ADMIN_MSGEDIT_PENDING:
        await handle_admin_msgedit_text(update, context)
        return

    # 0) Якщо адмін відповідає (Reply) на переслане повідомлення клієнта — надсилаємо відповідь клієнту
    if update.message.reply_to_message:
        key = (admin_id, update.message.reply_to_message.message_id)
        if key in FORWARD_MAP:
            client_chat_id = FORWARD_MAP.pop(key)
            try:
                sent = await context.bot.send_message(client_chat_id, update.message.text)
                _log_message(client_chat_id, "out", update.message.text, sent.message_id)
                await update.message.reply_text("✅ Надіслано клієнту.")
            except Exception as e:
                await update.message.reply_text(f"Не вдалось надіслати клієнту: {e}")
            return

    # 1) Якщо очікуємо текст для розсилки обраним (/sendto)
    if admin_id in SENDTO_AWAITING_TEXT:
        recipients = SENDTO_AWAITING_TEXT.pop(admin_id)
        text = update.message.text
        sent, failed = 0, 0
        for chat_id in recipients:
            try:
                await context.bot.send_message(chat_id, text, reply_markup=_keyboard_for_recipient(chat_id))
                sent += 1
            except Exception as e:
                logger.warning(f"Не вдалось надіслати {chat_id}: {e}")
                failed += 1
        await update.message.reply_text(f"Надіслано обраним: {sent}, помилок: {failed}.")
        return

    # 2) Якщо очікуємо текст для одного з кроків меню
    pending = MENU_PENDING.get(admin_id)
    if not pending:
        return
    action = pending["action"]
    text = update.message.text

    if action == "broadcast_text":
        MENU_PENDING.pop(admin_id, None)
        conn = db()
        rows = conn.execute("SELECT chat_id FROM subscribers WHERE active=1").fetchall()
        conn.close()
        sent, failed = 0, 0
        for r in rows:
            try:
                await context.bot.send_message(r["chat_id"], text, reply_markup=_keyboard_for_recipient(r["chat_id"]))
                sent += 1
            except Exception as e:
                logger.warning(f"Не вдалось надіслати {r['chat_id']}: {e}")
                failed += 1
        await update.message.reply_text(
            f"✅ Розсилка всім завершена. Надіслано: {sent}, помилок: {failed}.",
            reply_markup=_menu_back_keyboard(),
        )
        return

    if action == "setmsg_text":
        MENU_PENDING.pop(admin_id, None)
        segment = pending["segment"]
        conn = db()
        conn.execute("INSERT OR IGNORE INTO segments (name) VALUES (?)", (segment,))
        conn.execute("UPDATE segments SET message=? WHERE name=?", (text, segment))
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ Повідомлення для групи «{segment}» оновлено.",
            reply_markup=_menu_back_keyboard(),
        )
        return

    if action == "addsegment_text":
        MENU_PENDING.pop(admin_id, None)
        name = text.strip().split()[0] if text.strip() else ""
        if not name:
            await update.message.reply_text("Назва не може бути порожньою.", reply_markup=_menu_back_keyboard())
            return
        conn = db()
        conn.execute("INSERT OR IGNORE INTO segments (name) VALUES (?)", (name,))
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ Групу «{name}» створено.", reply_markup=_menu_back_keyboard()
        )
        return

    if action == "assortment_text":
        cat_key = pending.get("category", "")
        label = dict(ASSORTMENT_CATEGORIES).get(cat_key, cat_key)
        MENU_PENDING.pop(admin_id, None)
        _set_setting(f"assortment_{cat_key}", text)
        await update.message.reply_text(
            f"✅ Категорію «{label}» оновлено! Клієнти одразу побачать нову версію.",
            reply_markup=_menu_back_keyboard(),
        )
        return

    if action == "delivery_text":
        cat_key = pending.get("category", "retail")
        label = dict(DELIVERY_CATEGORIES).get(cat_key, cat_key)
        MENU_PENDING.pop(admin_id, None)
        _set_setting(f"delivery_{cat_key}", text)
        await update.message.reply_text(
            f"✅ Умови доставки для категорії «{label}» оновлено! Клієнти одразу побачать нову версію.",
            reply_markup=_menu_back_keyboard(),
        )
        return

    if action == "bc_name":
        MENU_PENDING.pop(admin_id, None)
        pending = BC_PENDING.setdefault(admin_id, {})
        pending["name"] = text.strip()
        MENU_PENDING[admin_id] = {"action": "bc_text"}
        if pending.get("is_question"):
            await update.message.reply_text(
                "Напишіть текст запитання (наприклад: «Завтра доставка, потрібна?») — або надішліть файл із підписом:"
            )
        else:
            await update.message.reply_text("Напишіть текст повідомлення для цієї розсилки — або надішліть файл із підписом:")
        return

    if action == "qna_text":
        MENU_PENDING.pop(admin_id, None)
        await qna_send(context, admin_id, text)
        return

    if action == "bc_text":
        MENU_PENDING.pop(admin_id, None)
        await _finalize_scheduled_broadcast(update, context, admin_id, text)
        return


# ---------- Меню з кнопками українською (альтернатива текстовим командам) ----------

def _menu_root_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Зараз (написати/запитати)", callback_data="menu_now")],
        [InlineKeyboardButton("📅 Заплановано (на потім)", callback_data="menu_scheduled")],
        [InlineKeyboardButton("👥 Клієнти", callback_data="menu_clients")],
        [InlineKeyboardButton("⚙️ Налаштування", callback_data="menu_settings")],
        [InlineKeyboardButton("⏳ Незавершені замовлення", callback_data="menu_pendingorders")],
    ])


def _menu_now_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 Всім зараз", callback_data="menu_broadcast"),
            InlineKeyboardButton("🎯 Обраним зараз", callback_data="menu_sendto"),
        ],
        [InlineKeyboardButton("✉️ Групі зараз (текст/PDF)", callback_data="wg_start")],
        [InlineKeyboardButton("❓ Запитати так/ні", callback_data="qna_start")],
        [InlineKeyboardButton("🔙 До меню", callback_data="menu_back")],
    ])


def _menu_scheduled_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Розсилка", callback_data="bc_start"),
            InlineKeyboardButton("📅❓ Запитання", callback_data="bcq_start"),
        ],
        [
            InlineKeyboardButton("📖 Заплановані", callback_data="bc_viewlist"),
            InlineKeyboardButton("🗑 Скасувати", callback_data="bc_cancellist"),
        ],
        [InlineKeyboardButton("🔙 До меню", callback_data="menu_back")],
    ])


def _menu_clients_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 Змінити групу", callback_data="menu_changeseg"),
            InlineKeyboardButton("🙋 Відповідальний", callback_data="menu_setresp"),
        ],
        [InlineKeyboardButton("🪪 Редагувати картку клієнта", callback_data="menu_editclientprofile")],
        [InlineKeyboardButton("📦 Кому показувати вибір пакування", callback_data="menu_packaging")],
        [InlineKeyboardButton("📋 Список клієнтів і відповідальних", callback_data="menu_resplist")],
        [InlineKeyboardButton("👤 Переглянути клієнтів", callback_data="menu_viewclients")],
        [InlineKeyboardButton("🔙 До меню", callback_data="menu_back")],
    ])


def _menu_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Створити нову групу", callback_data="menu_addsegment")],
        [InlineKeyboardButton("☕ Асортимент", callback_data="menu_editassortment"),
         InlineKeyboardButton("🚚 Доставка", callback_data="menu_editdelivery")],
        [InlineKeyboardButton("✏️ Повідомлення групи", callback_data="menu_setmsg")],
        [InlineKeyboardButton("📁 Переглянути групи", callback_data="menu_viewsegments")],
        [InlineKeyboardButton("🔙 До меню", callback_data="menu_back")],
    ])


def _menu_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 До меню", callback_data="menu_back")]])


def _get_setting(key: str, default: str = "") -> str:
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row and row["value"] is not None else default


def _set_setting(key: str, value: str) -> None:
    conn = db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


CLIENT_BTN_ASSORTMENT = "☕ Асортимент"
CLIENT_BTN_MANAGER = "💬 Зв'язок з менеджером"
CLIENT_BTN_DELIVERY = "🚚 Умови доставки"
CLIENT_BTN_ORDER = "📝 Замовити"
CLIENT_BTN_PROFILE = "🪪 Моя картка"

DELIVERY_COMMON_TAIL = (
    "\n\nЗамовлення прийняті до 09:00 - можуть бути доставлені КУР'ЄРОМ в той самий день.\n\n"
    "Замовлення новою поштою прийняті до 11:00 - відправляються у той самий день.\n\n"
    "Доставка на наступний після замовлення день з понеділка по п'ятницю, з 10 до 16.\n\n"
    "Доставка замовлень відбувається за умови повної передплати на рахунок, або по факту "
    "отримання кави.\n\n"
    "Кава обсмажується на професійному ростері COGEN C15"
)

DEFAULT_DELIVERY_RETAIL_TEXT = (
    "Умови доставки (роздріб):\n\n"
    "Доставка роздрібних замовлень від 2000грн безкоштовно, менше за тарифами перевізника "
    "або 190 грн по Києву." + DELIVERY_COMMON_TAIL
)

DEFAULT_DELIVERY_WHOLESALE_TEXT = (
    "Умови доставки (гурт):\n\n"
    "Доставка гуртових замовлень від 10 кг будь-якої кави - безкоштовно. "
    "До 10-ти кг - 190 грн. по Києву, або за тарифами нової пошти.\n\n"
    "Ви можете обрати по одному кілограму різних сортів, та отримати знижку в залежності "
    "від загального об'єму замовлення." + DELIVERY_COMMON_TAIL
)

DELIVERY_CATEGORIES = [
    ("retail", "🛍 Роздріб"),
    ("wholesale", "📦 Гурт"),
]


def _delivery_category_keyboard(prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"{prefix}:{key}")]
        for key, label in DELIVERY_CATEGORIES
    ]
    if prefix == "menu_deliverycat":
        buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
    return InlineKeyboardMarkup(buttons)


def _default_delivery_text(cat_key: str) -> str:
    return DEFAULT_DELIVERY_RETAIL_TEXT if cat_key == "retail" else DEFAULT_DELIVERY_WHOLESALE_TEXT


def _client_persistent_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(CLIENT_BTN_ASSORTMENT), KeyboardButton(CLIENT_BTN_DELIVERY)],
            [KeyboardButton(CLIENT_BTN_ORDER), KeyboardButton(CLIENT_BTN_PROFILE)],
            [KeyboardButton(CLIENT_BTN_MANAGER)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _keyboard_for_recipient(chat_id: int) -> ReplyKeyboardMarkup:
    """Якщо отримувач — адмін (навіть якщо він же й підписаний як клієнт),
    показуємо йому кнопку «📋 Меню», а не клієнтську клавіатуру."""
    if chat_id in ADMIN_IDS:
        return _persistent_menu_keyboard()
    return _client_persistent_keyboard()


ASSORTMENT_CATEGORIES = [
    ("arabika", "🌱 Арабіка"),
    ("blend", "🎨 Бленд"),
    ("palay", "✨ Палай"),
    ("drip", "💧 Дріп"),
    ("suputni", "🧰 Супутні товари"),
    ("school", "🎓 Школа бариста"),
]

ORDER_CATEGORIES = [c for c in ASSORTMENT_CATEGORIES if c[0] != "school"]


def _assortment_category_keyboard(prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"{prefix}:{key}")]
        for key, label in ASSORTMENT_CATEGORIES
    ]
    if prefix == "menu_assortcat":
        buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
    return InlineKeyboardMarkup(buttons)


async def client_show_assortment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "☕ Оберіть категорію:", reply_markup=_assortment_category_keyboard("clientassort")
    )


async def client_assortment_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, cat_key = query.data.split(":", 1)
    label = dict(ASSORTMENT_CATEGORIES).get(cat_key, cat_key)
    text = _get_setting(f"assortment_{cat_key}", "").strip()
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="clientassort_back")]])
    if not text:
        await query.edit_message_text(
            f"{label}\n\nНаразі ще не додано 🙈 Скоро оновимо — зазирніть трохи пізніше!",
            reply_markup=back_kb,
        )
        return
    await query.edit_message_text(f"{label}\n\n{text}", reply_markup=back_kb)


async def client_assortment_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "☕ Оберіть категорію:", reply_markup=_assortment_category_keyboard("clientassort")
    )


async def client_show_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚚 Які умови доставки вас цікавлять?",
        reply_markup=_delivery_category_keyboard("clientdelivery"),
    )


async def client_delivery_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, cat_key = query.data.split(":", 1)
    label = dict(DELIVERY_CATEGORIES).get(cat_key, cat_key)
    text = _get_setting(f"delivery_{cat_key}", _default_delivery_text(cat_key)).strip()
    await query.edit_message_text(
        f"🚚 {text}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="clientdelivery_back")]]),
    )


async def client_delivery_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🚚 Які умови доставки вас цікавлять?",
        reply_markup=_delivery_category_keyboard("clientdelivery"),
    )


async def client_contact_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = db()
    row = conn.execute("SELECT responsible_admin FROM subscribers WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    resp_admin = (row["responsible_admin"] if row else None) or (ADMIN_IDS[0] if ADMIN_IDS else None)
    button = _admin_link_button(resp_admin, "😊 Написати менеджеру") if resp_admin else None
    if button:
        await update.message.reply_text(
            "Раді допомогти! Тисніть кнопку нижче, щоб написати нам напряму — "
            "відповімо якомога швидше 💬",
            reply_markup=InlineKeyboardMarkup([[button]]),
        )
    else:
        await update.message.reply_text(
            "Раді допомогти! Наразі не можемо сформувати пряме посилання — "
            "спробуйте трохи пізніше, або напишіть нам в іншому зручному місці 🙏"
        )


# ---------- Картка клієнта (одноразова, редагована) ----------

PAYMENT_METHODS = ["Готівка", "Безготівковий"]


def _get_client_profile(chat_id: int):
    conn = db()
    row = conn.execute("SELECT * FROM client_profiles WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


DELIVERY_ZONE_OPTIONS = ["Київ лівий берег", "Київ правий берег", "Самовивіз", "НП (Нова Пошта)"]


def _delivery_zone_keyboard(callback_prefix: str) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(z, callback_data=f"{callback_prefix}:{z}")] for z in DELIVERY_ZONE_OPTIONS]
    return InlineKeyboardMarkup(buttons)


def _profile_summary_text(profile: dict, addresses: list, for_admin: bool = False) -> str:
    addr_lines = "\n".join(
        f"   {i+1}. {a['address']} — 📞 {a.get('phone') or '—'}" for i, a in enumerate(addresses)
    ) if addresses else "   (адрес ще немає)"
    title = "🪪 Картка клієнта:" if for_admin else "🪪 Ваша картка:"
    return (
        f"{title}\n\n"
        f"Зона доставки: {profile.get('delivery_zone') or '—'}\n"
        f"Ім'я: {profile.get('full_name') or '—'}\n"
        f"Назва точки: {profile.get('point_name') or '—'}\n"
        f"Контактний номер: {profile.get('phone') or '—'}\n"
        f"ФОП: {profile.get('fop') or '—'}\n"
        f"ІПН: {profile.get('ipn') or '—'}\n\n"
        f"Адреси:\n{addr_lines}"
    )


def _get_client_addresses(chat_id: int) -> list:
    conn = db()
    rows = conn.execute(
        "SELECT id, address, phone FROM client_addresses WHERE chat_id=? ORDER BY id", (chat_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _save_completed_order(chat_id: int, order: dict) -> None:
    conn = db()
    conn.execute(
        "INSERT INTO orders (chat_id, address, contact_phone, payment_method, items_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, order.get("address"), order.get("contact_phone"), order.get("payment_method"),
         json.dumps(order.get("items", []), ensure_ascii=False), datetime.now(TZ).isoformat()),
    )
    conn.commit()
    conn.close()


def _get_last_order_items(chat_id: int) -> list:
    conn = db()
    row = conn.execute(
        "SELECT items_json FROM orders WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,)
    ).fetchone()
    conn.close()
    if not row or not row["items_json"]:
        return []
    try:
        return json.loads(row["items_json"])
    except (json.JSONDecodeError, TypeError):
        return []


def _is_valid_phone(text: str) -> bool:
    digits = re.sub(r"\D", "", text)
    return 9 <= len(digits) <= 13


def _normalize_phone(text: str) -> str:
    digits = re.sub(r"\D", "", text)
    if digits.startswith("380") and len(digits) == 12:
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10:
        return "+38" + digits
    return "+" + digits if not text.strip().startswith("+") else text.strip()


PROFILE_STEPS = ["full_name", "point_name", "phone", "fop", "ipn"]
PROFILE_STEP_PROMPTS = {
    "full_name": "Напишіть, будь ласка, ваше ім'я:",
    "point_name": "Назва точки (кав'ярні/закладу):",
    "phone": "Контактний номер телефону (наприклад: +380671234567):",
    "fop": "ФОП (назва/номер, або «немає», якщо не застосовується):",
    "ipn": "ІПН (або «немає», якщо не застосовується):",
}
PROFILE_FIELD_LABELS = {
    "delivery_zone": "🗺 Зона доставки",
    "full_name": "Ім'я",
    "point_name": "Назва точки",
    "phone": "Телефон",
    "fop": "ФОП",
    "ipn": "ІПН",
}
PROFILE_EDITABLE_FIELDS = ["delivery_zone", "full_name", "point_name", "phone", "fop", "ipn"]


def _adminprofile_field_picker_keyboard(target_chat_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(PROFILE_FIELD_LABELS[f], callback_data=f"adminprofile_editfield:{target_chat_id}:{f}")]
        for f in PROFILE_EDITABLE_FIELDS
    ]
    buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
    return InlineKeyboardMarkup(buttons)


def _order_payment_keyboard() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(m, callback_data=f"order_payment:{m}")] for m in PAYMENT_METHODS]
    buttons.append([InlineKeyboardButton("❌ Скасувати замовлення", callback_data="order_cancel")])
    return InlineKeyboardMarkup(buttons)


def _profile_manage_keyboard(addresses: list) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton("✏️ Редагувати дані", callback_data="profile_edit")]]
    buttons.append([InlineKeyboardButton("➕ Додати адресу", callback_data="profile_addaddr")])
    for a in addresses:
        buttons.append([
            InlineKeyboardButton(f"✏️ {a['address'][:30]}", callback_data=f"profile_editaddr:{a['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"profile_deladdr:{a['id']}"),
        ])
    return InlineKeyboardMarkup(buttons)


async def profile_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    profile = _get_client_profile(chat_id)
    if not profile:
        PROFILE_PENDING[chat_id] = {"awaiting_zone": True, "data": {}}
        await update.message.reply_text(
            "Картка ще не заповнена. Заповнімо її зараз — це знадобиться для замовлень.\n\n"
            "Спочатку — оберіть зону доставки:",
            reply_markup=_delivery_zone_keyboard("profile_zone"),
        )
        return
    addresses = _get_client_addresses(chat_id)
    await update.message.reply_text(
        _profile_summary_text(profile, addresses), reply_markup=_profile_manage_keyboard(addresses)
    )


def _profile_field_picker_keyboard(prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"{prefix}:{key}")]
        for key, label in PROFILE_FIELD_LABELS.items()
    ]
    buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
    return InlineKeyboardMarkup(buttons)


async def profile_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Що саме хочете змінити?", reply_markup=_profile_field_picker_keyboard("profile_editfield")
    )


async def profile_editfield_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    _, field = query.data.split(":", 1)
    if field == "delivery_zone":
        PROFILE_PENDING[chat_id] = {"single_field": "delivery_zone"}
        await query.edit_message_text(
            "Оберіть нову зону доставки:", reply_markup=_delivery_zone_keyboard("profile_zonefield")
        )
        return
    PROFILE_PENDING[chat_id] = {"single_field": field}
    await query.edit_message_text(PROFILE_STEP_PROMPTS[field], reply_markup=_profile_cancel_keyboard())


async def profile_zonefield_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    _, zone = query.data.split(":", 1)
    PROFILE_PENDING.pop(chat_id, None)
    conn = db()
    conn.execute(
        "INSERT INTO client_profiles (chat_id, delivery_zone, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET delivery_zone=excluded.delivery_zone, updated_at=excluded.updated_at",
        (chat_id, zone, datetime.now(TZ).isoformat()),
    )
    conn.commit()
    conn.close()
    profile = _get_client_profile(chat_id) or {}
    addresses = _get_client_addresses(chat_id)
    await query.edit_message_text(
        f"✅ Зону доставки оновлено: {zone}\n\n" + _profile_summary_text(profile, addresses),
        reply_markup=_profile_manage_keyboard(addresses),
    )


async def profile_zone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    _, zone = query.data.split(":", 1)
    pending = PROFILE_PENDING.setdefault(chat_id, {"data": {}})
    pending["data"]["delivery_zone"] = zone
    pending.pop("awaiting_zone", None)
    pending["step_idx"] = 0
    await query.edit_message_text(
        f"Зона доставки: {zone}\n\n" + PROFILE_STEP_PROMPTS[PROFILE_STEPS[0]],
        reply_markup=_profile_cancel_keyboard(),
    )


def _address_phone_keyboard(same_phone: str | None, cancel_callback: str) -> InlineKeyboardMarkup:
    buttons = []
    if same_phone:
        buttons.append([InlineKeyboardButton(f"📞 Такий самий: {same_phone}", callback_data="addraddr_samephone")])
    buttons.append([InlineKeyboardButton("❌ Скасувати", callback_data=cancel_callback)])
    return InlineKeyboardMarkup(buttons)


def _profile_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="profile_cancelstep")]])


def _adminprofile_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="adminprofile_cancelstep")]])


async def addraddr_samephone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершує додавання адреси, підставивши основний контактний номер профілю
    замість повторного ручного введення того самого номера."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    if chat_id in PROFILE_PENDING and PROFILE_PENDING[chat_id].get("adding_address"):
        pending = PROFILE_PENDING[chat_id]
        profile = _get_client_profile(chat_id) or {}
        phone = profile.get("phone")
        if not phone:
            await query.answer("У профілі ще немає основного номера.", show_alert=True)
            return
        address_text = pending["address_data"]["address"]
        editing_id = pending.get("editing_address_id")
        PROFILE_PENDING.pop(chat_id, None)
        conn = db()
        if editing_id:
            conn.execute(
                "UPDATE client_addresses SET address=?, phone=? WHERE id=? AND chat_id=?",
                (address_text, phone, editing_id, chat_id),
            )
        else:
            conn.execute(
                "INSERT INTO client_addresses (chat_id, address, phone, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, address_text, phone, datetime.now(TZ).isoformat()),
            )
        conn.commit()
        conn.close()
        profile2 = _get_client_profile(chat_id) or {}
        addresses = _get_client_addresses(chat_id)
        done_label = "оновлено" if editing_id else "додано"
        await query.edit_message_text(
            f"✅ Адресу {done_label} (номер: {phone})!\n\n" + _profile_summary_text(profile2, addresses),
            reply_markup=_profile_manage_keyboard(addresses),
        )
        return

    if chat_id in ADMIN_ADD_ADDRESS_PENDING:
        pending = ADMIN_ADD_ADDRESS_PENDING[chat_id]
        target_chat_id = pending["target_chat_id"]
        profile = _get_client_profile(target_chat_id) or {}
        phone = profile.get("phone")
        if not phone:
            await query.answer("У картці клієнта ще немає основного номера.", show_alert=True)
            return
        address_text = pending["data"]["address"]
        editing_id = pending.get("editing_address_id")
        ADMIN_ADD_ADDRESS_PENDING.pop(chat_id, None)
        conn = db()
        if editing_id:
            conn.execute(
                "UPDATE client_addresses SET address=?, phone=? WHERE id=? AND chat_id=?",
                (address_text, phone, editing_id, target_chat_id),
            )
        else:
            conn.execute(
                "INSERT INTO client_addresses (chat_id, address, phone, created_at) VALUES (?, ?, ?, ?)",
                (target_chat_id, address_text, phone, datetime.now(TZ).isoformat()),
            )
        conn.commit()
        conn.close()
        profile2 = _get_client_profile(target_chat_id)
        addresses = _get_client_addresses(target_chat_id)
        done_label = "оновлено" if editing_id else "додано клієнту"
        await query.edit_message_text(
            f"✅ Адресу {done_label} (номер: {phone})!\n\n"
            + _profile_summary_text(profile2, addresses, for_admin=True),
            reply_markup=_adminprofile_manage_keyboard(target_chat_id, addresses),
        )
        return

    if chat_id in ADMIN_NEW_PROFILE_PENDING:
        pending = ADMIN_NEW_PROFILE_PENDING[chat_id]
        target_chat_id = pending["target_chat_id"]
        profile = _get_client_profile(target_chat_id) or {}
        phone = profile.get("phone")
        if not phone:
            await query.answer("У картці клієнта ще немає основного номера.", show_alert=True)
            return
        address_text = pending["address_data"]["address"]
        ADMIN_NEW_PROFILE_PENDING.pop(chat_id, None)
        conn = db()
        conn.execute(
            "INSERT INTO client_addresses (chat_id, address, phone, created_at) VALUES (?, ?, ?, ?)",
            (target_chat_id, address_text, phone, datetime.now(TZ).isoformat()),
        )
        conn.commit()
        conn.close()
        profile2 = _get_client_profile(target_chat_id)
        addresses = _get_client_addresses(target_chat_id)
        await query.edit_message_text(
            f"✅ Картку клієнта заповнено (номер адреси: {phone})!\n\n"
            + _profile_summary_text(profile2, addresses, for_admin=True),
            reply_markup=_adminprofile_manage_keyboard(target_chat_id, addresses),
        )
        return

    await query.edit_message_text("Це вже неактуально.")


async def profile_cancelstep_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    PROFILE_PENDING.pop(chat_id, None)
    profile = _get_client_profile(chat_id)
    addresses = _get_client_addresses(chat_id)
    if profile:
        await query.edit_message_text(
            "Скасовано.\n\n" + _profile_summary_text(profile, addresses),
            reply_markup=_profile_manage_keyboard(addresses),
        )
    else:
        await query.edit_message_text("Скасовано. Натисніть «🪪 Моя картка», щоб почати заново.")


async def adminprofile_cancelstep_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    ADMIN_EDIT_PROFILE_PENDING.pop(admin_id, None)
    ADMIN_ADD_ADDRESS_PENDING.pop(admin_id, None)
    ADMIN_NEW_PROFILE_PENDING.pop(admin_id, None)
    ADMIN_MSGEDIT_PENDING.pop(admin_id, None)
    await query.edit_message_text("Скасовано.", reply_markup=_menu_back_keyboard())


async def profile_addaddr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    PROFILE_PENDING[chat_id] = {"adding_address": True, "address_step": "text", "address_data": {}}
    await query.edit_message_text("Напишіть нову адресу:", reply_markup=_profile_cancel_keyboard())


async def profile_editaddr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    _, addr_id = query.data.split(":", 1)
    PROFILE_PENDING[chat_id] = {
        "adding_address": True, "editing_address_id": int(addr_id),
        "address_step": "text", "address_data": {},
    }
    await query.edit_message_text("Напишіть нову адресу (замінить стару):", reply_markup=_profile_cancel_keyboard())


async def profile_deladdr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    _, addr_id = query.data.split(":", 1)
    conn = db()
    conn.execute("DELETE FROM client_addresses WHERE id=? AND chat_id=?", (int(addr_id), chat_id))
    conn.commit()
    conn.close()
    profile = _get_client_profile(chat_id) or {}
    addresses = _get_client_addresses(chat_id)
    await query.edit_message_text(
        "✅ Адресу видалено.\n\n" + _profile_summary_text(profile, addresses),
        reply_markup=_profile_manage_keyboard(addresses),
    )


async def adminprofile_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Адмін обирає, яке саме поле картки клієнта редагувати."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    _, target_chat_id_str = query.data.split(":", 1)
    target_chat_id = int(target_chat_id_str)
    await query.edit_message_text(
        f"Що саме хочете змінити в картці клієнта (chat_id: {target_chat_id})?",
        reply_markup=_adminprofile_field_picker_keyboard(target_chat_id),
    )


async def adminprofile_editfield_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    _, target_chat_id_str, field = query.data.split(":", 2)
    target_chat_id = int(target_chat_id_str)
    if field == "delivery_zone":
        ADMIN_EDIT_PROFILE_PENDING[admin_id] = {"target_chat_id": target_chat_id, "single_field": "delivery_zone"}
        await query.edit_message_text(
            "Оберіть нову зону доставки:",
            reply_markup=_delivery_zone_keyboard(f"adminprofile_zonefield:{target_chat_id}"),
        )
        return
    ADMIN_EDIT_PROFILE_PENDING[admin_id] = {"target_chat_id": target_chat_id, "single_field": field}
    await query.edit_message_text(PROFILE_STEP_PROMPTS[field], reply_markup=_adminprofile_cancel_keyboard())


async def adminprofile_zonefield_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    _, target_chat_id_str, zone = query.data.split(":", 2)
    target_chat_id = int(target_chat_id_str)
    ADMIN_EDIT_PROFILE_PENDING.pop(admin_id, None)
    conn = db()
    conn.execute(
        "INSERT INTO client_profiles (chat_id, delivery_zone, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET delivery_zone=excluded.delivery_zone, updated_at=excluded.updated_at",
        (target_chat_id, zone, datetime.now(TZ).isoformat()),
    )
    conn.commit()
    conn.close()
    profile = _get_client_profile(target_chat_id) or {}
    addresses = _get_client_addresses(target_chat_id)
    await query.edit_message_text(
        f"✅ Зону доставки оновлено: {zone}\n\n" + _profile_summary_text(profile, addresses, for_admin=True),
        reply_markup=_adminprofile_manage_keyboard(target_chat_id, addresses),
    )


async def adminprofile_addaddr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Адмін додає нову адресу конкретному клієнту."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    _, target_chat_id_str = query.data.split(":", 1)
    target_chat_id = int(target_chat_id_str)
    ADMIN_ADD_ADDRESS_PENDING[admin_id] = {"target_chat_id": target_chat_id, "step": "text", "data": {}}
    await query.edit_message_text(
        f"Напишіть нову адресу для цього клієнта (chat_id: {target_chat_id}):",
        reply_markup=_adminprofile_cancel_keyboard(),
    )


async def adminprofile_editaddr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Адмін редагує наявну адресу клієнта."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    _, target_chat_id_str, addr_id = query.data.split(":", 2)
    target_chat_id = int(target_chat_id_str)
    ADMIN_ADD_ADDRESS_PENDING[admin_id] = {
        "target_chat_id": target_chat_id, "editing_address_id": int(addr_id), "step": "text", "data": {},
    }
    await query.edit_message_text(
        "Напишіть нову адресу (замінить стару):", reply_markup=_adminprofile_cancel_keyboard()
    )


def _adminprofile_manage_keyboard(target_chat_id: int, addresses: list) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("🧾 Заповнити всі дані одразу", callback_data=f"adminprofile_newwizard:{target_chat_id}")],
        [InlineKeyboardButton("✏️ Редагувати дані", callback_data=f"adminprofile_edit:{target_chat_id}")],
        [InlineKeyboardButton("➕ Додати адресу", callback_data=f"adminprofile_addaddr:{target_chat_id}")],
        [InlineKeyboardButton("💬 Історія повідомлень", callback_data=f"adminmsglog:{target_chat_id}")],
    ]
    for a in addresses:
        buttons.append([
            InlineKeyboardButton(f"✏️ {a['address'][:25]}", callback_data=f"adminprofile_editaddr:{target_chat_id}:{a['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"adminprofile_deladdr:{target_chat_id}:{a['id']}"),
        ])
    buttons.append([InlineKeyboardButton("✅ Готово / До меню", callback_data="menu_back")])
    return InlineKeyboardMarkup(buttons)


async def adminprofile_deladdr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Адмін видаляє одну з адрес клієнта."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    _, target_chat_id_str, addr_id = query.data.split(":", 2)
    target_chat_id = int(target_chat_id_str)
    conn = db()
    conn.execute("DELETE FROM client_addresses WHERE id=? AND chat_id=?", (int(addr_id), target_chat_id))
    conn.commit()
    conn.close()
    profile = _get_client_profile(target_chat_id)
    addresses = _get_client_addresses(target_chat_id)
    text = "✅ Адресу видалено.\n\n" + (_profile_summary_text(profile, addresses, for_admin=True) if profile else "(картка ще не заповнена)")
    await query.edit_message_text(text, reply_markup=_adminprofile_manage_keyboard(target_chat_id, addresses))


async def adminprofile_newwizard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Адмін погодився одразу заповнити картку нового клієнта — починаємо покроковий майстер."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    _, target_chat_id_str = query.data.split(":", 1)
    target_chat_id = int(target_chat_id_str)
    ADMIN_NEW_PROFILE_PENDING[admin_id] = {"target_chat_id": target_chat_id, "step_idx": 0, "data": {}}
    await query.edit_message_text(
        "Заповнимо картку клієнта. Спочатку — оберіть зону доставки:",
        reply_markup=_delivery_zone_keyboard("adminprofile_newzone"),
    )


async def adminprofile_newzone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    pending = ADMIN_NEW_PROFILE_PENDING.get(admin_id)
    if not pending:
        await query.edit_message_text("Цю картку вже неактуально заповнювати.")
        return
    _, zone = query.data.split(":", 1)
    pending["data"]["delivery_zone"] = zone
    await query.edit_message_text(
        f"Зона доставки: {zone}\n\n" + PROFILE_STEP_PROMPTS[PROFILE_STEPS[0]],
        reply_markup=_adminprofile_cancel_keyboard(),
    )


async def adminprofile_newskip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    await query.edit_message_text("Гаразд, картку можна буде заповнити пізніше через меню «👥 Клієнти».")


async def handle_admin_new_profile_text_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_chat.id
    pending = ADMIN_NEW_PROFILE_PENDING.get(admin_id)
    if not pending:
        return
    target_chat_id = pending["target_chat_id"]
    text = update.message.text or ""

    if pending.get("awaiting_address"):
        if pending.get("address_step") == "text":
            pending["address_data"] = {"address": text}
            pending["address_step"] = "phone"
            profile = _get_client_profile(target_chat_id) or {}
            await update.message.reply_text(
                "Тепер напишіть номер телефону для зв'язку по цій адресі:",
                reply_markup=_address_phone_keyboard(profile.get("phone"), "adminprofile_cancelstep"),
            )
            return
        if not _is_valid_phone(text):
            await update.message.reply_text(
                "Це не схоже на номер телефону 🤔 Спробуйте ще раз, наприклад: +380671234567"
            )
            return
        phone = _normalize_phone(text)
        address_text = pending["address_data"]["address"]
        ADMIN_NEW_PROFILE_PENDING.pop(admin_id, None)
        conn = db()
        conn.execute(
            "INSERT INTO client_addresses (chat_id, address, phone, created_at) VALUES (?, ?, ?, ?)",
            (target_chat_id, address_text, phone, datetime.now(TZ).isoformat()),
        )
        conn.commit()
        conn.close()
        profile = _get_client_profile(target_chat_id)
        addresses = _get_client_addresses(target_chat_id)
        await update.message.reply_text(
            "✅ Картку клієнта заповнено!\n\n" + _profile_summary_text(profile, addresses, for_admin=True),
            reply_markup=_adminprofile_manage_keyboard(target_chat_id, addresses),
        )
        return

    step_idx = pending["step_idx"]
    field = PROFILE_STEPS[step_idx]

    if field == "phone":
        if not _is_valid_phone(text):
            await update.message.reply_text(
                "Це не схоже на номер телефону 🤔 Спробуйте ще раз, наприклад: +380671234567"
            )
            return
        text = _normalize_phone(text)

    pending["data"][field] = text

    if step_idx + 1 < len(PROFILE_STEPS):
        pending["step_idx"] += 1
        await update.message.reply_text(
            PROFILE_STEP_PROMPTS[PROFILE_STEPS[pending["step_idx"]]],
            reply_markup=_adminprofile_cancel_keyboard(),
        )
        return

    data = pending["data"]
    conn = db()
    conn.execute(
        "INSERT INTO client_profiles (chat_id, full_name, point_name, phone, fop, ipn, delivery_zone, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET full_name=excluded.full_name, point_name=excluded.point_name, "
        "phone=excluded.phone, fop=excluded.fop, ipn=excluded.ipn, delivery_zone=excluded.delivery_zone, "
        "updated_at=excluded.updated_at",
        (target_chat_id, data["full_name"], data["point_name"], data["phone"], data["fop"],
         data["ipn"], data.get("delivery_zone"), datetime.now(TZ).isoformat()),
    )
    conn.commit()
    conn.close()
    existing_addresses = _get_client_addresses(target_chat_id)
    if existing_addresses:
        ADMIN_NEW_PROFILE_PENDING.pop(admin_id, None)
        profile = _get_client_profile(target_chat_id)
        await update.message.reply_text(
            "✅ Картку клієнта оновлено!\n\n" + _profile_summary_text(profile, existing_addresses, for_admin=True),
            reply_markup=_adminprofile_manage_keyboard(target_chat_id, existing_addresses),
        )
        return
    pending["awaiting_address"] = True
    pending["address_step"] = "text"
    await update.message.reply_text(
        "✅ Основні дані збережено! Залишилось додати хоча б одну адресу доставки.\n\nНапишіть адресу:",
        reply_markup=_adminprofile_cancel_keyboard(),
    )


def _message_log_screen(target_chat_id: int):
    logs = _get_message_log(target_chat_id, limit=15)
    if not logs:
        return (
            "Повідомлень із цим клієнтом ще немає (в історії тільки те, що пройшло через переписку з менеджером).",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data=f"menu_pickclientprofile:{target_chat_id}")]]),
        )
    lines = ["💬 Останні повідомлення з клієнтом:\n"]
    buttons = []
    for log in logs:
        icon = "📩" if log["direction"] == "in" else "📤"
        preview = (log["text"] or "")[:70]
        lines.append(f"{icon} {preview}")
        if log["direction"] == "out" and log.get("telegram_message_id"):
            buttons.append([
                InlineKeyboardButton(f"✏️ Редагувати #{log['id']}", callback_data=f"adminmsgedit:{target_chat_id}:{log['id']}"),
                InlineKeyboardButton(f"🗑 #{log['id']}", callback_data=f"adminmsgdel:{target_chat_id}:{log['id']}"),
            ])
    lines.append("\n(📩 — від клієнта, редагувати/видаляти не можна; 📤 — ваші повідомлення)")
    buttons.append([InlineKeyboardButton("🔙 Назад", callback_data=f"menu_pickclientprofile:{target_chat_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def adminmsglog_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    _, target_chat_id_str = query.data.split(":", 1)
    target_chat_id = int(target_chat_id_str)
    text, kb = _message_log_screen(target_chat_id)
    await query.edit_message_text(text, reply_markup=kb)


async def adminmsgedit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    _, target_chat_id_str, log_id_str = query.data.split(":", 2)
    target_chat_id, log_id = int(target_chat_id_str), int(log_id_str)
    ADMIN_MSGEDIT_PENDING[admin_id] = {"target_chat_id": target_chat_id, "log_id": log_id}
    await query.edit_message_text(
        "Напишіть новий текст цього повідомлення (замінить попередній у клієнта):",
        reply_markup=_adminprofile_cancel_keyboard(),
    )


async def adminmsgdel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    _, target_chat_id_str, log_id_str = query.data.split(":", 2)
    target_chat_id, log_id = int(target_chat_id_str), int(log_id_str)
    conn = db()
    row = conn.execute("SELECT * FROM message_log WHERE id=? AND chat_id=?", (log_id, target_chat_id)).fetchone()
    if row and row["telegram_message_id"]:
        try:
            await context.bot.delete_message(target_chat_id, row["telegram_message_id"])
        except Exception as e:
            logger.warning(f"Не вдалось видалити повідомлення в Telegram: {e}")
    conn.execute("DELETE FROM message_log WHERE id=?", (log_id,))
    conn.commit()
    conn.close()
    text, kb = _message_log_screen(target_chat_id)
    await query.edit_message_text(text, reply_markup=kb)


async def handle_admin_msgedit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_chat.id
    pending = ADMIN_MSGEDIT_PENDING.pop(admin_id, None)
    if not pending:
        return
    target_chat_id = pending["target_chat_id"]
    log_id = pending["log_id"]
    new_text = update.message.text or ""
    conn = db()
    row = conn.execute("SELECT * FROM message_log WHERE id=? AND chat_id=?", (log_id, target_chat_id)).fetchone()
    if not row or not row["telegram_message_id"]:
        conn.close()
        await update.message.reply_text("Це повідомлення вже недоступне для редагування.")
        return
    try:
        await context.bot.edit_message_text(chat_id=target_chat_id, message_id=row["telegram_message_id"], text=new_text)
        conn.execute("UPDATE message_log SET text=? WHERE id=?", (new_text, log_id))
        conn.commit()
        await update.message.reply_text("✅ Повідомлення клієнту оновлено.")
    except Exception as e:
        await update.message.reply_text(f"Не вдалось відредагувати повідомлення: {e}")
    conn.close()


async def handle_admin_add_address_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_chat.id
    pending = ADMIN_ADD_ADDRESS_PENDING.get(admin_id)
    if not pending:
        return
    target_chat_id = pending["target_chat_id"]
    text = update.message.text or ""

    if pending["step"] == "text":
        pending["data"]["address"] = text
        pending["step"] = "phone"
        profile = _get_client_profile(target_chat_id) or {}
        await update.message.reply_text(
            "Тепер напишіть номер телефону для зв'язку по цій адресі:",
            reply_markup=_address_phone_keyboard(profile.get("phone"), "adminprofile_cancelstep"),
        )
        return

    if not _is_valid_phone(text):
        await update.message.reply_text(
            "Це не схоже на номер телефону 🤔 Спробуйте ще раз, наприклад: +380671234567"
        )
        return
    phone = _normalize_phone(text)
    address_text = pending["data"]["address"]
    editing_id = pending.get("editing_address_id")
    ADMIN_ADD_ADDRESS_PENDING.pop(admin_id, None)
    conn = db()
    if editing_id:
        conn.execute(
            "UPDATE client_addresses SET address=?, phone=? WHERE id=? AND chat_id=?",
            (address_text, phone, editing_id, target_chat_id),
        )
    else:
        conn.execute(
            "INSERT INTO client_addresses (chat_id, address, phone, created_at) VALUES (?, ?, ?, ?)",
            (target_chat_id, address_text, phone, datetime.now(TZ).isoformat()),
        )
    conn.commit()
    conn.close()
    profile = _get_client_profile(target_chat_id)
    addresses = _get_client_addresses(target_chat_id)
    done_label = "оновлено" if editing_id else "додано клієнту"
    text_out = f"✅ Адресу {done_label}!\n\n" + (_profile_summary_text(profile, addresses, for_admin=True) if profile else "(картка ще не заповнена)")
    await update.message.reply_text(text_out, reply_markup=_adminprofile_manage_keyboard(target_chat_id, addresses))


async def handle_admin_edit_profile_text_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_chat.id
    pending = ADMIN_EDIT_PROFILE_PENDING.get(admin_id)
    if not pending:
        return

    target_chat_id = pending["target_chat_id"]
    field = pending["single_field"]
    text = update.message.text or ""

    if field == "phone":
        if not _is_valid_phone(text):
            await update.message.reply_text(
                "Це не схоже на номер телефону 🤔 Спробуйте ще раз, наприклад: +380671234567"
            )
            return
        text = _normalize_phone(text)

    ADMIN_EDIT_PROFILE_PENDING.pop(admin_id, None)
    conn = db()
    conn.execute(
        f"INSERT INTO client_profiles (chat_id, {field}, updated_at) VALUES (?, ?, ?) "
        f"ON CONFLICT(chat_id) DO UPDATE SET {field}=excluded.{field}, updated_at=excluded.updated_at",
        (target_chat_id, text, datetime.now(TZ).isoformat()),
    )
    conn.commit()
    conn.close()
    profile = _get_client_profile(target_chat_id) or {}
    addresses = _get_client_addresses(target_chat_id)
    await update.message.reply_text(
        f"✅ {PROFILE_FIELD_LABELS.get(field, field)} оновлено!\n\n" + _profile_summary_text(profile, addresses, for_admin=True),
        reply_markup=_adminprofile_manage_keyboard(target_chat_id, addresses),
    )


async def handle_profile_text_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pending = PROFILE_PENDING.get(chat_id)
    if not pending:
        return

    if pending.get("single_field"):
        field = pending["single_field"]
        text = update.message.text or ""
        if field == "phone":
            if not _is_valid_phone(text):
                await update.message.reply_text(
                    "Це не схоже на номер телефону 🤔 Спробуйте ще раз, наприклад: +380671234567"
                )
                return
            text = _normalize_phone(text)
        PROFILE_PENDING.pop(chat_id, None)
        conn = db()
        conn.execute(
            f"INSERT INTO client_profiles (chat_id, {field}, updated_at) VALUES (?, ?, ?) "
            f"ON CONFLICT(chat_id) DO UPDATE SET {field}=excluded.{field}, updated_at=excluded.updated_at",
            (chat_id, text, datetime.now(TZ).isoformat()),
        )
        conn.commit()
        conn.close()
        profile = _get_client_profile(chat_id) or {}
        addresses = _get_client_addresses(chat_id)
        await update.message.reply_text(
            f"✅ {PROFILE_FIELD_LABELS.get(field, field)} оновлено!\n\n" + _profile_summary_text(profile, addresses),
            reply_markup=_profile_manage_keyboard(addresses),
        )
        return

    if pending.get("awaiting_zone"):
        await update.message.reply_text(
            "Будь ласка, оберіть зону доставки кнопкою вище 👆",
            reply_markup=_delivery_zone_keyboard("profile_zone"),
        )
        return

    if pending.get("adding_address"):
        text = update.message.text or ""
        if pending["address_step"] == "text":
            pending["address_data"]["address"] = text
            pending["address_step"] = "phone"
            profile = _get_client_profile(chat_id) or {}
            await update.message.reply_text(
                "Тепер напишіть номер телефону для зв'язку по цій адресі "
                "(наприклад: +380671234567):",
                reply_markup=_address_phone_keyboard(profile.get("phone"), "profile_cancelstep"),
            )
            return

        # address_step == "phone"
        if not _is_valid_phone(text):
            await update.message.reply_text(
                "Це не схоже на номер телефону 🤔 Спробуйте ще раз, наприклад: +380671234567"
            )
            return
        phone = _normalize_phone(text)
        address_text = pending["address_data"]["address"]
        editing_id = pending.get("editing_address_id")
        PROFILE_PENDING.pop(chat_id, None)
        conn = db()
        if editing_id:
            conn.execute(
                "UPDATE client_addresses SET address=?, phone=? WHERE id=? AND chat_id=?",
                (address_text, phone, editing_id, chat_id),
            )
        else:
            conn.execute(
                "INSERT INTO client_addresses (chat_id, address, phone, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, address_text, phone, datetime.now(TZ).isoformat()),
            )
        conn.commit()
        conn.close()
        profile = _get_client_profile(chat_id) or {}
        addresses = _get_client_addresses(chat_id)
        done_label = "оновлено" if editing_id else "додано"
        await update.message.reply_text(
            f"✅ Адресу {done_label}!\n\n" + _profile_summary_text(profile, addresses),
            reply_markup=_profile_manage_keyboard(addresses),
        )
        return

    step_idx = pending["step_idx"]
    field = PROFILE_STEPS[step_idx]
    text = update.message.text or ""

    if field == "phone":
        if not _is_valid_phone(text):
            await update.message.reply_text(
                "Це не схоже на номер телефону 🤔 Спробуйте ще раз, наприклад: +380671234567"
            )
            return
        text = _normalize_phone(text)

    pending["data"][field] = text

    if step_idx + 1 < len(PROFILE_STEPS):
        pending["step_idx"] += 1
        await update.message.reply_text(
            PROFILE_STEP_PROMPTS[PROFILE_STEPS[pending["step_idx"]]],
            reply_markup=_profile_cancel_keyboard(),
        )
        return

    data = PROFILE_PENDING.pop(chat_id)["data"]
    conn = db()
    conn.execute(
        "INSERT INTO client_profiles (chat_id, full_name, point_name, phone, fop, ipn, delivery_zone, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET full_name=excluded.full_name, point_name=excluded.point_name, "
        "phone=excluded.phone, fop=excluded.fop, ipn=excluded.ipn, delivery_zone=excluded.delivery_zone, "
        "updated_at=excluded.updated_at",
        (chat_id, data["full_name"], data["point_name"], data["phone"], data["fop"],
         data["ipn"], data.get("delivery_zone"), datetime.now(TZ).isoformat()),
    )
    conn.commit()
    conn.close()
    addresses = _get_client_addresses(chat_id)
    if not addresses:
        PROFILE_PENDING[chat_id] = {"adding_address": True, "address_step": "text", "address_data": {}}
        await update.message.reply_text(
            "✅ Дані збережено! Залишилось додати хоча б одну адресу доставки.\n\nНапишіть адресу:"
        )
        return
    await update.message.reply_text(
        "✅ Картку збережено! Тепер можна оформлювати замовлення — натисніть «📝 Замовити».\n\n"
        + _profile_summary_text(data, addresses),
        reply_markup=_profile_manage_keyboard(addresses),
    )



# ---------- Опитувальник замовлення (клієнт) ----------

ORDER_WEIGHTS = ["1 кг", "0,5 кг", "0,25 кг"]
ORDER_QUANTITIES = ["1", "5", "10"]
COFFEE_CATEGORIES = ("arabika", "palay", "blend")


def _order_cancel_row(back_callback: str | None = None) -> list:
    row = []
    if back_callback:
        row.append(InlineKeyboardButton("⬅️ Назад", callback_data=back_callback))
    row.append(InlineKeyboardButton("❌ Скасувати замовлення", callback_data="order_cancel"))
    return row


def _order_date_keyboard(callback_prefix: str = "order_date") -> InlineKeyboardMarkup:
    now = datetime.now(TZ)
    today = now.date()
    buttons, row = [], []
    for i in range(21):
        d = today + timedelta(days=i)
        if d.weekday() >= 5:  # субота(5), неділя(6) — не приймаємо замовлення
            continue
        if i == 0 and now.hour >= 10:  # сьогоднішній день доступний тільки до 10:00
            continue
        label = f"{d.strftime('%d.%m')} ({WEEKDAY_LABELS_SHORT[d.weekday()]})"
        row.append(InlineKeyboardButton(label, callback_data=f"{callback_prefix}:{d.isoformat()}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
        if len(buttons) >= 6:  # досить ~12 доступних робочих днів наперед
            break
    if row:
        buttons.append(row)
    buttons.append(_order_cancel_row())
    return InlineKeyboardMarkup(buttons)


def _order_category_keyboard(back_callback: str | None = None) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"order_cat:{key}")]
        for key, label in ORDER_CATEGORIES
    ]
    buttons.append(_order_cancel_row(back_callback))
    return InlineKeyboardMarkup(buttons)


def _order_weight_keyboard() -> InlineKeyboardMarkup:
    buttons, row = [], []
    for w in ORDER_WEIGHTS:
        row.append(InlineKeyboardButton(w, callback_data=f"order_weight:{w}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append(_order_cancel_row("order_back_to_cat"))
    return InlineKeyboardMarkup(buttons)


def _order_qty_keyboard(back_callback: str = "order_back_to_weight") -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(q, callback_data=f"order_qty:{q}") for q in ORDER_QUANTITIES]
    return InlineKeyboardMarkup([
        row,
        [InlineKeyboardButton("✏️ Інша кількість", callback_data="order_qty_other")],
        _order_cancel_row(back_callback),
    ])


PACKAGING_OPTIONS = ["Крафт", "Бренд", "Білий не бренд", "Чорний"]


def _order_packaging_keyboard(back_callback: str = "order_back_to_weight") -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(p, callback_data=f"order_packaging:{p}")] for p in PACKAGING_OPTIONS]
    buttons.append(_order_cancel_row(back_callback))
    return InlineKeyboardMarkup(buttons)


def _after_address_screen(order: dict):
    """Після вибору адреси: якщо позиції вже скопійовані (повторне замовлення) — на перегляд,
    інакше — як завжди, на вибір категорії."""
    if order.get("items"):
        return _order_review_screen(order)
    return "Яку категорію товару додати до замовлення?", _order_category_keyboard("order_back_to_date")


def _order_review_step_screen(order: dict):
    """Показує позиції замовлення ПО ЧЕРЗІ, одну за раз — з кнопками
    «змінити кількість» / «прибрати» / «далі». Коли всі позиції переглянуто —
    показує підсумковий список з опціями додати/продовжити/скасувати."""
    items = order.get("items", [])
    if not items:
        return "Позицій поки немає.", _order_category_keyboard("order_back_to_date")

    idx = order.get("review_idx", 0)

    if idx >= len(items):
        lines = ["📋 Ось усі позиції вашого замовлення:\n"]
        for i, item in enumerate(items):
            cat_label = dict(ORDER_CATEGORIES).get(item.get("category"), item.get("category"))
            lines.append(f"{i+1}) {item.get('item_text', '—')} — {item.get('weight', '—')} × {item.get('qty', '—')} ({cat_label})")
        buttons = [
            [InlineKeyboardButton("➕ Додати нову позицію", callback_data="order_addmore")],
            [InlineKeyboardButton("✅ Все ок, продовжити", callback_data="order_finish")],
            [InlineKeyboardButton("❌ Скасувати замовлення", callback_data="order_cancel")],
        ]
        return "\n".join(lines), InlineKeyboardMarkup(buttons)

    item = items[idx]
    cat_label = dict(ORDER_CATEGORIES).get(item.get("category"), item.get("category"))
    text = (
        f"📋 Позиція {idx + 1} з {len(items)}:\n\n"
        f"{item.get('item_text', '—')} — {item.get('weight', '—')} × {item.get('qty', '—')} ({cat_label})"
    )
    buttons = [
        [
            InlineKeyboardButton("✏️ Змінити кількість", callback_data=f"order_reviewqty:{idx}"),
            InlineKeyboardButton("🗑 Прибрати", callback_data=f"order_reviewdel:{idx}"),
        ],
        [InlineKeyboardButton("➡️ Далі", callback_data="order_reviewnext")],
        [InlineKeyboardButton("❌ Скасувати замовлення", callback_data="order_cancel")],
    ]
    return text, InlineKeyboardMarkup(buttons)
    items = order.get("items", [])
    if not items:
        return "Позицій поки немає.", _order_category_keyboard("order_back_to_date")
    lines = ["📋 Ваші позиції (з попереднього замовлення) — можна змінити кількість чи прибрати зайве:\n"]
    buttons = []
    for i, item in enumerate(items):
        cat_label = dict(ORDER_CATEGORIES).get(item.get("category"), item.get("category"))
        lines.append(f"{i+1}) {item.get('item_text', '—')} — {item.get('weight', '—')} × {item.get('qty', '—')} ({cat_label})")
        buttons.append([
            InlineKeyboardButton(f"✏️ {i+1}", callback_data=f"order_reviewqty:{i}"),
            InlineKeyboardButton(f"🗑 {i+1}", callback_data=f"order_reviewdel:{i}"),
        ])
    buttons.append([InlineKeyboardButton("➕ Додати нову позицію", callback_data="order_addmore")])
    buttons.append([InlineKeyboardButton("✅ Все ок, продовжити", callback_data="order_finish")])
    buttons.append([InlineKeyboardButton("❌ Скасувати замовлення", callback_data="order_cancel")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _next_after_options(chat_id, back_callback):
    """Визначає наступний крок після ваги/помелу: пакування (якщо клієнта позначено) або одразу кількість."""
    if _client_needs_packaging(chat_id):
        return "packaging", "Яке пакування?", _order_packaging_keyboard(back_callback)
    return "qty", "Яка кількість?", _order_qty_keyboard(back_callback)


def _order_grind_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌰 Зерно", callback_data="order_grind:Зерно"),
            InlineKeyboardButton("⚙️ Молоте", callback_data="order_grind:Молоте"),
        ],
        _order_cancel_row("order_back_to_weight"),
    ])


GRIND_TYPE_OPTIONS = [
    "Турка",
    "Еспресо",
    "Чашка",
    "Офісна кавоварка",
    "Гейзер",
    "Френч прес",
    "V60 на 01 фільтр",
    "V60 на 02 фільтр",
    "Фільтр кава",
]


def _order_grind_type_keyboard() -> InlineKeyboardMarkup:
    buttons, row = [], []
    for i, g in enumerate(GRIND_TYPE_OPTIONS):
        row.append(InlineKeyboardButton(g, callback_data=f"order_grindtype:{i}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append(_order_cancel_row("order_back_to_grind"))
    return InlineKeyboardMarkup(buttons)


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else "—"


def _order_client_summary_text(order: dict, profile: dict) -> str:
    """Коротка версія підсумку — тільки те, що потрібно клієнту перед підтвердженням:
    назва кав'ярні, дата, самі позиції (з помелом/зерном) та спосіб оплати."""
    lines = [
        f"🏪 <b>{_esc(profile.get('point_name') or '—')}</b>",
        f"📅 Дата: {_esc(order.get('date') or '—')}",
        "",
    ]
    for i, item in enumerate(order.get("items", []), 1):
        grind_part = ""
        if item.get("grind"):
            grind_part = f", {_esc(item['grind'])}"
            if item.get("grind_type"):
                grind_part += f" ({_esc(item['grind_type'])})"
        lines.append(
            f"{i}) {_esc(item.get('item_text') or '—')} — {_esc(item.get('weight') or '—')} "
            f"× {_esc(item.get('qty') or '—')}{grind_part}"
        )
    lines.append("")
    lines.append(f"💳 Оплата: {_esc(order.get('payment_method') or '—')}")
    return "\n".join(lines)


def _order_summary_text(order: dict, profile: dict) -> str:
    lines = [
        "🆕 <b>НОВЕ ЗАМОВЛЕННЯ</b> 🆕",
        f"📅 <b>Дата виконання: {_esc(order.get('date') or '—')}</b>",
        "",
        f"👤 Клієнт: <b>{_esc(profile.get('full_name') or '—')}</b>",
        f"🏪 Точка: <b>{_esc(profile.get('point_name') or '—')}</b>",
        f"🗺 Зона доставки: {_esc(profile.get('delivery_zone') or '—')}",
        f"📍 Адреса: {_esc(order.get('address') or '—')}",
        f"📞 Телефон для доставки: {_esc(order.get('contact_phone') or '—')}",
        f"📞 Телефон: {_esc(profile.get('phone') or '—')}",
        f"🏢 ФОП: {_esc(profile.get('fop') or '—')}  |  ІПН: {_esc(profile.get('ipn') or '—')}",
        f"💳 Оплата: {_esc(order.get('payment_method') or '—')}",
        "",
        "📦 <b>Позиції для складу:</b>",
        "",
    ]
    for i, item in enumerate(order.get("items", []), 1):
        cat_label = dict(ORDER_CATEGORIES).get(item.get("category"), item.get("category"))
        grind_line = ""
        if item.get("grind"):
            grind_line = f"    Помел: {_esc(item['grind'])}"
            if item.get("grind_type"):
                grind_line += f" ({_esc(item['grind_type'])})"
            grind_line += "\n"
        packaging_line = f"    Пакування: {_esc(item['packaging'])}\n" if item.get("packaging") else ""
        lines.append(
            f"<b>{i}) {_esc(item.get('item_text') or '—')} — {_esc(item.get('weight') or '—')} "
            f"× {_esc(item.get('qty') or '—')}</b>\n"
            f"    Категорія: {_esc(cat_label)}\n"
            f"{grind_line}"
            f"{packaging_line}"
        )
    return "\n".join(lines)


def _parse_assortment_items(cat_key: str) -> list:
    """Розбирає текст асортименту категорії на окремі позиції (по рядках),
    прибираючи порожні рядки й декоративні розділювачі."""
    text = _get_setting(f"assortment_{cat_key}", "")
    items = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.strip("━-—=• ") == "":
            continue
        items.append(line)
    return items


ORDER_CONFIRM_REMINDER_MINUTES = 10


def _order_confirm_job_name(chat_id: int) -> str:
    return f"orderconfirm_{chat_id}"


def schedule_order_confirm_reminder(application: Application, chat_id: int) -> None:
    name = _order_confirm_job_name(chat_id)
    for job in application.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    application.job_queue.run_once(
        send_order_confirm_reminder,
        when=timedelta(minutes=ORDER_CONFIRM_REMINDER_MINUTES),
        name=name,
        data={"chat_id": chat_id},
    )


def cancel_order_confirm_reminder(application: Application, chat_id: int) -> None:
    name = _order_confirm_job_name(chat_id)
    for job in application.job_queue.get_jobs_by_name(name):
        job.schedule_removal()


async def send_order_confirm_reminder(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    order = ORDER_PENDING.get(chat_id)
    if not order or not order.get("awaiting_confirm"):
        return
    profile = _get_client_profile(chat_id) or {}
    summary = _order_client_summary_text(order, profile)
    target_admin = _resolve_target_admin(chat_id)
    if target_admin:
        try:
            await context.bot.send_message(
                target_admin,
                f"⏰ Клієнт досі не підтвердив замовлення (очікує вже {ORDER_CONFIRM_REMINDER_MINUTES}+ хв):\n\n"
                f"{summary}\n\nchat_id: {chat_id}. Можливо, варто написати клієнту особисто.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Не вдалось надіслати нагадування адміну про непідтверджене замовлення: {e}")


def _pending_orders_text() -> str:
    pending = [(cid, o) for cid, o in ORDER_PENDING.items() if o.get("awaiting_confirm")]
    if not pending:
        return "Немає замовлень, що зараз очікують підтвердження клієнтом."
    text = f"Замовлення, що очікують підтвердження ({len(pending)}):\n\n"
    for cid, o in pending:
        profile = _get_client_profile(cid) or {}
        label = profile.get("point_name") or str(cid)
        text += f"• {label} (chat_id: {cid}) — дата: {o.get('date') or '—'}, оплата: {o.get('payment_method') or '—'}\n"
    return text


async def pending_orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує адміну всі замовлення, що зараз очікують підтвердження клієнтом (ще не натиснули «Підтвердити»)."""
    if not is_admin(update):
        return
    await update.message.reply_text(_pending_orders_text())


async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    profile = _get_client_profile(chat_id)
    if not profile:
        PROFILE_PENDING[chat_id] = {"awaiting_zone": True, "data": {}}
        await update.message.reply_text(
            "Перш ніж оформити замовлення, заповніть, будь ласка, картку клієнта (це одноразово) 🙏\n\n"
            "Спочатку — оберіть зону доставки:",
            reply_markup=_delivery_zone_keyboard("profile_zone"),
        )
        return
    addresses = _get_client_addresses(chat_id)
    if not addresses:
        PROFILE_PENDING[chat_id] = {"adding_address": True, "address_step": "text", "address_data": {}}
        await update.message.reply_text(
            "Перш ніж оформити замовлення, додайте хоча б одну адресу доставки 🙏\n\nНапишіть адресу:",
            reply_markup=_profile_cancel_keyboard(),
        )
        return
    last_items = _get_last_order_items(chat_id)
    if last_items:
        await update.message.reply_text(
            "У вас є попереднє замовлення. Повторити його (можна буде змінити кількість чи додати щось), "
            "чи оформити нове з нуля?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔁 Повторити попереднє", callback_data="order_repeat")],
                [InlineKeyboardButton("🆕 Нове замовлення", callback_data="order_new")],
                [InlineKeyboardButton("❌ Скасувати", callback_data="order_cancel")],
            ]),
        )
        return
    ORDER_PENDING[chat_id] = {"step": "date", "items": []}
    await update.message.reply_text("На яку дату потрібне замовлення?", reply_markup=_order_date_keyboard())


async def qna_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка «📝 Замовити прямо тут» у запитанні так/ні — запускає той самий опитувальник замовлення."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    profile = _get_client_profile(chat_id)
    if not profile:
        PROFILE_PENDING[chat_id] = {"awaiting_zone": True, "data": {}}
        await query.edit_message_text(
            "Перш ніж оформити замовлення, заповніть, будь ласка, картку клієнта (це одноразово) 🙏\n\n"
            "Спочатку — оберіть зону доставки:",
            reply_markup=_delivery_zone_keyboard("profile_zone"),
        )
        return
    addresses = _get_client_addresses(chat_id)
    if not addresses:
        PROFILE_PENDING[chat_id] = {"adding_address": True, "address_step": "text", "address_data": {}}
        await query.edit_message_text(
            "Перш ніж оформити замовлення, додайте хоча б одну адресу доставки 🙏\n\nНапишіть адресу:",
            reply_markup=_profile_cancel_keyboard(),
        )
        return
    last_items = _get_last_order_items(chat_id)
    if last_items:
        await query.edit_message_text(
            "У вас є попереднє замовлення. Повторити його (можна буде змінити кількість чи додати щось), "
            "чи оформити нове з нуля?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔁 Повторити попереднє", callback_data="order_repeat")],
                [InlineKeyboardButton("🆕 Нове замовлення", callback_data="order_new")],
                [InlineKeyboardButton("❌ Скасувати", callback_data="order_cancel")],
            ]),
        )
        return
    ORDER_PENDING[chat_id] = {"step": "date", "items": []}
    await query.edit_message_text("На яку дату потрібне замовлення?", reply_markup=_order_date_keyboard())


async def handle_order_text_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    order = ORDER_PENDING.get(chat_id)
    if not order:
        return
    step = order.get("step")
    text = update.message.text or ""

    if step == "qty_other":
        order["current_item"]["qty"] = text
        order["items"].append(order.pop("current_item"))
        order["step"] = "addmore"
        buttons = [
            [InlineKeyboardButton("➕ Додати ще одну позицію", callback_data="order_addmore")],
            [InlineKeyboardButton("✅ Завершити замовлення", callback_data="order_finish")],
            [InlineKeyboardButton("❌ Скасувати замовлення", callback_data="order_cancel")],
        ]
        await update.message.reply_text(
            "✅ Позицію додано до замовлення!\n\n🛒 Що далі?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return


async def order_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    order = ORDER_PENDING.get(chat_id)
    data = query.data

    if data == "order_cancel":
        ORDER_PENDING.pop(chat_id, None)
        cancel_order_confirm_reminder(context.application, chat_id)
        await query.edit_message_text("Замовлення скасовано. Якщо передумаєте — просто натисніть «📝 Замовити» ще раз.")
        return

    if data == "order_new":
        ORDER_PENDING[chat_id] = {"step": "date", "items": []}
        await query.edit_message_text("На яку дату потрібне замовлення?", reply_markup=_order_date_keyboard())
        return

    if data == "order_repeat":
        last_items = _get_last_order_items(chat_id)
        order = ORDER_PENDING[chat_id] = {"step": "review", "items": last_items, "review_idx": 0}
        text, kb = _order_review_step_screen(order)
        await query.edit_message_text(text, reply_markup=kb)
        return

    if not order:
        await query.edit_message_text("Це замовлення вже неактуальне. Натисніть «📝 Замовити», щоб почати заново.")
        return

    if data.startswith("order_date:"):
        _, date_str = data.split(":", 1)
        d = datetime.fromisoformat(date_str).date()
        order["date"] = f"{d.strftime('%d.%m.%Y')} ({WEEKDAY_LABELS_SHORT[d.weekday()]})"
        addresses = _get_client_addresses(chat_id)
        if len(addresses) == 1:
            order["address"] = addresses[0]["address"]
            order["contact_phone"] = addresses[0].get("phone") or "—"
            text, kb = _after_address_screen(order)
            order["step"] = "review" if order.get("items") else "category"
            await query.edit_message_text(text, reply_markup=kb)
            return
        buttons = [
            [InlineKeyboardButton(f"{a['address'][:45]} ({a.get('phone') or '—'})", callback_data=f"order_addr:{a['id']}")]
            for a in addresses
        ]
        buttons.append(_order_cancel_row("order_back_to_date"))
        await query.edit_message_text(
            "На яку адресу оформити це замовлення?", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data == "order_back_to_date":
        order["step"] = "date"
        await query.edit_message_text("На яку дату потрібне замовлення?", reply_markup=_order_date_keyboard())
        return

    if data.startswith("order_addr:"):
        _, addr_id = data.split(":", 1)
        addresses = _get_client_addresses(chat_id)
        chosen = next((a for a in addresses if str(a["id"]) == addr_id), None)
        order["address"] = chosen["address"] if chosen else "—"
        order["contact_phone"] = (chosen.get("phone") if chosen else None) or "—"
        text, kb = _after_address_screen(order)
        order["step"] = "review" if order.get("items") else "category"
        await query.edit_message_text(text, reply_markup=kb)
        return

    if data == "order_review":
        text, kb = _order_review_step_screen(order)
        order["step"] = "review"
        await query.edit_message_text(text, reply_markup=kb)
        return

    if data == "order_reviewnext":
        order["review_idx"] = order.get("review_idx", 0) + 1
        text, kb = _order_review_step_screen(order)
        await query.edit_message_text(text, reply_markup=kb)
        return

    if data == "order_reviewcurrent":
        text, kb = _order_review_step_screen(order)
        await query.edit_message_text(text, reply_markup=kb)
        return

    if data.startswith("order_reviewqty:"):
        _, idx_str = data.split(":", 1)
        order["review_edit_idx"] = int(idx_str)
        await query.edit_message_text("Яка нова кількість для цієї позиції?", reply_markup=_order_qty_keyboard("order_reviewcurrent"))
        return

    if data.startswith("order_reviewdel:"):
        _, idx_str = data.split(":", 1)
        idx = int(idx_str)
        if 0 <= idx < len(order.get("items", [])):
            order["items"].pop(idx)
        order["review_idx"] = idx
        text, kb = _order_review_step_screen(order)
        await query.edit_message_text(text, reply_markup=kb)
        return

    NO_WEIGHT_CATEGORIES = ("drip", "suputni")

    if data.startswith("order_cat:"):
        _, cat_key = data.split(":", 1)
        items = _parse_assortment_items(cat_key)
        label = dict(ORDER_CATEGORIES).get(cat_key, cat_key)
        if not items:
            await query.edit_message_text(
                f"{label}\n\nЦя категорія ще порожня — зверніться до менеджера, або оберіть іншу категорію.",
                reply_markup=_order_category_keyboard("order_back_to_date"),
            )
            return
        order["current_item"] = {"category": cat_key}
        order["current_item_choices"] = items[:80]
        buttons, row = [], []
        for i, m in enumerate(order["current_item_choices"]):
            row.append(InlineKeyboardButton(m, callback_data=f"order_pick_item:{i}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append(_order_cancel_row("order_back_to_cat"))
        await query.edit_message_text(f"{label}\n\nОберіть позицію:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data == "order_back_to_cat":
        order["step"] = "category"
        order.pop("current_item", None)
        await query.edit_message_text(
            "Яку категорію товару додати до замовлення?",
            reply_markup=_order_category_keyboard("order_back_to_date"),
        )
        return

    if data.startswith("order_pick_item:"):
        _, idx_str = data.split(":", 1)
        idx = int(idx_str)
        choices = order.get("current_item_choices", [])
        if idx >= len(choices):
            await query.answer("Ця позиція вже неактуальна, спробуйте ще раз.", show_alert=True)
            return
        order["current_item"]["item_text"] = choices[idx]
        cat_key = order["current_item"]["category"]
        if cat_key in NO_WEIGHT_CATEGORIES:
            order["current_item"]["weight"] = "шт"
            order["step"] = "qty"
            await query.edit_message_text(
                f"Обрано: {choices[idx]}\n\nЯка кількість?",
                reply_markup=_order_qty_keyboard("order_back_to_cat"),
            )
        else:
            order["step"] = "weight"
            await query.edit_message_text(
                f"Обрано: {choices[idx]}\n\nОберіть вагу/фасування:", reply_markup=_order_weight_keyboard()
            )
        return

    if data.startswith("order_weight:"):
        _, weight = data.split(":", 1)
        order["current_item"]["weight"] = weight
        cat_key = order["current_item"]["category"]
        if cat_key in COFFEE_CATEGORIES:
            order["step"] = "grind"
            await query.edit_message_text("Зерно чи молоте?", reply_markup=_order_grind_keyboard())
        else:
            next_step, next_text, next_kb = _next_after_options(chat_id, "order_back_to_weight")
            order["step"] = next_step
            await query.edit_message_text(next_text, reply_markup=next_kb)
        return

    if data == "order_back_to_weight":
        order["step"] = "weight"
        await query.edit_message_text("Оберіть вагу/фасування:", reply_markup=_order_weight_keyboard())
        return

    if data.startswith("order_grind:"):
        _, grind = data.split(":", 1)
        order["current_item"]["grind"] = grind
        if grind == "Молоте":
            await query.edit_message_text("На який помел?", reply_markup=_order_grind_type_keyboard())
        else:
            next_step, next_text, next_kb = _next_after_options(chat_id, "order_back_to_weight")
            order["step"] = next_step
            await query.edit_message_text(next_text, reply_markup=next_kb)
        return

    if data == "order_back_to_grind":
        await query.edit_message_text("Зерно чи молоте?", reply_markup=_order_grind_keyboard())
        return

    if data.startswith("order_grindtype:"):
        _, idx_str = data.split(":", 1)
        idx = int(idx_str)
        order["current_item"]["grind_type"] = GRIND_TYPE_OPTIONS[idx]
        next_step, next_text, next_kb = _next_after_options(chat_id, "order_back_to_grind")
        order["step"] = next_step
        await query.edit_message_text(next_text, reply_markup=next_kb)
        return

    if data.startswith("order_packaging:"):
        _, packaging = data.split(":", 1)
        order["current_item"]["packaging"] = packaging
        order["step"] = "qty"
        back_to = "order_back_to_grind" if order["current_item"].get("grind") else "order_back_to_weight"
        await query.edit_message_text("Яка кількість?", reply_markup=_order_qty_keyboard(back_to))
        return

    if data.startswith("order_qty:"):
        _, qty = data.split(":", 1)
        if "review_edit_idx" in order:
            idx = order.pop("review_edit_idx")
            if 0 <= idx < len(order.get("items", [])):
                order["items"][idx]["qty"] = qty
            order["review_idx"] = idx + 1
            text, kb = _order_review_step_screen(order)
            await query.edit_message_text(text, reply_markup=kb)
            return
        order["current_item"]["qty"] = qty
        order["items"].append(order.pop("current_item"))
        order["step"] = "addmore"
        buttons = [
            [InlineKeyboardButton("➕ Додати ще одну позицію", callback_data="order_addmore")],
            [InlineKeyboardButton("✅ Завершити замовлення", callback_data="order_finish")],
            [InlineKeyboardButton("❌ Скасувати замовлення", callback_data="order_cancel")],
        ]
        await query.edit_message_text(
            "✅ Позицію додано до замовлення!\n\n🛒 Що далі?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data == "order_qty_other":
        order["step"] = "qty_other"
        await query.edit_message_text(
            "Напишіть потрібну кількість:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати замовлення", callback_data="order_cancel")]]),
        )
        return

    if data == "order_addmore":
        order["step"] = "category"
        await query.edit_message_text("Яку категорію товару додати?", reply_markup=_order_category_keyboard())
        return

    if data == "order_finish":
        if not order.get("items"):
            ORDER_PENDING.pop(chat_id, None)
            await query.edit_message_text("Замовлення порожнє, скасовано.")
            return
        if not order.get("date"):
            order["step"] = "finaldate"
            await query.edit_message_text(
                "На яку дату потрібне замовлення?", reply_markup=_order_date_keyboard("order_finaldate")
            )
            return
        if order.get("payment_method"):
            order["awaiting_confirm"] = True
            schedule_order_confirm_reminder(context.application, chat_id)
            profile = _get_client_profile(chat_id) or {}
            summary = _order_client_summary_text(order, profile)
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Змінити", callback_data="order_editagain")],
                [InlineKeyboardButton("✅ Підтвердити замовлення", callback_data="order_confirm")],
            ])
            await query.edit_message_text(
                summary,
                reply_markup=buttons,
                parse_mode="HTML",
            )
            return
        order["step"] = "payment"
        await query.edit_message_text(
            "Останній крок — оберіть спосіб оплати:", reply_markup=_order_payment_keyboard()
        )
        return

    if data.startswith("order_finaldate:"):
        _, date_str = data.split(":", 1)
        d = datetime.fromisoformat(date_str).date()
        order["date"] = f"{d.strftime('%d.%m.%Y')} ({WEEKDAY_LABELS_SHORT[d.weekday()]})"
        addresses = _get_client_addresses(chat_id)
        if len(addresses) == 1:
            order["address"] = addresses[0]["address"]
            order["contact_phone"] = addresses[0].get("phone") or "—"
            order["step"] = "payment"
            await query.edit_message_text(
                "Останній крок — оберіть спосіб оплати:", reply_markup=_order_payment_keyboard()
            )
            return
        buttons = [
            [InlineKeyboardButton(f"{a['address'][:45]} ({a.get('phone') or '—'})", callback_data=f"order_finaladdr:{a['id']}")]
            for a in addresses
        ]
        order["step"] = "finaladdr"
        await query.edit_message_text(
            "На яку адресу оформити це замовлення?", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("order_finaladdr:"):
        _, addr_id = data.split(":", 1)
        addresses = _get_client_addresses(chat_id)
        chosen = next((a for a in addresses if str(a["id"]) == addr_id), None)
        order["address"] = chosen["address"] if chosen else "—"
        order["contact_phone"] = (chosen.get("phone") if chosen else None) or "—"
        order["step"] = "payment"
        await query.edit_message_text(
            "Останній крок — оберіть спосіб оплати:", reply_markup=_order_payment_keyboard()
        )
        return

    if data.startswith("order_payment:"):
        _, method = data.split(":", 1)
        order["payment_method"] = method
        order["awaiting_confirm"] = True
        schedule_order_confirm_reminder(context.application, chat_id)
        profile = _get_client_profile(chat_id) or {}
        summary = _order_client_summary_text(order, profile)
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Змінити", callback_data="order_editagain")],
            [InlineKeyboardButton("✅ Підтвердити замовлення", callback_data="order_confirm")],
        ])
        await query.edit_message_text(
            summary,
            reply_markup=buttons,
            parse_mode="HTML",
        )
        return

    if data == "order_editagain":
        order["awaiting_confirm"] = False
        cancel_order_confirm_reminder(context.application, chat_id)
        order["review_idx"] = 0
        order["step"] = "review"
        text, kb = _order_review_step_screen(order)
        await query.edit_message_text(text, reply_markup=kb)
        return

    if data == "order_confirm":
        order = ORDER_PENDING.pop(chat_id, None)
        if not order:
            await query.edit_message_text("Це замовлення вже неактуальне.")
            return
        cancel_order_confirm_reminder(context.application, chat_id)
        _save_completed_order(chat_id, order)
        profile = _get_client_profile(chat_id) or {}
        summary = _order_summary_text(order, profile)
        client = update.effective_user
        target_admin = _resolve_target_admin(chat_id)
        await query.edit_message_text("✅ Замовлення оформлено!")
        await context.bot.send_message(
            chat_id,
            "Дякуємо! Ваше замовлення передано менеджеру, скоро з вами зв'яжуться 🙌\n\n"
            "👇 Кнопки внизу завжди тут, якщо треба щось інше.",
            reply_markup=_keyboard_for_recipient(chat_id),
        )
        if target_admin:
            try:
                sent = await context.bot.send_message(
                    target_admin,
                    f"{summary}\n\nВід: {_esc(client.full_name)} (@{_esc(client.username or '—')}, chat_id: {chat_id})\n\n"
                    f"Щоб відповісти клієнту — зробіть Reply на це повідомлення.",
                    parse_mode="HTML",
                )
                FORWARD_MAP[(target_admin, sent.message_id)] = chat_id
            except Exception as e:
                logger.warning(f"Не вдалось переслати замовлення адміну: {e}")
        return




def _persistent_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📋 Меню")]],
        resize_keyboard=True,
        is_persistent=True,
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    hint_msg = await update.message.reply_text(
        "👇👇👇\n"
        "Ось тут, унизу екрана, є кнопка «📋 Меню» — вона залишається там завжди. "
        "Натискайте на неї в будь-який момент, щоб відкрити меню одним тапом, без набору команди.",
        reply_markup=_persistent_menu_keyboard(),
    )
    try:
        await context.bot.pin_chat_message(
            update.effective_chat.id, hint_msg.message_id, disable_notification=True
        )
    except Exception as e:
        logger.warning(f"Не вдалось закріпити підказку про меню: {e}")
    await update.message.reply_text("Оберіть дію:", reply_markup=_menu_root_keyboard())


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    data = query.data

    if data == "menu_back":
        MENU_PENDING.pop(admin_id, None)
        await query.edit_message_text("Оберіть дію:", reply_markup=_menu_root_keyboard())
        return

    if data == "menu_settings":
        await query.edit_message_text("⚙️ Налаштування:", reply_markup=_menu_settings_keyboard())
        return

    if data == "menu_pendingorders":
        await query.edit_message_text(_pending_orders_text(), reply_markup=_menu_back_keyboard())
        return

    if data == "menu_now":
        await query.edit_message_text("📤 Написати або запитати зараз:", reply_markup=_menu_now_keyboard())
        return

    if data == "menu_scheduled":
        await query.edit_message_text("📅 Заплановано на потім:", reply_markup=_menu_scheduled_keyboard())
        return

    if data == "menu_clients":
        await query.edit_message_text("👥 Клієнти:", reply_markup=_menu_clients_keyboard())
        return

    if data == "menu_broadcast":
        MENU_PENDING[admin_id] = {"action": "broadcast_text"}
        await query.edit_message_text(
            "Напишіть текст, який хочете надіслати ВСІМ клієнтам одразу (просто звичайним повідомленням):"
        )
        return

    if data == "menu_sendto":
        await query.edit_message_text("Завантажую список клієнтів...")
        await _sendto_send_picker(context, admin_id)
        return

    if data == "menu_addsegment":
        MENU_PENDING[admin_id] = {"action": "addsegment_text"}
        await query.edit_message_text(
            "Напишіть назву нової групи одним словом, без пробілів, латиницею "
            "(наприклад: postiyni, vip, wholesale):"
        )
        return

    if data == "menu_editassortment":
        await query.edit_message_text(
            "☕ Яку категорію асортименту редагувати?",
            reply_markup=_assortment_category_keyboard("menu_assortcat"),
        )
        return

    if data.startswith("menu_assortcat:"):
        _, cat_key = data.split(":", 1)
        label = dict(ASSORTMENT_CATEGORIES).get(cat_key, cat_key)
        current = _get_setting(f"assortment_{cat_key}", "").strip()
        preview = f"\n\nЗараз там написано:\n{current}" if current else "\n\nЗараз там ще нічого не додано."
        MENU_PENDING[admin_id] = {"action": "assortment_text", "category": cat_key}
        await query.edit_message_text(
            f"Напишіть новий текст для категорії «{label}» — саме це побачать клієнти, "
            f"коли оберуть цю категорію." + preview
        )
        return

    if data == "menu_editdelivery":
        await query.edit_message_text(
            "🚚 Умови доставки для якої категорії редагувати?",
            reply_markup=_delivery_category_keyboard("menu_deliverycat"),
        )
        return

    if data.startswith("menu_deliverycat:"):
        _, cat_key = data.split(":", 1)
        label = dict(DELIVERY_CATEGORIES).get(cat_key, cat_key)
        current = _get_setting(f"delivery_{cat_key}", _default_delivery_text(cat_key)).strip()
        MENU_PENDING[admin_id] = {"action": "delivery_text", "category": cat_key}
        await query.edit_message_text(
            f"Напишіть новий текст умов доставки для категорії «{label}» — саме це побачать клієнти, "
            f"коли оберуть цю категорію.\n\nЗараз там написано:\n" + current
        )
        return

    if data == "menu_changeseg":
        conn = db()
        rows = conn.execute(
            "SELECT chat_id, name, username FROM subscribers WHERE active=1 ORDER BY joined_at DESC LIMIT 50"
        ).fetchall()
        conn.close()
        if not rows:
            await query.edit_message_text("Поки немає активних підписників.", reply_markup=_menu_back_keyboard())
            return
        buttons = [
            [InlineKeyboardButton(_client_display_label(r['chat_id'], r['name'], r['username']), callback_data=f"menu_pickclient:{r['chat_id']}")]
            for r in rows
        ]
        buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
        await query.edit_message_text(
            "Оберіть клієнта, якому хочете змінити групу:", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data == "menu_editclientprofile":
        conn = db()
        rows = conn.execute(
            "SELECT chat_id, name, username FROM subscribers WHERE active=1 ORDER BY joined_at DESC LIMIT 50"
        ).fetchall()
        conn.close()
        if not rows:
            await query.edit_message_text("Поки немає активних підписників.", reply_markup=_menu_back_keyboard())
            return
        buttons = [
            [InlineKeyboardButton(_client_display_label(r['chat_id'], r['name'], r['username']), callback_data=f"menu_pickclientprofile:{r['chat_id']}")]
            for r in rows
        ]
        buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
        await query.edit_message_text(
            "Картку якого клієнта переглянути/редагувати?", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data == "menu_packaging":
        await query.edit_message_text(
            "Оберіть клієнтів, яким показувати крок вибору пакування "
            "(тап додає/прибирає позначку, зберігається одразу):",
            reply_markup=_packaging_toggle_keyboard(),
        )
        return

    if data.startswith("menu_packagingtoggle:"):
        _, target_chat_id_str = data.split(":", 1)
        target_chat_id = int(target_chat_id_str)
        conn = db()
        row = conn.execute("SELECT needs_packaging FROM subscribers WHERE chat_id=?", (target_chat_id,)).fetchone()
        new_val = 0 if (row and row["needs_packaging"]) else 1
        conn.execute("UPDATE subscribers SET needs_packaging=? WHERE chat_id=?", (new_val, target_chat_id))
        conn.commit()
        conn.close()
        await query.edit_message_reply_markup(reply_markup=_packaging_toggle_keyboard())
        return

    if data.startswith("menu_pickclientprofile:"):
        _, target_chat_id_str = data.split(":", 1)
        target_chat_id = int(target_chat_id_str)
        profile = _get_client_profile(target_chat_id)
        addresses = _get_client_addresses(target_chat_id)
        buttons = _adminprofile_manage_keyboard(target_chat_id, addresses)
        if not profile:
            await query.edit_message_text(
                "Цей клієнт ще не заповнював картку. Можна одразу додати адресу нижче, "
                "або зачекати, поки клієнт заповнить решту сам.",
                reply_markup=buttons,
            )
            return
        await query.edit_message_text(
            _profile_summary_text(profile, addresses, for_admin=True), reply_markup=buttons
        )
        return

    if data == "menu_setresp":
        conn = db()
        rows = conn.execute(
            "SELECT chat_id, name, username FROM subscribers WHERE active=1 ORDER BY joined_at DESC LIMIT 50"
        ).fetchall()
        conn.close()
        if not rows:
            await query.edit_message_text("Поки немає активних підписників.", reply_markup=_menu_back_keyboard())
            return
        buttons = [
            [InlineKeyboardButton(_client_display_label(r['chat_id'], r['name'], r['username']), callback_data=f"menu_pickclientresp:{r['chat_id']}")]
            for r in rows
        ]
        buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
        await query.edit_message_text(
            "Оберіть клієнта, якому призначити відповідального:", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data == "menu_resplist":
        try:
            await query.edit_message_text(_clients_with_admin_text(), reply_markup=_menu_back_keyboard())
        except Exception as e:
            logger.warning(f"Не вдалось показати список клієнтів: {e}")
            await query.edit_message_text(
                "Не вдалось завантажити список (тимчасова помилка). Спробуйте ще раз або команду /resplist.",
                reply_markup=_menu_back_keyboard(),
            )
        return

    if data.startswith("menu_pickclientresp:"):
        _, chat_id_str = data.split(":", 1)
        chat_id = int(chat_id_str)
        admins = _admins_with_username()
        if not admins:
            await query.edit_message_text(
                "Ще жоден адмін не зареєстрований (юзернейм не заданий у Telegram, або ще ніхто "
                "з адмінів не писав боту). Кожен адмін має хоч раз написати боту будь-яку команду.",
                reply_markup=_menu_back_keyboard(),
            )
            return
        buttons = [[InlineKeyboardButton("🙋 Призначити себе", callback_data=f"menu_pickresp2:{chat_id}:{admin_id}")]]
        buttons += [
            [InlineKeyboardButton(a["name"], callback_data=f"menu_pickresp2:{chat_id}:{a['chat_id']}")]
            for a in admins
        ]
        buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
        await query.edit_message_text(
            "Оберіть відповідального:", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("menu_pickresp2:"):
        _, chat_id_str, admin_id_str = data.split(":", 2)
        chat_id, resp_admin_id = int(chat_id_str), int(admin_id_str)
        conn = db()
        conn.execute("UPDATE subscribers SET responsible_admin=? WHERE chat_id=?", (resp_admin_id, chat_id))
        conn.commit()
        client_row = conn.execute("SELECT name FROM subscribers WHERE chat_id=?", (chat_id,)).fetchone()
        admin_row = conn.execute("SELECT name FROM admins WHERE chat_id=?", (resp_admin_id,)).fetchone()
        conn.close()
        await query.edit_message_text(
            f"✅ {client_row['name'] if client_row else chat_id} тепер закріплений(-а) за "
            f"{admin_row['name'] if admin_row else resp_admin_id}.",
            reply_markup=_menu_back_keyboard(),
        )
        return

    if data == "menu_setmsg":
        conn = db()
        rows = conn.execute("SELECT name FROM segments").fetchall()
        conn.close()
        if not rows:
            await query.edit_message_text(
                "Груп ще немає. Створіть спочатку командою /addsegment назва.",
                reply_markup=_menu_back_keyboard(),
            )
            return
        buttons = [[InlineKeyboardButton(r["name"], callback_data=f"menu_setmsg_pick:{r['name']}")] for r in rows]
        buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
        await query.edit_message_text(
            "Для якої групи змінити повідомлення?", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data == "menu_viewsegments":
        await query.edit_message_text(_segments_text(), reply_markup=_menu_back_keyboard())
        return

    if data == "menu_viewclients":
        await query.edit_message_text(_clients_text(admin_id=admin_id), reply_markup=_menu_back_keyboard())
        return

    if data.startswith("menu_pickclient:"):
        _, chat_id_str = data.split(":", 1)
        chat_id = int(chat_id_str)
        conn = db()
        rows = conn.execute("SELECT name FROM segments").fetchall()
        conn.close()
        buttons = [
            [InlineKeyboardButton(r["name"], callback_data=f"menu_pickseg:{chat_id}:{r['name']}")] for r in rows
        ]
        buttons.append([InlineKeyboardButton("Без групи", callback_data=f"menu_pickseg:{chat_id}:__none__")])
        buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
        await query.edit_message_text(
            "Оберіть нову групу для цього клієнта:", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("menu_pickseg:"):
        _, chat_id_str, seg = data.split(":", 2)
        chat_id = int(chat_id_str)
        segment_value = None if seg == "__none__" else seg
        conn = db()
        conn.execute("UPDATE subscribers SET segment=? WHERE chat_id=?", (segment_value, chat_id))
        conn.commit()
        row = conn.execute("SELECT name FROM subscribers WHERE chat_id=?", (chat_id,)).fetchone()
        conn.close()
        client_name = row["name"] if row else str(chat_id)
        await query.edit_message_text(
            f"✅ Готово. {client_name} тепер у групі: {segment_value or 'без групи'}.",
            reply_markup=_menu_back_keyboard(),
        )
        return

    if data.startswith("menu_setmsg_pick:"):
        _, seg = data.split(":", 1)
        MENU_PENDING[admin_id] = {"action": "setmsg_text", "segment": seg}
        await query.edit_message_text(f"Напишіть новий текст повідомлення для групи «{seg}»:")
        return


# ---------- Майстер планування розсилок (кнопки: кому / коли / текст) ----------

def _bc_target_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📁 По групі", callback_data="bc_target_group")],
        [InlineKeyboardButton("🎯 Обраним людям", callback_data="bc_target_selected")],
        [InlineKeyboardButton("🔙 До меню", callback_data="menu_back")],
    ])


def _bc_group_keyboard() -> InlineKeyboardMarkup:
    conn = db()
    rows = conn.execute("SELECT name FROM segments").fetchall()
    conn.close()
    buttons = [[InlineKeyboardButton(r["name"], callback_data=f"bc_pickgroup:{r['name']}")] for r in rows]
    buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
    return InlineKeyboardMarkup(buttons)


# ---------- Миттєве написання групі (текст або файл — прямо зараз) ----------

def _wg_group_keyboard() -> InlineKeyboardMarkup:
    conn = db()
    rows = conn.execute("SELECT name FROM segments").fetchall()
    conn.close()
    buttons = [[InlineKeyboardButton(r["name"], callback_data=f"wg_pickgroup:{r['name']}")] for r in rows]
    buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
    return InlineKeyboardMarkup(buttons)


async def wg_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    data = query.data

    if data == "wg_start":
        conn = db()
        has_segments = conn.execute("SELECT COUNT(*) c FROM segments").fetchone()["c"]
        conn.close()
        if not has_segments:
            await query.edit_message_text("Груп ще немає.", reply_markup=_menu_back_keyboard())
            return
        await query.edit_message_text("Оберіть групу:", reply_markup=_wg_group_keyboard())
        return

    if data.startswith("wg_pickgroup:"):
        _, seg = data.split(":", 1)
        clients = _get_admin_clients(admin_id, segment=seg)
        if not clients:
            await query.edit_message_text(
                f"У вас немає власних клієнтів у групі «{seg}» 🙈",
                reply_markup=_menu_back_keyboard(),
            )
            return
        buttons = [
            [InlineKeyboardButton(f"📢 Усій групі ({len(clients)} осіб)", callback_data=f"wg_whole:{seg}")],
            [InlineKeyboardButton("🎯 Обрати конкретних", callback_data=f"wg_select:{seg}")],
            [InlineKeyboardButton("🔙 До меню", callback_data="menu_back")],
        ]
        await query.edit_message_text(
            f"Група «{seg}» — {len(clients)} ваших клієнтів. Кому написати зараз?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data.startswith("wg_whole:"):
        _, seg = data.split(":", 1)
        clients = _get_admin_clients(admin_id, segment=seg)
        SENDTO_AWAITING_TEXT[admin_id] = [c["chat_id"] for c in clients]
        await query.edit_message_text(
            f"Напишіть текст, або надішліть файл — піде одразу всім вашим клієнтам групи «{seg}» "
            f"({len(clients)} осіб):"
        )
        return

    if data.startswith("wg_select:"):
        _, seg = data.split(":", 1)
        clients = _get_admin_clients(admin_id, segment=seg)
        SENDTO_SELECTIONS[admin_id] = set()
        context.chat_data["sendto_clients"] = clients
        await query.edit_message_text(
            f"Ваші клієнти в групі «{seg}» — оберіть, кому надіслати:",
            reply_markup=_build_sendto_keyboard(admin_id, clients),
        )
        return


def _bc_select_keyboard(admin_id: int, clients: list) -> InlineKeyboardMarkup:
    selected = BC_SELECTIONS.get(admin_id, set())
    buttons = []
    for c in clients:
        mark = "✅" if c["chat_id"] in selected else "⬜"
        buttons.append([InlineKeyboardButton(
            f"{mark} {_client_display_label(c['chat_id'], c['name'], c['username'])}", callback_data=f"bc_toggle:{c['chat_id']}"
        )])
    buttons.append([InlineKeyboardButton("➕ Додати ще людину (всі клієнти)", callback_data="bc_showall")])
    buttons.append([
        InlineKeyboardButton("⬅️ Назад", callback_data="bc_backtotarget"),
        InlineKeyboardButton("➡️ Далі", callback_data="bc_selectdone"),
    ])
    buttons.append([InlineKeyboardButton("❌ Скасувати", callback_data="menu_back")])
    return InlineKeyboardMarkup(buttons)


def _bc_cancel_keyboard(admin_id: int, rows: list) -> InlineKeyboardMarkup:
    selected = BC_CANCEL_SELECTIONS.get(admin_id, set())
    buttons = []
    for r in rows:
        mark = "✅" if r["id"] in selected else "⬜"
        label = f"{mark} {_bc_name_prefix(r)}{'❓ ' if r['is_question'] else ''}{_bc_target_label(r['target_type'], r['target_value'])} — {_bc_timing_label(r)}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"bc_canceltoggle:{r['id']}")])
    buttons.append([
        InlineKeyboardButton("🗑 Скасувати обрані", callback_data="bc_cancelconfirmall"),
    ])
    buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
    return InlineKeyboardMarkup(buttons)


def _bc_timing_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📆 Одноразово, конкретна дата", callback_data="bc_timing_once")],
        [InlineKeyboardButton("🔁 Щотижня, певний день", callback_data="bc_timing_weekly")],
        [InlineKeyboardButton("🔙 До меню", callback_data="menu_back")],
    ])


def _bc_date_keyboard() -> InlineKeyboardMarkup:
    today = datetime.now(TZ).date()
    buttons, row = [], []
    for i in range(14):
        d = today + timedelta(days=i)
        label = f"{d.strftime('%d.%m')} ({WEEKDAY_LABELS_SHORT[d.weekday()]})"
        row.append(InlineKeyboardButton(label, callback_data=f"bc_date:{d.isoformat()}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
    return InlineKeyboardMarkup(buttons)


def _bc_weekday_keyboard() -> InlineKeyboardMarkup:
    buttons, row = [], []
    for i, label in enumerate(WEEKDAY_LABELS_SHORT):
        row.append(InlineKeyboardButton(label, callback_data=f"bc_weekday:{i}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
    return InlineKeyboardMarkup(buttons)


def _bc_hour_keyboard() -> InlineKeyboardMarkup:
    buttons, row = [], []
    for h in BC_HOURS:
        row.append(InlineKeyboardButton(f"{h:02d}:00", callback_data=f"bc_hour:{h}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
    return InlineKeyboardMarkup(buttons)


def _bc_minute_keyboard(hour: int) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(f"{hour:02d}:{m:02d}", callback_data=f"bc_min:{m}") for m in BC_MINUTES]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("🔙 До меню", callback_data="menu_back")]])


async def _finalize_scheduled_broadcast(
    update: Update, context: ContextTypes.DEFAULT_TYPE, admin_id: int, message_text: str, file_id: str | None = None
):
    """Зберігає й планує розсилку (звичайну або питання так/ні), опційно з прикріпленим файлом.
    Використовується і коли адмін надіслав текст, і коли надіслав файл замість тексту."""
    bc = BC_PENDING.pop(admin_id, {})
    BC_SELECTIONS.pop(admin_id, None)

    target_type = bc.get("target_type")
    target_value = bc.get("target_value")
    kind = bc.get("kind")
    hour = bc.get("hour")
    minute = bc.get("minute", 0)
    is_question = 1 if bc.get("is_question") else 0

    if not all([target_type, target_value, kind]) or hour is None:
        await update.message.reply_text(
            "Щось пішло не так під час налаштування розсилки. Спробуйте ще раз через меню.",
            reply_markup=_menu_back_keyboard(),
        )
        return

    conn = db()
    bc_name = bc.get("name")
    if kind == "once":
        date_str = bc.get("date")
        run_at = datetime.combine(
            datetime.fromisoformat(date_str).date(), time(hour=hour, minute=minute), tzinfo=TZ
        )
        cur = conn.execute(
            "INSERT INTO broadcasts (kind, target_type, target_value, message, run_at, is_question, file_id, created_by, name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, target_type, target_value, message_text, run_at.isoformat(), is_question, file_id, admin_id, bc_name, datetime.now(TZ).isoformat()),
        )
    else:
        weekday = bc.get("weekday")
        hhmm = f"{hour:02d}:{minute:02d}"
        cur = conn.execute(
            "INSERT INTO broadcasts (kind, target_type, target_value, message, weekday, hhmm, is_question, file_id, created_by, name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, target_type, target_value, message_text, weekday, hhmm, is_question, file_id, admin_id, bc_name, datetime.now(TZ).isoformat()),
        )
    broadcast_id = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM broadcasts WHERE id=?", (broadcast_id,)).fetchone()
    conn.close()

    schedule_broadcast_job(context.application, row)

    kind_label = "Запитання так/ні" if is_question else "Розсилку"
    file_note = "\n📎 З прикріпленим файлом" if file_id else ""
    name_line = f"Назва: {bc_name}\n" if bc_name else ""
    await update.message.reply_text(
        f"✅ {kind_label} заплановано!{file_note}\n\n"
        f"{name_line}"
        f"Кому: {_bc_target_label(target_type, target_value)}\n"
        f"Коли: {_bc_timing_label(row)}\n"
        f"Текст: {message_text or '(без тексту)'}",
        reply_markup=_menu_back_keyboard(),
    )


async def bc_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    data = query.data

    if data == "bc_start":
        BC_PENDING[admin_id] = {}
        BC_SELECTIONS.pop(admin_id, None)
        await query.edit_message_text("Кому запланувати розсилку?", reply_markup=_bc_target_keyboard())
        return

    if data == "bc_backtotarget":
        is_q = BC_PENDING.get(admin_id, {}).get("is_question", False)
        BC_PENDING[admin_id] = {"is_question": True} if is_q else {}
        BC_SELECTIONS.pop(admin_id, None)
        prompt = "Кому запланувати запитання так/ні?" if is_q else "Кому запланувати розсилку?"
        await query.edit_message_text(prompt, reply_markup=_bc_target_keyboard())
        return

    if data == "bcq_start":
        BC_PENDING[admin_id] = {"is_question": True}
        BC_SELECTIONS.pop(admin_id, None)
        await query.edit_message_text(
            "Кому запланувати запитання так/ні?", reply_markup=_bc_target_keyboard()
        )
        return

    if data == "bc_target_group":
        conn = db()
        has_segments = conn.execute("SELECT COUNT(*) c FROM segments").fetchone()["c"]
        conn.close()
        if not has_segments:
            await query.edit_message_text(
                "Груп ще немає. Спочатку створіть групу через меню.", reply_markup=_menu_back_keyboard()
            )
            return
        await query.edit_message_text("Оберіть групу:", reply_markup=_bc_group_keyboard())
        return

    if data.startswith("bc_pickgroup:"):
        _, seg = data.split(":", 1)
        clients = _get_admin_clients(admin_id, segment=seg)
        if not clients:
            await query.edit_message_text(
                f"У вас немає власних клієнтів у групі «{seg}» 🙈 Спочатку призначте собі клієнтів "
                f"у цій групі (меню → «📋 Список клієнтів і відповідальних»).",
                reply_markup=_menu_back_keyboard(),
            )
            return
        BC_PENDING.setdefault(admin_id, {})["group_for_selection"] = seg
        buttons = [
            [InlineKeyboardButton(f"📢 Усій групі ({len(clients)} осіб)", callback_data="bc_wholegroup")],
            [InlineKeyboardButton("🎯 Обрати конкретних", callback_data="bc_selectingroup")],
            [InlineKeyboardButton("🔙 До меню", callback_data="menu_back")],
        ]
        await query.edit_message_text(
            f"Група «{seg}» — {len(clients)} ваших клієнтів. Кому надіслати?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data == "bc_wholegroup":
        seg = BC_PENDING.get(admin_id, {}).get("group_for_selection")
        clients = _get_admin_clients(admin_id, segment=seg)
        BC_PENDING.setdefault(admin_id, {})["target_type"] = "selected"
        BC_PENDING[admin_id]["target_value"] = ",".join(str(c["chat_id"]) for c in clients)
        await query.edit_message_text("Коли надіслати?", reply_markup=_bc_timing_keyboard())
        return

    if data == "bc_selectingroup":
        seg = BC_PENDING.get(admin_id, {}).get("group_for_selection")
        clients = _get_admin_clients(admin_id, segment=seg)
        BC_PENDING.setdefault(admin_id, {})["target_type"] = "selected"
        BC_SELECTIONS[admin_id] = set()
        context.chat_data["bc_clients"] = clients
        await query.edit_message_text(
            f"Ваші клієнти в групі «{seg}» — оберіть, кому надіслати (тапніть, щоб додати/прибрати):",
            reply_markup=_bc_select_keyboard(admin_id, clients),
        )
        return

    if data == "bc_target_selected":
        clients = _get_admin_clients(admin_id)
        if not clients:
            await query.edit_message_text(
                "У вас поки немає закріплених клієнтів 🙈 Спочатку призначте собі клієнтів "
                "(меню → «📋 Список клієнтів і відповідальних»).",
                reply_markup=_menu_back_keyboard(),
            )
            return
        BC_PENDING.setdefault(admin_id, {})["target_type"] = "selected"
        BC_SELECTIONS[admin_id] = set()
        context.chat_data["bc_clients"] = clients
        await query.edit_message_text(
            "Оберіть людей (тапніть, щоб додати/прибрати позначку):",
            reply_markup=_bc_select_keyboard(admin_id, clients),
        )
        return

    if data.startswith("bc_toggle:"):
        _, chat_id_str = data.split(":", 1)
        chat_id = int(chat_id_str)
        selected = BC_SELECTIONS.setdefault(admin_id, set())
        selected.discard(chat_id) if chat_id in selected else selected.add(chat_id)
        clients = context.chat_data.get("bc_clients", [])
        await query.edit_message_reply_markup(reply_markup=_bc_select_keyboard(admin_id, clients))
        return

    if data == "bc_showall":
        conn = db()
        rows = conn.execute(
            "SELECT chat_id, name, username FROM subscribers WHERE active=1 ORDER BY joined_at DESC LIMIT 200"
        ).fetchall()
        conn.close()
        context.chat_data["bc_clients"] = [dict(r) for r in rows]
        await query.edit_message_reply_markup(reply_markup=_bc_select_keyboard(admin_id, context.chat_data["bc_clients"]))
        return

    if data == "bc_selectdone":
        selected = BC_SELECTIONS.get(admin_id, set())
        if not selected:
            await query.answer("Оберіть хоча б одну людину.", show_alert=True)
            return
        BC_PENDING.setdefault(admin_id, {})["target_value"] = ",".join(str(x) for x in selected)
        await query.edit_message_text("Коли надіслати?", reply_markup=_bc_timing_keyboard())
        return

    if data == "bc_timing_once":
        BC_PENDING.setdefault(admin_id, {})["kind"] = "once"
        await query.edit_message_text("Оберіть дату:", reply_markup=_bc_date_keyboard())
        return

    if data == "bc_timing_weekly":
        BC_PENDING.setdefault(admin_id, {})["kind"] = "weekly"
        await query.edit_message_text("Оберіть день тижня:", reply_markup=_bc_weekday_keyboard())
        return

    if data.startswith("bc_date:"):
        _, date_str = data.split(":", 1)
        BC_PENDING.setdefault(admin_id, {})["date"] = date_str
        await query.edit_message_text("Оберіть годину:", reply_markup=_bc_hour_keyboard())
        return

    if data.startswith("bc_weekday:"):
        _, wd_str = data.split(":", 1)
        BC_PENDING.setdefault(admin_id, {})["weekday"] = int(wd_str)
        await query.edit_message_text("Оберіть годину:", reply_markup=_bc_hour_keyboard())
        return

    if data.startswith("bc_hour:"):
        _, hh_str = data.split(":", 1)
        hh = int(hh_str)
        BC_PENDING.setdefault(admin_id, {})["hour"] = hh
        await query.edit_message_text("Оберіть хвилини:", reply_markup=_bc_minute_keyboard(hh))
        return

    if data.startswith("bc_min:"):
        _, mm_str = data.split(":", 1)
        pending = BC_PENDING.setdefault(admin_id, {})
        pending["minute"] = int(mm_str)
        MENU_PENDING[admin_id] = {"action": "bc_name"}
        await query.edit_message_text(
            "Дайте цій розсилці коротку назву для себе (клієнти її не побачать, це лише для вашого списку):"
        )
        return

    if data == "bc_cancellist":
        conn = db()
        rows = conn.execute(
            "SELECT * FROM broadcasts WHERE active=1 AND created_by=? ORDER BY id DESC", (admin_id,)
        ).fetchall()
        conn.close()
        if not rows:
            await query.edit_message_text("У вас немає активних запланованих розсилок.", reply_markup=_menu_back_keyboard())
            return
        BC_CANCEL_SELECTIONS[admin_id] = set()
        context.chat_data["bc_cancel_rows"] = [dict(r) for r in rows]
        await query.edit_message_text(
            "Оберіть, які розсилки скасувати (тапніть, щоб позначити одну чи кілька):",
            reply_markup=_bc_cancel_keyboard(admin_id, context.chat_data["bc_cancel_rows"]),
        )
        return

    if data.startswith("bc_canceltoggle:"):
        _, bid_str = data.split(":", 1)
        bid = int(bid_str)
        selected = BC_CANCEL_SELECTIONS.setdefault(admin_id, set())
        selected.discard(bid) if bid in selected else selected.add(bid)
        rows = context.chat_data.get("bc_cancel_rows", [])
        await query.edit_message_reply_markup(reply_markup=_bc_cancel_keyboard(admin_id, rows))
        return

    if data == "bc_cancelconfirmall":
        selected = BC_CANCEL_SELECTIONS.get(admin_id, set())
        if not selected:
            await query.answer("Оберіть хоча б одну розсилку.", show_alert=True)
            return
        conn = db()
        count = 0
        for bid in selected:
            row = conn.execute("SELECT id FROM broadcasts WHERE id=? AND created_by=?", (bid, admin_id)).fetchone()
            if row:
                conn.execute("UPDATE broadcasts SET active=0 WHERE id=?", (bid,))
                count += 1
                for job in context.application.job_queue.get_jobs_by_name(_bc_job_name(bid)):
                    job.schedule_removal()
        conn.commit()
        conn.close()
        BC_CANCEL_SELECTIONS.pop(admin_id, None)
        await query.edit_message_text(f"✅ Скасовано розсилок: {count}.", reply_markup=_menu_back_keyboard())
        return

    if data == "bc_viewlist":
        conn = db()
        rows = conn.execute(
            "SELECT * FROM broadcasts WHERE active=1 AND created_by=? ORDER BY id DESC", (admin_id,)
        ).fetchall()
        conn.close()
        if not rows:
            await query.edit_message_text("У вас немає активних запланованих розсилок.", reply_markup=_menu_back_keyboard())
            return
        text = "Ваші заплановані розсилки:\n\n"
        for r in rows:
            marker = "❓ Запитання так/ні" if r["is_question"] else "📌"
            name_line = f"   Назва: {r['name']}\n" if r["name"] else ""
            text += (
                f"{marker} Кому: {_bc_target_label(r['target_type'], r['target_value'])}\n"
                f"{name_line}"
                f"   Коли: {_bc_timing_label(r)}\n"
                f"   Текст: {r['message']}\n\n"
            )
        await query.edit_message_text(text, reply_markup=_menu_back_keyboard())
        return


# ---------- Майстер "Запитати так/ні" з персональним посиланням на відповідального ----------

def _qna_target_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📁 По групі", callback_data="qna_target_group")],
        [InlineKeyboardButton("🎯 Обраним людям", callback_data="qna_target_selected")],
        [InlineKeyboardButton("🔙 До меню", callback_data="menu_back")],
    ])


def _qna_group_keyboard() -> InlineKeyboardMarkup:
    conn = db()
    rows = conn.execute("SELECT name FROM segments").fetchall()
    conn.close()
    buttons = [[InlineKeyboardButton(r["name"], callback_data=f"qna_pickgroup:{r['name']}")] for r in rows]
    buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
    return InlineKeyboardMarkup(buttons)


def _qna_select_keyboard(admin_id: int, clients: list) -> InlineKeyboardMarkup:
    selected = QNA_SELECTIONS.get(admin_id, set())
    buttons = []
    for c in clients:
        mark = "✅" if c["chat_id"] in selected else "⬜"
        buttons.append([InlineKeyboardButton(
            f"{mark} {_client_display_label(c['chat_id'], c['name'], c['username'])}", callback_data=f"qna_toggle:{c['chat_id']}"
        )])
    buttons.append([
        InlineKeyboardButton("⬅️ Назад", callback_data="qna_backtotarget"),
        InlineKeyboardButton("➡️ Далі", callback_data="qna_selectdone"),
    ])
    buttons.append([InlineKeyboardButton("❌ Скасувати", callback_data="menu_back")])
    return InlineKeyboardMarkup(buttons)


async def qna_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    data = query.data

    if data == "qna_start":
        QNA_PENDING[admin_id] = {}
        QNA_SELECTIONS.pop(admin_id, None)
        await query.edit_message_text("Кому поставити запитання?", reply_markup=_qna_target_keyboard())
        return

    if data == "qna_backtotarget":
        QNA_PENDING[admin_id] = {}
        QNA_SELECTIONS.pop(admin_id, None)
        await query.edit_message_text("Кому поставити запитання?", reply_markup=_qna_target_keyboard())
        return

    if data == "qna_target_group":
        conn = db()
        has_segments = conn.execute("SELECT COUNT(*) c FROM segments").fetchone()["c"]
        conn.close()
        if not has_segments:
            await query.edit_message_text("Груп ще немає.", reply_markup=_menu_back_keyboard())
            return
        await query.edit_message_text("Оберіть групу:", reply_markup=_qna_group_keyboard())
        return

    if data.startswith("qna_pickgroup:"):
        _, seg = data.split(":", 1)
        clients = _get_admin_clients(admin_id, segment=seg)
        if not clients:
            await query.edit_message_text(
                f"У вас немає власних клієнтів у групі «{seg}» 🙈 Спочатку призначте собі клієнтів "
                f"у цій групі (меню → «📋 Список клієнтів і відповідальних»).",
                reply_markup=_menu_back_keyboard(),
            )
            return
        QNA_PENDING.setdefault(admin_id, {})["group_for_selection"] = seg
        buttons = [
            [InlineKeyboardButton(f"📢 Усій групі ({len(clients)} осіб)", callback_data="qna_wholegroup")],
            [InlineKeyboardButton("🎯 Обрати конкретних", callback_data="qna_selectingroup")],
            [InlineKeyboardButton("🔙 До меню", callback_data="menu_back")],
        ]
        await query.edit_message_text(
            f"Група «{seg}» — {len(clients)} ваших клієнтів. Кому поставити запитання?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data == "qna_wholegroup":
        seg = QNA_PENDING.get(admin_id, {}).get("group_for_selection")
        clients = _get_admin_clients(admin_id, segment=seg)
        QNA_PENDING.setdefault(admin_id, {})["target_type"] = "selected"
        QNA_PENDING[admin_id]["target_value"] = ",".join(str(c["chat_id"]) for c in clients)
        MENU_PENDING[admin_id] = {"action": "qna_text"}
        await query.edit_message_text(
            "Напишіть текст запитання (наприклад: «Завтра доставка, потрібна?») — або надішліть файл із підписом:"
        )
        return

    if data == "qna_selectingroup":
        seg = QNA_PENDING.get(admin_id, {}).get("group_for_selection")
        clients = _get_admin_clients(admin_id, segment=seg)
        QNA_PENDING.setdefault(admin_id, {})["target_type"] = "selected"
        QNA_SELECTIONS[admin_id] = set()
        context.chat_data["qna_clients"] = clients
        await query.edit_message_text(
            f"Ваші клієнти в групі «{seg}» — оберіть, кому поставити запитання:",
            reply_markup=_qna_select_keyboard(admin_id, clients),
        )
        return

    if data == "qna_target_selected":
        clients = _get_admin_clients(admin_id)
        if not clients:
            await query.edit_message_text(
                "У вас поки немає закріплених клієнтів 🙈 Спочатку призначте собі клієнтів "
                "(меню → «📋 Список клієнтів і відповідальних»).",
                reply_markup=_menu_back_keyboard(),
            )
            return
        QNA_PENDING.setdefault(admin_id, {})["target_type"] = "selected"
        QNA_SELECTIONS[admin_id] = set()
        context.chat_data["qna_clients"] = clients
        await query.edit_message_text(
            "Оберіть людей (тапніть, щоб додати/прибрати позначку):",
            reply_markup=_qna_select_keyboard(admin_id, clients),
        )
        return

    if data.startswith("qna_toggle:"):
        _, chat_id_str = data.split(":", 1)
        chat_id = int(chat_id_str)
        selected = QNA_SELECTIONS.setdefault(admin_id, set())
        selected.discard(chat_id) if chat_id in selected else selected.add(chat_id)
        clients = context.chat_data.get("qna_clients", [])
        await query.edit_message_reply_markup(reply_markup=_qna_select_keyboard(admin_id, clients))
        return

    if data == "qna_selectdone":
        selected = QNA_SELECTIONS.get(admin_id, set())
        if not selected:
            await query.answer("Оберіть хоча б одну людину.", show_alert=True)
            return
        QNA_PENDING.setdefault(admin_id, {})["target_value"] = ",".join(str(x) for x in selected)
        MENU_PENDING[admin_id] = {"action": "qna_text"}
        await query.edit_message_text(
            "Напишіть текст запитання (наприклад: «Завтра доставка, потрібна?») — або надішліть файл із підписом:"
        )
        return

    if data.startswith("qna_no:"):
        _, client_chat_id_str = data.split(":", 1)
        client_chat_id = int(client_chat_id_str)
        await query.edit_message_text("Дякуємо, що дали знати! 🙏 Якщо передумаєте — завжди можна написати нам.")
        conn = db()
        row = conn.execute(
            "SELECT name, username, responsible_admin FROM subscribers WHERE chat_id=?", (client_chat_id,)
        ).fetchone()
        conn.close()
        if row:
            target_admin = row["responsible_admin"] or (ADMIN_IDS[0] if ADMIN_IDS else None)
            note = f"❌ {row['name']} (@{row['username'] or '—'}) відповів(-ла) «Ні» на запитання."
            if target_admin:
                try:
                    await context.bot.send_message(target_admin, note)
                except Exception as e:
                    logger.warning(f"Не вдалось сповістити відповідального {target_admin}: {e}")
        return


async def qna_send(context: ContextTypes.DEFAULT_TYPE, admin_id: int, question_text: str, file_id: str | None = None):
    pending = QNA_PENDING.pop(admin_id, {})
    QNA_SELECTIONS.pop(admin_id, None)
    target_type = pending.get("target_type")
    target_value = pending.get("target_value")
    if not target_type or not target_value:
        await context.bot.send_message(admin_id, "Щось пішло не так, спробуйте ще раз через меню.")
        return

    conn = db()
    if target_type == "segment":
        rows = conn.execute(
            "SELECT chat_id, responsible_admin FROM subscribers WHERE segment=? AND active=1", (target_value,)
        ).fetchall()
    else:
        ids = [int(x) for x in target_value.split(",") if x]
        rows = conn.execute(
            f"SELECT chat_id, responsible_admin FROM subscribers WHERE chat_id IN "
            f"({','.join('?' * len(ids))})", ids
        ).fetchall()
    conn.close()

    full_text = question_text + QNA_CLARIFICATION
    fallback_admin = ADMIN_IDS[0] if ADMIN_IDS else None
    sent, failed = 0, 0
    for r in rows:
        resp_admin = r["responsible_admin"] or fallback_admin
        yes_button = _admin_link_button(resp_admin, YES_BUTTON_LABEL) if resp_admin else None
        buttons = [
            [InlineKeyboardButton(ORDER_VIA_BOT_LABEL, callback_data="qna_order")],
        ]
        if yes_button:
            buttons.append([yes_button])
        buttons.append([InlineKeyboardButton("❌ Ні", callback_data=f"qna_no:{r['chat_id']}")])
        try:
            if file_id:
                await context.bot.send_document(
                    r["chat_id"], file_id, caption=full_text, reply_markup=InlineKeyboardMarkup(buttons)
                )
            else:
                await context.bot.send_message(r["chat_id"], full_text, reply_markup=InlineKeyboardMarkup(buttons))
            sent += 1
        except Exception as e:
            logger.warning(f"Не вдалось надіслати {r['chat_id']}: {e}")
            failed += 1

    await context.bot.send_message(admin_id, f"✅ Запитання надіслано. Отримали: {sent}, помилок: {failed}.")


# ---------- Автоматична розсилка по сегменту (виклик за розкладом) ----------

async def send_segment_broadcast(context: ContextTypes.DEFAULT_TYPE):
    segment = context.job.data["segment"]
    logger.info(f"[FIRE] Спрацювало завдання розсилки для сегмента «{segment}» о {datetime.now(TZ)}")
    conn = db()
    seg_row = conn.execute("SELECT message FROM segments WHERE name=?", (segment,)).fetchone()
    if not seg_row or not seg_row["message"]:
        conn.close()
        if ADMIN_IDS:
            await notify_admins(context, f"⚠️ Розсилка для сегмента «{segment}» не відправлена — не задано текст (/setmsg).")
        return
    rows = conn.execute("SELECT chat_id FROM subscribers WHERE segment=? AND active=1", (segment,)).fetchall()
    conn.close()

    sent, failed = 0, 0
    for r in rows:
        try:
            await context.bot.send_message(r["chat_id"], seg_row["message"], reply_markup=_keyboard_for_recipient(r["chat_id"]))
            sent += 1
        except Exception as e:
            logger.warning(f"Не вдалось надіслати {r['chat_id']}: {e}")
            failed += 1

    if ADMIN_IDS:
        await notify_admins(context, f"✅ Автоматична розсилка сегменту «{segment}» виконана. Надіслано: {sent}, помилок: {failed}")


def _job_name(segment: str, kind: str, weekday: int, hh: int, mm: int) -> str:
    return f"segment_{segment}_{kind}_{weekday}_{hh:02d}{mm:02d}"


def create_segment_job(application: Application, segment: str, kind: str, weekday: int, hh: int, mm: int):
    """Створює (або перестворює) ОДНЕ конкретне завдання розсилки для сегмента.
    kind: 'weekly' (щотижня, weekday=0-6), 'biweekly' (раз на 2 тижні, weekday=0-6),
    'monthly' (раз на місяць, weekday тут означає день місяця 1-28)."""
    name = _job_name(segment, kind, weekday, hh, mm)
    for job in application.job_queue.get_jobs_by_name(name):
        job.schedule_removal()

    if kind == "monthly":
        new_job = application.job_queue.run_monthly(
            send_segment_broadcast,
            when=time(hour=hh, minute=mm, tzinfo=TZ),
            day=weekday,
            name=name,
            data={"segment": segment},
        )
    elif kind == "biweekly":
        now = datetime.now(TZ)
        days_ahead = (weekday - now.weekday()) % 7
        first_run = (now + timedelta(days=days_ahead)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if first_run <= now:
            first_run += timedelta(days=7)
        new_job = application.job_queue.run_repeating(
            send_segment_broadcast,
            interval=timedelta(weeks=2),
            first=first_run,
            name=name,
            data={"segment": segment},
        )
    else:  # weekly
        new_job = application.job_queue.run_daily(
            send_segment_broadcast,
            time=time(hour=hh, minute=mm, tzinfo=TZ),
            days=(weekday,),
            name=name,
            data={"segment": segment},
        )
    logger.info(
        f"[SCHEDULE] Створено завдання {name}: kind={kind}, weekday/day={weekday}, "
        f"time={hh:02d}:{mm:02d} TZ={TZ}. Наступний запуск: {getattr(new_job, 'next_t', '—')}"
    )


def remove_all_segment_jobs(application: Application, segment: str) -> int:
    """Видаляє УСІ заплановані завдання для сегмента (використовується в /setschedule, який замінює розклад повністю)."""
    prefix = f"segment_{segment}_"
    jobs = [j for j in application.job_queue.jobs() if j.name and j.name.startswith(prefix)]
    for j in jobs:
        j.schedule_removal()
    return len(jobs)


async def load_schedules_on_startup(application: Application):
    conn = db()
    rows = conn.execute("SELECT segment, weekday, hhmm, kind FROM schedules").fetchall()
    conn.close()
    for r in rows:
        hh, mm = map(int, r["hhmm"].split(":"))
        kind = r["kind"] or "weekly"
        create_segment_job(application, r["segment"], kind, r["weekday"], hh, mm)
        logger.info(f"Заплановано розсилку для сегмента {r['segment']}: {kind} {r['weekday']} {r['hhmm']}")


# ---------- Заплановані розсилки (майстер: кому / коли / текст) ----------

def _bc_job_name(broadcast_id: int) -> str:
    return f"bcast_{broadcast_id}"


def _bc_name_prefix(row) -> str:
    try:
        name = row["name"]
    except (KeyError, IndexError):
        name = None
    return f"🏷 {name} — " if name else ""


def _bc_target_label(target_type: str, target_value: str) -> str:
    if target_type == "segment":
        return f"група «{target_value}»"
    count = len([x for x in target_value.split(",") if x])
    return f"обрані ({count} осіб)"


def _bc_timing_label(row) -> str:
    if row["kind"] == "once":
        dt = datetime.fromisoformat(row["run_at"])
        return f"одноразово, {dt.strftime('%d.%m.%Y %H:%M')}"
    return f"щотижня, {WEEKDAYS_UA[row['weekday']]} о {row['hhmm']}"


YES_BUTTON_LABEL = "💬 Написати менеджеру"
ORDER_VIA_BOT_LABEL = "📝 Замовити тут"
QNA_CLARIFICATION = "\n\n👇 Оберіть один із варіантів нижче:"


async def send_scheduled_broadcast(context: ContextTypes.DEFAULT_TYPE):
    broadcast_id = context.job.data["broadcast_id"]
    conn = db()
    row = conn.execute("SELECT * FROM broadcasts WHERE id=? AND active=1", (broadcast_id,)).fetchone()
    if not row:
        conn.close()
        return

    is_question = bool(row["is_question"]) if "is_question" in row.keys() else False

    if row["target_type"] == "segment":
        recip_rows = conn.execute(
            "SELECT chat_id, responsible_admin FROM subscribers WHERE segment=? AND active=1",
            (row["target_value"],),
        ).fetchall()
    else:
        ids = [int(x) for x in row["target_value"].split(",") if x]
        recip_rows = conn.execute(
            f"SELECT chat_id, responsible_admin FROM subscribers WHERE chat_id IN "
            f"({','.join('?' * len(ids))})", ids
        ).fetchall() if ids else []

    if row["kind"] == "once":
        conn.execute("UPDATE broadcasts SET active=0 WHERE id=?", (broadcast_id,))
        conn.commit()
    conn.close()

    fallback_admin = ADMIN_IDS[0] if ADMIN_IDS else None
    file_id = row["file_id"] if "file_id" in row.keys() else None
    sent, failed = 0, 0
    for r in recip_rows:
        try:
            if is_question:
                resp_admin = r["responsible_admin"] or fallback_admin
                yes_button = _admin_link_button(resp_admin, YES_BUTTON_LABEL) if resp_admin else None
                buttons = [[InlineKeyboardButton(ORDER_VIA_BOT_LABEL, callback_data="qna_order")]]
                if yes_button:
                    buttons.append([yes_button])
                buttons.append([InlineKeyboardButton("❌ Ні", callback_data=f"qna_no:{r['chat_id']}")])
                question_text = (row["message"] or "") + QNA_CLARIFICATION
                if file_id:
                    await context.bot.send_document(
                        r["chat_id"], file_id, caption=question_text, reply_markup=InlineKeyboardMarkup(buttons)
                    )
                else:
                    await context.bot.send_message(
                        r["chat_id"], question_text, reply_markup=InlineKeyboardMarkup(buttons)
                    )
            else:
                if file_id:
                    await context.bot.send_document(
                        r["chat_id"], file_id, caption=row["message"] or None, reply_markup=_keyboard_for_recipient(r["chat_id"])
                    )
                else:
                    await context.bot.send_message(
                        r["chat_id"], row["message"], reply_markup=_keyboard_for_recipient(r["chat_id"])
                    )
            sent += 1
        except Exception as e:
            logger.warning(f"Не вдалось надіслати {r['chat_id']}: {e}")
            failed += 1

    created_by = row["created_by"] if "created_by" in row.keys() else None
    notify_target = created_by or (ADMIN_IDS[0] if ADMIN_IDS else None)
    if notify_target:
        label = "запитання" if is_question else "розсилка"
        try:
            await context.bot.send_message(
                notify_target,
                f"✅ Запланован{'е' if is_question else 'а'} {label} "
                f"({_bc_target_label(row['target_type'], row['target_value'])}) "
                f"виконан{'о' if is_question else 'а'}. Надіслано: {sent}, помилок: {failed}.",
            )
        except Exception as e:
            logger.warning(f"Не вдалось надіслати підсумок розсилки адміну {notify_target}: {e}")


def schedule_broadcast_job(application: Application, row) -> None:
    name = _bc_job_name(row["id"])
    for job in application.job_queue.get_jobs_by_name(name):
        job.schedule_removal()

    if row["kind"] == "once":
        run_at = datetime.fromisoformat(row["run_at"])
        application.job_queue.run_once(
            send_scheduled_broadcast, when=run_at, name=name, data={"broadcast_id": row["id"]}
        )
    else:
        hh, mm = map(int, row["hhmm"].split(":"))
        application.job_queue.run_daily(
            send_scheduled_broadcast,
            time=time(hour=hh, minute=mm, tzinfo=TZ),
            days=(row["weekday"],),
            name=name,
            data={"broadcast_id": row["id"]},
        )


async def load_broadcasts_on_startup(application: Application):
    conn = db()
    rows = conn.execute("SELECT * FROM broadcasts WHERE active=1").fetchall()
    now = datetime.now(TZ)
    for r in rows:
        if r["kind"] == "once" and datetime.fromisoformat(r["run_at"]) <= now:
            # Час одноразової розсилки вже минув, поки бот не працював — прибираємо, щоб не спрацювала заднім числом
            conn.execute("UPDATE broadcasts SET active=0 WHERE id=?", (r["id"],))
            continue
        schedule_broadcast_job(application, r)
    conn.commit()
    conn.close()


async def testsegment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Негайно запускає розсилку для сегмента — для перевірки тексту й доставки без очікування розкладу."""
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Використання: /testsegment сегмент\nПриклад: /testsegment staff")
        return
    segment = context.args[0]
    fake_job = type("FakeJob", (), {"data": {"segment": segment}})()
    fake_context = type("FakeContext", (), {"job": fake_job, "bot": context.bot})()
    await update.message.reply_text(f"Запускаю тестову розсилку для «{segment}» прямо зараз...")
    await send_segment_broadcast(fake_context)


async def jobs_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    jobs = context.application.job_queue.jobs()
    if not jobs:
        await update.message.reply_text("Заплановних завдань немає.")
        return
    text = f"Поточний час сервера: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}\n\nЗавдання:\n\n"
    for j in jobs:
        next_run = j.next_t.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S %Z") if j.next_t else "не заплановано"
        text += f"• {j.name} — наступний запуск: {next_run}\n"
    await update.message.reply_text(text)


async def diag_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Діагностика: показує останні збережені замовлення в базі (для перевірки функції «Повторити»)."""
    if not is_admin(update):
        return
    conn = db()
    rows = conn.execute(
        "SELECT id, chat_id, address, payment_method, items_json, created_at FROM orders ORDER BY id DESC LIMIT 10"
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text(
            "У таблиці orders поки що НЕМАЄ жодного запису. Це означає, що новий код "
            "ще не задеплоївся на Railway, або жодне замовлення ще не пройшло крок оплати."
        )
        return
    text = f"Останні {len(rows)} замовлень у базі:\n\n"
    for r in rows:
        try:
            item_count = len(json.loads(r["items_json"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            item_count = "?"
        text += (
            f"#{r['id']} — chat_id: {r['chat_id']} — {item_count} позицій — "
            f"{r['payment_method'] or '—'} — {r['created_at']}\n"
        )
    await update.message.reply_text(text)


# ---------- Довідка ----------

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команди: /start — підписатись, /stop — відписатись.")
        return
    await update.message.reply_text(
        "Команди адміністратора:\n\n"
        "/addsegment назва — створити сегмент клієнтів\n"
        "/setschedule сегмент день ГГ:ХХ — ЗАМІНИТИ весь розклад сегмента одним записом (mon..sun)\n"
        "/addschedule сегмент день ГГ:ХХ — ДОДАТИ ще один щотижневий час розсилки, не видаляючи наявні\n"
        "/addschedulebiweekly сегмент день ГГ:ХХ — розсилка раз на 2 тижні\n"
        "/addschedulemonthly сегмент день_місяця ГГ:ХХ — розсилка раз на місяць (день 1-28)\n"
        "/removeschedule сегмент день ГГ:ХХ — видалити один конкретний запис розкладу\n"
        "/setmsg сегмент текст — задати повідомлення для автоматичної розсилки сегмента\n"
        "/segments — список сегментів, усіх їхніх розкладів і повідомлень\n"
        "/clients — список активних підписників\n"
        "/setsegment @юзернейм_або_chat_id сегмент — будь-коли перепризначити клієнту сегмент\n"
        "/broadcast текст — надіслати повідомлення ОДРАЗУ всім підписникам (акції, зміни цін)\n"
        "/broadcastfile — надішліть PDF (або інший файл) боту з підписом, що починається на "
        "«/broadcastfile ваш текст», і він одразу розійде цей файл усім підписникам\n"
        "/jobs — перевірити заплановані розсилки і час наступного запуску (діагностика)\n"
        "/pendingorders — замовлення, що клієнти почали оформлювати, але ще не підтвердили\n"
        "/testsegment сегмент — надіслати розсилку сегмента ПРЯМО ЗАРАЗ, без очікування розкладу (для перевірки)\n"
        "/sendto — обрати конкретних людей зі списку (кнопками) і надіслати їм окреме повідомлення\n"
        "/menu — відкрити меню з кнопками українською (усі дії без потреби набирати команди)\n"
        "\nУ меню також доступні: призначення відповідального адміна клієнту, "
        "запитання «так/ні» з персональним посиланням на відповідального, планування розсилок на дату/день, "
        "заповнення картки нового клієнта одразу після підписки, перегляд/редагування/видалення своїх "
        "повідомлень в історії листування з клієнтом (кнопка «💬 Історія повідомлень» у картці клієнта), "
        "та скасування одразу кількох запланованих розсилок.\n"
    )


async def load_all_on_startup(application: Application):
    await load_schedules_on_startup(application)
    await load_broadcasts_on_startup(application)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задано BOT_TOKEN у .env")
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS не задано — сповіщення адмінам не працюватимуть")

    init_db()

    application = Application.builder().token(BOT_TOKEN).post_init(load_all_on_startup).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("addsegment", addsegment))
    application.add_handler(CommandHandler("segments", segments_list))
    application.add_handler(CommandHandler("setschedule", setschedule))
    application.add_handler(CommandHandler("addschedule", addschedule))
    application.add_handler(CommandHandler("addschedulebiweekly", addschedulebiweekly))
    application.add_handler(CommandHandler("addschedulemonthly", addschedulemonthly))
    application.add_handler(CommandHandler("removeschedule", removeschedule))
    application.add_handler(CommandHandler("setmsg", setmsg))
    application.add_handler(CommandHandler("clients", clients_list))
    application.add_handler(CommandHandler("resplist", resplist_command))
    application.add_handler(CommandHandler("bulkresp", bulkresp_command))
    application.add_handler(CommandHandler("setsegment", setsegment))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(
        MessageHandler(filters.Document.ALL, handle_admin_document)
    )
    application.add_handler(CommandHandler("jobs", jobs_list))
    application.add_handler(CommandHandler("diagorders", diag_orders))
    application.add_handler(CommandHandler("pendingorders", pending_orders_command))
    application.add_handler(CommandHandler("testsegment", testsegment))
    application.add_handler(CommandHandler("sendto", sendto_start))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(MessageHandler(filters.Regex("^📋 Меню$"), menu_command))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CLIENT_BTN_ASSORTMENT)}$"), client_show_assortment))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CLIENT_BTN_DELIVERY)}$"), client_show_delivery))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CLIENT_BTN_MANAGER)}$"), client_contact_manager))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CLIENT_BTN_ORDER)}$"), order_start))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CLIENT_BTN_PROFILE)}$"), profile_show))
    application.add_handler(CallbackQueryHandler(profile_edit_callback, pattern=r"^profile_edit$"))
    application.add_handler(CallbackQueryHandler(profile_editfield_callback, pattern=r"^profile_editfield:"))
    application.add_handler(CallbackQueryHandler(profile_zonefield_callback, pattern=r"^profile_zonefield:"))
    application.add_handler(CallbackQueryHandler(profile_zone_callback, pattern=r"^profile_zone:"))
    application.add_handler(CallbackQueryHandler(profile_addaddr_callback, pattern=r"^profile_addaddr$"))
    application.add_handler(CallbackQueryHandler(profile_deladdr_callback, pattern=r"^profile_deladdr:"))
    application.add_handler(CallbackQueryHandler(profile_editaddr_callback, pattern=r"^profile_editaddr:"))
    application.add_handler(CallbackQueryHandler(profile_cancelstep_callback, pattern=r"^profile_cancelstep$"))
    application.add_handler(CallbackQueryHandler(adminprofile_cancelstep_callback, pattern=r"^adminprofile_cancelstep$"))
    application.add_handler(CallbackQueryHandler(addraddr_samephone_callback, pattern=r"^addraddr_samephone$"))
    application.add_handler(CallbackQueryHandler(adminprofile_edit_callback, pattern=r"^adminprofile_edit:"))
    application.add_handler(CallbackQueryHandler(adminprofile_editfield_callback, pattern=r"^adminprofile_editfield:"))
    application.add_handler(CallbackQueryHandler(adminprofile_zonefield_callback, pattern=r"^adminprofile_zonefield:"))
    application.add_handler(CallbackQueryHandler(adminprofile_addaddr_callback, pattern=r"^adminprofile_addaddr:"))
    application.add_handler(CallbackQueryHandler(adminprofile_deladdr_callback, pattern=r"^adminprofile_deladdr:"))
    application.add_handler(CallbackQueryHandler(adminprofile_newwizard_callback, pattern=r"^adminprofile_newwizard:"))
    application.add_handler(CallbackQueryHandler(adminprofile_newzone_callback, pattern=r"^adminprofile_newzone:"))
    application.add_handler(CallbackQueryHandler(adminprofile_newskip_callback, pattern=r"^adminprofile_newskip$"))
    application.add_handler(CallbackQueryHandler(adminmsglog_callback, pattern=r"^adminmsglog:"))
    application.add_handler(CallbackQueryHandler(adminmsgedit_callback, pattern=r"^adminmsgedit:"))
    application.add_handler(CallbackQueryHandler(adminmsgdel_callback, pattern=r"^adminmsgdel:"))
    application.add_handler(CallbackQueryHandler(adminprofile_editaddr_callback, pattern=r"^adminprofile_editaddr:"))
    application.add_handler(CallbackQueryHandler(client_assortment_category_callback, pattern=r"^clientassort:"))
    application.add_handler(CallbackQueryHandler(client_assortment_back_callback, pattern=r"^clientassort_back$"))
    application.add_handler(CallbackQueryHandler(client_delivery_category_callback, pattern=r"^clientdelivery:"))
    application.add_handler(CallbackQueryHandler(client_delivery_back_callback, pattern=r"^clientdelivery_back$"))
    application.add_handler(CallbackQueryHandler(sendto_toggle_callback, pattern=r"^sendto_toggle:"))
    application.add_handler(CallbackQueryHandler(sendto_showall_callback, pattern=r"^sendto_showall$"))
    application.add_handler(CallbackQueryHandler(sendto_done_callback, pattern=r"^sendto_done$"))
    application.add_handler(CallbackQueryHandler(sendto_cancel_callback, pattern=r"^sendto_cancel$"))
    application.add_handler(CallbackQueryHandler(assign_callback, pattern=r"^assign:"))
    application.add_handler(CallbackQueryHandler(pickresp_callback, pattern=r"^pickresp:"))
    application.add_handler(CallbackQueryHandler(respme_callback, pattern=r"^respme:"))
    application.add_handler(CallbackQueryHandler(menu_router, pattern=r"^menu_"))
    application.add_handler(CallbackQueryHandler(bc_router, pattern=r"^(bc_|bcq_)"))
    application.add_handler(CallbackQueryHandler(qna_order_callback, pattern=r"^qna_order$"))
    application.add_handler(CallbackQueryHandler(qna_router, pattern=r"^qna_"))
    application.add_handler(CallbackQueryHandler(wg_router, pattern=r"^wg_"))
    application.add_handler(CallbackQueryHandler(order_router, pattern=r"^order_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_text))

    logger.info("Бот запущено")
    application.run_polling()


if __name__ == "__main__":
    main()
