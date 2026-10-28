"""Userbot: egasining Telegram akkaunti orqali odamlarga yozish (Telethon).

- Sessiya bazada SHIFRLANGAN holda saqlanadi (kalit bot tokenidan olinadi —
  bazaning o'zini olgan odam ham uni o'qiy olmaydi).
- Akkaunt Mini App orqali ulanadi: telefon -> kod -> (bo'lsa) ikki bosqichli parol.
- Faqat "Odamlar" ro'yxatidagi kishilardan kelgan xabarlar ko'riladi (kuzatuv.py hal qiladi).
"""

import os
import re
import time
import base64
import hashlib
import logging
from difflib import SequenceMatcher

from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient, events, functions, types
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError,
    PasswordHashInvalidError, PhoneNumberInvalidError, FloodWaitError,
)
from telethon.sessions import StringSession

import db
from translit import to_cyrillic

log = logging.getLogger("reminder-bot.userbot")

SESSION_KEY = "tg_session"
HONORIFICS = {"aka", "akaga", "akadan", "akani", "opa", "opaga", "domla", "ustoz", "janob",
              "akam", "ака", "опа", "акага"}


def _fernet():
    seed = ("eslatma-session:" + os.environ["TELEGRAM_BOT_TOKEN"]).encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(seed).digest()))


def encrypt(s):
    return _fernet().encrypt(s.encode()).decode()


def decrypt(s):
    try:
        return _fernet().decrypt(s.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def _norm(s):
    s = (s or "").lower().replace("ʻ", "'").replace("’", "'").replace("`", "'")
    s = re.sub(r"[^\w\s']", " ", s)
    words = [w for w in s.split() if w not in HONORIFICS]
    return " ".join(words)


def name_score(query, cand):
    """Ism o'xshashligi 0..1. Lotin va kirill ikkalasi solishtiriladi.
    Ismdagi raqam ("Ibrohim aka 755") mos kelsa — deyarli aniq o'sha odam."""
    q_lat, c = _norm(query), _norm(cand)
    if not q_lat or not c:
        return 0.0
    q_nums = set(re.findall(r"\d{2,}", q_lat))
    num_hit = bool(q_nums & set(re.findall(r"\d{2,}", c)))
    q_lat = re.sub(r"\d+", " ", q_lat).strip() or q_lat
    c = re.sub(r"\b\d+\b", " ", c).strip() or c
    base = _name_only(q_lat, c, query)
    if num_hit:
        return 0.99 if base >= 0.6 else base + 0.15
    return base


def _name_only(q_lat, c, query):
    q_cyr = _norm(to_cyrillic(query))
    best = 0.0
    for q in {q_lat, q_cyr}:
        q_first = q.split()[0]
        for w in c.split():
            best = max(best, SequenceMatcher(None, q_first, w).ratio())
        best = max(best, SequenceMatcher(None, q, c).ratio())
        if q_first and (c.startswith(q_first) or q_first in c.split()):
            best = max(best, 0.95)
    return best


def user_label(u):
    name = " ".join(x for x in [getattr(u, "first_name", None), getattr(u, "last_name", None)] if x)
    return name or (("@" + u.username) if getattr(u, "username", None) else str(u.id))


class UserBot:
    def __init__(self):
        self.api_id = int(os.environ.get("TG_API_ID", "0") or 0)
        self.api_hash = os.environ.get("TG_API_HASH", "")
        self.client = None
        self.me = None
        self.on_message = None  # async fn(event) — kuzatuv.py beradi
        self._login = None  # {"client", "phone", "hash"}
        self._people_cache = (0, [])

    @property
    def configured(self):
        return bool(self.api_id and self.api_hash)

    @property
    def connected(self):
        return self.client is not None and self.me is not None

    def _new_client(self, session=""):
        return TelegramClient(
            StringSession(session), self.api_id, self.api_hash,
            device_model="Eslatma bot", system_version="Railway", app_version="2.0",
        )

    # ---------- ishga tushish ----------

    async def start(self):
        """Bazadagi sessiya bilan ulanadi. Ulanmasa False."""
        if not self.configured:
            log.info("TG_API_ID/TG_API_HASH yo'q — userbot o'chiq.")
            return False
        enc = await db.get_setting(SESSION_KEY)
        session = decrypt(enc) if enc else None
        if not session:
            log.info("Userbot sessiyasi yo'q — Mini App orqali akkauntni ulang.")
            return False
        client = self._new_client(session)
        await client.connect()
        if not await client.is_user_authorized():
            log.warning("Userbot sessiyasi eskirgan — qayta ulash kerak.")
            await client.disconnect()
            await db.set_setting(SESSION_KEY, None)
            return False
        await self._activate(client)
        return True

    async def _activate(self, client):
        self.client = client
        self.me = await client.get_me()
        client.add_event_handler(self._incoming, events.NewMessage(incoming=True))
        log.info("Userbot ulandi: %s", user_label(self.me))

    async def _incoming(self, event):
        if not event.is_private or not self.on_message:
            return
        try:
            await self.on_message(event)
        except Exception as e:  # noqa: BLE001
            log.exception("Kiruvchi xabarni ishlashda xato: %s", e)

    async def stop(self):
        if self.client:
            await self.client.disconnect()

    # ---------- akkauntni ulash (Mini App) ----------

    async def send_code(self, phone):
        if not self.configured:
            raise ValueError("Railway'da TG_API_ID va TG_API_HASH qo'yilmagan.")
        phone = re.sub(r"[^\d+]", "", phone or "")
        if not phone.startswith("+"):
            phone = "+" + phone
        if self._login:
            await self._login["client"].disconnect()
        client = self._new_client()
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
        except PhoneNumberInvalidError:
            await client.disconnect()
            raise ValueError("Telefon raqami noto'g'ri. Masalan: +998901234567")
        except FloodWaitError as e:
            await client.disconnect()
            raise ValueError(f"Telegram juda ko'p urinish deb to'xtatdi. {e.seconds // 60 + 1} daqiqadan keyin urinib ko'ring.")
        self._login = {"client": client, "phone": phone, "hash": sent.phone_code_hash, "t": time.time()}
        return True

    async def sign_in(self, code):
        if not self._login:
            raise ValueError("Avval telefon raqamini yuboring.")
        code = re.sub(r"\D", "", code or "")
        c = self._login["client"]
        try:
            await c.sign_in(self._login["phone"], code, phone_code_hash=self._login["hash"])
        except SessionPasswordNeededError:
            return "password"
        except PhoneCodeInvalidError:
            raise ValueError("Kod noto'g'ri. Telegram'dan kelgan kodni qayta tekshiring.")
        except PhoneCodeExpiredError:
            raise ValueError("Kodning muddati o'tgan. Telefon raqamini qayta yuboring.")
        await self._finish()
        return "ok"

    async def sign_in_password(self, password):
        if not self._login:
            raise ValueError("Avval telefon raqamini yuboring.")
        try:
            await self._login["client"].sign_in(password=password)
        except PasswordHashInvalidError:
            raise ValueError("Ikki bosqichli parol noto'g'ri.")
        await self._finish()
        return "ok"

    async def _finish(self):
        client = self._login["client"]
        self._login = None
        await db.set_setting(SESSION_KEY, encrypt(client.session.save()))
        if self.client:
            await self.client.disconnect()
        await self._activate(client)

    async def logout(self):
        if self.client:
            try:
                await self.client.log_out()
            except Exception:  # noqa: BLE001
                await self.client.disconnect()
        self.client, self.me = None, None
        await db.set_setting(SESSION_KEY, None)

    # ---------- odamlar ----------

    async def _all_people(self):
        """Kontaktlar + oxirgi yozishmalardagi odamlar (10 daqiqa keshlanadi)."""
        ts, cached = self._people_cache
        if time.time() - ts < 600 and cached:
            return cached
        seen, out = set(), []
        res = await self.client(functions.contacts.GetContactsRequest(hash=0))
        for u in getattr(res, "users", []):
            if not u.bot and not u.deleted and u.id not in seen:
                seen.add(u.id)
                out.append(u)
        async for d in self.client.iter_dialogs(limit=300):
            u = d.entity
            if isinstance(u, types.User) and not u.bot and not u.is_self and u.id not in seen:
                seen.add(u.id)
                out.append(u)
        self._people_cache = (time.time(), out)
        return out

    async def find_people(self, name, limit=5):
        people = list(await self._all_people())
        # Telegram'ning o'z qidiruvi ham (kontakt nomi, username) — kirill va lotinda
        seen = {u.id for u in people}
        for q in {re.sub(r"\b(aka|akaga|opa|opaga)\b", "", name, flags=re.I).strip(), to_cyrillic(name)}:
            q = q.strip()
            if len(q) < 3:
                continue
            try:
                res = await self.client(functions.contacts.SearchRequest(q=q[:60], limit=10))
                for u in res.users:
                    if not u.bot and u.id not in seen:
                        seen.add(u.id)
                        people.append(u)
            except Exception as e:  # noqa: BLE001
                log.debug("contacts.search xato: %s", e)
        scored = []
        for u in people:
            label = user_label(u)
            s = max(name_score(name, label), name_score(name, u.username or ""))
            if s >= 0.6:
                scored.append((s, u))
        scored.sort(key=lambda x: -x[0])
        return [{"id": u.id, "name": user_label(u), "username": u.username,
                 "phone": u.phone, "score": round(s, 2)} for s, u in scored[:limit]]

    async def resolve(self, text):
        """@username, t.me havola yoki telefon raqamidan odamni topadi."""
        t = (text or "").strip()
        m = re.search(r"(?:t\.me/|@)([A-Za-z][\w]{3,})", t)
        if m:
            try:
                u = await self.client.get_entity(m.group(1))
            except Exception:  # noqa: BLE001
                raise ValueError(f"@{m.group(1)} topilmadi. Username to'g'ri yozilganini tekshiring.")
            if not isinstance(u, types.User) or u.bot:
                raise ValueError("Bu odam emas (kanal yoki bot).")
            return {"id": u.id, "name": user_label(u), "username": u.username, "phone": u.phone}
        digits = re.sub(r"\D", "", t)
        if len(digits) >= 9:
            phone = "+" + (digits if digits.startswith("998") or len(digits) > 9 else "998" + digits)
            res = await self.client(functions.contacts.ImportContactsRequest([
                types.InputPhoneContact(client_id=0, phone=phone, first_name="Eslatma", last_name="")]))
            if not res.users:
                raise ValueError(f"{phone} raqamida Telegram akkaunt topilmadi yoki u raqamini yashirgan.")
            u = res.users[0]
            return {"id": u.id, "name": user_label(u), "username": u.username, "phone": u.phone}
        raise ValueError("Username (@ bilan) yoki telefon raqamini yozing.")

    async def get_user(self, uid):
        u = await self.client.get_entity(uid)
        return {"id": u.id, "name": user_label(u), "username": u.username, "phone": u.phone}

    # ---------- yuborish va kuzatish ----------

    async def send(self, uid, text, reply_to=None):
        msg = await self.client.send_message(uid, text, reply_to=reply_to)
        return msg.id

    async def read_outbox_max(self, uid):
        """Odam bizning xabarlarimizni qaysi id gacha o'qigan."""
        peer = await self.client.get_input_entity(uid)
        res = await self.client(functions.messages.GetPeerDialogsRequest(
            peers=[types.InputDialogPeer(peer=peer)]))
        return res.dialogs[0].read_outbox_max_id if res.dialogs else 0
