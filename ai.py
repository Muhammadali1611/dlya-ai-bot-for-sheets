"""AI qismi: foydalanuvchining o'zbekcha gapini Claude (Anthropic) orqali tushunadi.

- Javob har doim tuzilgan bo'ladi (tool use) — JSON buzilib qolmaydi.
- Oxirgi suhbat tarixi beriladi, shuning uchun "uni 10 ga o'tkaz" kabi gaplarni ham tushunadi.
- AI bergan vaqt jadvali noto'g'ri bo'lsa, xatoni Claude'ga qaytarib, o'zi tuzatadi.
- Istalgan mavzuda suhbatlasha oladi (savol-javob, maslahat, hisob-kitob va h.k.).

Muhit o'zgaruvchilari:
  ANTHROPIC_API_KEY — Claude kaliti (console.anthropic.com)
  AI_MODEL          — ixtiyoriy, standart: claude-sonnet-5-5
"""

import os
import logging

import anthropic

import triggers

log = logging.getLogger("reminder-bot.ai")

MODEL = os.environ.get("AI_MODEL", "claude-sonnet-5-5")

def _api_key():
    """ANTHROPIC_API_KEY bo'lmasa, eski GEMINI_API_KEY nomiga qo'yilgan Claude kalitini ham oladi."""
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    if not key:
        old = os.environ.get("GEMINI_API_KEY", "")
        if old.startswith("sk-ant-"):
            log.warning("Claude kaliti GEMINI_API_KEY nomida turibdi — ANTHROPIC_API_KEY ga o'tkazing.")
            key = old
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY topilmadi — Railway Variables'ga qo'shing.")
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


def understand(user_text, now_str, reminders, history=None):
    """Foydalanuvchi gapini tushunib, amallar lug'atini qaytaradi.

    history — [(user_text, bot_reply), ...] oxirgi suhbatlar (eskidan yangiga).
    """
    system = [
        # O'zgarmas qism keshlanadi — tezroq va arzonroq ishlaydi
        {"type": "text", "text": SYSTEM_RULES, "cache_control": {"type": "ephemeral"}},
        {
            "type": "text",
            "text": (
                f"HOZIRGI VAQT (Toshkent): {now_str}\n\n"
                f"FOYDALANUVCHINING MAVJUD ESLATMALARI:\n{_render_reminders(reminders)}"
            ),
        },
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
