"""Supabase (Postgres) ombori — "eslatma" bo'limi.

Bot o'zining alohida login'i (eslatma_bot) bilan ulanadi; boshqa jadvallarga huquqi yo'q.

Ulanish:
  DATABASE_URL  — to'liq manzil (postgresql://...), bo'lsa shu ishlatiladi;
  aks holda DB_PASSWORD + SUPABASE_REF (+ SUPABASE_REGION) dan Supabase pooler manzili
  avtomatik tuziladi va bir nechta host sinab ko'riladi.
"""

import os
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg

log = logging.getLogger("reminder-bot.db")

TZ = ZoneInfo(os.environ.get("TZ", "Asia/Tashkent"))

_pool = None

COLS = """id, title, kind, trigger, human_time, next_at, status, bitguncha,
          odam_id, xabar_matni, last_fired_at, created_at"""


def _candidate_dsns():
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return [url]
    pwd = os.environ.get("DB_PASSWORD", "").strip()
    ref = os.environ.get("SUPABASE_REF", "qzemxaplmsrraojkzidz").strip()
    region = os.environ.get("SUPABASE_REGION", "ap-northeast-2").strip()
    user = os.environ.get("DB_USER", "eslatma_bot").strip()
    if not pwd:
        raise RuntimeError("DATABASE_URL yoki DB_PASSWORD Railway Variables'da yo'q.")
    # aws-1 ishlashi tasdiqlangan (28.10.2026), aws-0 zaxira
    hosts = [f"aws-1-{region}.pooler.supabase.com", f"aws-0-{region}.pooler.supabase.com"]
    # Session pooler (5432) — doimiy ulanish uchun eng mosi
    return [f"postgresql://{user}.{ref}:{pwd}@{h}:5432/postgres" for h in hosts]


async def init():
    global _pool
    last = None
    for dsn in _candidate_dsns():
        host = dsn.rsplit("@", 1)[-1]
        try:
            _pool = await asyncpg.create_pool(
                dsn, min_size=1, max_size=4, command_timeout=30,
                statement_cache_size=0,  # pooler bilan xavfsiz
                server_settings={"application_name": "eslatma-bot"},
            )
            async with _pool.acquire() as c:
                await c.fetchval("select 1 from eslatma.vazifalar limit 1")
            log.info("Bazaga ulandi: %s", host)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("Bazaga ulanib bo'lmadi (%s): %s", host, e)
    raise RuntimeError(f"Bazaga ulanib bo'lmadi: {last}")


async def close():
    if _pool:
        await _pool.close()


# ---------- yordamchi ----------

def _row(r):
    d = dict(r)
    t = d.get("trigger")
    d["trigger"] = json.loads(t) if isinstance(t, str) else (t or {})
    return d


def left_text(next_at, now=None):
    """'2 kun 03:15' yoki '⏰ Vaqti keldi'."""
    if next_at is None:
        return ""
    now = now or datetime.now(TZ)
    sec = int((next_at - now).total_seconds())
    if sec <= 0:
        return "⏰ Vaqti keldi"
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d} kun {h:02d}:{m:02d}"


def _decorate(rows):
    """Raqam (1, 2, 3...) va AI/ro'yxat uchun qulay maydonlarni qo'shadi."""
    now = datetime.now(TZ)
    out = []
    for i, r in enumerate(rows, start=1):
        d = _row(r)
        d["num"] = i
        nx = d["next_at"]
        d["next"] = nx.astimezone(TZ).strftime("%d.%m.%Y %H:%M") if nx else ""
        if d["status"] == "pauza":
            d["left"] = "⏸ Pauza"
        elif nx is None and d["kind"] == "once":
            d["left"] = "⏰ Vaqti o'tgan"
        else:
            d["left"] = left_text(nx, now)
        d["trigger_json"] = json.dumps(d["trigger"], ensure_ascii=False)
        out.append(d)
    return out


# ---------- o'qish ----------

async def list_tasks():
    rows = await _pool.fetch(f"select {COLS} from eslatma.vazifalar order by id")
    return _decorate(rows)


async def get_task(tid):
    for t in await list_tasks():  # raqam (num) to'g'ri chiqishi uchun
        if t["id"] == tid:
            return t
    return None


async def due_tasks(now):
    rows = await _pool.fetch(
        f"""select {COLS} from eslatma.vazifalar
            where next_at is not null and next_at <= $1 and status <> 'pauza'
            order by next_at""",
        now,
    )
    return [_row(r) for r in rows]


async def history(limit=100):
    rows = await _pool.fetch(
        "select id, vazifa_id, title, human_time, done_at, izoh from eslatma.tarix "
        "order by done_at desc limit $1", limit)
    return [dict(r) for r in rows]


# ---------- yozish ----------

async def add_task(title, kind, trigger, human_time, next_at, bitguncha=False,
                   status="faol"):
    return await _pool.fetchval(
        """insert into eslatma.vazifalar (title, kind, trigger, human_time, next_at, bitguncha, status)
           values ($1, $2, $3::jsonb, $4, $5, $6, $7) returning id""",
        title, kind, json.dumps(trigger, ensure_ascii=False), human_time, next_at,
        bitguncha, status,
    )


async def delete_task(tid):
    return await _pool.fetchval("delete from eslatma.vazifalar where id = $1 returning id", tid)


async def mark_done(tid, izoh=None):
    """Tarixga yozadi. Bir martalik yoki 'bitguncha' vazifa — butunlay yopiladi;
    takroriy — shu navbat uchun 'bitdi', keyingi navbatda o'zi qayta 'faol' bo'ladi."""
    async with _pool.acquire() as c, c.transaction():
        r = await c.fetchrow(f"select {COLS} from eslatma.vazifalar where id = $1 for update", tid)
        if not r:
            return None
        await c.execute(
            "insert into eslatma.tarix (vazifa_id, title, human_time, izoh) values ($1, $2, $3, $4)",
            tid, r["title"], r["human_time"], izoh)
        if r["kind"] == "once" or r["bitguncha"]:
            await c.execute("delete from eslatma.vazifalar where id = $1", tid)
        else:
            await c.execute(
                "update eslatma.vazifalar set status = 'bitdi', updated_at = now() where id = $1", tid)
        return _row(r)


async def claim_fire(tid, old_next, new_next):
    """Eslatma yuborishdan oldin navbatni 'egallaydi' (ikki marta yuborilmasin).
    True qaytsa — yuborish kerak."""
    res = await _pool.execute(
        """update eslatma.vazifalar
           set next_at = $3, last_fired_at = now(), updated_at = now(),
               status = case when status = 'bitdi' then 'faol' else status end
           where id = $1 and next_at = $2""",
        tid, old_next, new_next,
    )
    return res.endswith(" 1")


async def set_status(tid, status, next_at=None):
    await _pool.execute(
        "update eslatma.vazifalar set status = $2, next_at = $3, updated_at = now() where id = $1",
        tid, status, next_at)


async def set_next(tid, next_at):
    await _pool.execute(
        "update eslatma.vazifalar set next_at = $2, updated_at = now() where id = $1", tid, next_at)


async def monthly_reset():
    res = await _pool.execute(
        "update eslatma.vazifalar set status = 'faol', updated_at = now() "
        "where status = 'bitdi' and kind in ('recurring', 'interval')")
    return int(res.split()[-1])
