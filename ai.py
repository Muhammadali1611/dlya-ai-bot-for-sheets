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
    or "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite,gemini-2.5-flash",
).split(",") if m.strip()]
_cooldown = {}  # model -> qachongacha ishlatilmaydi (time.time())


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

## Misollar

"har oyni birinchi dushanba majlis" →
  {"title":"Ishchilar bilan majlis","kind":"recurring","trigger":{"day":"1st mon","hour":9,"minute":0},"human_time":"Har oyning 1-dushanbasi 9:00"}
"har oyning oxirgi juma kuni 17:00 hisobot" →
  {"kind":"recurring","trigger":{"day":"last fri","hour":17,"minute":0},"human_time":"Har oyning oxirgi jumasi 17:00"}
"ish kunlari 8:30 da" → {"kind":"recurring","trigger":{"day_of_week":"mon-fri","hour":8,"minute":30}}
"har oyning 25-sanasida oylik" → {"kind":"recurring","trigger":{"day":25,"hour":10,"minute":0}}
"har 3 kunda 11:00 omborni tekshir" → {"kind":"interval","trigger":{"days":3,"start":"<bugun yoki ertaga>T11:00:00"}}
"ertaga 10 da bank, payshanba 15:00 da Rustam aka bilan uchrashuv" → ikkita "once" vazifa.
"""


def _render_reminders(reminders):
    if not reminders:
        return "(hozircha eslatma yo'q)"
    lines = []
    for r in reminders:
        today = f" — BUGUN: {r['today_times']}" if r.get("today_times") else ""
        lines.append(
            f'№{r["num"]} (id={r["id"]}): "{r["title"]}" — {r["human_time"]} — '
            f'keyingi vaqt: {r.get("next") or "-"} — holati: {r["status"]}{today}'
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


def _context_text(now_str, reminders):
    return (
        f"HOZIRGI VAQT (Toshkent): {now_str}\n\n"
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
                          "trigger": {...}, "human_time": "..."}],
 "ids_to_delete": [<id>], "ids_done": [<id>]}
Bo'sh ro'yxatlarni ham [] qilib yoz.
"""

_gclient = None


def _get_gclient():
    global _gclient
    if _gclient is None:
        from google import genai
        key = _clean(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        _gclient = genai.Client(api_key=key)
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
        from google.genai import errors as gerr
        now = time.time()
        models = [m for m in GEMINI_MODELS if _cooldown.get(m, 0) <= now] or GEMINI_MODELS
        last_err = None
        for model in models:
            try:
                resp = _get_gclient().models.generate_content(
                    model=model, contents=contents, config=config)
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
                if code in (500, 503) or "overloaded" in txt or "unavailable" in txt:
                    log.warning("%s band (%s), keyingisi sinab ko'rilmoqda", model, code)
                    continue
                raise
        raise last_err

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
