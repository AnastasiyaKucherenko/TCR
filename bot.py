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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
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
        "Привіт! Дякуємо, що підписались на новини нашої ростерії ☕️\n"
        "Тут ви отримуватимете новини про кавові новинки, акції та зміни в асортименті.\n\n"
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


async def segments_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    conn = db()
    rows = conn.execute("SELECT * FROM segments").fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Сегментів ще немає. Створіть: /addsegment назва")
        return
    text = "Сегменти:\n\n"
    for r in rows:
        conn = db()
        count = conn.execute("SELECT COUNT(*) c FROM subscribers WHERE segment=? AND active=1", (r["name"],)).fetchone()["c"]
        conn.close()
        day = WEEKDAYS_UA.get(r["schedule_day"], "не задано")
        text += (
            f"📁 {r['name']} ({count} клієнтів)\n"
            f"   Розклад: {day} {r['schedule_time'] or ''}\n"
            f"   Повідомлення: {r['message'][:60] + '...' if r['message'] and len(r['message']) > 60 else (r['message'] or '(порожнє)')}\n\n"
        )
    await update.message.reply_text(text)


async def setschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if len(context.args) < 2 or context.args[0] not in [] and len(context.args) < 3:
        pass
    if len(context.args) < 3:
        await update.message.reply_text(
            "Використання: /setschedule сегмент день ГГ:ХХ\n"
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
    conn.execute(
        "UPDATE segments SET schedule_day=?, schedule_time=? WHERE name=?",
        (WEEKDAYS[day_str], time_str, name),
    )
    conn.commit()
    conn.close()

    schedule_segment_job(context.application, name, WEEKDAYS[day_str], hh, mm)
    await update.message.reply_text(
        f"Готово. Сегмент «{name}» тепер отримує розсилку щотижня "
        f"у {WEEKDAYS_UA[WEEKDAYS[day_str]]} о {time_str}."
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


async def clients_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    conn = db()
    rows = conn.execute("SELECT * FROM subscribers WHERE active=1 ORDER BY joined_at DESC").fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Поки немає активних підписників.")
        return
    text = f"Активних підписників: {len(rows)}\n\n"
    for r in rows[:50]:
        text += f"• {r['name']} (@{r['username'] or '—'}) — сегмент: {r['segment'] or 'немає'}\n"
    if len(rows) > 50:
        text += f"\n...і ще {len(rows) - 50}"
    await update.message.reply_text(text)


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


# ---------- Автоматична розсилка по сегменту (виклик за розкладом) ----------

async def send_segment_broadcast(context: ContextTypes.DEFAULT_TYPE):
    segment = context.job.data["segment"]
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


def schedule_segment_job(application: Application, name: str, weekday: int, hh: int, mm: int):
    # прибираємо старе завдання для цього сегмента, якщо було
    for job in application.job_queue.get_jobs_by_name(f"segment_{name}"):
        job.schedule_removal()
    application.job_queue.run_daily(
        send_segment_broadcast,
        time=time(hour=hh, minute=mm, tzinfo=TZ),
        days=(weekday,),
        name=f"segment_{name}",
        data={"segment": name},
    )


async def load_schedules_on_startup(application: Application):
    conn = db()
    rows = conn.execute("SELECT * FROM segments WHERE schedule_day IS NOT NULL AND schedule_time IS NOT NULL").fetchall()
    conn.close()
    for r in rows:
        hh, mm = map(int, r["schedule_time"].split(":"))
        schedule_segment_job(application, r["name"], r["schedule_day"], hh, mm)
        logger.info(f"Заплановано розсилку для сегмента {r['name']}: {WEEKDAYS_UA[r['schedule_day']]} {r['schedule_time']}")


# ---------- Довідка ----------

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Команди: /start — підписатись, /stop — відписатись.")
        return
    await update.message.reply_text(
        "Команди адміністратора:\n\n"
        "/addsegment назва — створити сегмент клієнтів\n"
        "/setschedule сегмент день ГГ:ХХ — задати щотижневий розклад (mon..sun)\n"
        "/setmsg сегмент текст — задати повідомлення для автоматичної розсилки сегмента\n"
        "/segments — список сегментів, розкладів і повідомлень\n"
        "/clients — список активних підписників\n"
        "/broadcast текст — надіслати повідомлення ОДРАЗУ всім підписникам (акції, зміни цін)\n"
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
    application.add_handler(CommandHandler("setmsg", setmsg))
    application.add_handler(CommandHandler("clients", clients_list))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CallbackQueryHandler(assign_callback, pattern=r"^assign:"))

    logger.info("Бот запущено")
    application.run_polling()


if __name__ == "__main__":
    main()
