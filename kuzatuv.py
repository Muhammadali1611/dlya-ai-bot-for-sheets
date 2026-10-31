"""Odamlarga vazifa yuborish va javobni kuzatish.

Oqim:
  vazifa vaqti keldi -> egasining profilidan odamga xabar (kirill/lotin) -> egasiga nusxa
  -> odam o'qidimi? (har daqiqada tekshiriladi)
  -> o'qib, FOLLOWUP[0] daqiqa javob bermasa — xabarga reply qilib so'raydi; FOLLOWUP[1] da yana
  -> baribir javob bo'lmasa — egasiga xabar
  -> odam javob yozsa (matn yoki ovoz) — AI tushunadi: bajarildi / ko'chirildi / ma'lumot / muammo
"""

import os
import re
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import ai
import db
import triggers
from translit import to_cyrillic
from userbot import name_score

log = logging.getLogger("reminder-bot.kuzatuv")

TZ = ZoneInfo(os.environ.get("TZ", "Asia/Tashkent"))
OWNER_ID = int(os.environ.get("OWNER_ID", "7242956193"))
FOLLOWUP = [int(x) for x in os.environ.get("FOLLOWUP_MINUTES", "60,180").split(",") if x.strip()]
GIVE_UP_AFTER = int(os.environ.get("GIVE_UP_MINUTES", "60"))   # oxirgi so'rovdan keyin
UNREAD_ALERT = int(os.environ.get("UNREAD_ALERT_MINUTES", "360"))  # o'qilmasa egasiga xabar

PENDING = {}    # egasi username/raqam yozishini kutyapmiz: {"tid", "name", "until"}
REQUESTS = {}   # aniqlashtirilayotgan so'rovlar: {tg_id: [{"dialog": [...], "until": dt, "q_msg": id}, ...]}


def _active(uid):
    """Shu odam bilan hali tugamagan so'rovlar (eskirganlari tashlanadi)."""
    now = now_tz()
    lst = [r for r in REQUESTS.get(uid, []) if r.get("until") and now < r["until"]]
    if lst:
        REQUESTS[uid] = lst
    else:
        REQUESTS.pop(uid, None)
    return lst


def _drop(uid, req):
    lst = [r for r in REQUESTS.get(uid, []) if r is not req]
    if lst:
        REQUESTS[uid] = lst
    else:
        REQUESTS.pop(uid, None)
# Faqat shu so'zlar bor xabarlar AI'ga yuboriladi (boshqa yozishmalaringiz AI'ga bormaydi)
CHATS = {}      # suhbat bufferi: {chat_id: {"name", "msgs": [(kim, matn, dt)], "last": dt, "kiril": bool}}
CHAT_QUIET = int(os.environ.get("CHAT_QUIET_SECONDS", "90"))      # shuncha jimlikdan keyin tahlil
CHAT_DAILY_LIMIT = int(os.environ.get("CHAT_DAILY_LIMIT", "800"))  # kuniga AI tahlillari chegarasi
LISTEN_ALL = os.environ.get("LISTEN_ALL", "on") != "off"
LISTEN_VOICE = os.environ.get("LISTEN_VOICE", "on") != "off"
VOICE_MAX_SEC = int(os.environ.get("VOICE_MAX_SECONDS", "300"))   # bundan uzun ovozlar olinmaydi
VOICE_PER_CHAT = 6                                                  # bitta tahlilga ko'pi bilan
_daily = {"day": None, "n": 0}
# Vaqt yoki va'da belgisi bo'lmagan suhbat AI'ga yuborilmaydi (limitni tejash uchun)
ACTION_RE = re.compile(
    r"(\d|ertaga|indin|bugun|kechqurun|ertalab|tushdan|dushanba|seshanba|chorshanba|payshanba|juma|shanba|"
    r"yakshanba|hafta|oy oxiri|soat|eslat|beraman|qilaman|tashlayman|yuboraman|o'tkazaman|jo'nataman|"
    r"kelaman|boraman|olib kelaman|qo'ng'iroq|ko'rishamiz|uchrashamiz|ertага|эртага|бугун|соат|"
    r"бераман|қиламан|юбораман|ўтказаман|келаман|бораман|завтра|сегодня|в \d)", re.I)
# Kunga bog'langan reja: "ertaga jumaga boramiz", "2 kuni bankka borish kerak" — darhol aniqlashtiramiz
PLAN_DAY_RE = re.compile(
    r"(ertaga|indin|dushanba|seshanba|chorshanba|payshanba|juma|shanba|yakshanba|keyingi hafta|"
    r"\b\d{1,2}\s*-?\s*(kuni|sida|sana|noyabr|dekabr|yanvar)|"
    r"эртага|индин|душанба|сешанба|чоршанба|пайшанба|жума|шанба|якшанба|\b\d{1,2}\s*-?\s*(куни|сида))", re.I)
PLAN_ACT_RE = re.compile(
    r"(boramiz|boraman|borish|borasiz|kelaman|kelamiz|kelish|kelasiz|uchrash|ko'rish|ko‘rish|qilamiz|qilish kerak|"
    r"kerak|olib|beraman|berasiz|to'la|tola|yuborasiz|tayyorla|чиқамиз|борамиз|бораман|бориш|келаман|келамиз|"
    r"yeymiz|ovqat|gaplashamiz|chiqamiz|ketamiz|yig'ilamiz|majlis|"
    r"учраш|кўриш|қиламиз|керак|оламиз|берасиз|тайёрла|еймиз|овқат|гаплашамиз|кетамиз)", re.I)
REASK_MIN = int(os.environ.get("REASK_MINUTES", "60"))   # savolga javob bo'lmasa, shuncha daqiqadan keyin yana so'raydi
REMIND_RE = re.compile(r"(eslat|eslab|esla[bt]ib|esimga sol|yodimga sol|эслат|эслаб|напомн|напомин)", re.I)
BOT = None      # telegram.Bot
UB = None       # userbot.UserBot
REMINDER_KB = None


def init(bot, ub, reminder_kb):
    global BOT, UB, REMINDER_KB
    BOT, UB, REMINDER_KB = bot, ub, reminder_kb
    ub.on_message = on_message
    ub.on_outgoing = on_outgoing


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


async def owner(text, kb=None, parse=False):
    try:
        await BOT.send_message(OWNER_ID, text, reply_markup=kb, parse_mode="Markdown" if parse else None)
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
        wait_username(tid, name)
        await owner(
            f"👤 «{name}» ni kontaktlaringizdan topa olmadim.\n\n"
            "Uning @username'ini yoki telefon raqamini yozib yuboring — shu odamga bog'layman.\n"
            "(Masalan: @ibrohim_aka yoki 90 123 45 67)")
        return
    rows = []
    for c in cands:
        extra = f" (@{c['username']})" if c.get("username") else (f" (+{c['phone'][-4:]}…)" if c.get("phone") else "")
        rows.append([InlineKeyboardButton(f"{c['name']}{extra}"[:60], callback_data=f"odam:{tid}:{c['id']}")])
    rows.append([InlineKeyboardButton("✍️ Username yoki raqam yozaman", callback_data=f"odamyoz:{tid}")])
    rows.append([InlineKeyboardButton("❌ O'zimga eslatma qolsin", callback_data=f"odam:{tid}:0")])
    await owner(f"👤 «{name}» — Telegram'da qaysi biri?\nBir marta tanlasangiz, keyin o'zim taniyman.",
                InlineKeyboardMarkup(rows))


def wait_username(tid, name):
    PENDING.clear()
    PENDING.update({"tid": tid, "name": name, "until": now_tz() + timedelta(minutes=30)})


async def try_resolve_text(text):
    """Egasi username/raqam yozgan bo'lsa — odamni bog'laydi. Javob matni yoki None."""
    if not PENDING or now_tz() > PENDING["until"]:
        PENDING.clear()
        return None
    t = text.strip()
    if not (t.startswith("@") or "t.me/" in t or (sum(ch.isdigit() for ch in t) >= 9 and len(t) <= 25)):
        return None
    tid = PENDING["tid"]
    try:
        info = await UB.resolve(t)
    except ValueError as e:
        return f"⚠️ {e}"
    PENDING.clear()
    return await choose(tid, info["id"], info)


async def choose(tid, uid, info=None):
    """Egasi odamni tanladi. Natija matnini qaytaradi."""
    task = await db.get_task(tid)
    if not task:
        return "Bu vazifa endi yo'q."
    if uid == 0:
        return f"Mayli. «{task['title']}» ni sizga eslatib turaman."
    info = info or await UB.get_user(uid)
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
        if t.get("manba") == "sorov":
            reason += " (bu odamning o'zi eslatishni so'ragan edi)"
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
    if t.get("manba") in ("sorov", "suhbat"):
        # Kelishilgan reja / o'zi so'ragan eslatma: yuboramiz va javobini kuzatamiz —
        # javob bo'lmasa "shu ish nima bo'ldi?" deb o'zi so'raydi
        await db.add_send(t["id"], t["odam_id"], msg, mid, t["odam_tg"], t["title"])
        why = "kelishilgan edi" if t.get("manba") == "sorov" else "yozishmada gaplashilgan edi"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Bajarildi", callback_data=f"done:{t['id']}")]])
        await owner(f"📤 {who}ga eslatdim ({why}):\n«{msg}»\n\nJavobini kuzataman.", kb)
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


async def reask_tick():
    """Aniqlashtiruvchi savolga javob kelmasa: bir marta yana so'raydi, keyin egasiga aytadi."""
    if not UB or not UB.connected or not REQUESTS:
        return
    now = now_tz()
    for uid, req in [(u, r) for u, lst in list(REQUESTS.items()) for r in list(lst)]:
        asked = req.get("asked_at")
        if not asked or req.get("dialog", [("u", "")])[-1][0] != "men":
            continue
        try:
            if not req.get("reasked") and now - asked >= timedelta(minutes=REASK_MIN):
                q = req.get("last_q") or "Soat nechada?"
                q2 = q if q.lower().startswith(("kechirasiz", "uzr")) else "Kechirasiz, " + q
                mid = await UB.send(uid, for_person(q2, req.get("alifbo")), reply_to=req.get("q_msg"))
                req.update(reasked=True, asked_at=now, until=now + timedelta(minutes=REASK_MIN * 2 + 30))
                req["dialog"].append(("men", q2))
                log.info("Aniqlashtirish qayta so'raldi: %s", uid)
            elif req.get("reasked") and now - asked >= timedelta(minutes=REASK_MIN * 2):
                _drop(uid, req)
                first = next((m for k, m in req["dialog"] if k == "u"), "")
                await owner(f"⚠️ {req.get('name', 'Odam')} vaqtini aytmadi (2 marta so'radim):\n«{first[:200]}»\n"
                            "Kerak bo'lsa botga o'zingiz vaqtini yozing.")
        except Exception as e:  # noqa: BLE001
            log.warning("Qayta so'rashda xato (%s): %s", uid, e)


async def followup_tick():
    await reask_tick()
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

def is_cyrillic(text):
    cyr = len(re.findall(r"[А-Яа-яЁёЎўҚқҒғҲҳ]", text or ""))
    lat = len(re.findall(r"[A-Za-z]", text or ""))
    return cyr > lat


async def on_message(event):
    uid = event.sender_id
    text = (event.message.message or "").strip()

    reqs = _active(uid) if text else []
    # Ochiq savol bo'lsa, kun so'zi bor har xabarni avval yangi reja deb ko'ramiz
    # ("4 kuni soat 3da ovqat yeymiz"); reja bo'lmasa — ochiq savolga javob sifatida olinadi
    is_new = bool(text) and bool(REMIND_RE.search(text) or (
        PLAN_DAY_RE.search(text) and (PLAN_ACT_RE.search(text) or reqs)))

    # 1) Bizning aniqlashtiruvchi savolimizga reply qilib javob berdi
    rto = event.reply_to_msg_id
    pick = next((r for r in reqs if rto and r.get("q_msg") == rto), None)
    if pick:
        await handle_request(event, text, pick)
        return

    # 2) Yangi so'rov yoki kunga bog'langan reja — alohida suhbat sifatida aniqlashtiramiz
    if is_new and await handle_request(event, text):
        return

    # 3) Ochiq savolimiz bor ("soat nechada?") — oddiy javob ("10da") eng oxirgi savolga tegishli
    if reqs:
        await handle_request(event, text, reqs[-1])
        return

    # 4) Biz yuborgan vazifaga javob
    person = await db.get_person_by_tg(uid)
    s = await db.open_send_for(person["id"], event.reply_to_msg_id) if person else None
    if s:
        await handle_reply(event, person, s)
        return

    # 4) Qolgan hamma xabar — suhbat bufferiga, jim qolganda tahlil qilinadi
    await _buffer(event, "u")


# ---------- barcha shaxsiy suhbatlarni tinglash ----------

async def on_outgoing(event):
    """Siz o'zingiz yozgan xabar — suhbat bufferiga."""
    await _buffer(event, "men")


async def _buffer(event, who):
    if not LISTEN_ALL:
        return
    m = event.message
    text = (m.message or "").strip()
    audio = None
    if LISTEN_VOICE and (getattr(m, "voice", None) or getattr(m, "video_note", None)):
        dur = getattr(getattr(m, "file", None), "duration", None) or 0
        if dur and dur > VOICE_MAX_SEC:
            return
        audio = True  # yuklab olish pastda, chat tekshirilgandan keyin
    elif len(text) < 4:
        return
    chat_id = getattr(event, "chat_id", None) or event.sender_id
    c = CHATS.get(chat_id)
    if c is None:
        ent = await event.get_chat()
        if getattr(ent, "bot", False) or getattr(ent, "deleted", False):
            return
        name = " ".join(x for x in [getattr(ent, "first_name", None), getattr(ent, "last_name", None)] if x) \
            or getattr(ent, "username", None) or "odam"
        known = await db.get_person_by_tg(chat_id)
        c = CHATS[chat_id] = {"name": known["ism"] if known else name, "tg_name": name, "msgs": [],
                              "kiril": None, "username": getattr(ent, "username", None),
                              "phone": getattr(ent, "phone", None)}
    if audio:
        if sum(1 for x in c["msgs"] if x[3]) >= VOICE_PER_CHAT:
            c["msgs"] = [x for x in c["msgs"] if not x[3]] + [x for x in c["msgs"] if x[3]][1:]
        try:
            data = await event.download_media(bytes)
        except Exception as e:  # noqa: BLE001
            log.warning("Ovozli xabarni yuklab bo'lmadi: %s", e)
            return
        mime = (m.file.mime_type if getattr(m, "file", None) else None) or (
            "video/mp4" if getattr(m, "video_note", None) else "audio/ogg")
        audio = (data, mime)
    c["msgs"].append((who, text[:600], now_tz(), audio))
    c["msgs"] = c["msgs"][-25:]
    c["last"] = now_tz()
    if who == "u" and c["kiril"] is None:
        c["kiril"] = is_cyrillic(text)


async def flush_chats():
    """Jim qolgan suhbatlarni AI bilan tahlil qiladi (har daqiqada chaqiriladi)."""
    if not CHATS:
        return
    now = now_tz()
    for chat_id in list(CHATS):
        c = CHATS[chat_id]
        if (now - c["last"]).total_seconds() < CHAT_QUIET:
            continue
        CHATS.pop(chat_id, None)
        joined = " ".join(m for _, m, _, _ in c["msgs"])
        has_voice = any(a for _, _, _, a in c["msgs"])
        if not has_voice and not ACTION_RE.search(joined):
            continue
        if _daily["day"] != now.date():
            _daily.update(day=now.date(), n=0)
        if _daily["n"] >= CHAT_DAILY_LIMIT:
            log.warning("Suhbat tahlili kunlik chegarasiga yetdi (%s)", CHAT_DAILY_LIMIT)
            continue
        _daily["n"] += 1
        try:
            found = await asyncio.to_thread(
                ai.analyze_chat, [(k, m, t.strftime("%H:%M"), a) for k, m, t, a in c["msgs"]], c["name"],
                now.strftime("%Y-%m-%d %H:%M (%A)"))
        except Exception as e:  # noqa: BLE001
            if not c.get("retried"):
                # Yo'qolib ketmasin: 2 daqiqadan keyin yana bir marta urinamiz
                c["retried"] = True
                c["last"] = now
                if chat_id not in CHATS:
                    CHATS[chat_id] = c
                else:  # shu orada yangi xabarlar kelgan — birlashtiramiz
                    CHATS[chat_id]["msgs"] = (c["msgs"] + CHATS[chat_id]["msgs"])[-25:]
                log.warning("Suhbatni tahlil qilib bo'lmadi (%s), qayta urinaman: %s", c["name"], e)
            else:
                log.warning("Suhbatni tahlil qilib bo'lmadi (%s): %s", c["name"], e)
            continue
        if found:
            await _save_found(chat_id, c, found)


async def _save_found(chat_id, c, found):
    existing = await db.list_tasks()
    lines, buttons = [], []
    pid = None
    for v in found[:4]:
        title = v["title"].strip()
        # bir xil vazifa allaqachon bor bo'lsa — qayta yozmaymiz
        if any(t["trigger"] == v["trigger"] and name_score(title, t["title"]) >= 0.8 for t in existing):
            continue
        if v["kimga"] == "u":
            if pid is None:
                known = await db.get_person_by_tg(chat_id)
                if known:
                    pid = known["id"]
                else:
                    pid = await db.upsert_person(chat_id, c["name"], c["tg_name"], c.get("username"), c.get("phone"))
                    await db.set_alifbo(pid, "kiril" if c.get("kiril") else "lotin")
            tid = await db.add_task(title, v["kind"], v["trigger"], v.get("human_time") or "", v["next"],
                                    odam_id=pid, odam_ism=c["name"], xabar_matni=v["xabar"], manba="suhbat")
            lines.append(f"• {when_text(v['next'])} — {c['name']}ga eslataman: «{v['xabar']}»")
        else:
            tid = await db.add_task(title, v["kind"], v["trigger"], v.get("human_time") or "", v["next"],
                                    manba="suhbat")
            lines.append(f"• {when_text(v['next'])} — sizga: {title}")
        if v.get("sabab"):
            lines.append(f"   ↳ «{v['sabab'][:120]}»")
        buttons.append([InlineKeyboardButton(f"❌ Bekor: {title[:40]}", callback_data=f"del:{tid}")])
        log.info("Suhbatdan vazifa: chat=%s vazifa=%s", chat_id, tid)
    if lines:
        await owner(f"👂 {c['name']} bilan yozishmangizdan vazifa oldim:\n" + "\n".join(lines),
                    InlineKeyboardMarkup(buttons))


async def handle_request(event, text, req=None):
    uid = event.sender_id
    sender = await event.get_sender()
    if getattr(sender, "bot", False):
        return False
    name = " ".join(x for x in [getattr(sender, "first_name", None), getattr(sender, "last_name", None)] if x) or "odam"
    if req is None:
        req = {"dialog": [], "kiril": is_cyrillic(text)}
        REQUESTS.setdefault(uid, []).append(req)
    req["dialog"].append(("u", text))
    req["until"] = now_tz() + timedelta(minutes=30)
    try:
        r = await asyncio.to_thread(ai.analyze_request, req["dialog"], name,
                                    now_tz().strftime("%Y-%m-%d %H:%M (%A)"))
    except Exception as e:  # noqa: BLE001
        log.exception("So'rovni tahlil qilib bo'lmadi: %s", e)
        _drop(uid, req)
        if REMIND_RE.search(text):
            await owner(f"💬 {name} sizdan eslatma so'radi, lekin AI tushuna olmadi:\n«{text}»")
            return True
        return False
    if not r.get("sorov"):
        was_dialog = len(req["dialog"]) > 1
        _drop(uid, req)
        return was_dialog  # suhbat o'rtasida bekor qilingan bo'lsa ham — bu xabar ishlandi
    alifbo = "kiril" if req["kiril"] else "lotin"
    if existed_alifbo := (await db.get_person_by_tg(uid) or {}).get("alifbo"):
        alifbo = existed_alifbo
    if not r.get("tayyor"):
        q = r.get("savol") or "Qaysi kuni soat nechada?"
        req["dialog"].append(("men", q))
        req.update(name=name, alifbo=alifbo, asked_at=now_tz(), last_q=q,
                   until=now_tz() + timedelta(minutes=REASK_MIN * 3 + 30))
        m = await _reply(event, for_person(q, alifbo))
        req["q_msg"] = getattr(m, "id", None)
        if not req.get("told_owner"):
            req["told_owner"] = True
            await owner(f"💬 {name}: «{text[:200]}»\n↳ Aniqlashtiryapman: «{q}»")
        return True

    _drop(uid, req)
    existed = await db.get_person_by_tg(uid)
    if existed:
        pid, name = existed["id"], existed["ism"]
        alifbo = existed.get("alifbo") or alifbo
    else:
        pid = await db.upsert_person(uid, name, name, getattr(sender, "username", None),
                                     getattr(sender, "phone", None))
        await db.set_alifbo(pid, alifbo)  # o'zi qaysi yozuvda yozgan bo'lsa, shunda javob beramiz
    nxt = triggers.next_fire(r["kind"], r["trigger"])
    tid = await db.add_task(r.get("title") or f"{name} — eslatma", r["kind"], r["trigger"],
                            r.get("human_time") or "", nxt, odam_id=pid, odam_ism=name,
                            xabar_matni=r.get("xabar") or None, manba="sorov")
    await _reply(event, for_person(r.get("javob") or "Xo'p, eslataman.", alifbo))
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Bekor qilish", callback_data=f"del:{tid}")]])
    await owner(f"🙋 {name} bilan kelishildi — vazifa yozdim, vaqtida unga o'zim yozaman:\n"
                f"«{r.get('xabar') or r.get('title')}»\n🕒 {r.get('human_time')} ({when_text(nxt)})", kb)
    log.info("So'rov qabul qilindi: odam=%s vazifa=%s", pid, tid)
    return True


async def handle_reply(event, person, s):
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
        m = await event.reply(text)
        if UB and m is not None:
            UB.mark_own(m.chat_id, m.id)
        return m
    except Exception as e:  # noqa: BLE001
        log.warning("Odamga javob yozib bo'lmadi: %s", e)
        return None
