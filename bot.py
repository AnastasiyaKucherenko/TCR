"""
Telegram-бот для ростерії: збір клієнтів через /start, сегменти з власним
щотижневим розкладом розсилки, і команда для миттєвої розсилки всім
(зміни, прайси, акції).

Автор: згенеровано Claude для конкретного кейсу ростерії.
"""

import logging
import os
import sqlite3
from datetime import datetime, time
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
    return update.effective_chat.id in ADMIN_IDS


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
        "Якщо захочете відписатись — просто напишіть /stop."
    )

    if ADMIN_IDS:
        conn = db()
        segments = [r["name"] for r in conn.execute("SELECT name FROM segments")]
        conn.close()
        buttons = [
            [InlineKeyboardButton(seg, callback_data=f"assign:{chat.id}:{seg}")]
            for seg in segments
        ]
        buttons.append([InlineKeyboardButton("Без сегмента", callback_data=f"assign:{chat.id}:__none__")])
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
            await context.bot.send_message(r["chat_id"], text)
            sent += 1
        except Exception as e:
            logger.warning(f"Не вдалось надіслати {r['chat_id']}: {e}")
            failed += 1
    await update.message.reply_text(f"Розсилка завершена. Надіслано: {sent}, помилок: {failed}")


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
            await context.bot.send_document(r["chat_id"], document.file_id, caption=text or None)
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


async def _sendto_send_picker(context: ContextTypes.DEFAULT_TYPE, admin_id: int):
    conn = db()
    rows = conn.execute(
        "SELECT chat_id, name, username FROM subscribers WHERE active=1 ORDER BY joined_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    if not rows:
        await context.bot.send_message(admin_id, "Поки немає активних підписників.")
        return

    SENDTO_SELECTIONS[admin_id] = set()
    clients = [dict(r) for r in rows]
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
        return

    # 1) Якщо очікуємо текст для розсилки обраним (/sendto)
    if admin_id in SENDTO_AWAITING_TEXT:
        recipients = SENDTO_AWAITING_TEXT.pop(admin_id)
        text = update.message.text
        sent, failed = 0, 0
        for chat_id in recipients:
            try:
                await context.bot.send_message(chat_id, text)
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
                await context.bot.send_message(r["chat_id"], text)
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


# ---------- Меню з кнопками українською (альтернатива текстовим командам) ----------

def _menu_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Написати всім", callback_data="menu_broadcast")],
        [InlineKeyboardButton("🎯 Написати обраним", callback_data="menu_sendto")],
        [InlineKeyboardButton("👥 Змінити групу клієнта", callback_data="menu_changeseg")],
        [InlineKeyboardButton("✏️ Змінити повідомлення групи", callback_data="menu_setmsg")],
        [InlineKeyboardButton("📁 Переглянути групи", callback_data="menu_viewsegments")],
        [InlineKeyboardButton("👤 Переглянути клієнтів", callback_data="menu_viewclients")],
    ])


def _menu_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 До меню", callback_data="menu_back")]])


def _persistent_menu_keyboard() -> ReplyKeyboardMarkup:
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
    await update.message.reply_text("Оберіть дію:", reply_markup=_menu_main_keyboard())


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        return
    admin_id = update.effective_chat.id
    data = query.data

    if data == "menu_back":
        MENU_PENDING.pop(admin_id, None)
        await query.edit_message_text("Оберіть дію:", reply_markup=_menu_main_keyboard())
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
            await context.bot.send_message(r["chat_id"], seg_row["message"])
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
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задано BOT_TOKEN у .env")
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS не задано — сповіщення адмінам не працюватимуть")

    init_db()

    application = Application.builder().token(BOT_TOKEN).post_init(load_schedules_on_startup).build()

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
    application.add_handler(CommandHandler("setsegment", setsegment))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(
        MessageHandler(filters.Document.ALL & filters.CaptionRegex(r"(?i)^/broadcastfile"), broadcast_file)
    )
    application.add_handler(CommandHandler("jobs", jobs_list))
    application.add_handler(CommandHandler("testsegment", testsegment))
    application.add_handler(CommandHandler("sendto", sendto_start))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(MessageHandler(filters.Regex("^📋 Меню$"), menu_command))
    application.add_handler(CallbackQueryHandler(sendto_toggle_callback, pattern=r"^sendto_toggle:"))
    application.add_handler(CallbackQueryHandler(sendto_done_callback, pattern=r"^sendto_done$"))
    application.add_handler(CallbackQueryHandler(sendto_cancel_callback, pattern=r"^sendto_cancel$"))
    application.add_handler(CallbackQueryHandler(assign_callback, pattern=r"^assign:"))
    application.add_handler(CallbackQueryHandler(menu_router, pattern=r"^menu_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_text))

    logger.info("Бот запущено")
    application.run_polling()


if __name__ == "__main__":
    main()
