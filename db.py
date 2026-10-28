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
          odam_id, odam_ism, xabar_matni, last_fired_at, created_at"""
# Vazifa + biriktirilgan odam (ismi, telegram id, alifbosi)
TASK_SELECT = """select v.id, v.title, v.kind, v.trigger, v.human_time, v.next_at, v.status,
       v.bitguncha, v.odam_id, v.odam_ism, v.xabar_matni, v.last_fired_at, v.created_at, v.manba,
       o.ism as odam, o.tg_user_id as odam_tg, o.alifbo as odam_alifbo
  from eslatma.vazifalar v left join eslatma.odamlar o on o.id = v.odam_id"""


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
    rows = await _pool.fetch(f"{TASK_SELECT} order by v.id")
    return _decorate(rows)


async def get_task(tid):
    for t in await list_tasks():  # raqam (num) to'g'ri chiqishi uchun
        if t["id"] == tid:
            return t
    return None


async def due_tasks(now):
    rows = await _pool.fetch(
        f"""{TASK_SELECT}
            where v.next_at is not null and v.next_at <= $1 and v.status <> 'pauza'
            order by v.next_at""",
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
                   status="faol", odam_id=None, odam_ism=None, xabar_matni=None, manba="men"):
    return await _pool.fetchval(
        """insert into eslatma.vazifalar
             (title, kind, trigger, human_time, next_at, bitguncha, status, odam_id, odam_ism, xabar_matni, manba)
           values ($1, $2, $3::jsonb, $4, $5, $6, $7, $8, $9, $10, $11) returning id""",
        title, kind, json.dumps(trigger, ensure_ascii=False), human_time, next_at,
        bitguncha, status, odam_id, odam_ism, xabar_matni, manba,
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
        # Shu vazifa bo'yicha javob kutilayotgan xabarlar ham yopiladi
        await c.execute(
            """update eslatma.yuborishlar set holat = 'bajarildi'
               where vazifa_id = $1 and holat in ('yuborildi', 'soraldi', 'oqilmadi', 'javob', 'javobsiz')""",
            tid)
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


async def set_task_person(tid, odam_id):
    await _pool.execute(
        "update eslatma.vazifalar set odam_id = $2, updated_at = now() where id = $1", tid, odam_id)


# ---------- odamlar ----------

async def list_people():
    rows = await _pool.fetch(
        """select o.*, (select count(*) from eslatma.vazifalar v where v.odam_id = o.id) as vazifa_soni
           from eslatma.odamlar o order by o.ism""")
    return [dict(r) for r in rows]


async def get_person(pid):
    r = await _pool.fetchrow("select * from eslatma.odamlar where id = $1", pid)
    return dict(r) if r else None


async def get_person_by_tg(tg_user_id):
    r = await _pool.fetchrow("select * from eslatma.odamlar where tg_user_id = $1", tg_user_id)
    return dict(r) if r else None


async def upsert_person(tg_user_id, ism, tg_ism=None, username=None, telefon=None):
    """Telegram ID bo'yicha odamni yaratadi yoki yangilaydi; id qaytaradi."""
    return await _pool.fetchval(
        """insert into eslatma.odamlar (tg_user_id, ism, tg_ism, username, telefon)
           values ($1, $2, $3, $4, $5)
           on conflict (tg_user_id) do update
             set ism = excluded.ism, tg_ism = excluded.tg_ism,
                 username = coalesce(excluded.username, eslatma.odamlar.username),
                 telefon = coalesce(excluded.telefon, eslatma.odamlar.telefon)
           returning id""",
        tg_user_id, ism, tg_ism, username, telefon)


async def set_alifbo(pid, alifbo):
    await _pool.execute("update eslatma.odamlar set alifbo = $2 where id = $1", pid, alifbo)


async def delete_person(pid):
    return await _pool.fetchval("delete from eslatma.odamlar where id = $1 returning id", pid)


# ---------- yuborishlar (odamlarga ketgan xabarlar) ----------

async def add_send(vazifa_id, odam_id, matn, tg_msg_id, tg_chat_id, vazifa_title=None):
    async with _pool.acquire() as c, c.transaction():
        # Shu vazifa bo'yicha oldingi javobsiz xabar endi kuzatilmaydi
        await c.execute(
            """update eslatma.yuborishlar set holat = 'almashdi'
               where vazifa_id = $1 and holat in ('yuborildi', 'soraldi', 'oqilmadi', 'javob', 'javobsiz')""", vazifa_id)
        return await c.fetchval(
            """insert into eslatma.yuborishlar (vazifa_id, odam_id, matn, tg_msg_id, tg_chat_id, vazifa_title)
               values ($1, $2, $3, $4, $5, $6) returning id""",
            vazifa_id, odam_id, matn, tg_msg_id, tg_chat_id, vazifa_title)


SEND_SELECT = """select y.*, o.ism as odam, o.alifbo, coalesce(v.title, y.vazifa_title) as title,
       v.kind, v.trigger, v.human_time
  from eslatma.yuborishlar y
  left join eslatma.odamlar o on o.id = y.odam_id
  left join eslatma.vazifalar v on v.id = y.vazifa_id"""


async def open_sends():
    """Javob kutilayotgan xabarlar (oxirgi 3 kun)."""
    rows = await _pool.fetch(
        f"""{SEND_SELECT}
            where y.holat in ('yuborildi', 'soraldi', 'oqilmadi') and y.yuborilgan_at > now() - interval '3 days'
            order by y.yuborilgan_at""")
    return [_send(r) for r in rows]


async def open_send_for(odam_id, reply_to_msg=None):
    """Odamning javobi qaysi xabarga tegishli: reply qilingan bo'lsa o'sha, aks holda oxirgisi."""
    if reply_to_msg:
        r = await _pool.fetchrow(
            f"{SEND_SELECT} where y.odam_id = $1 and y.tg_msg_id = $2 order by y.id desc limit 1",
            odam_id, reply_to_msg)
        if r:
            return _send(r)
    r = await _pool.fetchrow(
        f"""{SEND_SELECT}
            where y.odam_id = $1 and y.holat in ('yuborildi', 'soraldi', 'oqilmadi', 'javob', 'javobsiz')
              and y.yuborilgan_at > now() - interval '3 days'
            order by y.yuborilgan_at desc limit 1""", odam_id)
    return _send(r) if r else None


def _send(r):
    d = dict(r)
    t = d.get("trigger")
    d["trigger"] = json.loads(t) if isinstance(t, str) else (t or {})
    return d


async def update_send(sid, **fields):
    if not fields:
        return
    keys = list(fields)
    sets = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(keys))
    vals = [json.dumps(v, ensure_ascii=False) if k == "ai_qaror" else v for k, v in fields.items()]
    await _pool.execute(f"update eslatma.yuborishlar set {sets} where id = $1", sid, *vals)


async def recent_sends(limit=50):
    rows = await _pool.fetch(f"{SEND_SELECT} order by y.yuborilgan_at desc limit $1", limit)
    return [_send(r) for r in rows]


# ---------- sozlamalar ----------

async def get_setting(kalit):
    return await _pool.fetchval("select qiymat from eslatma.sozlamalar where kalit = $1", kalit)


async def set_setting(kalit, qiymat):
    if qiymat is None:
        await _pool.execute("delete from eslatma.sozlamalar where kalit = $1", kalit)
        return
    await _pool.execute(
        """insert into eslatma.sozlamalar (kalit, qiymat) values ($1, $2)
           on conflict (kalit) do update set qiymat = excluded.qiymat, updated_at = now()""",
        kalit, qiymat)


async def update_task(tid, title, kind, trigger, human_time, next_at, bitguncha,
                      odam_id, odam_ism, xabar_matni, status="faol"):
    """Mini App'dagi tahrirlash formasidan to'liq yangilash."""
    return await _pool.fetchval(
        """update eslatma.vazifalar
           set title = $2, kind = $3, trigger = $4::jsonb, human_time = $5, next_at = $6,
               bitguncha = $7, odam_id = $8, odam_ism = $9, xabar_matni = $10, status = $11,
               updated_at = now()
           where id = $1 returning id""",
        tid, title, kind, json.dumps(trigger, ensure_ascii=False), human_time, next_at,
        bitguncha, odam_id, odam_ism, xabar_matni, status)
