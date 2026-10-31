"""AI qismi: foydalanuvchining o'zbekcha gapini tushunadi — Claude yoki Gemini orqali.

Qaysi biri ishlatiladi:
  - ANTHROPIC_API_KEY da haqiqiy Claude API kaliti (sk-ant-api...) bo'lsa — Claude;
  - aks holda GEMINI_API_KEY bo'lsa — Gemini (bepul).
  AI_PROVIDER=claude yoki AI_PROVIDER=gemini bilan majburan tanlash mumkin.

- Javob har doim tuzilgan bo'ladi (tool use) — JSON buzilib qolmaydi.
- Oxirgi suhbat tarixi beriladi, shuning uchun "uni 10 ga o'tkaz" kabi gaplarni ham tushunadi.
- AI bergan vaqt jadvali noto'g'ri bo'lsa, xatoni Claude'ga qaytarib, o'zi tuzatadi.
- Istalgan mavzuda suhbatlasha oladi (savol-javob, maslahat, hisob-kitob va h.k.).

Muhit o'zgaruvchilari:
  ANTHROPIC_API_KEY — Claude kaliti (console.anthropic.com)
  AI_MODEL          — ixtiyoriy, standart: claude-sonnet-5-5
  GEMINI_API_KEY    — Gemini kaliti (aistudio.google.com)
  GEMINI_MODELS     — ixtiyoriy, vergul bilan modellar tartibi
                      (standart: gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite,gemini-2.5-flash)
"""

import os
import json
import re
import time
import logging

import anthropic

import triggers

log = logging.getLogger("reminder-bot.ai")

MODEL = os.environ.get("AI_MODEL", "claude-sonnet-5-5")
# Eng aqllisidan boshlab tartib. Biri limitga yetsa yoki ishlamasa — keyingisiga o'tadi.
# Har model o'z bepul limitiga ega, shuning uchun kunlik bepul so'rovlar ko'payadi.
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS",
    os.environ.get("GEMINI_MODEL", "")
    # Tez modellar birinchi: oddiy buyruqlar uchun yetarli aqlli va 1-3 soniyada javob beradi
    or "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.8-flash,gemini-3.6-flash",
).split(",") if m.strip()]
_cooldown = {}  # model -> qachongacha ishlatilmaydi (time.time())
# Pullik hisob ochilgach: aqlli ro'yxat (Pro birinchi) — sizning buyruqlaringiz va odamlar javobi uchun;
# tez ro'yxat (Flash birinchi) — barcha suhbatlarni tinglash uchun (ko'p va arzon bo'lishi kerak).
SMART_MODELS = [m.strip() for m in os.environ.get("GEMINI_SMART_MODELS", "").split(",") if m.strip()]
_auto_done = False
SMART_TIMEOUT_MS = int(os.environ.get("AI_SMART_TIMEOUT_MS", "30000"))


def _ver(name):
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


def _auto_models():
    """Kalitda qaysi modellar ochiq ekanini bir marta so'rab, eng kuchlilarini tartiblaydi."""
    global _auto_done, GEMINI_MODELS, SMART_MODELS
    if _auto_done:
        return
    _auto_done = True
    try:
        names = []
        for m in _get_gclient().models.list():
            n = (m.name or "").replace("models/", "")
            acts = getattr(m, "supported_actions", None) or []
            if not n.startswith("gemini-") or (acts and "generateContent" not in acts):
                continue
            if any(x in n for x in ("tts", "image", "embedding", "live", "native-audio", "robotics",
                                    "computer-use", "exp-", "learnlm", "nano")):
                continue
            names.append(n)
        log.info("Ochiq Gemini modellar: %s", ", ".join(sorted(names)))

        def best(kind, exclude=()):
            c = [n for n in names if kind(n) and n not in exclude]
            # yangi versiya birinchi; teng bo'lsa barqaror (preview emas) birinchi
            return sorted(c, key=lambda n: (_ver(n), "preview" not in n, -len(n)), reverse=True)

        pro = best(lambda n: "-pro" in n)
        flash = best(lambda n: "flash" in n and "lite" not in n)
        lite = best(lambda n: "flash-lite" in n)
        if not os.environ.get("GEMINI_MODELS") and (flash or lite):
            GEMINI_MODELS = (flash[:2] + lite[:2]) or GEMINI_MODELS
        if not SMART_MODELS:
            SMART_MODELS = pro[:1] + flash[:2] + lite[:1]
        log.info("Aqlli tartib: %s | Tez tartib: %s", SMART_MODELS, GEMINI_MODELS)
    except Exception as e:  # noqa: BLE001
        log.warning("Modellar ro'yxatini olib bo'lmadi: %s", e)


def _clean(v):
    v = (v or "").strip().strip('"').strip("'").strip()
    if v.lower().startswith("bearer "):
        v = v[7:].strip()
    return v


def provider():
    """Qaysi AI ishlatilishini aniqlaydi: 'claude', 'gemini' yoki None."""
    forced = os.environ.get("AI_PROVIDER", "").strip().lower()
    claude_key = _clean(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY"))
    gemini_key = _clean(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    # Obuna tokeni (sk-ant-oat...) bot uchun emas — e'tiborga olinmaydi
    claude_ok = claude_key.startswith("sk-ant-") and not claude_key.startswith("sk-ant-oat")
    gemini_ok = bool(gemini_key) and not gemini_key.startswith("sk-ant-")
    if forced in ("claude", "gemini"):
        return forced
    if claude_ok:
        return "claude"
    if gemini_ok:
        return "gemini"
    return None


def _api_key():
    """ANTHROPIC_API_KEY bo'lmasa, eski GEMINI_API_KEY nomiga qo'yilgan Claude kalitini ham oladi."""
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    if not key:
        old = os.environ.get("GEMINI_API_KEY", "").strip().strip('"').strip("'")
        if old.startswith("sk-ant-"):
            log.warning("Claude kaliti GEMINI_API_KEY nomida turibdi — ANTHROPIC_API_KEY ga o'tkazing.")
            key = old
    # Nusxa olinganda qo'shilib qolgan bo'sh joy, qo'shtirnoq, "Bearer" ni tozalaydi
    key = (key or "").strip().strip('"').strip("'").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY topilmadi — Railway Variables'ga qo'shing.")
    if not key.startswith("sk-ant-"):
        log.error("Kalit sk-ant- bilan boshlanmayapti (boshi: %s...) — bu Claude API kaliti emas.", key[:6])
    return key


_client = None


def _get_client():
    # Kerak bo'lganda yaratiladi: kalit bo'lmasa ham bot yonadi va eslatmalar yuborilaveradi
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=_api_key())
    return _client

TOOL_NAME = "javob_berish"

TOOL = {
    "name": TOOL_NAME,
    "description": "Foydalanuvchiga javob berish va eslatmalar bilan amallar bajarish. Har bir xabarga aynan bir marta chaqiriladi.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "delete", "done", "update", "list", "chat"],
                "description": "Asosiy amal. Bir nechta amal bo'lsa — eng muhimini yoz, qolganini ro'yxatlarda ber.",
            },
            "reply": {
                "type": "string",
                "description": "Foydalanuvchiga o'zbekcha (lotin) javob. action=list bo'lsa bo'sh qoldirsa ham bo'ladi.",
            },
            "reminders_to_create": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Qisqa, aniq vazifa matni"},
                        "kind": {"type": "string", "enum": ["once", "recurring", "interval"]},
                        "trigger": {"type": "object", "description": "Qoidalarga mos vaqt jadvali"},
                        "human_time": {"type": "string", "description": "O'qishga qulay vaqt, masalan: Har oyning 1-dushanbasi 9:00"},
                        "bitguncha": {"type": "boolean", "description": "true — 'bitmaguncha/bitguncha har kuni' kabi: bajarildi deyilganda butunlay yopiladi"},
                        "kimga": {"type": "string", "description": "Vazifa boshqa odamga yuborilsa — uning ismi ('Abbosxon aka'). O'zimga bo'lsa bo'sh."},
                        "xabar": {"type": "string", "description": "kimga bo'lsa — o'sha odamga boradigan tayyor, hurmatli, qisqa xabar (lotin)."},
                    },
                    "required": ["title", "kind", "trigger", "human_time"],
                },
            },
            "ids_to_delete": {"type": "array", "items": {"type": "integer"}},
            "ids_done": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["action", "reply", "reminders_to_create", "ids_to_delete", "ids_done"],
    },
}

SYSTEM_RULES = """Sen — shaxsiy aqlli yordamchi va eslatma-botsan. Telegram'da ishlaysan.
Foydalanuvchi o'zbek tilida (ba'zan xato bilan, qisqartirib, rus so'zlari aralash) yozadi —
sen uning MAQSADINI tushunishga harakat qil, so'zma-so'z emas.

Sen har bir xabarga `javob_berish` asbobini AYNAN BIR MARTA chaqirib javob berasan.

## Ikki xil ish qilasan

1) ESLATMALAR — yaratish, o'zgartirish, bajarildi deb belgilash, o'chirish, ro'yxat ko'rsatish.
2) ODDIY SUHBAT (action "chat") — istalgan mavzuda: savol-javob, maslahat, matn yozib berish,
   hisob-kitob, tarjima, g'oya va h.k. Bunda to'liq, foydali, aqlli javob ber. Telegram uchun
   qulay yoz: qisqa xatboshlar, kerak bo'lsa "•" bilan ro'yxat. Markdown belgilari (**, ##) ishlatma.

## Eslatmalar bilan ishlash

- Ro'yxatda har vazifaning №, id, vaqti, keyingi vaqti va holati bor. Foydalanuvchi № bilan
  gapiradi ("3 bitdi", "2-ni o'chir"); sen ids_* ichiga o'sha №ga mos **id** ni yozasan.
- "bitdi", "bajardim", "qildim", "tugadi", "bo'ldi", "gaplashdim", "bordim" → action "done".
- "o'chir", "kerak emas", "bekor qil", "olib tashla" → action "delete".
- O'ZGARTIRISH ("7-ni soat 10 ga o'tkaz", "uni har kuni qil") → action "update":
  eski id ni ids_to_delete ga yoz va yangisini reminders_to_create ga to'liq yoz.
- Bitta xabarda BIR NECHTA vazifa bo'lishi mumkin ("ertaga bankka, juma kuni hisobot, har kuni
  8 da sport") — hammasini alohida-alohida reminders_to_create ga yoz. Hech birini tashlab ketma.
- "Bugun nima ishlarim bor", "ertaga nima bor", "shu hafta" → action "chat" va javobni pastdagi
  ro'yxatdan (keyingi vaqt / bugungi vaqtlar ustunidan) aniq tuzib ber: soati bilan, tartib bilan.
  Hech narsa bo'lmasa ham ochiq ayt. To'liq ro'yxat so'rasa → action "list".
- Vaqt aytilmasa mantiqan to'ldir (ertalabki ish → 9:00, tushlik → 13:00, kechki → 18:00)
  va reply'da qaysi vaqtni tanlaganingni ayt.
- Agar gap haqiqatan tushunarsiz bo'lsa — taxmin qilib noto'g'ri vazifa yaratma, action "chat"
  qilib, aniq bitta savol ber.
- reply har doim samimiy, qisqa va o'zbekcha bo'lsin. Yaratilgan vazifalarni reply'da sanab o't.

## BOSHQA ODAMGA YUBORILADIGAN VAZIFALAR
Gapda "<ism> akaga/opaga/ga" + yubor / yuborish kerak / ayt / aytish kerak / eslat / eslatib tur / yoz /
xabar ber / so'ra bo'lsa — bu vazifa O'SHA ODAMGA foydalanuvchining profilidan yuboriladi (kimga TO'LDIRILADI).
Ism yonidagi raqam yoki laqab (masalan "Ibrohimjon aka 755", "Akmal aka Isuzu") — kimga ning bir qismi,
chunki foydalanuvchi telefonida odamlar shunday saqlangan. Uni kimga ga qo'sh: "Ibrohimjon aka 755".
Vaqt aytilmasa va "hozir/darhol" ma'nosi bo'lsa — run_date = HOZIRGI VAQT + 1 daqiqa.
- kimga: odamning ismi, murojaat bilan, bosh kelishikda ("Abbosxon aka", "Ibrohimjon aka", "Dilnoza opa").
- xabar: o'sha odamga boradigan tayyor xabar. Odamga "siz" deb, hurmat bilan, QISQA va ANIQ yoz,
  ismi bilan boshla. Yoshi kattalar o'qiydi — uzun gap, ortiqcha so'z, emoji ishlatma.
  Masalan: "Abbosxon aka, laminat ombor xonasining tozaligini ko'rib oling."
            "Ibrohimjon aka, bugun qaysi agent bilan regionga chiqasiz?"
- title: foydalanuvchi uchun qisqa nom ("Abbosxon aka — laminat ombor tozaligi").
- Agar foydalanuvchi o'zi qiladigan ish bo'lsa ("Abbosxon akaga telefon qilishim kerak",
  "Abbosxon aka bilan gaplashishim kerak") — kimga BO'SH, bu oddiy eslatma.
- Nima yuborish kerakligi aytilmagan bo'lsa ("Ibrohimjon akaga yuborish kerak") — taxmin qilma:
  action "chat", reply'da "Ibrohimjon akaga nima deb yozay va qachon?" deb so'ra.
- Mavjud vazifani odamga biriktirish ("4-vazifani Akmal akaga biriktir") → action "update":
  eskisini o'chir, yangisini kimga va xabar bilan to'liq yoz.

## SANA VA TAKRORNI TO'G'RI TUSHUNISH — eng ko'p xato shu yerda
- TAKRORIY (recurring/interval) faqat gapda "har", "doim", "har kuni/hafta/oy", "har safar" kabi so'z
  BO'LSA. Aks holda — BIR MARTALIK (once).
- "5 kuni", "5-da", "5-sanada", "5 noyabr" — bu OYNING 5-KUNI (sana), haftaning 5-kuni EMAS.
  Eng yaqin kelayotgan 5-sanani ol (bu oyda o'tgan bo'lsa — keyingi oy). "Juma" degan so'z bo'lmasa,
  hech qachon jumaga aylantirma.
- "har oyning 5-kuni" — recurring {"day":5}. "har 5 kunda" — interval {"days":5}.
- reply'da sanani ANIQ yoz: "5-noyabr (chorshanba) 8:00 da ..." — "ertaga", "juma kuni" demasdan.
  Shunda foydalanuvchi xatoni darrov ko'radi.
- human_time ham aniq sana bilan bo'lsin (bir martalik uchun): "5-noyabr 8:00".

## VAQT JADVALI (trigger) QOIDALARI — juda muhim

kind = "once" (bir martalik):
  {"run_date": "YYYY-MM-DDTHH:MM:00"}   — Toshkent vaqti.
  "ertaga", "indinga", "3 kundan keyin", "keyingi dushanba", "oy oxirida" — HOZIRGI VAQTdan
  hisoblab aniq sanaga aylantir. Vaqt allaqachon o'tgan bo'lsa — keyingi mos kunni ol.

kind = "recurring" (kalendar bo'yicha takror) — faqat shu kalitlar:
  "day_of_week": "mon".."sun", ro'yxat "mon,wed,fri", oraliq "mon-fri"
  "day": oy kuni 1-31, yoki "1st mon", "2nd tue", "3rd wed", "4th thu", "last fri", yoki "last" (oyning oxirgi kuni)
  "month": 1-12, ro'yxat "1,4,7,10"
  "hour": 0-23 (MAJBURIY), "minute": 0-59
  DIQQAT: "1st mon" kabi qiymat "day" ga yoziladi, "day_of_week" ga EMAS.
  "day" va "day_of_week" ni birga ishlatma.

kind = "interval" (har N kun/hafta/soatda):
  {"days": 3, "start": "YYYY-MM-DDTHH:MM:00"}  — "har 3 kunda"
  {"weeks": 2, "start": "..."}                  — "har 2 haftada"
  {"hours": 4, "start": "..."}                  — "har 4 soatda"
  start — birinchi eslatma vaqti (bugun vaqt hali o'tmagan bo'lsa bugun, aks holda ertaga).
  "Har kuni"/"har hafta" uchun interval EMAS, recurring ishlat.

bitguncha (ixtiyoriy, true/false):
  "bitmaguncha har kuni", "bitguncha eslatib tur", "ish bitmaguncha" → bitguncha: true.
  Bunday vazifa "bitdi" deyilganda butunlay yopiladi (keyingi kunlarda eslatilmaydi).
  Oddiy takroriy vazifalarda (har shanba hisobot) bitguncha: false.

## Misollar

"har oyni birinchi dushanba majlis" →
  {"title":"Ishchilar bilan majlis","kind":"recurring","trigger":{"day":"1st mon","hour":9,"minute":0},"human_time":"Har oyning 1-dushanbasi 9:00"}
"har oyning oxirgi juma kuni 17:00 hisobot" →
  {"kind":"recurring","trigger":{"day":"last fri","hour":17,"minute":0},"human_time":"Har oyning oxirgi jumasi 17:00"}
"ish kunlari 8:30 da" → {"kind":"recurring","trigger":{"day_of_week":"mon-fri","hour":8,"minute":30}}
"har oyning 25-sanasida oylik" → {"kind":"recurring","trigger":{"day":25,"hour":10,"minute":0}}
"har 3 kunda 11:00 omborni tekshir" → {"kind":"interval","trigger":{"days":3,"start":"<bugun yoki ertaga>T11:00:00"}}
"ertaga 10 da bank, payshanba 15:00 da Rustam aka bilan uchrashuv" → ikkita "once" vazifa.
"Abbosxon akaga har 3 kunda laminat ombor xonasini tozaligini ko'rib olishni ayt" →
  {"title":"Abbosxon aka — laminat ombor tozaligi","kind":"interval","trigger":{"days":3,"start":"<bugun yoki ertaga>T09:00:00"},"human_time":"Har 3 kunda 9:00","kimga":"Abbosxon aka","xabar":"Abbosxon aka, laminat ombor xonasining tozaligini ko'rib oling."}
"Ibrohimjon aka 755 ga ertaga 9 da hisobotni yuborsin deb yoz" →
  {"title":"Ibrohimjon aka — hisobot","kind":"once","trigger":{"run_date":"<ertaga>T09:00:00"},"human_time":"Ertaga 9:00","kimga":"Ibrohimjon aka 755","xabar":"Ibrohimjon aka, hisobotni yuborib bering."}
"elevator ishi bitmaguncha har kuni 8 da eslat" →
  {"title":"Elevator ishini tekshirish","kind":"recurring","trigger":{"hour":8,"minute":0},"human_time":"Har kuni 8:00 (bitguncha)","bitguncha":true}
"""


def _render_reminders(reminders):
    if not reminders:
        return "(hozircha eslatma yo'q)"
    lines = []
    for r in reminders:
        today = f" — BUGUN: {r['today_times']}" if r.get("today_times") else ""
        lines.append(
            f'№{r["num"]} (id={r["id"]}): "{r["title"]}" — {r["human_time"]} — '
            f'keyingi vaqt: {r.get("next") or "-"} — holati: {r["status"]}'
            f'{" — bitguncha" if r.get("bitguncha") else ""}'
            f'{(" — kimga: " + (r.get("odam") or r.get("odam_ism"))) if (r.get("odam") or r.get("odam_ism")) else ""}{today}'
        )
    return "\n".join(lines)


def _call(system, messages):
    return _get_client().messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": TOOL_NAME},
        messages=messages,
    )


def _tool_input(resp):
    for block in resp.content:
        if block.type == "tool_use" and block.name == TOOL_NAME:
            return block
    raise ValueError("AI javobida asbob chaqiruvi yo'q")


def _validate(result):
    """Yaratilayotgan vazifalarning vaqt jadvalini tekshiradi. Xatolar ro'yxatini qaytaradi."""
    errors = []
    for i, r in enumerate(result.get("reminders_to_create") or []):
        try:
            nxt = triggers.next_fire(r.get("kind"), r.get("trigger"))
            if nxt is None:
                errors.append(f"#{i + 1} «{r.get('title')}»: bu jadval hech qachon ishlamaydi.")
        except Exception as e:  # noqa: BLE001
            errors.append(f"#{i + 1} «{r.get('title')}»: trigger xato — {e}")
    return errors


PEOPLE = []  # bot.py yangilab turadi: ma'lum odamlar ismlari


def _context_text(now_str, reminders):
    ppl = ", ".join(PEOPLE) if PEOPLE else "(hali yo'q)"
    return (
        f"HOZIRGI VAQT (Toshkent): {now_str}\n\n"
        f"MA'LUM ODAMLAR (kimga uchun shu ismlarni ishlat): {ppl}\n\n"
        f"FOYDALANUVCHINING MAVJUD ESLATMALARI:\n{_render_reminders(reminders)}"
    )


def understand(user_text, now_str, reminders, history=None):
    """Foydalanuvchi gapini tushunib, amallar lug'atini qaytaradi.

    history — [(user_text, bot_reply), ...] oxirgi suhbatlar (eskidan yangiga).
    """
    p = provider()
    if p == "claude":
        return _understand_claude(user_text, now_str, reminders, history)
    if p == "gemini":
        return _understand_gemini(user_text, now_str, reminders, history)
    raise RuntimeError("AI kaliti topilmadi — Railway'ga GEMINI_API_KEY yoki ANTHROPIC_API_KEY qo'shing.")


def _understand_claude(user_text, now_str, reminders, history=None):
    system = [
        # O'zgarmas qism keshlanadi — tezroq va arzonroq ishlaydi
        {"type": "text", "text": SYSTEM_RULES, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": _context_text(now_str, reminders)},
    ]

    messages = []
    for u, a in history or []:
        messages.append({"role": "user", "content": u})
        messages.append({"role": "assistant", "content": a or "Bo'ldi."})
    messages.append({"role": "user", "content": user_text})

    resp = _call(system, messages)
    block = _tool_input(resp)
    result = dict(block.input)

    # Vaqt jadvali xato bo'lsa — Claude'ga xatoni ko'rsatib, bir marta tuzattiramiz
    errors = _validate(result)
    if errors:
        log.warning("Trigger xatosi, qayta so'ralmoqda: %s", errors)
        messages.append({"role": "assistant", "content": resp.content})
        messages.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": block.id,
                "is_error": True,
                "content": "Quyidagi vaqt jadvallari noto'g'ri, qoidalarga qarab tuzatib, "
                           "javobni to'liq qaytadan ber:\n" + "\n".join(errors),
            }],
        })
        resp = _call(system, messages)
        result = dict(_tool_input(resp).input)

    return result


# ---------------- Gemini ----------------

GEMINI_FORMAT = """
## JAVOB FORMATI (Gemini uchun)
Asbob chaqirish o'rniga FAQAT bitta JSON obyekt qaytar, boshqa hech qanday matn yozma:
{"action": "create|delete|done|update|list|chat",
 "reply": "<o'zbekcha javob>",
 "reminders_to_create": [{"title": "...", "kind": "once|recurring|interval",
                          "trigger": {...}, "human_time": "...", "bitguncha": false,
                          "kimga": "", "xabar": ""}],
 "ids_to_delete": [<id>], "ids_done": [<id>]}
Bo'sh ro'yxatlarni ham [] qilib yoz.
"""

_gclient = None


def _get_gclient():
    global _gclient
    if _gclient is None:
        from google import genai
        key = _clean(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        from google.genai import types
        # Bir model 12 soniyada javob bermasa — kutmasdan keyingisiga o'tamiz
        _gclient = genai.Client(api_key=key, http_options=types.HttpOptions(
            timeout=int(os.environ.get("AI_TIMEOUT_MS", "12000"))))
    return _gclient


def _parse_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    if not text.startswith("{"):
        a, b = text.find("{"), text.rfind("}")
        if a != -1 and b > a:
            text = text[a:b + 1]
    data = json.loads(text)
    for k in ("reminders_to_create", "ids_to_delete", "ids_done"):
        data[k] = data.get(k) or []
    data.setdefault("action", "chat")
    data.setdefault("reply", "")
    return data


def _understand_gemini(user_text, now_str, reminders, history=None):
    from google.genai import types

    system = SYSTEM_RULES + GEMINI_FORMAT + "\n" + _context_text(now_str, reminders)
    contents = []
    for u, a in history or []:
        contents.append(types.Content(role="user", parts=[types.Part(text=u)]))
        contents.append(types.Content(role="model", parts=[types.Part(text=a or "Bo'ldi.")]))
    contents.append(types.Content(role="user", parts=[types.Part(text=user_text)]))
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=0.2,
        response_mime_type="application/json",
    )

    def call():
        return _gemini_generate(contents, config, smart=True)

    raw = call()

    try:
        result = _parse_json(raw)
        errors = _validate(result)
    except (json.JSONDecodeError, ValueError, AttributeError) as e:
        result, errors = None, [f"JSON o'qib bo'lmadi: {e}"]

    if errors:
        log.warning("Gemini javobida xato, qayta so'ralmoqda: %s", errors)
        contents.append(types.Content(role="model", parts=[types.Part(text=raw or "{}")]))
        contents.append(types.Content(role="user", parts=[types.Part(
            text="Javobingda xato bor, qoidalarga qarab tuzatib, to'liq JSON'ni qaytadan ber:\n"
                 + "\n".join(errors))]))
        result = _parse_json(call())
    return result


def _has_audio(contents):
    try:
        return any(getattr(p, "inline_data", None) for c in contents for p in (c.parts or []))
    except Exception:  # noqa: BLE001
        return False


def _gemini_generate(contents, config, smart=False):
    """Modellarni tartib bilan sinaydi; limit tugasa keyingisiga o'tadi.
    smart=True — Pro birinchi (sizning buyruqlaringiz). Ovozli so'rovlarga ko'proq vaqt beriladi."""
    from google.genai import errors as gerr, types as gtypes
    _auto_models()
    if _has_audio(contents) and not getattr(config, "http_options", None):
        config.http_options = gtypes.HttpOptions(timeout=int(os.environ.get("AI_AUDIO_TIMEOUT_MS", "60000")))
    elif smart and not getattr(config, "http_options", None):
        config.http_options = gtypes.HttpOptions(timeout=SMART_TIMEOUT_MS)
    order = (SMART_MODELS or GEMINI_MODELS) if smart else GEMINI_MODELS
    now = time.time()
    models = [m for m in order if _cooldown.get(m, 0) <= now] or order
    last_err = None
    for model in models:
        try:
            t0 = time.time()
            resp = _get_gclient().models.generate_content(
                model=model, contents=contents, config=config)
            log.info("AI javobi: %s, %.1f s", model, time.time() - t0)
            return resp.text or ""
        except gerr.APIError as e:
            code, txt = getattr(e, "code", 0), str(e).lower()
            last_err = e
            if code == 429 or "quota" in txt or "exhausted" in txt:
                # minutlik limit bo'lsa tez qaytadi, kunlik bo'lsa uzoqroq dam oladi
                wait = 3600 if "day" in txt or "daily" in txt else 90
                _cooldown[model] = time.time() + wait
                log.warning("%s limitga yetdi, keyingi modelga o'tilmoqda", model)
                continue
            if code in (404, 400) and ("not found" in txt or "not supported" in txt
                                       or "model" in txt and "key" not in txt):
                _cooldown[model] = time.time() + 86400
                log.warning("%s mavjud emas, keyingi modelga o'tilmoqda: %s", model, e)
                continue
            if code in (500, 503, 504) or "overloaded" in txt or "unavailable" in txt or "deadline" in txt:
                _cooldown[model] = time.time() + 300  # 5 daqiqa unga urinmaymiz
                log.warning("%s band (%s), keyingisi sinab ko'rilmoqda", model, code)
                continue
            raise
        except Exception as e:  # noqa: BLE001 — timeout / tarmoq
            if "timeout" in str(e).lower() or "timed out" in str(e).lower() or e.__class__.__name__.endswith("Timeout"):
                _cooldown[model] = time.time() + 300
                log.warning("%s vaqtida javob bermadi, keyingisi", model)
                last_err = e
                continue
            raise
    raise last_err



# ---------------- Odamning javobini tahlil qilish ----------------

REPLY_RULES = """Sen — Muhammadalining yordamchisisan. U o'z Telegram profilidan bir odamga vazifa yuborgan,
o'sha odam javob yozdi (matn yoki ovozli xabar). Javobni tushunib, nima bo'lganini aniqla.

Faqat bitta JSON qaytar:
{"qaror": "bajarildi" | "kochirildi" | "malumot" | "muammo",
 "matn": "<javob matni; ovozli bo'lsa — eshitganingni so'zma-so'z yoz (asl tilida)>",
 "yangi_vaqt": "YYYY-MM-DDTHH:MM:00" yoki null,
 "xulosa": "<Muhammadali uchun 1 jumla, o'zbek lotinida>"}

Qoidalar:
- bajarildi: ish qilinganini aytdi ("qildim", "bo'ldi", "tayyor", "tekshirdim, toza", "jo'natdim", "ha, gaplashdim").
- kochirildi: hozir emas, keyinroq qilishini aytdi ("ertaga qilaman", "soat 4 da", "tushdan keyin",
  "1 soatdan keyin"). yangi_vaqt — HOZIRGI VAQTdan hisoblab aniq sana-vaqt (Toshkent).
  Vaqt noaniq bo'lsa ("keyinroq") — 2 soatdan keyin; "ertaga" deb vaqtsiz aytsa — ertaga 9:00.
- muammo: qila olmasligini, to'siq borligini, yordam kerakligini aytdi.
- malumot: savol berdi, ma'lumot berdi yoki boshqa narsa (ish qilingani aniq emas).
- Shubha bo'lsa "bajarildi" DEMA — "malumot" qil."""


def analyze_reply(task_title, sent_text, reply_text, audio=None, audio_mime="audio/ogg", now_str=""):
    """Odam javobini tahlil qiladi. Ovozli xabar bo'lsa audio (bytes) beriladi. Faqat Gemini."""
    from google.genai import types

    ctx = (f"HOZIRGI VAQT (Toshkent): {now_str}\n"
           f"VAZIFA: {task_title}\n"
           f"YUBORILGAN XABAR: {sent_text}\n")
    parts = [types.Part(text=ctx)]
    if audio:
        parts.append(types.Part(text="ODAMNING JAVOBI — ovozli xabar:"))
        parts.append(types.Part.from_bytes(data=audio, mime_type=audio_mime))
    else:
        parts.append(types.Part(text=f"ODAMNING JAVOBI: {reply_text}"))
    config = types.GenerateContentConfig(
        system_instruction=REPLY_RULES, temperature=0.1, response_mime_type="application/json")
    raw = _gemini_generate([types.Content(role="user", parts=parts)], config, smart=True)
    data = _parse_reply(raw)
    if not data.get("matn"):
        data["matn"] = reply_text or ""
    return data


def _parse_reply(raw):
    text = (raw or "").strip()
    a, b = text.find("{"), text.rfind("}")
    if a != -1 and b > a:
        text = text[a:b + 1]
    data = json.loads(text)
    if data.get("qaror") not in ("bajarildi", "kochirildi", "malumot", "muammo"):
        data["qaror"] = "malumot"
    if data.get("qaror") == "kochirildi" and not data.get("yangi_vaqt"):
        data["qaror"] = "malumot"
    return data


# ---------------- Boshqa odam egasidan eslatma so'rasa ----------------

REQUEST_RULES = """Sen — Muhammadalining shaxsiy yordamchisisan va UNING Telegram akkauntidan yozasan.
Boshqa bir odam Muhammadaliga shaxsiy xabar yozdi. Agar u Muhammadalidan O'ZIGA nimanidir eslatib
qo'yishni so'rayotgan bo'lsa ("ertaga 8 da ishga borishimni eslatib qo'ying", "dushanba kuni pulni
eslatarsiz"), shu so'rovni aniqlab ol.

Faqat bitta JSON qaytar:
{"sorov": true|false,
 "tayyor": true|false,
 "savol": "<aniqlashtiruvchi savol yoki null>",
 "title": "<Muhammadali uchun qisqa nom: 'Akmal aka — ishga borish'>",
 "kind": "once"|"recurring"|"interval",
 "trigger": {...},
 "human_time": "<'29-oktabr 8:00'>",
 "xabar": "<eslatma vaqti kelganda O'SHA ODAMGA boradigan xabar>",
 "javob": "<hozir o'sha odamga yoziladigan tasdiq>"}

Qoidalar:
- sorov=false: bu eslatma so'rovi emas (oddiy gap, savol, boshqa ish). Shunda boshqa maydonlar null.
- Sana va soat ANIQ bo'lishi shart. "ertaga 8 da" — aniq (ertalab 8:00). "keyinroq", "bir kun",
  "ertalab" (soatsiz) — aniq emas: tayyor=false va savol ber, masalan
  "Xo'p, qaysi kuni soat nechada eslatay?" yoki "Ertaga soat nechada eslatay?".
  Faqat yetishmayotgan narsani so'ra, bitta qisqa savol.
- tayyor=true bo'lsa trigger qoidalari: once -> {"run_date":"YYYY-MM-DDTHH:MM:00"} (Toshkent vaqti,
  HOZIRGI VAQTdan hisobla); recurring -> {"day_of_week":"mon",...,"hour":8,"minute":0};
  interval -> {"days":N,"start":"..."}.
- xabar: o'sha odamga hurmat bilan, ismi bilan, qisqa: "Akmal aka, bugun soat 8:00 da ishga borishingiz kerak."
- javob: qisqa tasdiq: "Xo'p, ertaga soat 8:00 da eslataman."
- Hamma matn o'zbekcha lotin yozuvida (keyin kerak bo'lsa kirillga o'giriladi).
- Muhammadalining nomidan yozayotganingni bildirma, oddiy odamdek qisqa yoz."""


def analyze_request(dialog, sender_name, now_str):
    """dialog — [(kim, matn), ...]: 'u' — odam, 'men' — bizning javoblarimiz."""
    from google.genai import types

    lines = "\n".join(f"{'ODAM' if k == 'u' else 'MEN'}: {m}" for k, m in dialog)
    text = (f"HOZIRGI VAQT (Toshkent): {now_str}\nODAMNING ISMI: {sender_name}\n\n"
            f"YOZISHMA:\n{lines}")
    config = types.GenerateContentConfig(
        system_instruction=REQUEST_RULES, temperature=0.1, response_mime_type="application/json")
    raw = _gemini_generate([types.Content(role="user", parts=[types.Part(text=text)])], config, smart=True)
    t = (raw or "").strip()
    a, b = t.find("{"), t.rfind("}")
    data = json.loads(t[a:b + 1] if a != -1 and b > a else t)
    if data.get("sorov") and data.get("tayyor"):
        try:
            nxt = triggers.next_fire(data.get("kind"), data.get("trigger"))
            if nxt is None:
                raise ValueError("o'tgan")
        except Exception:  # noqa: BLE001
            data["tayyor"] = False
            data["savol"] = data.get("savol") or "Qaysi kuni soat nechada eslatay?"
    return data


# ---------------- Suhbatdan vazifa topish (barcha shaxsiy chatlar) ----------------

CHAT_RULES = """Sen — Muhammadalining shaxsiy yordamchisisan. Senga uning bitta odam bilan shaxsiy
Telegram yozishmasining so'nggi qismi beriladi (MEN — Muhammadali, U — suhbatdosh).
Vazifang: shu yozishmada vaqtga bog'liq, unutilmasligi kerak bo'lgan ish bormi — aniqlash.

Faqat bitta JSON qaytar:
{"vazifalar": [
  {"kimga": "men" | "u",
   "title": "<Muhammadali uchun qisqa nom, kim bilan bog'liqligi bilan: 'Akmal aka — pulni tekshirish'>",
   "kind": "once" | "recurring",
   "trigger": {...},
   "human_time": "<'Ertaga 9:00'>",
   "xabar": "<kimga='u' bo'lsa — o'sha odamga boradigan eslatma; 'men' bo'lsa null>",
   "sabab": "<yozishmadagi qaysi gapdan olganing, qisqa iqtibos>"}
]}
Hech narsa bo'lmasa: {"vazifalar": []}

Qachon vazifa qilinadi:
1) MEN suhbatdoshga eslatishni va'da qildi: "ertaga soat 9 da eslataman", "dushanba yozaman" →
   kimga="u", o'sha vaqtda UNGA xabar: "Akmal aka, ... eslatib qo'yaman degandim: <ish>".
2) MEN biror ishni qilishni va'da qildi: "ertaga hisobotni tashlab beraman", "soat 3 da qo'ng'iroq
   qilaman" → kimga="men", o'sha vaqtda Muhammadalining o'ziga eslatma.
3) U nimanidir qilishni va'da qildi va Muhammadali buni kutyapti: "ertaga pulni o'tkazaman",
   "juma kuni yukni jo'nataman" → kimga="men": o'sha vaqtda "<ism> ... ni va'da qilgan edi —
   tekshiring" degan eslatma.
4) Kelishilgan uchrashuv/qo'ng'iroq aniq vaqt bilan: "ertaga 10 da ofisda ko'rishamiz" →
   kimga="men", 30 daqiqa oldin eslatma.

Qat'iy qoidalar:
- Faqat ANIQ vaqt (sana yoki "ertaga/bugun/dushanba" + soat) bo'lsa vazifa qil. Vaqt aniq bo'lmasa
  ("keyinroq", "tezda", "bir kun") — vazifa QILMA.
- "ertaga" (soatsiz) — 9:00 deb ol. "ertalab" — 9:00, "tushdan keyin" — 14:00, "kechqurun" — 18:00.
- O'tib ketgan vaqt uchun vazifa qilma. Oddiy salomlashish, hazil, savol-javob — vazifa emas.
- Allaqachon bajarilgan narsa ("o'tkazdim", "jo'natdim") — vazifa emas.
- Shubha bo'lsa — vazifa QILMA. Kam, lekin aniq bo'lsin.
- xabar aynan belgilangan VAQTDA yuboriladi — unda "ertaga", "keyin" kabi so'zlar bo'lmasin.
  Yaxshi: "Akmal aka, eslatib qo'yaman degandim — ishga borish vaqti bo'ldi."
- trigger: once -> {"run_date":"YYYY-MM-DDTHH:MM:00"} HOZIRGI VAQTdan hisoblab (Toshkent).
- Matnlar o'zbekcha lotinda.
- Ovozli xabarlarni diqqat bilan eshit: ulardagi va'da va vaqtlar ham xuddi yozilgandek hisoblanadi.
  "sabab" ga ovozli xabardan eshitgan gapingni yoz va boshiga 🎤 qo'y."""


def analyze_chat(dialog, contact_name, now_str):
    """dialog — [("men"|"u", matn, "HH:MM", audio|None), ...]; audio = (bytes, mime).
    Ovozli xabarlar ham shu bitta so'rovda eshitiladi. {"vazifalar": [...]} qaytaradi."""
    from google.genai import types

    parts = [types.Part(text=f"HOZIRGI VAQT (Toshkent): {now_str}\nSUHBATDOSH: {contact_name}\n\nYOZISHMA:")]
    for item in dialog:
        k, m, t = item[0], item[1], item[2]
        audio = item[3] if len(item) > 3 else None
        who = "MEN" if k == "men" else "U"
        if audio:
            parts.append(types.Part(text=f"[{t}] {who} (ovozli xabar):"))
            parts.append(types.Part.from_bytes(data=audio[0], mime_type=audio[1]))
            if m:
                parts.append(types.Part(text=f"(izoh: {m})"))
        else:
            parts.append(types.Part(text=f"[{t}] {who}: {m}"))
    config = types.GenerateContentConfig(
        system_instruction=CHAT_RULES, temperature=0.1, response_mime_type="application/json")
    raw = _gemini_generate([types.Content(role="user", parts=parts)], config)
    t = (raw or "").strip()
    a, b = t.find("{"), t.rfind("}")
    data = json.loads(t[a:b + 1] if a != -1 and b > a else t)
    ok = []
    for v in data.get("vazifalar") or []:
        try:
            nxt = triggers.next_fire(v.get("kind"), v.get("trigger"))
            if nxt is None or v.get("kimga") not in ("men", "u") or not v.get("title"):
                continue
            from datetime import datetime as _dt
            if nxt <= _dt.now(triggers.TZ):
                continue  # o'tib ketgan vaqt
            if v["kimga"] == "u" and not v.get("xabar"):
                continue
            v["next"] = nxt
            ok.append(v)
        except Exception:  # noqa: BLE001
            continue
    return ok
