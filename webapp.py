"""Mini App: Telegram ichida ochiladigan sayt va uning API'si.

Xavfsizlik: har bir so'rov Telegram bergan initData imzosi bilan tekshiriladi
(bot tokeni orqali HMAC). Faqat OWNER_ID ga ruxsat.
"""

import os
import hmac
import json
import time
import hashlib
import logging
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

import db
import triggers

log = logging.getLogger("reminder-bot.web")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_ID", "7242956193"))
MAX_AGE = 7 * 24 * 3600  # initData 7 kungacha amal qiladi
STATIC = Path(__file__).parent / "webapp"
DEV_TOKEN = os.environ.get("WEBAPP_DEV_TOKEN", "")  # faqat lokal sinov uchun


def verify_init_data(init_data, bot_token=TOKEN, now=None):
    """Telegram WebApp initData imzosini tekshiradi. To'g'ri bo'lsa user lug'atini qaytaradi."""
    if not init_data:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    got = pairs.pop("hash", "")
    if not got:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        return None
    try:
        if (now or time.time()) - int(pairs.get("auth_date", "0")) > MAX_AGE:
            return None
        return json.loads(pairs.get("user", "{}"))
    except (ValueError, TypeError):
        return None


@web.middleware
async def auth(request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    init = request.headers.get("X-Init-Data", "")
    if DEV_TOKEN and init == "dev:" + DEV_TOKEN:
        user = {"id": OWNER_ID}
    else:
        user = verify_init_data(init)
    if not user or user.get("id") != OWNER_ID:
        return web.json_response({"error": "ruxsat yo'q"}, status=403)
    return await handler(request)


def _task_json(t):
    nx = t["next_at"]
    return {
        "id": t["id"], "num": t["num"], "title": t["title"], "kind": t["kind"],
        "human_time": t["human_time"], "status": t["status"], "bitguncha": t["bitguncha"],
        "next_at": nx.isoformat() if nx else None,
        "run_date": t["trigger"].get("run_date") if t["kind"] == "once" else None,
        "odam": t.get("odam") or t.get("odam_ism"), "odam_tanlangan": bool(t.get("odam_id")),
    }


async def api_tasks(request):
    tasks = await db.list_tasks()
    return web.json_response({"tasks": [_task_json(t) for t in tasks]})


async def api_history(request):
    rows = await db.history(100)
    return web.json_response({"history": [
        {"title": r["title"], "human_time": r["human_time"], "done_at": r["done_at"].isoformat()}
        for r in rows]})


async def api_add(request):
    handle_text, err_text = request.app["handle_text"], request.app["err_text"]
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "matn bo'sh"}, status=400)
    try:
        reply, _ = await handle_text(text)
    except Exception as e:  # noqa: BLE001
        log.exception("Mini App'dan qo'shishda xato: %s", e)
        return web.json_response({"error": err_text(e)}, status=502)
    return web.json_response({"reply": reply})


async def api_done(request):
    tid = int(request.match_info["tid"])
    r = await db.mark_done(tid)
    return web.json_response({"ok": bool(r)})


async def api_delete(request):
    tid = int(request.match_info["tid"])
    return web.json_response({"ok": bool(await db.delete_task(tid))})


async def api_pause(request):
    """Pauza <-> faol. Faollashtirilganda keyingi vaqt qayta hisoblanadi."""
    tid = int(request.match_info["tid"])
    t = await db.get_task(tid)
    if not t:
        return web.json_response({"ok": False}, status=404)
    if t["status"] == "pauza":
        nxt = triggers.next_fire(t["kind"], t["trigger"])
        if t["kind"] == "once" and nxt is not None and nxt <= db.datetime.now(db.TZ):
            nxt = None  # bir martalikning vaqti o'tgan — "bitdi"ni kutadi
        await db.set_status(tid, "faol", nxt)
    else:
        await db.set_status(tid, "pauza", None)
    return web.json_response({"ok": True})


# ---------- akkaunt (userbot) ----------

def _ub(request):
    return request.app["ub"]


async def _json_err(fn):
    try:
        return web.json_response(await fn())
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:  # noqa: BLE001
        log.exception("Akkaunt amalida xato: %s", e)
        return web.json_response({"error": "Telegram bilan bog'lanishda xato. Qayta urinib ko'ring."}, status=502)


async def api_tg_status(request):
    ub = _ub(request)
    me = ub.me if ub and ub.connected else None
    return web.json_response({
        "configured": bool(ub and ub.configured),
        "connected": bool(me),
        "name": (" ".join(x for x in [me.first_name, me.last_name] if x) if me else None),
        "phone": (("+" + me.phone[:5] + "…" + me.phone[-2:]) if me and me.phone else None),
    })


async def api_tg_code(request):
    body = await request.json()

    async def go():
        await _ub(request).send_code(body.get("phone", ""))
        return {"ok": True}
    return await _json_err(go)


async def _after_login(request):
    bot = request.app["bot"]
    if bot:
        try:
            await bot.send_message(OWNER_ID, "🔗 Akkauntingiz ulandi. Endi odamlarga sizning nomingizdan "
                                             "vazifa yubora olaman.")
        except Exception:  # noqa: BLE001
            pass


async def api_tg_login(request):
    body = await request.json()

    async def go():
        res = await _ub(request).sign_in(body.get("code", ""))
        if res == "ok":
            await _after_login(request)
        return {"result": res}
    return await _json_err(go)


async def api_tg_password(request):
    body = await request.json()

    async def go():
        await _ub(request).sign_in_password(body.get("password", ""))
        await _after_login(request)
        return {"result": "ok"}
    return await _json_err(go)


async def api_tg_logout(request):
    async def go():
        await _ub(request).logout()
        return {"ok": True}
    return await _json_err(go)


# ---------- odamlar va kuzatuv ----------

async def api_people(request):
    people = await db.list_people()
    return web.json_response({"people": [
        {"id": p["id"], "ism": p["ism"], "tg_ism": p.get("tg_ism"), "username": p.get("username"),
         "alifbo": p["alifbo"], "vazifa_soni": p["vazifa_soni"]} for p in people]})


async def api_people_alifbo(request):
    pid = int(request.match_info["pid"])
    alifbo = (await request.json()).get("alifbo")
    if alifbo not in ("kiril", "lotin"):
        return web.json_response({"error": "alifbo noto'g'ri"}, status=400)
    await db.set_alifbo(pid, alifbo)
    return web.json_response({"ok": True})


async def api_people_delete(request):
    pid = int(request.match_info["pid"])
    return web.json_response({"ok": bool(await db.delete_person(pid))})


HOLAT_UZ = {
    "yuborildi": "Yuborildi", "soraldi": "Qayta so'raldi", "oqilmadi": "O'qimadi",
    "javob": "Javob berdi", "javobsiz": "Javob bermadi", "bajarildi": "Bajarildi",
    "kochirildi": "Ko'chirdi", "almashdi": "Yangisi yuborildi",
}


async def api_sends(request):
    rows = await db.recent_sends(40)
    return web.json_response({"sends": [{
        "odam": r["odam"], "title": r["title"], "matn": r["matn"],
        "yuborilgan_at": r["yuborilgan_at"].isoformat(),
        "oqilgan": bool(r["oqilgan_at"]), "javob": r["javob"], "javob_turi": r["javob_turi"],
        "holat": r["holat"], "holat_uz": HOLAT_UZ.get(r["holat"], r["holat"]),
    } for r in rows]})


async def index(request):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def health(request):
    return web.Response(text="ok")


def build_app(handle_text=None, err_text=str, ub=None, bot=None):
    app = web.Application(middlewares=[auth])
    app["handle_text"], app["err_text"], app["ub"], app["bot"] = handle_text, err_text, ub, bot
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/tasks", api_tasks)
    app.router.add_get("/api/history", api_history)
    app.router.add_post("/api/tasks", api_add)
    app.router.add_post("/api/tasks/{tid:\\d+}/done", api_done)
    app.router.add_post("/api/tasks/{tid:\\d+}/pause", api_pause)
    app.router.add_delete("/api/tasks/{tid:\\d+}", api_delete)
    app.router.add_get("/api/tg/status", api_tg_status)
    app.router.add_post("/api/tg/code", api_tg_code)
    app.router.add_post("/api/tg/login", api_tg_login)
    app.router.add_post("/api/tg/password", api_tg_password)
    app.router.add_post("/api/tg/logout", api_tg_logout)
    app.router.add_get("/api/people", api_people)
    app.router.add_post("/api/people/{pid:\\d+}/alifbo", api_people_alifbo)
    app.router.add_delete("/api/people/{pid:\\d+}", api_people_delete)
    app.router.add_get("/api/sends", api_sends)
    return app


async def start(handle_text, err_text, port=8080, ub=None, bot=None):
    runner = web.AppRunner(build_app(handle_text, err_text, ub, bot))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Mini App %s-portda ishlayapti", port)
    return runner
