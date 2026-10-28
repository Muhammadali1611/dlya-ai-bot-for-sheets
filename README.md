# AI Eslatma Bot v2 — Supabase + Mini App

Shaxsiy Telegram bot (faqat egasi uchun). O'zbek tilida oddiy yozasiz — AI tushunadi,
vazifani Supabase bazasiga yozadi va vaqti kelganda raqamlangan eslatma yuboradi.
Botning pastidagi **📋 Vazifalar** tugmasi Telegram ichida ilova (Mini App) ochadi.

## Imkoniyatlar
- "ertaga 10 da bank", "har shanba 9:00 hisobot", "har oyning 1-dushanbasi majlis",
  "har 3 kunda 11:00 omborni tekshir", "bitmaguncha har kuni 8 da elevatorni eslat"
- "3 bitdi", "1, 4 bitdi" — AI'siz, darhol (limit sarflanmaydi)
- "Bugun nima ishlarim bor?" va istalgan mavzuda suhbat
- Mini App: navbatdagi ish va jonli sanoq, vaqt bo'yicha guruhlar, ✅ / ⏸ / o'chirish,
  oddiy tilda yangi vazifa qo'shish, bajarilganlar tarixi

## Odamlarga sizning nomingizdan yuborish (3-bosqich)
- "Abbosxon akaga har 3 kunda laminat omborni tekshirishni ayt" — vazifa Abbosxon akaga
  **sizning Telegram profilingizdan** boradi (kirillcha; Mini App'da har odam uchun lotin ham tanlash mumkin).
- Birinchi marta bot kontaktlaringizdan "qaysi biri?" deb so'raydi, keyin o'zi taniydi.
- Odam o'qib 1 soat javob bermasa — xabarga reply qilib so'raydi, 3 soatdan keyin yana;
  baribir javob bo'lmasa sizga xabar beradi. O'qimasa 6 soatdan keyin aytadi.
- Javobni (matn yoki ovozli xabar) AI tushunadi: bajarildi → belgilaydi; "ertaga 4 da" → vaqtni
  ko'chiradi; muammo/savol → sizga yuboradi.
- Bot faqat "Odamlar" ro'yxatidagilarning, faqat vazifa yuborilgandan keyingi javoblarini ko'radi.
- Akkaunt Mini App → Odamlar → "Akkauntni ulash" orqali ulanadi. Sessiya bazada shifrlangan.

## Barcha shaxsiy suhbatlarni tinglash
- Bot sizning barcha shaxsiy chatlaringizni (kiruvchi va o'zingiz yozgan xabarlarni) kuzatadi.
- "Ertaga soat 9 da eslataman" deb yozsangiz — o'sha vaqtda o'sha odamga eslatma boradi.
  "Ertaga hisobotni tashlab beraman" — sizga eslatma. "Ertaga pulni o'tkazaman" desa — tekshirish eslatmasi.
- Suhbat 1,5 daqiqa jim qolgach birga tahlil qilinadi; vaqt/va'da belgisi yo'q suhbatlar AI'ga bormaydi.
- Topilgan har vazifa haqida sizga xabar va "❌ Bekor" tugmasi keladi.
- Guruhlar kuzatilmaydi. O'chirish: Railway'da LISTEN_ALL=off. Kunlik chegara: CHAT_DAILY_LIMIT (250).

## Qanday ishlaydi
- Vazifaning keyingi vaqti bazada (`next_at`). Bot har 30 soniyada vaqti kelganlarni tekshiradi —
  qayta yonsa ham, Mini App'dan o'zgartirilsa ham hech narsa yo'qolmaydi.
- Takroriy vazifa "bitdi" deyilsa — shu navbat bajarildi, keyingisida yana eslatadi.
  Bir martalik yoki "bitguncha" vazifa "bitdi" deyilsa — butunlay yopilib, tarixga o'tadi.

## Baza
Supabase "Muhammadali1611's Project", alohida `eslatma` bo'limi (schema) — `schema.sql`.
Bot faqat o'zining `eslatma_bot` login'i bilan ulanadi va boshqa jadvallarga huquqi yo'q.

## Railway Variables
| Nomi | Qiymati |
|---|---|
| TELEGRAM_BOT_TOKEN | BotFather tokeni |
| GEMINI_API_KEY | Gemini kaliti (aistudio.google.com) |
| OWNER_ID | Egasining Telegram ID si |
| DB_PASSWORD | `eslatma_bot` login paroli (yoki to'liq `DATABASE_URL`) |
| TZ | Asia/Tashkent |
| TG_API_ID, TG_API_HASH | my.telegram.org dan (profildan yuborish uchun) |
| MORNING_AT | ixtiyoriy, ertalabki xulosa vaqti (standart 07:30, "off" — o'chiq) |
| FOLLOWUP_MINUTES | ixtiyoriy, qayta so'rash (standart "60,180") |

Mini App manzili Railway'ning public domain'idan avtomatik olinadi (Settings → Networking →
Generate Domain). Kerak bo'lsa `WEBAPP_URL` bilan qo'lda berish mumkin.

## Fayllar
- `bot.py` — bot, eslatmalar tsikli, ishga tushirish
- `ai.py` — Gemini/Claude yordamida gapni tushunish
- `db.py` — Supabase (Postgres) bilan ishlash
- `triggers.py` — vaqt jadvallari
- `webapp.py`, `webapp/index.html` — Mini App va uning API'si
- `userbot.py` — sizning akkauntingiz orqali yozish (Telethon)
- `kuzatuv.py` — odamlarga yuborish, o'qildi/javob kuzatuvi, qayta so'rash
- `translit.py` — lotin → kirill
- `schema.sql`, `schema_v3.sql` — baza tuzilmasi
- `tools/migrate_from_sheets.py` — eski Google Sheets'dan bir martalik ko'chirish
