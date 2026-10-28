"""Vaqt jadvallari: AI qaytargan trigger'ni tekshiradi va APScheduler trigger'iga aylantiradi.

Uch xil tur bor:
  once       — bir martalik:   {"run_date": "2026-10-15T10:00:00"}
  recurring  — kalendar bo'yicha takroriy (cron):
               {"day_of_week": "mon-fri", "hour": 9, "minute": 0}
               {"day": "1st mon", "hour": 9, "minute": 0}      # har oyning 1-dushanbasi
               {"day": "last", "hour": 18, "minute": 0}        # har oyning oxirgi kuni
               {"month": "1,4,7,10", "day": 1, "hour": 10}     # har chorak boshida
  interval   — har N kun/hafta/soatda:
               {"days": 3, "start": "2026-10-12T11:00:00"}
               {"weeks": 2, "start": "2026-10-13T09:00:00"}
"""

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

TZ = ZoneInfo(os.environ.get("TZ", "Asia/Tashkent"))

CRON_KEYS = {"year", "month", "day", "week", "day_of_week", "hour", "minute"}
INTERVAL_KEYS = {"weeks", "days", "hours", "minutes"}


def _local(dt_str):
    d = datetime.fromisoformat(dt_str)
    if d.tzinfo is None:
        d = d.replace(tzinfo=TZ)
    return d


def build(kind, params):
    """AI bergan ma'lumotdan APScheduler trigger yasaydi. Noto'g'ri bo'lsa ValueError."""
    if not isinstance(params, dict):
        raise ValueError("trigger lug'at (obyekt) bo'lishi kerak")

    if kind == "once":
        if "run_date" not in params:
            raise ValueError("bir martalik vazifada run_date yo'q")
        return DateTrigger(run_date=_local(params["run_date"]))

    if kind == "recurring":
        clean = {k: v for k, v in params.items() if k in CRON_KEYS and v not in (None, "")}
        extra = set(params) - CRON_KEYS
        if extra:
            raise ValueError(f"recurring uchun noma'lum kalitlar: {sorted(extra)}")
        if "hour" not in clean:
            raise ValueError("recurring uchun hour majburiy")
        clean.setdefault("minute", 0)
        return CronTrigger(timezone=TZ, **clean)

    if kind == "interval":
        step = {k: int(params[k]) for k in INTERVAL_KEYS if params.get(k)}
        if not step or sum(step.values()) <= 0:
            raise ValueError("interval uchun weeks/days/hours/minutes dan biri kerak")
        start = _local(params["start"]) if params.get("start") else None
        if start is None:
            raise ValueError("interval uchun start (birinchi marta qachon) kerak")
        return IntervalTrigger(timezone=TZ, start_date=start, **step)

    raise ValueError(f"noma'lum tur: {kind}")


def next_fire(kind, params, now=None):
    """Keyingi ishga tushish vaqti (yoki None — vaqti o'tib ketgan bo'lsa)."""
    now = now or datetime.now(TZ)
    trig = build(kind, params)
    if kind == "once":
        return trig.run_date
    return trig.get_next_fire_time(None, now)


# ---------- Mini App formasi uchun: oddiy jadval <-> trigger ----------

KUNLAR = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
KUNLAR_UZ = {"mon": "dushanba", "tue": "seshanba", "wed": "chorshanba", "thu": "payshanba",
             "fri": "juma", "sat": "shanba", "sun": "yakshanba"}
OYLAR_UZ = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust",
            "sentabr", "oktabr", "noyabr", "dekabr"]


def from_form(f, now=None):
    """Forma ma'lumotidan (kind, trigger, human_time) yasaydi.

    f = {"tur": "once"|"daily"|"weekly"|"monthly"|"interval",
         "vaqt": "HH:MM", "sana": "YYYY-MM-DD" (once), "kunlar": ["mon",...] (weekly),
         "oy_kuni": 1-31 (monthly), "har": N (interval, kunda)}
    """
    now = now or datetime.now(TZ)
    tur = f.get("tur")
    try:
        hh, mm = [int(x) for x in (f.get("vaqt") or "09:00").split(":")[:2]]
    except ValueError:
        raise ValueError("Vaqt noto'g'ri (masalan 09:00)")
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("Vaqt noto'g'ri (masalan 09:00)")
    t = f"{hh}:{mm:02d}"

    if tur == "once":
        sana = f.get("sana") or now.strftime("%Y-%m-%d")
        try:
            d = datetime.fromisoformat(f"{sana}T{hh:02d}:{mm:02d}:00")
        except ValueError:
            raise ValueError("Sana noto'g'ri")
        return "once", {"run_date": d.strftime("%Y-%m-%dT%H:%M:00")}, f"{d.day}-{OYLAR_UZ[d.month - 1]} {t}"
    if tur == "daily":
        return "recurring", {"hour": hh, "minute": mm}, f"Har kuni {t}"
    if tur == "weekly":
        kunlar = [k for k in KUNLAR if k in (f.get("kunlar") or [])]
        if not kunlar:
            raise ValueError("Hafta kunlaridan kamida bittasini tanlang")
        if kunlar == KUNLAR[:5]:
            nom = "Ish kunlari"
        elif len(kunlar) == 1:
            nom = "Har " + KUNLAR_UZ[kunlar[0]]
        else:
            nom = "Har " + ", ".join(KUNLAR_UZ[k] for k in kunlar)
        return "recurring", {"day_of_week": ",".join(kunlar), "hour": hh, "minute": mm}, f"{nom} {t}"
    if tur == "monthly":
        day = f.get("oy_kuni")
        if day == "last":
            return "recurring", {"day": "last", "hour": hh, "minute": mm}, f"Har oyning oxirgi kuni {t}"
        try:
            day = int(day)
        except (TypeError, ValueError):
            raise ValueError("Oyning kunini tanlang")
        if not 1 <= day <= 31:
            raise ValueError("Oyning kuni 1 dan 31 gacha")
        return "recurring", {"day": day, "hour": hh, "minute": mm}, f"Har oyning {day}-kuni {t}"
    if tur == "interval":
        try:
            n = int(f.get("har") or 0)
        except ValueError:
            n = 0
        if n < 1:
            raise ValueError("Necha kunda bir marta — 1 dan katta son kiriting")
        start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if start <= now:
            start += timedelta(days=1)
        nom = "Har kuni" if n == 1 else f"Har {n} kunda"
        return "interval", {"days": n, "start": start.strftime("%Y-%m-%dT%H:%M:00")}, f"{nom} {t}"
    raise ValueError("Jadval turini tanlang")


def to_form(kind, trig):
    """Mavjud trigger'ni formaga qaytaradi (tahrirlash oynasi uchun). Murakkab bo'lsa tur='custom'."""
    trig = trig or {}
    if kind == "once":
        d = datetime.fromisoformat(trig["run_date"])
        return {"tur": "once", "sana": d.strftime("%Y-%m-%d"), "vaqt": d.strftime("%H:%M")}
    if kind == "interval" and trig.get("days") and not (trig.get("weeks") or trig.get("hours")):
        st = datetime.fromisoformat(trig["start"])
        return {"tur": "interval", "har": int(trig["days"]), "vaqt": st.strftime("%H:%M")}
    if kind == "interval" and trig.get("weeks") and not trig.get("days"):
        st = datetime.fromisoformat(trig["start"])
        return {"tur": "interval", "har": int(trig["weeks"]) * 7, "vaqt": st.strftime("%H:%M")}
    if kind == "recurring":
        keys = set(trig) - {"hour", "minute"}
        vaqt = f"{int(trig.get('hour', 9)):02d}:{int(trig.get('minute', 0)):02d}"
        if not keys:
            return {"tur": "daily", "vaqt": vaqt}
        if keys == {"day_of_week"}:
            dow = str(trig["day_of_week"])
            if dow == "mon-fri":
                kunlar = KUNLAR[:5]
            else:
                kunlar = [k for k in KUNLAR if k in dow.split(",")]
            if kunlar:
                return {"tur": "weekly", "kunlar": kunlar, "vaqt": vaqt}
        if keys == {"day"}:
            d = str(trig["day"])
            if d == "last" or d.isdigit():
                return {"tur": "monthly", "oy_kuni": "last" if d == "last" else int(d), "vaqt": vaqt}
    return {"tur": "custom"}
