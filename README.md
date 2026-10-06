# AI Eslatma Bot — Google Sheets versiyasi

Telegram bot: o'zbek tilida oddiy yozasiz, u tushunadi va vazifani **Google Sheets** jadvaliga yozadi.
Vaqti kelganda raqamlangan eslatma yuboradi. "3 bitdi" desangiz, vazifa **Bajarilganlar** varag'iga o'tadi.

Misollar:
- "har chorshanba 14:00 do'konga borish"
- "har oyning birinchi dushanbasi ishchilar bilan majlis"
- "ertaga 10:00 bankka bor"
- "3 bitdi" yoki "1, 4 bitdi" — bajarildi
- "3-ni o'chir" — butunlay o'chirish
- "eslatmalarim" yoki /list

## Jadval qanday ishlaydi
- **Vazifalar** — faol vazifalar. "⏳ Qolgan vaqt" har daqiqada o'zi teskari sanaydi.
- **Bajarilganlar** — "bitdi" deyilgan vazifalar (sana va oy bilan).
- Bir martalik vazifa "bitdi" deyilsa — Vazifalar'dan o'chadi, Bajarilganlar'ga tushadi.
- Takroriy vazifa "bitdi" deyilsa — Bajarilganlar'ga yoziladi, Vazifalar'da "Bitdi" bo'lib turadi,
  keyingi navbati kelganda yoki oy boshida avtomatik yana "Faol" bo'ladi.
- Kulrang ustunlar (trigger, chat_id, id) bot uchun — qo'lda o'zgartirmang.

## Railway Variables
| Nomi | Qiymati |
|---|---|
| TELEGRAM_BOT_TOKEN | BotFather tokeni |
| GEMINI_API_KEY | Gemini kaliti (aistudio.google.com) |
| SHEET_ID | Jadval havolasidagi `/d/` va `/edit` orasidagi qism |
| GOOGLE_CREDENTIALS | Service account JSON faylining **butun matni** (nusxa olib qo'yiladi) |
| TZ | Asia/Tashkent |

Endi Volume va DB_PATH kerak emas — hamma ma'lumot Sheets'da.

## Muhim
- Jadval service account emailiga **Muharrir** qilib ulashilgan bo'lishi kerak.
- JSON kalitni GitHub'ga yuklamang (.gitignore himoya qiladi).

## Fayllar
- bot.py — asosiy bot (eslatma, "bitdi", oy boshida reset)
- ai.py — Gemini yordamida o'zbekcha gapni tushunadi
- db.py — Google Sheets bilan ishlaydi
- requirements.txt, Procfile, railway.json — joylash uchun
