"""AI eslatma boti — Google Sheets versiyasi.

Foydalanuvchi o'zbekcha yozadi -> AI (Gemini) tushunadi -> vazifa Sheets'ga yoziladi
-> vaqti kelganda bot raqamlangan eslatma yuboradi -> "3 bitdi" desangiz
vazifa Bajarilganlar varag'iga o'tadi.
"""

import os
import re
import json
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import db
import ai

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("reminder-bot")

TZ = ZoneInfo(os.environ.get("TZ", "Asia/Tashkent"))
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

WELCOME = (
    "Assalomu alaykum! Men sizning AI eslatma yordamchingizman \U0001F916\n\n"
    "Menga oddiy tilda yozing, men eslatib turaman. Masalan:\n"
    "• har chorshanba 14:00 do'konga borish\n"
    "• har oyning birinchi dushanbasi ishchilar bilan majlis\n"
    "• har kuni 8:00 tabletka ich\n"
    "• ertaga 10:00 bankka bor\n\n"
    "Bajarganingizda: «3 bitdi»\n"
    "O'chirish uchun: «3-ni o'chir»\n"
    "Ro'yxat uchun: /list yoki «eslatmalarim»\n\n"
    "Hammasi Google Sheets jadvalida ham ko'rinib turadi \U0001F4CA"
)

# "3 bitdi", "1, 4 bajarildi", "2 va 5 tugadi" — AI'siz tez ishlaydi
DONE_RE = re.compile(
    r"^\s*((?:\d+\s*(?:,|va|\s)\s*)*\d+)\s*[-)]?\s*(?:ni\s+)?"
    r"(bitdi|bajarildi|bajardim|tugadi|qildim|tayyor)\W*$",
    re.IGNORECASE,
)


# ---------- vaqt hisoblash ----------

def next_fire(kind, params, now=None):
    now = now or datetime.now(TZ)
    if kind == "once":
        d = datetime.fromisoformat(params["run_date"])
        if d.tzinfo is None:
            d = d.replace(tzinfo=TZ)
        return d
    return CronTrigger(timezone=TZ, **params).get_next_fire_time(None, now)


# ---------- jadvalga qo'yish ----------

async def fire(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    rem = await asyncio.to_thread(db.get_reminder, data["id"])
    if not rem:  # Sheets'dan qo'lda o'chirilgan bo'lsa
        context.job.schedule_removal()
        return

    text = (
        f"\U0001F514 {rem['num']}) Vazifa: {rem['title']}\n"
        f"\U0001F552 {rem['human_time']}\n\n"
        f"Bajarsangiz yozing: «{rem['num']} bitdi»"
    )
    try:
        await context.bot.send_message(chat_id=rem["chat_id"], text=text)
    except Exception as e:
        log.warning("Xabar yuborilmadi (id=%s): %s", rem["id"], e)

    nxt = None
    if rem["kind"] == "recurring":
        nxt = next_fire("recurring", json.loads(rem["trigger_json"]))
    await asyncio.to_thread(db.after_fire, rem["id"], nxt)


def schedule_one(job_queue, rem):
    params = json.loads(rem["trigger_json"])
    if rem["kind"] == "once":
        run_date = next_fire("once", params)
        if run_date <= datetime.now(TZ):
            return False  # vaqti o'tib ketgan — eslatma yuborilgan, "bitdi"ni kutadi
        trigger = DateTrigger(run_date=run_date)
    else:
        trigger = CronTrigger(timezone=TZ, **params)

    job_queue.run_custom(
        fire, job_kwargs={"trigger": trigger}, name=str(rem["id"]),
        data={"id": rem["id"]},
    )
    log.info("Jadvalga qo'shildi: id=%s (%s)", rem["id"], rem["human_time"])
    return True


def remove_jobs(job_queue, rid):
    for j in job_queue.get_jobs_by_name(str(rid)):
        j.schedule_removal()


async def monthly_reset_job(context: ContextTypes.DEFAULT_TYPE):
    n = await asyncio.to_thread(db.monthly_reset)
    log.info("Oy boshi: %d ta takroriy vazifa qayta Faol qilindi.", n)


# ---------- buyruqlar ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_list(update, update.effective_chat.id)


async def send_list(update, chat_id):
    rems = await asyncio.to_thread(db.list_reminders, chat_id)
    if not rems:
        await update.message.reply_text("Hozircha vazifa yo'q.")
        return
    lines = []
    for r in rems:
        mark = "✅" if r["status"] == "Bitdi" else "⏳"
        left = f" — {mark} {r['left']}" if r["left"] else ""
        lines.append(f"{r['num']}) {r['title']} — {r['human_time']}{left}")
    await update.message.reply_text(
        "\U0001F4CB Vazifalaringiz:\n" + "\n".join(lines)
        + "\n\nBajarganingizda: «<raqam> bitdi»"
    )


async def do_done(update, context, chat_id, ids):
    done_titles = []
    for rid in ids:
        rem = await asyncio.to_thread(db.get_reminder, rid)
        if not rem or rem["chat_id"] != chat_id:
            continue
        if rem["kind"] == "once":
            remove_jobs(context.job_queue, rid)
        await asyncio.to_thread(db.mark_done, rid)
        done_titles.append(rem["title"])
    return done_titles


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    # 1) Tez yo'l: "3 bitdi"
    m = DONE_RE.match(text)
    if m:
        nums = [int(n) for n in re.findall(r"\d+", m.group(1))]
        rems = await asyncio.to_thread(db.list_reminders, chat_id)
        by_num = {r["num"]: r["id"] for r in rems}
        ids = [by_num[n] for n in nums if n in by_num]
        if not ids:
            await update.message.reply_text(
                "Bunday raqamli vazifa topilmadi \U0001F914 /list bilan tekshirib ko'ring."
            )
            return
        titles = await do_done(update, context, chat_id, ids)
        await update.message.reply_text(
            "Barakalla! ✅ Bajarilganlarga o'tkazdim:\n• " + "\n• ".join(titles)
        )
        return

    # 2) Qolgan hamma narsa — AI orqali
    now = datetime.now(TZ)
    now_str = now.strftime("%Y-%m-%d %H:%M (%A)")
    reminders = await asyncio.to_thread(db.list_reminders, chat_id)
    own_ids = {r["id"] for r in reminders}

    try:
        result = await asyncio.to_thread(ai.understand, text, now_str, reminders)
    except Exception as e:
        log.exception("AI xatosi: %s", e)
        await update.message.reply_text(
            "Kechirasiz, buni tushunolmadim \U0001F648 Boshqacharoq yozib ko'ring."
        )
        return

    action = result.get("action", "chat")

    for rid in result.get("ids_to_delete", []) or []:
        if rid in own_ids:
            remove_jobs(context.job_queue, rid)
            await asyncio.to_thread(db.delete_reminder, rid)

    done_ids = [rid for rid in (result.get("ids_done", []) or []) if rid in own_ids]
    if done_ids:
        await do_done(update, context, chat_id, done_ids)

    for r in result.get("reminders_to_create", []) or []:
        try:
            nxt = next_fire(r["kind"], r["trigger"])  # trigger noto'g'ri bo'lsa shu yerda xato
            rid = await asyncio.to_thread(
                db.add_reminder, chat_id, r["title"], r["kind"], r["trigger"],
                r.get("human_time", ""), nxt,
            )
            schedule_one(context.job_queue, {
                "id": rid, "kind": r["kind"], "human_time": r.get("human_time", ""),
                "trigger_json": json.dumps(r["trigger"]),
            })
        except Exception as e:
            log.exception("Vazifa yaratishda xato: %s", e)
            await update.message.reply_text(
                f"⚠️ «{r.get('title', '')}» ni yozib bo'lmadi. Boshqacharoq aytib ko'ring."
            )

    if action == "list":
        await send_list(update, chat_id)
        return

    reply = result.get("reply") or "Bo'ldi ✅"
    await update.message.reply_text(reply)


# ---------- ishga tushish ----------

async def post_init(app: Application):
    await asyncio.to_thread(db.init_db)
    rems = await asyncio.to_thread(db.all_reminders)
    count, refresh = 0, {}
    for rem in rems:
        try:
            if schedule_one(app.job_queue, rem):
                count += 1
            if rem["kind"] == "recurring":
                refresh[rem["id"]] = next_fire("recurring", json.loads(rem["trigger_json"]))
        except Exception as e:
            log.exception("Yuklashda xato (id=%s): %s", rem.get("id"), e)
    await asyncio.to_thread(db.refresh_next_times, refresh)

    # Har oyning 1-kuni 00:01 da takroriy vazifalar avtomatik reset
    app.job_queue.run_custom(
        monthly_reset_job,
        job_kwargs={"trigger": CronTrigger(day=1, hour=0, minute=1, timezone=TZ)},
        name="monthly_reset",
    )
    log.info("Bot tayyor. %d ta vazifa jadvalga qo'yildi.", count)


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("Bot ishga tushmoqda...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
