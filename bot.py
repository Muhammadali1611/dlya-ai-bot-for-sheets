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
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters,
)
from apscheduler.triggers.cron import CronTrigger

import db
import ai
import triggers
import webapp
import kuzatuv
from userbot import UserBot

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
MORNING_AT = os.environ.get("MORNING_AT", "07:30")  # ertalabki xulosa vaqti ("off" — o'chiq)

HISTORY = deque(maxlen=int(os.environ.get("HISTORY_LEN", "8")))
UB = UserBot()
TG_LIMIT = 4000

WELCOME = (
    "Assalomu alaykum! Men sizning AI yordamchingizman \U0001F916\n\n"
    "Menga oddiy tilda yozing, men eslatib turaman. Masalan:\n"
    "• har chorshanba 14:00 do'konga borish\n"
    "• har oyning birinchi dushanbasi ishchilar bilan majlis\n"
    "• ertaga 10:00 bankka bor\n"
    "• har 3 kunda 11:00 omborni tekshir\n"
    "• bitmaguncha har kuni 8:00 da elevator ishini eslat\n"
    "• Abbosxon akaga har 3 kunda laminat omborni tekshirishni ayt\n\n"
    "«Bugun nima ishlarim bor?» deb so'rasangiz ham bo'ladi.\n\n"
    "Bajarganingizda: eslatma ostidagi ✅ tugmasi yoki «3 bitdi»\n"
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
        who = t.get("odam") or t.get("odam_ism")
        who = f" → {who}" if who and who.lower() not in t["title"].lower() else ""
        lines.append(f"{t['num']}) {t['title']}{who} — {t['human_time']}{left}")
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


def _norm(s):
    return re.sub(r"[^\w]+", " ", (s or "").lower()).strip()


async def create_tasks(items):
    """AI qaytargan vazifalarni yaratadi. (yaratilganlar, xatolar) qaytaradi."""
    created, errors = [], []
    existing = await db.list_tasks() if items else []
    for r in items or []:
        try:
            same = next((t for t in existing if _norm(t["title"]) == _norm(r.get("title"))
                         and t["kind"] == r.get("kind") and t["trigger"] == r.get("trigger")), None)
            if same:
                errors.append(f"ℹ️ «{same['title']}» allaqachon bor ({same['num']}-vazifa), qayta yozmadim.")
                continue
            nxt = next_fire(r["kind"], r["trigger"])
            if nxt is None:
                raise ValueError("vaqti o'tib ketgan")
            kimga = (r.get("kimga") or "").strip() or None
            known = await kuzatuv.match_known(kimga) if kimga else None
            tid = await db.add_task(
                r["title"], r["kind"], r["trigger"], r.get("human_time", ""), nxt,
                bitguncha=bool(r.get("bitguncha")),
                odam_id=known["id"] if known else None, odam_ism=kimga,
                xabar_matni=(r.get("xabar") or "").strip() or None,
            )
            created.append(tid)
            if kimga and not known:
                # Javob xabaridan keyin "qaysi odam?" deb so'raymiz
                asyncio.get_running_loop().call_later(
                    1.5, lambda t=tid, n=kimga: asyncio.ensure_future(kuzatuv.ask_choose(t, n)))
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
    try:
        ai.PEOPLE = [p["ism"] for p in await db.list_people()]
    except Exception:  # noqa: BLE001
        pass
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
    to_create = result.get("reminders_to_create") or []
    created, errors = await create_tasks(to_create)

    action = result.get("action", "chat")
    reply = result.get("reply") or ""
    HISTORY.append((text, reply or f"[{action}]"))
    if errors and to_create and not created and action == "create":
        reply = "\n".join(errors)  # hech narsa yaratilmadi — AI'ning "yozib oldim"i noto'g'ri bo'lardi
    elif errors:
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
    # Odam bog'lash uchun username/raqam kutilayotgan bo'lsa — AI'siz, darhol
    if kuzatuv.PENDING:
        try:
            res = await kuzatuv.try_resolve_text(update.message.text)
        except Exception as e:  # noqa: BLE001
            log.exception("Odamni bog'lashda xato: %s", e)
            res = "⚠️ Odamni topishda xato bo'ldi, qayta yozib ko'ring."
        if res:
            await update.message.reply_text(res)
            return
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
    mine = []
    for t in await db.due_tasks(now):
        old = t["next_at"]
        new = None
        if t["kind"] in ("recurring", "interval"):
            after = max(now, old) + timedelta(seconds=1)
            new = next_fire(t["kind"], t["trigger"], now=after)
        if not await db.claim_fire(t["id"], old, new):
            continue  # boshqa jarayon allaqachon yubordi
        num = t["id"]
        if t["kind"] == "once":
            t["human_time"] = db.once_time(t["trigger"]) or t["human_time"]
        if t.get("odam_id") or t.get("odam_ism"):
            try:
                await kuzatuv.deliver(t, num)
            except Exception as e:  # noqa: BLE001
                log.exception("Odamga yuborishda xato (id=%s): %s", t["id"], e)
            continue
        t["late"] = (now - old).total_seconds() > 600
        mine.append(t)
    if mine:
        await send_reminders(bot, mine)


async def send_reminders(bot, items):
    """Bir vaqtda kelgan eslatmalar — bitta xabarda (har biriga ✅ tugma)."""
    try:
        if len(items) == 1:
            t = items[0]
            text = (f"\U0001F514 {t['id']}) {t['title']}\n\U0001F552 {t['human_time']}"
                    + (" (kechikib)" if t["late"] else ""))
            await bot.send_message(OWNER_ID, text, reply_markup=reminder_kb(t["id"]))
        else:
            when = now_tz().strftime("%H:%M")
            lines = [f"\U0001F514 {when} — {len(items)} ta ish:", ""]
            lines += [f"{t['id']}) {t['title']}" for t in items]
            btns = [InlineKeyboardButton(f"✅ {t['id']}", callback_data=f"done:{t['id']}") for t in items]
            rows = [btns[i:i + 4] for i in range(0, len(btns), 4)]
            ids = ",".join(str(t["id"]) for t in items)
            if len(ids) <= 50:
                rows.append([InlineKeyboardButton("⏰ Hammasi 1 soatdan keyin", callback_data=f"snzall:{ids}")])
            await bot.send_message(OWNER_ID, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
        log.info("Eslatma yuborildi: %s", [t["id"] for t in items])
    except Exception as e:  # noqa: BLE001
        log.warning("Eslatma yuborilmadi (%s): %s", [t["id"] for t in items], e)


def reminder_kb(tid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Bajarildi", callback_data=f"done:{tid}"),
        InlineKeyboardButton("⏰ 1 soatdan keyin", callback_data=f"snz:{tid}:60"),
        InlineKeyboardButton("📅 Ertaga", callback_data=f"snz:{tid}:tm"),
    ]])


async def snooze(tid, how):
    """Eslatmani keyinga suradi. Takroriy vazifaning asosiy jadvali o'zgarmaydi:
    navbatdagi eslatma suriladi, keyingilari odatdagidek davom etadi."""
    t = await db.get_task(tid)
    if not t or t["status"] == "bitdi":
        return None
    now = now_tz()
    if how == "tm":
        base = t["next_at"] if t["kind"] == "once" and t["next_at"] else None
        if t["kind"] == "recurring":
            hour, minute = int(t["trigger"].get("hour", 9)), int(t["trigger"].get("minute", 0))
        elif t["kind"] == "interval" and t["trigger"].get("start"):
            st = datetime.fromisoformat(t["trigger"]["start"])
            hour, minute = st.hour, st.minute
        else:
            hour, minute = (base or now).hour, (base or now).minute
        at = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    else:
        at = now + timedelta(minutes=int(how))
    if t["kind"] != "once":
        regular = next_fire(t["kind"], t["trigger"], now=now + timedelta(seconds=1))
        if regular and regular < at:
            at = regular  # odatdagi navbat baribir ertaroq keladi
    await db.set_status(tid, "faol" if t["status"] == "pauza" else t["status"], at)
    return at


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or q.from_user.id != OWNER_ID:
        return
    parts = (q.data or "").split(":")
    if parts[0] == "snzall" and len(parts) > 1:
        ids = [int(x) for x in parts[1].split(",") if x.isdigit()]
        done = 0
        for i in ids:
            if await snooze(i, "60"):
                done += 1
        await q.answer(f"{done} ta ish 1 soatga surildi")
        if q.message:
            at = (now_tz() + timedelta(hours=1)).strftime("%H:%M")
            await q.edit_message_text((q.message.text or "") + f"\n\n⏰ Qolganlari {at} da yana eslatiladi")
        return
    try:
        tid = int(parts[1])
    except (IndexError, ValueError):
        await q.answer()
        return
    base = (q.message.text or "") if q.message else ""
    if parts[0] == "del":
        t = await db.get_task(tid)
        ok = await db.delete_task(tid)
        await q.answer("Bekor qilindi" if ok else "Bu vazifa endi yo'q")
        if q.message:
            await q.edit_message_text(base + ("\n\n❌ Bekor qilindi (unga xabar bormaydi)" if ok else ""))
        return
    if parts[0] == "odamyoz":
        t = await db.get_task(tid)
        kuzatuv.wait_username(tid, (t or {}).get("odam_ism") or "odam")
        await q.answer()
        if q.message:
            await q.edit_message_text(base + "\n\n✍️ @username yoki telefon raqamini yozib yuboring.")
        return
    if parts[0] == "odam" and len(parts) > 2:
        try:
            msg = await kuzatuv.choose(tid, int(parts[2]))
        except Exception as e:  # noqa: BLE001
            log.exception("Odam tanlashda xato: %s", e)
            msg = "⚠️ Odamni saqlab bo'lmadi, qayta urinib ko'ring."
        await q.answer()
        if q.message:
            await q.edit_message_text(base + "\n\n" + msg)
        return
    if parts[0] == "done":
        r = await db.mark_done(tid)
        await q.answer("Bajarildi ✅" if r else "Bu vazifa endi yo'q")
        if not q.message:
            return
        markup = getattr(q.message, "reply_markup", None)
        grouped = markup and sum(len(row) for row in markup.inline_keyboard) > 3
        if grouped:
            # Guruhli xabar: shu qatorni ✅ qilib, tugmasini olib tashlaymiz
            new_text = "\n".join(("✅ " + ln) if ln.startswith(f"{tid}) ") else ln for ln in base.split("\n"))
            rows = [[b for b in row if b.callback_data != f"done:{tid}"] for row in markup.inline_keyboard]
            rows = [row for row in rows if row]
            if not any(b.callback_data.startswith("done:") for row in rows for b in row):
                rows = []
            await q.edit_message_text(new_text, reply_markup=InlineKeyboardMarkup(rows) if rows else None)
        else:
            await q.edit_message_text(base + ("\n\n✅ Bajarildi" if r else "\n\n— vazifa topilmadi"))
    elif parts[0] == "snz" and len(parts) > 2:
        at = await snooze(tid, parts[2])
        if not at:
            await q.answer("Bu vazifa endi yo'q")
            return
        when = at.astimezone(TZ)
        label = ("ertaga " if when.date() > now_tz().date() else "") + when.strftime("%H:%M")
        await q.answer(f"Eslataman: {label}")
        if q.message:
            await q.edit_message_text(base + f"\n\n⏰ Yana eslataman: {label}")
    else:
        await q.answer()


async def morning_digest(bot):
    now = now_tz()
    tasks = await db.list_tasks()
    today, late = [], []
    for t in tasks:
        if t["status"] == "pauza":
            continue
        if t["kind"] == "once" and t["next_at"] is None:
            late.append(t)
            continue
        for hhmm in filter(None, today_times(t, now).split(", ")):
            today.append((hhmm, t))
    today.sort(key=lambda x: x[0])
    if not today and not late:
        return
    lines = [f"☀️ Xayrli tong! Bugun {len(today)} ta ish:"] if today else ["☀️ Xayrli tong! Bugun rejada ish yo'q."]
    lines += [f"{hhmm} — {t['num']}) {t['title']}" for hhmm, t in today]
    if late:
        lines += ["", f"⚠️ Muddati o'tgan, bajarilmagan ({len(late)}):"]
        lines += [f"{t['num']}) {t['title']}" for t in late]
        lines += ["", "Bajarilganlarini «<raqam> bitdi» deb yozing."]
    await send_long(bot, "\n".join(lines))


async def scheduler_loop(bot):
    last_month = now_tz().month
    start = now_tz()
    # Bot xulosa vaqtidan keyin yonsa — bugungisi allaqachon yuborilgan deb hisoblaymiz
    last_digest = start.date() if start.strftime("%H:%M") >= MORNING_AT else None
    tick = 0
    while True:
        tick += 1
        try:
            await fire_due(bot)
            if tick % 2 == 0:  # har daqiqada
                await kuzatuv.followup_tick()
                await kuzatuv.flush_chats()
            if tick % 120 == 1:  # soatiga bir marta
                gone = await db.archive_stale_once(24)
                if gone:
                    log.info("Avtomatik tarixga o'tkazildi: %s", [g["id"] for g in gone])
            now = now_tz()
            if MORNING_AT != "off" and last_digest != now.date() and now.strftime("%H:%M") >= MORNING_AT:
                last_digest = now.date()
                await morning_digest(bot)
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
    app.add_handler(CallbackQueryHandler(on_button))
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

    kuzatuv.init(app.bot, UB, reminder_kb)

    async def _probe():
        try:
            log.info("Gemini tarifi: %s", await asyncio.to_thread(ai.billing_probe))
        except Exception as e:  # noqa: BLE001
            log.warning("Gemini tarifini tekshirib bo'lmadi: %s", e)
    asyncio.create_task(_probe())
    try:
        await UB.start()
    except Exception as e:  # noqa: BLE001
        log.exception("Userbot ishga tushmadi: %s", e)

    runner = await webapp.start(handle_text, ai_error_text, port=int(os.environ.get("PORT", "8080")),
                                ub=UB, bot=app.bot)
    sched = asyncio.create_task(scheduler_loop(app.bot))
    log.info("Bot tayyor. AI: %s. Mini App: %s. Userbot: %s", ai.provider(), url or "(manzil yo'q)",
             "ulangan" if UB.connected else "ulanmagan")

    try:
        await asyncio.Event().wait()
    finally:
        sched.cancel()
        await UB.stop()
        await runner.cleanup()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
