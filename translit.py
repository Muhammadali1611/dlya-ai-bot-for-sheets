"""O'zbek lotin yozuvidan kirillga o'girish (odamlarga yuboriladigan xabarlar uchun).

Qoidalar: sh→ш, ch→ч, o'→ў, g'→ғ, yo→ё, yu→ю, ya→я, ye→е,
so'z boshida e→э, tutuq belgisi (') → ъ. Raqamlar, havolalar, @username o'zgarmaydi.
"""

import re

APOS = "'’ʼʻ`‘"

# Uzunroq birikmalar birinchi tekshiriladi
PAIRS = [
    ("sh", "ш"), ("ch", "ч"), ("yo", "ё"), ("yu", "ю"), ("ya", "я"), ("ye", "е"),
]
SINGLE = {
    "a": "а", "b": "б", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "ҳ", "i": "и",
    "j": "ж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "қ",
    "r": "р", "s": "с", "t": "т", "u": "у", "v": "в", "x": "х", "y": "й", "z": "з",
    "c": "с", "w": "в",
}
VOWELS = set("aeiouAEIOU")
SKIP = re.compile(r"(https?://\S+|@\w+|#\w+|\S+@\S+)")


def _case(src, out):
    if src.isupper() and len(src) > 1:
        return out.upper()
    if src[0].isupper():
        return out[0].upper() + out[1:]
    return out


def _word(w):
    res, i, n = [], 0, len(w)
    while i < n:
        ch = w[i]
        low = ch.lower()
        nxt = w[i + 1] if i + 1 < n else ""
        # o' va g'
        if low in "og" and nxt and nxt in APOS:
            res.append(_case(ch, "ў" if low == "o" else "ғ"))
            i += 2
            continue
        pair = (ch + nxt).lower()
        hit = next((c for p, c in PAIRS if p == pair), None)
        if hit:
            res.append(_case(ch, hit))
            i += 2
            continue
        if ch in APOS:
            # so'z ichidagi tutuq belgisi (ma'no, san'at) → ъ
            res.append("ъ" if 0 < i < n - 1 else ch)
            i += 1
            continue
        if low == "e" and (i == 0 or not w[i - 1].isalpha()):
            res.append(_case(ch, "э"))
            i += 1
            continue
        if low in SINGLE:
            res.append(_case(ch, SINGLE[low]))
        else:
            res.append(ch)
        i += 1
    return "".join(res)


def to_cyrillic(text):
    """Lotin matnni kirillga o'giradi; havola, @username, email o'zgarmaydi."""
    if not text:
        return text
    parts = SKIP.split(text)
    out = []
    for k, part in enumerate(parts):
        if k % 2 == 1:  # o'zgarmaydigan bo'lak
            out.append(part)
        else:
            out.append(re.sub(r"[A-Za-z" + APOS + r"]+", lambda m: _word(m.group(0)), part))
    return "".join(out)
