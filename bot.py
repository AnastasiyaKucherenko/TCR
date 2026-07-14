"""
Telegram-бот для ростерії: збір клієнтів через /start, сегменти з власним
щотижневим розкладом розсилки, і команда для миттєвої розсилки всім
(зміни, прайси, акції).

Автор: згенеровано Claude для конкретного кейсу ростерії.
"""

import logging
import os
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

# Стан опитувальника замовлення клієнта: chat_id клієнта -> {"step":..., "point_name":..., ...}
ORDER_PENDING: dict[int, dict] = {}

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
    bc_cols = [r["name"] for r in conn.execute("PRAGMA table_info(broadcasts)").fetchall()]
    if "is_question" not in bc_cols:
        conn.execute("ALTER TABLE broadcasts ADD COLUMN is_question INTEGER DEFAULT 0")
    if "file_id" not in bc_cols:
        conn.execute("ALTER TABLE broadcasts ADD COLUMN file_id TEXT")
    if "created_by" not in bc_cols:
        conn.execute("ALTER TABLE broadcasts ADD COLUMN created_by INTEGER")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
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

    await update.message.reply_text(
        "Привіт! Дякуємо, що підписались на новини нашої ростерії 🫶\n"
        "Тут ви отримуватимете нагадування, інформацію про кавові новинки, акції та зміни в асортименті😉\n\n"
        "Якщо захочете відписатись — просто напишіть /stop.",
        reply_markup=_client_persistent_keyboard(),
    )

    admins_avail = _admins_with_username()
    if len(admins_avail) >= 2:
        buttons = [
            [InlineKeyboardButton(a["name"], callback_data=f"pickresp:{a['chat_id']}")]
            for a in admins_avail
        ]
        await update.message.reply_text(
            "Оберіть, хто з команди буде вашим відповідальним контактом "
            "(до цієї людини вестиме кнопка «Так» у наших повідомленнях):",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    elif len(admins_avail) == 1:
        conn = db()
        conn.execute(
            "UPDATE subscribers SET responsible_admin=? WHERE chat_id=?",
            (admins_avail[0]["chat_id"], chat.id),
        )
        conn.commit()
        conn.close()

    if ADMIN_IDS:
        conn = db()
        segments = [r["name"] for r in conn.execute("SELECT name FROM segments")]
        conn.close()
        buttons = [
            [InlineKeyboardButton(seg, callback_data=f"assign:{chat.id}:{seg}")]
            for seg in segments
        ]
        buttons.append([InlineKeyboardButton("Без сегмента", callback_data=f"assign:{chat.id}:__none__")])
        buttons.append([InlineKeyboardButton("🙋 Я відповідальний", callback_data=f"respme:{chat.id}")])
        await notify_admins(
            context,
            f"🆕 Новий підписник: {user.full_name} (@{user.username or '—'})\n"
            f"chat_id: {chat.id}\nПризначити сегмент:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


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
    """Адмін одним тапом призначає себе відповідальним за клієнта."""
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
    conn.close()
    name = admin_row["name"] if admin_row else "адмін"
    current_text = query.message.text or ""
    await query.edit_message_text(current_text + f"\n\n🙋 Відповідальний: {name}")


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
            "SELECT weekday, hhmm FROM schedules WHERE segment=? ORDER BY weekday, hhmm", (r["name"],)
        ).fetchall()
        if sched_rows:
            schedule_lines = "\n".join(
                f"      • {WEEKDAYS_UA[s['weekday']]} о {s['hhmm']}" for s in sched_rows
            )
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
    """Повністю ЗАМІНЮЄ розклад сегмента на один запис (видаляє всі попередні)."""
    if not is_admin(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Використання: /setschedule сегмент день ГГ:ХХ\n"
            "⚠️ Ця команда ЗАМІНЮЄ весь розклад сегмента одним записом.\n"
            "Якщо потрібно ДОДАТИ ще один час (не видаляючи наявні) — використовуйте /addschedule.\n"
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
        "INSERT INTO schedules (segment, weekday, hhmm) VALUES (?, ?, ?)",
        (name, WEEKDAYS[day_str], time_str),
    )
    conn.commit()
    conn.close()

    remove_all_segment_jobs(context.application, name)
    create_segment_job(context.application, name, WEEKDAYS[day_str], hh, mm)
    await update.message.reply_text(
        f"Готово. Розклад сегмента «{name}» ЗАМІНЕНО одним записом: "
        f"{WEEKDAYS_UA[WEEKDAYS[day_str]]} о {time_str}.\n"
        f"Щоб додати ще один час без видалення цього — /addschedule {name} день ГГ:ХХ"
    )


async def addschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ДОДАЄ ще один час розсилки для сегмента, не видаляючи наявні."""
    if not is_admin(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Використання: /addschedule сегмент день ГГ:ХХ\n"
            "Додає ще один час розсилки для сегмента (наявні розклади лишаються).\n"
            "Приклад: /addschedule wholesale thu 16:00"
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
        "INSERT OR IGNORE INTO schedules (segment, weekday, hhmm) VALUES (?, ?, ?)",
        (name, WEEKDAYS[day_str], time_str),
    )
    conn.commit()
    count = conn.execute("SELECT COUNT(*) c FROM schedules WHERE segment=?", (name,)).fetchone()["c"]
    conn.close()

    create_segment_job(context.application, name, WEEKDAYS[day_str], hh, mm)
    await update.message.reply_text(
        f"Додано. Сегмент «{name}» тепер отримує розсилку у {WEEKDAYS_UA[WEEKDAYS[day_str]]} о {time_str} "
        f"(додатково до вже наявних). Всього записів розкладу: {count}."
    )


async def removeschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Видаляє один конкретний запис розкладу сегмента."""
    if not is_admin(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Використання: /removeschedule сегмент день ГГ:ХХ\n"
            "Видаляє один конкретний час розсилки (інші лишаються).\n"
            "Приклад: /removeschedule wholesale thu 16:00"
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
    conn.execute(
        "DELETE FROM schedules WHERE segment=? AND weekday=? AND hhmm=?",
        (name, WEEKDAYS[day_str], time_str),
    )
    conn.commit()
    conn.close()

    job_name = _job_name(name, WEEKDAYS[day_str], hh, mm)
    for job in context.application.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    await update.message.reply_text(
        f"Видалено запис розкладу для «{name}»: {WEEKDAYS_UA[WEEKDAYS[day_str]]} о {time_str}."
    )


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


def _clients_text() -> str:
    conn = db()
    rows = conn.execute("SELECT * FROM subscribers WHERE active=1 ORDER BY joined_at DESC").fetchall()
    conn.close()
    if not rows:
        return "Поки немає активних підписників."
    text = f"Активних підписників: {len(rows)}\n\n"
    for r in rows[:50]:
        text += f"• {r['name']} (@{r['username'] or '—'}) — сегмент: {r['segment'] or 'немає'}\n"
    if len(rows) > 50:
        text += f"\n...і ще {len(rows) - 50}"
    return text


async def clients_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(_clients_text())


def _clients_with_admin_text() -> str:
    conn = db()
    rows = conn.execute(
        "SELECT s.chat_id, s.name, s.username, s.segment, a.name as admin_name "
        "FROM subscribers s LEFT JOIN admins a ON s.responsible_admin = a.chat_id "
        "WHERE s.active=1 ORDER BY s.joined_at DESC"
    ).fetchall()
    conn.close()
    if not rows:
        return "Поки немає активних підписників."
    text = f"Клієнти та їхні відповідальні ({len(rows)}):\n\n"
    for r in rows[:80]:
        admin_label = r["admin_name"] if r["admin_name"] else "❓ не призначено"
        text += (
            f"• {r['name']} (@{r['username'] or '—'}) — chat_id: {r['chat_id']}\n"
            f"   Група: {r['segment'] or 'немає'} | Відповідальний: {admin_label}\n"
        )
    if len(rows) > 80:
        text += f"\n...і ще {len(rows) - 80}"
    text += (
        "\n\nШвидко призначити відповідального відразу кільком клієнтам:\n"
        "/bulkresp @юзернейм_адміна id1,id2,id3\n"
        "або одразу всій групі:\n"
        "/bulkresp @юзернейм_адміна segment:назва_групи"
    )
    return text


async def resplist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(_clients_with_admin_text())


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
            await context.bot.send_message(r["chat_id"], text, reply_markup=_client_persistent_keyboard())
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
                    chat_id, document.file_id, caption=caption or None, reply_markup=_client_persistent_keyboard()
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
                    r["chat_id"], document.file_id, caption=caption or None, reply_markup=_client_persistent_keyboard()
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
                r["chat_id"], document.file_id, caption=text or None, reply_markup=_client_persistent_keyboard()
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
        label = f"{mark} {c['name']} (@{c['username'] or '—'})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"sendto_toggle:{c['chat_id']}")])
    buttons.append([
        InlineKeyboardButton("✅ Надіслати обраним", callback_data="sendto_done"),
        InlineKeyboardButton("❌ Скасувати", callback_data="sendto_cancel"),
    ])
    return InlineKeyboardMarkup(buttons)


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
    admin_id = update.effective_chat.id
    if not is_admin(update):
        if admin_id in ORDER_PENDING:
            await handle_order_text_step(update, context)
        else:
            await forward_client_text(update, context)
        return

    # 0) Якщо адмін відповідає (Reply) на переслане повідомлення клієнта — надсилаємо відповідь клієнту
    if update.message.reply_to_message:
        key = (admin_id, update.message.reply_to_message.message_id)
        if key in FORWARD_MAP:
            client_chat_id = FORWARD_MAP.pop(key)
            try:
                await context.bot.send_message(client_chat_id, update.message.text)
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
                await context.bot.send_message(chat_id, text, reply_markup=_client_persistent_keyboard())
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
                await context.bot.send_message(r["chat_id"], text, reply_markup=_client_persistent_keyboard())
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
        MENU_PENDING.pop(admin_id, None)
        _set_setting("delivery", text)
        await update.message.reply_text(
            "✅ Умови доставки оновлено! Клієнти одразу побачать нову версію, "
            "коли натиснуть «🚚 Умови доставки».",
            reply_markup=_menu_back_keyboard(),
        )
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

DEFAULT_DELIVERY_TEXT = (
    "Умови доставки:\n\n"
    "Доставка роздрібних замовлень від 2000грн безкоштовно, менше за тарифами перевізника "
    "або 190 грн по Києву.\n\n"
    "Доставка гуртових замовлень від 10 кг будь-якої кави - безкоштовно. "
    "До 10-ти кг - 190 грн. по Києву, або за тарифами нової пошти.\n\n"
    "Ви можете обрати по одному кілограму різних сортів, та отримати знижку в залежності "
    "від загального об'єму замовлення.\n\n"
    "Замовлення прийняті до 09:00 - можуть бути доставлені КУР'ЄРОМ в той самий день.\n\n"
    "Замовлення новою поштою прийняті до 11:00 - відправляються у той самий день.\n\n"
    "Доставка на наступний після замовлення день з понеділка по п'ятницю, з 10 до 16.\n\n"
    "Доставка замовлень відбувається за умови повної передплати на рахунок, або по факту "
    "отримання кави.\n\n"
    "Кава обсмажується на професійному ростері COGEN C15"
)


def _client_persistent_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(CLIENT_BTN_ASSORTMENT), KeyboardButton(CLIENT_BTN_DELIVERY)],
            [KeyboardButton(CLIENT_BTN_ORDER), KeyboardButton(CLIENT_BTN_MANAGER)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


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
    if not text:
        await query.edit_message_text(
            f"{label}\n\nНаразі ще не додано 🙈 Скоро оновимо — зазирніть трохи пізніше!"
        )
        return
    await query.edit_message_text(f"{label}\n\n{text}")


async def client_show_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = _get_setting("delivery", DEFAULT_DELIVERY_TEXT).strip()
    await update.message.reply_text(f"🚚 {text}")


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


# ---------- Опитувальник замовлення (клієнт) ----------

ORDER_WEIGHTS = ["1 кг", "0,5 кг", "0,25 кг"]


def _order_date_keyboard() -> InlineKeyboardMarkup:
    today = datetime.now(TZ).date()
    buttons, row = [], []
    for i in range(14):
        d = today + timedelta(days=i)
        label = f"{d.strftime('%d.%m')} ({WEEKDAY_LABELS_SHORT[d.weekday()]})"
        row.append(InlineKeyboardButton(label, callback_data=f"order_date:{d.isoformat()}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Скасувати замовлення", callback_data="order_cancel")])
    return InlineKeyboardMarkup(buttons)


def _order_category_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"order_cat:{key}")]
        for key, label in ORDER_CATEGORIES
    ]
    buttons.append([InlineKeyboardButton("❌ Скасувати замовлення", callback_data="order_cancel")])
    return InlineKeyboardMarkup(buttons)


def _order_weight_keyboard() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(w, callback_data=f"order_weight:{w}") for w in ORDER_WEIGHTS]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("❌ Скасувати замовлення", callback_data="order_cancel")]])


def _order_summary_text(order: dict) -> str:
    lines = [
        f"📝 Нове замовлення",
        f"Точка: {order.get('point_name', '—')}",
        f"Адреса: {order.get('address', '—')}",
        f"Дата: {order.get('date', '—')}",
        "",
        "Позиції:",
    ]
    for i, item in enumerate(order.get("items", []), 1):
        cat_label = dict(ORDER_CATEGORIES).get(item.get("category"), item.get("category"))
        lines.append(
            f"{i}. {cat_label} — {item.get('item_text', '—')} — {item.get('weight', '—')}\n"
            f"   Примітка: {item.get('note') or '—'}"
        )
    return "\n".join(lines)


async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ORDER_PENDING[chat_id] = {"step": "point_name", "items": []}
    await update.message.reply_text(
        "📝 Оформімо замовлення! Напишіть, будь ласка, назву вашої точки (кав'ярні/закладу):"
    )


async def handle_order_text_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    order = ORDER_PENDING.get(chat_id)
    if not order:
        return
    step = order.get("step")
    text = update.message.text or ""

    if step == "point_name":
        order["point_name"] = text
        order["step"] = "address"
        await update.message.reply_text("Дякуємо! Тепер напишіть адресу доставки:")
        return

    if step == "address":
        order["address"] = text
        order["step"] = "date"
        await update.message.reply_text("На яку дату потрібне замовлення?", reply_markup=_order_date_keyboard())
        return

    if step == "item_text":
        order["current_item"]["item_text"] = text
        order["step"] = "weight"
        await update.message.reply_text("Оберіть вагу:", reply_markup=_order_weight_keyboard())
        return

    if step == "note":
        order["current_item"]["note"] = text
        order["items"].append(order.pop("current_item"))
        order["step"] = "addmore"
        buttons = [
            [InlineKeyboardButton("➕ Додати ще позицію", callback_data="order_addmore")],
            [InlineKeyboardButton("✅ Завершити замовлення", callback_data="order_finish")],
            [InlineKeyboardButton("❌ Скасувати замовлення", callback_data="order_cancel")],
        ]
        await update.message.reply_text(
            "Додано! Хочете додати ще одну позицію, чи завершити замовлення?",
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
        await query.edit_message_text("Замовлення скасовано. Якщо передумаєте — просто натисніть «📝 Замовити» ще раз.")
        return

    if not order:
        await query.edit_message_text("Це замовлення вже неактуальне. Натисніть «📝 Замовити», щоб почати заново.")
        return

    if data.startswith("order_date:"):
        _, date_str = data.split(":", 1)
        d = datetime.fromisoformat(date_str).date()
        order["date"] = f"{d.strftime('%d.%m.%Y')} ({WEEKDAY_LABELS_SHORT[d.weekday()]})"
        order["step"] = "category"
        await query.edit_message_text("Яку категорію товару додати до замовлення?", reply_markup=_order_category_keyboard())
        return

    if data.startswith("order_cat:"):
        _, cat_key = data.split(":", 1)
        order["current_item"] = {"category": cat_key}
        order["step"] = "item_text"
        label = dict(ORDER_CATEGORIES).get(cat_key, cat_key)
        await query.edit_message_text(
            f"{label}\n\nНапишіть, будь ласка, назву позиції (можна подивитись у «☕ Асортимент»):"
        )
        return

    if data.startswith("order_weight:"):
        _, weight = data.split(":", 1)
        order["current_item"]["weight"] = weight
        order["step"] = "note"
        await query.edit_message_text(
            "Примітка до цієї позиції (пакування, помел тощо)? Якщо немає — напишіть «немає»:"
        )
        return

    if data == "order_addmore":
        order["step"] = "category"
        await query.edit_message_text("Яку категорію товару додати?", reply_markup=_order_category_keyboard())
        return

    if data == "order_finish":
        order = ORDER_PENDING.pop(chat_id, None)
        if not order or not order.get("items"):
            await query.edit_message_text("Замовлення порожнє, скасовано.")
            return
        summary = _order_summary_text(order)
        client = update.effective_user
        target_admin = _resolve_target_admin(chat_id)
        await query.edit_message_text(
            "✅ Дякуємо! Ваше замовлення передано менеджеру, скоро з вами зв'яжуться."
        )
        if target_admin:
            try:
                sent = await context.bot.send_message(
                    target_admin,
                    f"{summary}\n\nВід: {client.full_name} (@{client.username or '—'}, chat_id: {chat_id})\n\n"
                    f"Щоб відповісти клієнту — зробіть Reply на це повідомлення.",
                )
                FORWARD_MAP[(target_admin, sent.message_id)] = chat_id
            except Exception as e:
                logger.warning(f"Не вдалось переслати замовлення адміну: {e}")
        return



    return ReplyKeyboardMarkup(
        [[KeyboardButton("📋 Меню")]],
        resize_keyboard=True,
        is_persistent=True,
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(
        "Кнопка «📋 Меню» тепер закріплена внизу — можна відкривати меню одним тапом, без набору команди.",
        reply_markup=_persistent_menu_keyboard(),
    )
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
        current = _get_setting("delivery", DEFAULT_DELIVERY_TEXT).strip()
        MENU_PENDING[admin_id] = {"action": "delivery_text"}
        await query.edit_message_text(
            "Напишіть новий текст умов доставки — саме це побачать клієнти, коли натиснуть "
            "свою кнопку «🚚 Умови доставки».\n\nЗараз там написано:\n" + current
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
            [InlineKeyboardButton(f"{r['name']} (@{r['username'] or '—'})", callback_data=f"menu_pickclient:{r['chat_id']}")]
            for r in rows
        ]
        buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
        await query.edit_message_text(
            "Оберіть клієнта, якому хочете змінити групу:", reply_markup=InlineKeyboardMarkup(buttons)
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
            [InlineKeyboardButton(f"{r['name']} (@{r['username'] or '—'})", callback_data=f"menu_pickclientresp:{r['chat_id']}")]
            for r in rows
        ]
        buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
        await query.edit_message_text(
            "Оберіть клієнта, якому призначити відповідального:", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data == "menu_resplist":
        await query.edit_message_text(_clients_with_admin_text(), reply_markup=_menu_back_keyboard())
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
        await query.edit_message_text(_clients_text(), reply_markup=_menu_back_keyboard())
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
            f"{mark} {c['name']} (@{c['username'] or '—'})", callback_data=f"bc_toggle:{c['chat_id']}"
        )])
    buttons.append([
        InlineKeyboardButton("➡️ Далі", callback_data="bc_selectdone"),
        InlineKeyboardButton("❌ Скасувати", callback_data="menu_back"),
    ])
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
    if kind == "once":
        date_str = bc.get("date")
        run_at = datetime.combine(
            datetime.fromisoformat(date_str).date(), time(hour=hour, minute=minute), tzinfo=TZ
        )
        cur = conn.execute(
            "INSERT INTO broadcasts (kind, target_type, target_value, message, run_at, is_question, file_id, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, target_type, target_value, message_text, run_at.isoformat(), is_question, file_id, admin_id, datetime.now(TZ).isoformat()),
        )
    else:
        weekday = bc.get("weekday")
        hhmm = f"{hour:02d}:{minute:02d}"
        cur = conn.execute(
            "INSERT INTO broadcasts (kind, target_type, target_value, message, weekday, hhmm, is_question, file_id, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, target_type, target_value, message_text, weekday, hhmm, is_question, file_id, admin_id, datetime.now(TZ).isoformat()),
        )
    broadcast_id = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM broadcasts WHERE id=?", (broadcast_id,)).fetchone()
    conn.close()

    schedule_broadcast_job(context.application, row)

    kind_label = "Запитання так/ні" if is_question else "Розсилку"
    file_note = "\n📎 З прикріпленим файлом" if file_id else ""
    await update.message.reply_text(
        f"✅ {kind_label} заплановано!{file_note}\n\n"
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
        MENU_PENDING[admin_id] = {"action": "bc_text"}
        if pending.get("is_question"):
            await query.edit_message_text(
                "Напишіть текст запитання (наприклад: «Завтра доставка, потрібна?») — або надішліть файл із підписом:"
            )
        else:
            await query.edit_message_text("Напишіть текст повідомлення для цієї розсилки — або надішліть файл із підписом:")
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
        buttons = [
            [InlineKeyboardButton(
                f"{'❓ ' if r['is_question'] else ''}{_bc_target_label(r['target_type'], r['target_value'])} — {_bc_timing_label(r)}",
                callback_data=f"bc_cancelpick:{r['id']}",
            )]
            for r in rows
        ]
        buttons.append([InlineKeyboardButton("🔙 До меню", callback_data="menu_back")])
        await query.edit_message_text(
            "Оберіть, яку розсилку скасувати:", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("bc_cancelpick:"):
        _, bid_str = data.split(":", 1)
        bid = int(bid_str)
        conn = db()
        row = conn.execute("SELECT * FROM broadcasts WHERE id=? AND created_by=?", (bid, admin_id)).fetchone()
        conn.close()
        if not row:
            await query.edit_message_text("Цю розсилку вже не знайдено.", reply_markup=_menu_back_keyboard())
            return
        preview = row["message"][:80] + ("..." if len(row["message"]) > 80 else "")
        buttons = [
            [InlineKeyboardButton("✅ Так, скасувати", callback_data=f"bc_cancelconfirm:{bid}")],
            [InlineKeyboardButton("🔙 До меню", callback_data="menu_back")],
        ]
        await query.edit_message_text(
            f"Скасувати цю розсилку?\n\n"
            f"Кому: {_bc_target_label(row['target_type'], row['target_value'])}\n"
            f"Коли: {_bc_timing_label(row)}\n"
            f"Текст: {preview}",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data.startswith("bc_cancelconfirm:"):
        _, bid_str = data.split(":", 1)
        bid = int(bid_str)
        conn = db()
        row = conn.execute("SELECT id FROM broadcasts WHERE id=? AND created_by=?", (bid, admin_id)).fetchone()
        if not row:
            conn.close()
            await query.edit_message_text("Цю розсилку вже не знайдено.", reply_markup=_menu_back_keyboard())
            return
        conn.execute("UPDATE broadcasts SET active=0 WHERE id=?", (bid,))
        conn.commit()
        conn.close()
        for job in context.application.job_queue.get_jobs_by_name(_bc_job_name(bid)):
            job.schedule_removal()
        await query.edit_message_text("✅ Розсилку скасовано.", reply_markup=_menu_back_keyboard())
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
            text += (
                f"{marker} Кому: {_bc_target_label(r['target_type'], r['target_value'])}\n"
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
            f"{mark} {c['name']} (@{c['username'] or '—'})", callback_data=f"qna_toggle:{c['chat_id']}"
        )])
    buttons.append([
        InlineKeyboardButton("➡️ Далі", callback_data="qna_selectdone"),
        InlineKeyboardButton("❌ Скасувати", callback_data="menu_back"),
    ])
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
            [InlineKeyboardButton("❌ Ні", callback_data=f"qna_no:{r['chat_id']}")],
        ]
        if yes_button:
            buttons.append([yes_button])
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
            await context.bot.send_message(r["chat_id"], seg_row["message"], reply_markup=_client_persistent_keyboard())
            sent += 1
        except Exception as e:
            logger.warning(f"Не вдалось надіслати {r['chat_id']}: {e}")
            failed += 1

    if ADMIN_IDS:
        await notify_admins(context, f"✅ Автоматична розсилка сегменту «{segment}» виконана. Надіслано: {sent}, помилок: {failed}")


def _job_name(segment: str, weekday: int, hh: int, mm: int) -> str:
    return f"segment_{segment}_{weekday}_{hh:02d}{mm:02d}"


def create_segment_job(application: Application, segment: str, weekday: int, hh: int, mm: int):
    """Створює (або перестворює) ОДНЕ конкретне завдання розсилки, не чіпаючи інші розклади цього сегмента."""
    name = _job_name(segment, weekday, hh, mm)
    for job in application.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    new_job = application.job_queue.run_daily(
        send_segment_broadcast,
        time=time(hour=hh, minute=mm, tzinfo=TZ),
        days=(weekday,),
        name=name,
        data={"segment": segment},
    )
    logger.info(
        f"[SCHEDULE] Створено завдання {name}: weekday={weekday}, "
        f"time={hh:02d}:{mm:02d} TZ={TZ}. Наступний запуск: {new_job.next_t}"
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
    rows = conn.execute("SELECT segment, weekday, hhmm FROM schedules").fetchall()
    conn.close()
    for r in rows:
        hh, mm = map(int, r["hhmm"].split(":"))
        create_segment_job(application, r["segment"], r["weekday"], hh, mm)
        logger.info(f"Заплановано розсилку для сегмента {r['segment']}: {WEEKDAYS_UA[r['weekday']]} {r['hhmm']}")


# ---------- Заплановані розсилки (майстер: кому / коли / текст) ----------

def _bc_job_name(broadcast_id: int) -> str:
    return f"bcast_{broadcast_id}"


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


YES_BUTTON_LABEL = "✅ Так, зв'язатися з менеджером 😊"
QNA_CLARIFICATION = (
    "\n\n💬 Щоб домовитись — просто натисніть «Так», і одразу потрапите в особистий чат "
    "з менеджером, там і поспілкуємось 😊 Відповідати сюди, у бота, не потрібно — тут ми "
    "лише надсилаємо новини й нагадування."
)


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
                buttons = [[InlineKeyboardButton("❌ Ні", callback_data=f"qna_no:{r['chat_id']}")]]
                if yes_button:
                    buttons.append([yes_button])
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
                        r["chat_id"], file_id, caption=row["message"] or None, reply_markup=_client_persistent_keyboard()
                    )
                else:
                    await context.bot.send_message(
                        r["chat_id"], row["message"], reply_markup=_client_persistent_keyboard()
                    )
            sent += 1
        except Exception as e:
            logger.warning(f"Не вдалось надіслати {r['chat_id']}: {e}")
            failed += 1

    if ADMIN_IDS:
        label = "запитання" if is_question else "розсилка"
        await notify_admins(
            context,
            f"✅ Запланован{'е' if is_question else 'а'} {label} "
            f"({_bc_target_label(row['target_type'], row['target_value'])}) "
            f"виконан{'о' if is_question else 'а'}. Надіслано: {sent}, помилок: {failed}.",
        )


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


# ---------- Довідка ----------

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команди: /start — підписатись, /stop — відписатись.")
        return
    await update.message.reply_text(
        "Команди адміністратора:\n\n"
        "/addsegment назва — створити сегмент клієнтів\n"
        "/setschedule сегмент день ГГ:ХХ — ЗАМІНИТИ весь розклад сегмента одним записом (mon..sun)\n"
        "/addschedule сегмент день ГГ:ХХ — ДОДАТИ ще один час розсилки, не видаляючи наявні (для 2+ разів на тиждень)\n"
        "/removeschedule сегмент день ГГ:ХХ — видалити один конкретний запис розкладу\n"
        "/setmsg сегмент текст — задати повідомлення для автоматичної розсилки сегмента\n"
        "/segments — список сегментів, усіх їхніх розкладів і повідомлень\n"
        "/clients — список активних підписників\n"
        "/setsegment @юзернейм_або_chat_id сегмент — будь-коли перепризначити клієнту сегмент\n"
        "/broadcast текст — надіслати повідомлення ОДРАЗУ всім підписникам (акції, зміни цін)\n"
        "/broadcastfile — надішліть PDF (або інший файл) боту з підписом, що починається на "
        "«/broadcastfile ваш текст», і він одразу розійде цей файл усім підписникам\n"
        "/jobs — перевірити заплановані розсилки і час наступного запуску (діагностика)\n"
        "/testsegment сегмент — надіслати розсилку сегмента ПРЯМО ЗАРАЗ, без очікування розкладу (для перевірки)\n"
        "/sendto — обрати конкретних людей зі списку (кнопками) і надіслати їм окреме повідомлення\n"
        "/menu — відкрити меню з кнопками українською (усі дії без потреби набирати команди)\n"
        "\nУ меню також доступні: призначення відповідального адміна клієнту, "
        "запитання «так/ні» з персональним посиланням на відповідального, планування розсилок на дату/день.\n"
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
    application.add_handler(CommandHandler("testsegment", testsegment))
    application.add_handler(CommandHandler("sendto", sendto_start))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(MessageHandler(filters.Regex("^📋 Меню$"), menu_command))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CLIENT_BTN_ASSORTMENT)}$"), client_show_assortment))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CLIENT_BTN_DELIVERY)}$"), client_show_delivery))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CLIENT_BTN_MANAGER)}$"), client_contact_manager))
    application.add_handler(MessageHandler(filters.Regex(f"^{re.escape(CLIENT_BTN_ORDER)}$"), order_start))
    application.add_handler(CallbackQueryHandler(client_assortment_category_callback, pattern=r"^clientassort:"))
    application.add_handler(CallbackQueryHandler(sendto_toggle_callback, pattern=r"^sendto_toggle:"))
    application.add_handler(CallbackQueryHandler(sendto_done_callback, pattern=r"^sendto_done$"))
    application.add_handler(CallbackQueryHandler(sendto_cancel_callback, pattern=r"^sendto_cancel$"))
    application.add_handler(CallbackQueryHandler(assign_callback, pattern=r"^assign:"))
    application.add_handler(CallbackQueryHandler(pickresp_callback, pattern=r"^pickresp:"))
    application.add_handler(CallbackQueryHandler(respme_callback, pattern=r"^respme:"))
    application.add_handler(CallbackQueryHandler(menu_router, pattern=r"^menu_"))
    application.add_handler(CallbackQueryHandler(bc_router, pattern=r"^(bc_|bcq_)"))
    application.add_handler(CallbackQueryHandler(qna_router, pattern=r"^qna_"))
    application.add_handler(CallbackQueryHandler(wg_router, pattern=r"^wg_"))
    application.add_handler(CallbackQueryHandler(order_router, pattern=r"^order_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_text))

    logger.info("Бот запущено")
    application.run_polling()


if __name__ == "__main__":
    main()

