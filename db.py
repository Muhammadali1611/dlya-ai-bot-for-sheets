"""Google Sheets ombori.

'Vazifalar' varag'i      — faol vazifalar (bot shu yerga yozadi)
'Bajarilganlar' varag'i  — "bitdi" deyilgan vazifalar tarixi

Ustunlar (Vazifalar):
A №  | B Vazifa | C Turi | D Jadval | E Keyingi vaqt | F Qolgan vaqt (formula)
G Holat | H trigger | I chat_id | J id
"""

import os
import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread

TZ_NAME = os.environ.get("TZ", "Asia/Tashkent")
TZ = ZoneInfo(TZ_NAME)
SHEET_ID = os.environ["SHEET_ID"]

ACTIVE_SHEET = "Vazifalar"
DONE_SHEET = "Bajarilganlar"

KIND_UZ = {"once": "bir martalik", "recurring": "takroriy"}
KIND_EN = {v: k for k, v in KIND_UZ.items()}

LEFT_FORMULA = (
    '=IF(E{r}="","",IF(E{r}<=NOW(),"⏰ Vaqti keldi",'
    'INT(E{r}-NOW())&" kun "&TEXT(E{r}-NOW(),"hh:mm")))'
)
DATE_FMT = {"numberFormat": {"type": "DATE_TIME", "pattern": "dd.mm.yyyy hh:mm"}}

_lock = threading.Lock()
_sh = None
_active = None
_done = None


def _client():
    raw = os.environ.get("GOOGLE_CREDENTIALS")
    if raw:
        return gspread.service_account_from_dict(json.loads(raw))
    return gspread.service_account(
        filename=os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    )


def _fmt(dt):
    """Sheets tushunadigan sana-vaqt (Toshkent mahalliy vaqti)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    global _sh, _active, _done
    _sh = _client().open_by_key(SHEET_ID)
    _active = _sh.worksheet(ACTIVE_SHEET)
    _done = _sh.worksheet(DONE_SHEET)
    # Vaqt mintaqasi Toshkent, formulalar har daqiqada yangilanadi.
    # Locale en_US — formulalardagi vergul (,) har doim to'g'ri tushunilsin
    # (o'zbek/rus locale'da ; kerak bo'ladi va #ERROR! chiqadi).
    _sh.batch_update({"requests": [{
        "updateSpreadsheetProperties": {
            "properties": {"timeZone": TZ_NAME, "autoRecalc": "MINUTE",
                           "locale": "en_US"},
            "fields": "timeZone,autoRecalc,locale",
        }
    }]})
    _fix_formulas()


def _fix_formulas():
    """Mavjud qatorlardagi № va 'Qolgan vaqt' formulalarini qayta yozadi
    (eski #ERROR! kataklarni tuzatadi)."""
    data = []
    for rem in _all():
        r = rem["row"]
        data.append({"range": f"A{r}", "values": [["=ROW()-1"]]})
        data.append({"range": f"F{r}", "values": [[LEFT_FORMULA.format(r=r)]]})
    if data:
        _active.batch_update(data, value_input_option="USER_ENTERED")
    done = _done.get_all_values()
    ddata = [{"range": f"A{i}", "values": [["=ROW()-1"]]}
             for i in range(2, len(done) + 1) if any(done[i - 1])]
    if ddata:
        _done.batch_update(ddata, value_input_option="USER_ENTERED")


def _parse(values):
    """Varaqdagi qatorlarni lug'atlarga aylantiradi. № = qator raqami - 1."""
    out = []
    for i, row in enumerate(values[1:], start=2):
        row = (row + [""] * 10)[:10]
        rid = row[9].strip()
        if not rid:
            continue
        try:
            rid = int(float(rid))
            chat_id = int(float(row[8]))
        except ValueError:
            continue
        out.append({
            "row": i,
            "num": i - 1,
            "id": rid,
            "title": row[1],
            "kind": KIND_EN.get(row[2].strip(), row[2].strip()),
            "human_time": row[3],
            "left": row[5],
            "status": row[6].strip() or "Faol",
            "trigger_json": row[7],
            "chat_id": chat_id,
        })
    return out


def _all():
    return _parse(_active.get_all_values())


def _find(rid):
    for r in _all():
        if r["id"] == rid:
            return r
    return None


def _next_id(values):
    ids = [0]
    for row in values[1:]:
        if len(row) >= 10 and row[9].strip():
            try:
                ids.append(int(float(row[9])))
            except ValueError:
                pass
    for v in _done.col_values(6)[1:]:
        try:
            ids.append(int(float(v)))
        except ValueError:
            pass
    return max(ids) + 1


# ---------- o'qish ----------

def all_reminders():
    with _lock:
        return _all()


def list_reminders(chat_id):
    with _lock:
        return [r for r in _all() if r["chat_id"] == chat_id]


def get_reminder(rid):
    with _lock:
        return _find(rid)


# ---------- yozish ----------

def add_reminder(chat_id, title, kind, trigger, human_time, next_dt):
    with _lock:
        values = _active.get_all_values()
        r = len(values) + 1
        new_id = _next_id(values)
        row = [
            "=ROW()-1",
            title,
            KIND_UZ.get(kind, kind),
            human_time,
            _fmt(next_dt),
            LEFT_FORMULA.format(r=r),
            "Faol",
            json.dumps(trigger, ensure_ascii=False),
            str(chat_id),
            new_id,
        ]
        _active.update(values=[row], range_name=f"A{r}:J{r}",
                       value_input_option="USER_ENTERED")
        _active.format(f"E{r}", DATE_FMT)
        return new_id


def delete_reminder(rid):
    with _lock:
        rem = _find(rid)
        if rem:
            _active.delete_rows(rem["row"])
        return rem


def mark_done(rid):
    """Vazifani Bajarilganlar varag'iga yozadi.
    Bir martalik — Vazifalar'dan o'chadi. Takroriy — 'Bitdi' bo'lib turadi va
    keyingi navbati kelganda (yoki oy boshida) o'zi qayta 'Faol' bo'ladi."""
    with _lock:
        rem = _find(rid)
        if not rem:
            return None
        now = datetime.now(TZ)
        d = len(_done.get_all_values()) + 1
        _done.update(
            values=[["=ROW()-1", rem["title"], rem["human_time"], _fmt(now),
                     now.strftime("%Y-%m"), rem["id"]]],
            range_name=f"A{d}:F{d}",
            value_input_option="USER_ENTERED",
        )
        _done.format(f"D{d}", DATE_FMT)
        if rem["kind"] == "once":
            _active.delete_rows(rem["row"])
        else:
            _active.update(values=[["Bitdi"]], range_name=f"G{rem['row']}")
        return rem


def after_fire(rid, next_dt=None):
    """Eslatma yuborilgach: takroriy vazifa uchun keyingi vaqtni yangilaydi va
    holatini yana 'Faol' qiladi (avtomatik reset)."""
    with _lock:
        rem = _find(rid)
        if not rem:
            return
        r = rem["row"]
        if next_dt is not None:
            _active.update(values=[[_fmt(next_dt)]], range_name=f"E{r}",
                           value_input_option="USER_ENTERED")
        _active.update(values=[["Faol"]], range_name=f"G{r}")


def refresh_next_times(mapping):
    """{id: next_dt} — bot qayta yonganda keyingi vaqtlarni bir yo'la yangilaydi."""
    if not mapping:
        return
    with _lock:
        data = []
        for rem in _all():
            dt = mapping.get(rem["id"])
            if dt is not None:
                data.append({"range": f"E{rem['row']}", "values": [[_fmt(dt)]]})
        if data:
            _active.batch_update(data, value_input_option="USER_ENTERED")


def monthly_reset():
    """Oy boshida barcha 'Bitdi' takroriy vazifalarni yana 'Faol' qiladi."""
    with _lock:
        data = [
            {"range": f"G{r['row']}", "values": [["Faol"]]}
            for r in _all()
            if r["kind"] == "recurring" and r["status"] == "Bitdi"
        ]
        if data:
            _active.batch_update(data)
        return len(data)
