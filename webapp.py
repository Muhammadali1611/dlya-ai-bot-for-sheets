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


async def index(request):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def health(request):
    return web.Response(text="ok")


def build_app(handle_text=None, err_text=str):
    app = web.Application(middlewares=[auth])
    app["handle_text"], app["err_text"] = handle_text, err_text
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/tasks", api_tasks)
    app.router.add_get("/api/history", api_history)
    app.router.add_post("/api/tasks", api_add)
    app.router.add_post("/api/tasks/{tid:\\d+}/done", api_done)
    app.router.add_post("/api/tasks/{tid:\\d+}/pause", api_pause)
    app.router.add_delete("/api/tasks/{tid:\\d+}", api_delete)
    return app


async def start(handle_text, err_text, port=8080):
    runner = web.AppRunner(build_app(handle_text, err_text))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Mini App %s-portda ishlayapti", port)
    return runner
