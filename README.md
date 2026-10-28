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

Mini App manzili Railway'ning public domain'idan avtomatik olinadi (Settings → Networking →
Generate Domain). Kerak bo'lsa `WEBAPP_URL` bilan qo'lda berish mumkin.

## Fayllar
- `bot.py` — bot, eslatmalar tsikli, ishga tushirish
- `ai.py` — Gemini/Claude yordamida gapni tushunish
- `db.py` — Supabase (Postgres) bilan ishlash
- `triggers.py` — vaqt jadvallari
- `webapp.py`, `webapp/index.html` — Mini App va uning API'si
- `schema.sql` — baza tuzilmasi
- `tools/migrate_from_sheets.py` — eski Google Sheets'dan bir martalik ko'chirish
