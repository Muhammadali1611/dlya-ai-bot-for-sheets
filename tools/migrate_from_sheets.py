"""Bir martalik: Google Sheets'dagi vazifalarni Supabase uchun SQL'ga aylantiradi.

Ishlatish:
  python tools/migrate_from_sheets.py vazifalar.json bajarilganlar.json > migrate.sql

JSON — Sheets API 'values' massivi (sarlavha qatori bilan).
Muhammadali bilan 28.10.2026 da kelishilgan qarorlar quyida (eski sheet id bo'yicha).
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import triggers  # noqa: E402

TZ = ZoneInfo("Asia/Tashkent")
OWNER_CHAT = "7242956193"

DELETE = {15, 17, 19, 21, 22, 29, 34}   # dublikatlar, xato vazifa, kerak emaslar
DONE_NOW = {31}                          # Laminat kanalini o'tkazish — bitdi
PAUSE = {7}                              # Nakladnoy — keyin boshqa odamga biriktiriladi
OVERRIDE = {
    8: dict(kind="interval", trigger={"days": 3, "start": "2026-10-29T11:00:00"},
            human_time="Har 3 kunda 11:00"),
    28: dict(kind="recurring", trigger={"hour": 8, "minute": 0},
             human_time="Har kuni 8:00 (bitguncha)", bitguncha=True),
}
KIND = {"bir martalik": "once", "takroriy": "recurring", "har N vaqtda": "interval"}


def q(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    return "'" + str(v).replace("'", "''") + "'"


def parse_done_at(s):
    for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=TZ)
        except ValueError:
            pass
    raise ValueError(f"sanani o'qib bo'lmadi: {s!r}")


def main(active_path, done_path):
    active = json.load(open(active_path))[1:]
    done = json.load(open(done_path))[1:]
    now = datetime.now(TZ)
    out = ["begin;"]
    stats = {"ko'chirildi": 0, "o'chirildi": 0, "bitdi": 0, "boshqa odamniki": 0, "tarix": 0}

    for row in active:
        row = (row + [""] * 10)[:10]
        if not row[9].strip():
            continue
        sid = int(float(row[9]))
        if row[8].strip() != OWNER_CHAT:
            stats["boshqa odamniki"] += 1
            continue
        if sid in DELETE:
            stats["o'chirildi"] += 1
            continue
        title, human = row[1], row[3]
        if sid in DONE_NOW:
            out.append(f"insert into eslatma.tarix (title, human_time, done_at, izoh) values "
                       f"({q(title)}, {q(human)}, now(), 'Sheets''dan ko''chirishda bitdi');")
            stats["bitdi"] += 1
            continue
        kind = KIND[row[2].strip()]
        trig = json.loads(row[7])
        bitguncha = False
        status = "faol"
        if sid in OVERRIDE:
            o = OVERRIDE[sid]
            kind, trig, human = o["kind"], o["trigger"], o["human_time"]
            bitguncha = o.get("bitguncha", False)
        nxt = triggers.next_fire(kind, trig, now=now)
        if kind == "once" and nxt is not None and nxt <= now:
            nxt = None  # vaqti o'tgan, "bitdi" kutilmoqda
        if sid in PAUSE:
            status, nxt = "pauza", None
        out.append(
            "insert into eslatma.vazifalar (title, kind, trigger, human_time, next_at, status, bitguncha) "
            f"values ({q(title)}, {q(kind)}, {q(json.dumps(trig, ensure_ascii=False))}::jsonb, "
            f"{q(human)}, {q(nxt.isoformat() if nxt else None)}, {q(status)}, {q(bitguncha)});")
        stats["ko'chirildi"] += 1

    for row in done:
        row = (row + [""] * 6)[:6]
        if not row[1].strip():
            continue
        out.append(f"insert into eslatma.tarix (title, human_time, done_at) values "
                   f"({q(row[1])}, {q(row[2])}, {q(parse_done_at(row[3]).isoformat())});")
        stats["tarix"] += 1

    out.append("commit;")
    print("\n".join(out))
    print(f"-- {stats}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
