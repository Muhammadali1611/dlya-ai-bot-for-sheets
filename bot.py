"""AI eslatma boti v2 — Supabase + Mini App.

Foydalanuvchi o'zbekcha yozadi -> AI tushunadi -> vazifa Supabase'ga yoziladi
-> har 30 soniyada vaqti kelgan vazifalar tekshiriladi va eslatma yuboriladi
-> "3 bitdi" (yoki Mini App'dagi ✅) -> vazifa tarixga o'tadi.

Bot faqat egasi (OWNER_ID) uchun ishlaydi.
"""

import os
import re
import json
import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    Update, MenuButtonWebApp, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
)
from apscheduler.triggers.cron import CronTrigger

import db
import ai
import triggers
import webapp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # har 10 soniyalik getUpdates log'i shart emas
log = logging.getLogger("reminder-bot")

TZ = ZoneInfo(os.environ.get("TZ", "Asia/Tashkent"))
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_ID", "7242956193"))
CHECK_EVERY = int(os.environ.get("CHECK_EVERY", "30"))  # soniya

HISTORY = deque(maxlen=int(os.environ.get("HISTORY_LEN", "8")))
TG_LIMIT = 4000

WELCOME = (
    "Assalomu alaykum! Men sizning AI yordamchingizman \U0001F916\n\n"
    "Menga oddiy tilda yozing, men eslatib turaman. Masalan:\n"
    "• har chorshanba 14:00 do'konga borish\n"
    "• har oyning birinchi dushanbasi ishchilar bilan majlis\n"
    "• ertaga 10:00 bankka bor\n"
    "• har 3 kunda 11:00 omborni tekshir\n"
    "• bitmaguncha har kuni 8:00 da elevator ishini eslat\n\n"
    "«Bugun nima ishlarim bor?» deb so'rasangiz ham bo'ladi.\n\n"
    "Bajarganingizda: «3 bitdi»\n"
    "Ro'yxat: /list  •  Ilova: pastdagi «📋 Vazifalar» tugmasi"
)

DONE_RE = re.compile(
    r"^\s*((?:\d+\s*(?:,|va|\s)\s*)*\d+)\s*[-)]?\s*(?:ni\s+)?"
    r"(bitdi|bajarildi|bajardim|tugadi|qildim|tayyor)\W*$",
    re.IGNORECASE,
)

next_fire = triggers.next_fire


# ---------- yordamchilar ----------

def now_tz():
    return datetime.now(TZ)


def today_times(task, now=None):
    """Vazifa bugun qaysi soatlarda ishlashi ("09:00, 15:00" yoki "")."""
    now = now or now_tz()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    try:
        trig = triggers.build(task["kind"], task["trigger"])
    except Exception:
        return ""
    times, t = [], trig.get_next_fire_time(None, start)
    while t is not None and t < end and len(times) < 24:
        times.append(t.astimezone(TZ).strftime("%H:%M"))
        t = trig.get_next_fire_time(t, t + timedelta(seconds=1))
    return ", ".join(times)


def webapp_url():
    url = os.environ.get("WEBAPP_URL", "").strip()
    if not url and os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        url = "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]
    return url.rstrip("/")


async def send_long(bot, text):
    text = text or "Bo'ldi ✅"
    while text:
        if len(text) <= TG_LIMIT:
            await bot.send_message(OWNER_ID, text)
            return
        cut = text.rfind("\n", 0, TG_LIMIT)
        if cut < TG_LIMIT // 2:
            cut = TG_LIMIT
        await bot.send_message(OWNER_ID, text[:cut])
        text = text[cut:].lstrip("\n")


def list_text(tasks):
    if not tasks:
        return "Hozircha vazifa yo'q."
    lines = []
    for t in tasks:
        mark = {"bitdi": "✅", "pauza": "⏸"}.get(t["status"], "⏳")
        left = f" — {mark} {t['left']}" if t["left"] else ""
        lines.append(f"{t['num']}) {t['title']} — {t['human_time']}{left}")
    return ("\U0001F4CB Vazifalaringiz:\n" + "\n".join(lines)
            + "\n\nBajarganingizda: «<raqam> bitdi»")


# ---------- asosiy mantiq (bot ham, Mini App ham ishlatadi) ----------

async def mark_done_ids(ids):
    titles = []
    for tid in ids:
        r = await db.mark_done(tid)
        if r:
            titles.append(r["title"])
    return titles


async def create_tasks(items):
    """AI qaytargan vazifalarni yaratadi. (yaratilganlar, xatolar) qaytaradi."""
    created, errors = [], []
    for r in items or []:
        try:
            nxt = next_fire(r["kind"], r["trigger"])
            if nxt is None:
                raise ValueError("vaqti o'tib ketgan")
            tid = await db.add_task(
                r["title"], r["kind"], r["trigger"], r.get("human_time", ""), nxt,
                bitguncha=bool(r.get("bitguncha")),
            )
            created.append(tid)
            log.info("Yangi vazifa id=%s: %s (%s)", tid, r["title"], r.get("human_time"))
        except Exception as e:  # noqa: BLE001
            log.exception("Vazifa yaratishda xato: %s", e)
            errors.append(f"⚠️ «{r.get('title', '')}» ni yozib bo'lmadi. Boshqacharoq aytib ko'ring.")
    return created, errors


async def handle_text(text):
    """Foydalanuvchi matnini bajaradi. (javob_matni, ro'yxat_kerakmi) qaytaradi."""
    tasks = await db.list_tasks()

    # 1) Tez yo'l: "3 bitdi" — AI'siz, limitni sarflamaydi
    m = DONE_RE.match(text)
    if m:
        by_num = {t["num"]: t["id"] for t in tasks}
        ids = [by_num[int(n)] for n in re.findall(r"\d+", m.group(1)) if int(n) in by_num]
        if not ids:
            return "Bunday raqamli vazifa topilmadi \U0001F914 /list bilan tekshirib ko'ring.", False
        titles = await mark_done_ids(ids)
        return "Barakalla! ✅ Bajarildi:\n• " + "\n• ".join(titles), False

    # 2) Qolgan hamma narsa — AI orqali
    now = now_tz()
    for t in tasks:
        t["today_times"] = today_times(t, now)
    result = await asyncio.to_thread(
        ai.understand, text, now.strftime("%Y-%m-%d %H:%M (%A)"), tasks, list(HISTORY))

    own = {t["id"] for t in tasks}
    for tid in result.get("ids_to_delete") or []:
        if tid in own:
            await db.delete_task(tid)
    done_ids = [tid for tid in (result.get("ids_done") or []) if tid in own]
    if done_ids:
        await mark_done_ids(done_ids)
    _, errors = await create_tasks(result.get("reminders_to_create"))

    action = result.get("action", "chat")
    reply = result.get("reply") or ""
    HISTORY.append((text, reply or f"[{action}]"))
    if errors:
        reply = (reply + "\n\n" + "\n".join(errors)).strip()
    return reply or "Bo'ldi ✅", action == "list"


def ai_error_text(e):
    """AI xatosini foydalanuvchiga tushunarli matnga aylantiradi."""
    txt = str(e).lower()
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if isinstance(e, RuntimeError) and "kalit" in txt:
        return "⚠️ AI kaliti qo'yilmagan. Railway Variables'ga GEMINI_API_KEY qo'shing."
    if code == 429 or "quota" in txt or "exhausted" in txt:
        return "⏳ AI bepul limiti vaqtincha tugadi, birozdan keyin yozing. «3 bitdi» va /list ishlayveradi."
    if code in (401, 403) or "api key" in txt:
        return "⚠️ AI kaliti noto'g'ri. Railway'da GEMINI_API_KEY ni tekshiring."
    return "Kechirasiz, xatolik bo'ldi \U0001F648 Yana bir bor yozib ko'ring."


# ---------- Telegram handlerlari ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_long(context.bot, list_text(await db.list_tasks()))


async def app_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = webapp_url()
    if not url:
        await update.message.reply_text("Mini App manzili hali sozlanmagan.")
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📋 Vazifalarni ochish", web_app=WebAppInfo(url))]])
    await update.message.reply_text("Vazifalaringiz ilovada:", reply_markup=kb)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=OWNER_ID, action="typing")
    try:
        reply, show_list = await handle_text(update.message.text)
    except Exception as e:  # noqa: BLE001
        log.exception("Xabarni bajarishda xato: %s", e)
        await update.message.reply_text(ai_error_text(e))
        return
    await send_long(context.bot, reply)
    if show_list:
        await send_long(context.bot, list_text(await db.list_tasks()))


async def stranger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Egasidan boshqa odam yozsa."""
    if update.effective_message:
        await update.effective_message.reply_text("Bu shaxsiy bot. Kechirasiz \U0001F64F")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Kutilmagan xato: %s", context.error, exc_info=context.error)


# ---------- eslatmalar tsikli ----------

async def fire_due(bot):
    now = now_tz()
    for t in await db.due_tasks(now):
        old = t["next_at"]
        new = None
        if t["kind"] in ("recurring", "interval"):
            after = max(now, old) + timedelta(seconds=1)
            new = next_fire(t["kind"], t["trigger"], now=after)
        if not await db.claim_fire(t["id"], old, new):
            continue  # boshqa jarayon allaqachon yubordi
        num = next((x["num"] for x in await db.list_tasks() if x["id"] == t["id"]), "?")
        late = (now - old).total_seconds() > 600
        text = (
            f"\U0001F514 {num}) {t['title']}\n"
            f"\U0001F552 {t['human_time']}"
            + (" (kechikib)" if late else "")
            + f"\n\nBajarsangiz yozing: «{num} bitdi»"
        )
        try:
            await bot.send_message(OWNER_ID, text)
            log.info("Eslatma yuborildi: id=%s", t["id"])
        except Exception as e:  # noqa: BLE001
            log.warning("Eslatma yuborilmadi (id=%s): %s", t["id"], e)


async def scheduler_loop(bot):
    last_month = now_tz().month
    while True:
        try:
            await fire_due(bot)
            m = now_tz().month
            if m != last_month:
                n = await db.monthly_reset()
                log.info("Oy boshi: %d ta takroriy vazifa qayta faol qilindi.", n)
                last_month = m
        except Exception as e:  # noqa: BLE001
            log.exception("Eslatmalar tsiklida xato: %s", e)
        await asyncio.sleep(CHECK_EVERY)


# ---------- ishga tushish ----------

async def main():
    await db.init()

    app = Application.builder().token(TOKEN).build()
    owner = filters.User(user_id=OWNER_ID)
    app.add_handler(CommandHandler("start", start, filters=owner))
    app.add_handler(CommandHandler("list", list_cmd, filters=owner))
    app.add_handler(CommandHandler("app", app_cmd, filters=owner))
    app.add_handler(MessageHandler(owner & filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(MessageHandler(~owner, stranger))
    app.add_error_handler(on_error)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    url = webapp_url()
    if url:
        try:
            await app.bot.set_chat_menu_button(
                chat_id=OWNER_ID, menu_button=MenuButtonWebApp("📋 Vazifalar", WebAppInfo(url)))
        except Exception as e:  # noqa: BLE001
            log.warning("Menyu tugmasini qo'yib bo'lmadi: %s", e)

    runner = await webapp.start(handle_text, ai_error_text, port=int(os.environ.get("PORT", "8080")))
    sched = asyncio.create_task(scheduler_loop(app.bot))
    log.info("Bot tayyor. AI: %s. Mini App: %s", ai.provider(), url or "(manzil yo'q)")

    try:
        await asyncio.Event().wait()
    finally:
        sched.cancel()
        await runner.cleanup()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
