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
from datetime import datetime
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
