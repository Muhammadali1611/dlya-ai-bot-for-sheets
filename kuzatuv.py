"""Odamlarga vazifa yuborish va javobni kuzatish.

Oqim:
  vazifa vaqti keldi -> egasining profilidan odamga xabar (kirill/lotin) -> egasiga nusxa
  -> odam o'qidimi? (har daqiqada tekshiriladi)
  -> o'qib, FOLLOWUP[0] daqiqa javob bermasa — xabarga reply qilib so'raydi; FOLLOWUP[1] da yana
  -> baribir javob bo'lmasa — egasiga xabar
  -> odam javob yozsa (matn yoki ovoz) — AI tushunadi: bajarildi / ko'chirildi / ma'lumot / muammo
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import ai
import db
from translit import to_cyrillic
from userbot import name_score

log = logging.getLogger("reminder-bot.kuzatuv")

TZ = ZoneInfo(os.environ.get("TZ", "Asia/Tashkent"))
OWNER_ID = int(os.environ.get("OWNER_ID", "7242956193"))
FOLLOWUP = [int(x) for x in os.environ.get("FOLLOWUP_MINUTES", "60,180").split(",") if x.strip()]
GIVE_UP_AFTER = int(os.environ.get("GIVE_UP_MINUTES", "60"))   # oxirgi so'rovdan keyin
UNREAD_ALERT = int(os.environ.get("UNREAD_ALERT_MINUTES", "360"))  # o'qilmasa egasiga xabar

BOT = None      # telegram.Bot
UB = None       # userbot.UserBot
REMINDER_KB = None


def init(bot, ub, reminder_kb):
    global BOT, UB, REMINDER_KB
    BOT, UB, REMINDER_KB = bot, ub, reminder_kb
    ub.on_message = on_message


def now_tz():
    return datetime.now(TZ)


def for_person(text, alifbo):
    return to_cyrillic(text) if (alifbo or "kiril") == "kiril" else text


def when_text(dt):
    dt = dt.astimezone(TZ)
    today = now_tz().date()
    if dt.date() == today:
        day = "bugun"
    elif dt.date() == today + timedelta(days=1):
        day = "ertaga"
    else:
        oy = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust",
              "sentabr", "oktabr", "noyabr", "dekabr"][dt.month - 1]
        day = f"{dt.day}-{oy}"
    return f"{day} {dt:%H:%M}"


async def owner(text, kb=None):
    try:
        await BOT.send_message(OWNER_ID, text, reply_markup=kb)
    except Exception as e:  # noqa: BLE001
        log.warning("Egasiga xabar yuborilmadi: %s", e)


# ---------- odamni aniqlash ----------

async def match_known(name):
    """Avval tanlangan odamlar orasidan ism bo'yicha topadi."""
    best, best_s = None, 0.0
    for p in await db.list_people():
        s = name_score(name, p["ism"])
        if s > best_s:
            best, best_s = p, s
    return best if best_s >= 0.85 else None


async def ask_choose(tid, name):
    """Yangi ism: egasidan Telegram'dagi qaysi odam ekanini so'raydi."""
    if not UB or not UB.connected:
        await owner(
            f"👤 «{name}» ga yuborish uchun akkauntingiz ulanmagan.\n"
            "Mini App → «Odamlar» → «Akkauntni ulash». Ulanmaguncha bu vazifani sizga eslataman.")
        return
    try:
        cands = await UB.find_people(name)
    except Exception as e:  # noqa: BLE001
        log.exception("Odam qidirishda xato: %s", e)
        cands = []
    if not cands:
        await owner(
            f"👤 «{name}» ni kontaktlaringiz va yozishmalaringizdan topa olmadim.\n"
            "Telefoningizdagi kontakt nomini aytib qayta yozing (masalan: «4-vazifani Abbos Karimovga biriktir»).\n"
            "Hozircha bu vazifani sizga eslataman.")
        return
    rows = []
    for c in cands:
        extra = f" (@{c['username']})" if c.get("username") else (f" (+{c['phone'][-4:]}…)" if c.get("phone") else "")
        rows.append([InlineKeyboardButton(f"{c['name']}{extra}"[:60], callback_data=f"odam:{tid}:{c['id']}")])
    rows.append([InlineKeyboardButton("❌ Hech biri emas", callback_data=f"odam:{tid}:0")])
    await owner(f"👤 «{name}» — Telegram'da qaysi biri?\nBir marta tanlasangiz, keyin o'zim taniyman.",
                InlineKeyboardMarkup(rows))


async def choose(tid, uid):
    """Egasi odamni tanladi. Natija matnini qaytaradi."""
    task = await db.get_task(tid)
    if not task:
        return "Bu vazifa endi yo'q."
    if uid == 0:
        return f"Mayli. «{task['title']}» ni sizga eslatib turaman."
    info = await UB.get_user(uid)
    name = task.get("odam_ism") or info["name"]
    pid = await db.upsert_person(uid, name, info["name"], info.get("username"), info.get("phone"))
    # Shu ism bilan kutib turgan boshqa vazifalar ham shu odamga
    for t in await db.list_tasks():
        if t["id"] == tid or (not t["odam_id"] and t.get("odam_ism") and name_score(name, t["odam_ism"]) >= 0.85):
            await db.set_task_person(t["id"], pid)
    return f"✅ {name} = {info['name']}. Vazifa vaqti kelganda unga sizning nomingizdan yuboriladi."


# ---------- yuborish ----------

async def deliver(t, num):
    """Vaqti kelgan, odamga biriktirilgan vazifani yuboradi."""
    text = (t.get("xabar_matni") or t["title"]).strip()
    who = t.get("odam") or t.get("odam_ism") or "odam"
    reason = None
    if not UB or not UB.connected:
        reason = "akkauntingiz ulanmagan"
    elif not t.get("odam_tg"):
        reason = "odam hali tanlanmagan"
    if reason:
        await owner(f"🔔 {num}) {t['title']}\n📤 {who}ga yuborish kerak edi, lekin {reason}.\n\nXabar: {text}",
                    REMINDER_KB(t["id"]))
        if reason == "odam hali tanlanmagan" and UB and UB.connected:
            await ask_choose(t["id"], who)
        return
    msg = for_person(text, t.get("odam_alifbo"))
    try:
        mid = await UB.send(t["odam_tg"], msg)
    except Exception as e:  # noqa: BLE001
        log.exception("Odamga yuborishda xato: %s", e)
        await owner(f"⚠️ {who}ga yuborib bo'lmadi ({e.__class__.__name__}).\n🔔 {num}) {t['title']}",
                    REMINDER_KB(t["id"]))
        return
    await db.add_send(t["id"], t["odam_id"], msg, mid, t["odam_tg"], t["title"])
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Bajarildi", callback_data=f"done:{t['id']}")]])
    await owner(f"📤 {who}ga yuborildi ({num}-vazifa):\n«{msg}»\n\nJavobini kuzataman.", kb)
    log.info("Odamga yuborildi: vazifa=%s odam=%s", t["id"], t["odam_id"])


# ---------- kuzatuv ----------

FOLLOW_TEXTS = [
    "{ism}, шу иш нима бўлди?",
    "{ism}, шу ишни қилдингизми? Жавоб ёзиб юборинг, илтимос.",
]


async def followup_tick():
    if not UB or not UB.connected:
        return
    sends = await db.open_sends()
    if not sends:
        return
    now = now_tz()
    read_cache = {}
    for s in sends:
        chat = s["tg_chat_id"]
        try:
            if not s["oqilgan_at"]:
                if chat not in read_cache:
                    read_cache[chat] = await UB.read_outbox_max(chat)
                if read_cache[chat] >= (s["tg_msg_id"] or 0):
                    await db.update_send(s["id"], oqilgan_at=now,
                                         holat="yuborildi" if s["holat"] == "oqilmadi" else s["holat"])
                    s["oqilgan_at"] = now
                elif s["holat"] == "yuborildi" and now - s["yuborilgan_at"] >= timedelta(minutes=UNREAD_ALERT):
                    await db.update_send(s["id"], holat="oqilmadi")
                    await owner(f"📭 {s['odam']} hali o'qimadi: {s['title']}\n(yuborilgan: {when_text(s['yuborilgan_at'])})")
                    continue
                if not s["oqilgan_at"]:
                    continue
            n = s["sorash_soni"]
            if n < len(FOLLOW_TEXTS) and n < len(FOLLOWUP):
                if now >= s["oqilgan_at"] + timedelta(minutes=FOLLOWUP[n]):
                    ism = s["odam"] or ""
                    q = FOLLOW_TEXTS[n].format(ism=to_cyrillic(ism))
                    if (s.get("alifbo") or "kiril") == "lotin":
                        q = ["{ism}, shu ish nima bo'ldi?",
                             "{ism}, shu ishni qildingizmi? Javob yozib yuboring, iltimos."][n].format(ism=ism)
                    await UB.send(chat, q, reply_to=s["tg_msg_id"])
                    await db.update_send(s["id"], sorash_soni=n + 1, oxirgi_sorash_at=now, holat="soraldi")
                    log.info("Qayta so'raldi (%s): yuborish=%s", n + 1, s["id"])
            elif s["oxirgi_sorash_at"] and now >= s["oxirgi_sorash_at"] + timedelta(minutes=GIVE_UP_AFTER):
                await db.update_send(s["id"], holat="javobsiz")
                kb = REMINDER_KB(s["vazifa_id"]) if s["vazifa_id"] else None
                await owner(f"⚠️ {s['odam']} javob bermadi ({n} marta so'radim):\n{s['title']}", kb)
        except Exception as e:  # noqa: BLE001
            log.exception("Kuzatuvda xato (yuborish=%s): %s", s["id"], e)


# ---------- odamning javobi ----------

async def on_message(event):
    person = await db.get_person_by_tg(event.sender_id)
    if not person:
        return  # faqat "Odamlar" ro'yxatidagilar
    s = await db.open_send_for(person["id"], event.reply_to_msg_id)
    if not s:
        return  # bu odamga hozir kutilayotgan vazifa yo'q — shaxsiy yozishmaga tegmaymiz

    m = event.message
    audio, mime, kind, text = None, "audio/ogg", "matn", (m.message or "").strip()
    if m.voice or m.audio or m.video_note:
        audio = await event.download_media(bytes)
        mime = (m.file.mime_type if m.file else None) or ("video/mp4" if m.video_note else "audio/ogg")
        kind = "ovoz"
    elif not text:
        kind = "boshqa"
        text = "[rasm yoki fayl yubordi]"

    if kind == "boshqa":
        result = {"qaror": "malumot", "matn": text, "xulosa": "Rasm yoki fayl yubordi — Telegram'da ko'ring."}
    else:
        try:
            result = await asyncio.to_thread(
                ai.analyze_reply, s["title"] or "", s["matn"], text, audio, mime,
                now_tz().strftime("%Y-%m-%d %H:%M (%A)"))
        except Exception as e:  # noqa: BLE001
            log.exception("Javobni tahlil qilib bo'lmadi: %s", e)
            result = {"qaror": "malumot", "matn": text or "(ovozli xabar)",
                      "xulosa": "AI tahlil qila olmadi — o'zingiz ko'ring."}

    q, said = result.get("qaror"), (result.get("matn") or text or "").strip()
    who = person["ism"]
    icon = "🎤 " if kind == "ovoz" else ""
    await db.update_send(s["id"], javob=said, javob_turi=kind, ai_qaror=result)
    alifbo = person.get("alifbo")

    if q == "bajarildi":
        if s["vazifa_id"]:
            await db.mark_done(s["vazifa_id"], izoh=f"{who}: {said}"[:500])
        await db.update_send(s["id"], holat="bajarildi")
        await _reply(event, for_person("Rahmat!", alifbo))
        await owner(f"✅ {who}: {icon}«{said}»\n→ «{s['title']}» bajarildi deb belgiladim.")
    elif q == "kochirildi":
        try:
            dt = datetime.fromisoformat(result["yangi_vaqt"])
            dt = dt if dt.tzinfo else dt.replace(tzinfo=TZ)
        except (KeyError, TypeError, ValueError):
            dt = None
        if dt and dt > now_tz() and s["vazifa_id"]:
            await db.set_next(s["vazifa_id"], dt)
            await db.update_send(s["id"], holat="kochirildi")
            await _reply(event, for_person(f"Xo'p, {when_text(dt)} da yana eslataman.", alifbo))
            await owner(f"🕓 {who} {when_text(dt)} ga ko'chirdi: {icon}«{said}»\n({s['title']})")
        else:
            await db.update_send(s["id"], holat="javob")
            await owner(f"💬 {who} javob berdi: {icon}«{said}»\n({s['title']})",
                        REMINDER_KB(s["vazifa_id"]) if s["vazifa_id"] else None)
    else:
        await db.update_send(s["id"], holat="javob")
        head = "⚠️ Muammo" if q == "muammo" else "💬 Javob"
        xul = result.get("xulosa") or ""
        await owner(f"{head} — {who}: {icon}«{said}»\n{xul}\n({s['title']})".strip(),
                    REMINDER_KB(s["vazifa_id"]) if s["vazifa_id"] else None)


async def _reply(event, text):
    try:
        await event.reply(text)
    except Exception as e:  # noqa: BLE001
        log.warning("Odamga javob yozib bo'lmadi: %s", e)
