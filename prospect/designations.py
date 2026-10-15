"""Designations: the job titles a seller filters People by, per product.

An ATS seller looks for the head of talent acquisition, the HR director and
the COO; a Glynac seller for the compliance officer. config/designations.yml
holds a library of designations, each a label and a few plain phrases, and
lists which ones each product shows.

Titles arrive in every shape ("VP, HR", "Director of Human Resources", "Sr.
Talent Acquisition Partner"), so titles and phrases are read the same way:
lowercase, punctuation and small words dropped, short forms spelled out. A
phrase matches when each of its words starts a word of the title, in any
order. The People index tags every title with the designations it matches
when it is built, so a designation filter is one indexed read; a title typed
by hand is matched with the same rules in SQL.
"""

from __future__ import annotations

import re
import time

import yaml

from . import config

GENERAL = "_all"

# Short forms spelled out, on titles and phrases alike. "vice president" is
# one word here, so President never matches a vice president.
ABBR = {
    "vp": "vicepresident", "svp": "senior vicepresident", "evp": "executive vicepresident",
    "avp": "assistant vicepresident", "hr": "human resources", "ops": "operations",
    "dir": "director", "mgr": "manager", "mgmt": "management", "sr": "senior", "snr": "senior",
    "jr": "junior", "asst": "assistant", "assoc": "associate", "admin": "administrator",
    "ceo": "chief executive officer", "coo": "chief operating officer",
    "cfo": "chief financial officer", "cio": "chief investment officer",
    "cco": "chief compliance officer", "cto": "chief technology officer",
    "cmo": "chief marketing officer", "chro": "chief human resources officer",
    "cro": "chief risk officer", "md": "managing director", "it": "information technology",
    "pm": "portfolio manager", "mktg": "marketing", "bd": "business development",
}
STOP = {"of", "the", "and", "for", "to", "in", "at", "a", "an", "our", "on", "with"}
_VP = re.compile(r"\bvice[\s\-]*pres(ident)?\b|\bv\.\s*p\.?(?=\s|,|$)")


def words(text: str | None) -> list[str]:
    t = _VP.sub(" vicepresident ", (text or "").lower())
    out: list[str] = []
    for w in re.findall(r"[a-z0-9]+", t):
        if w in STOP:
            continue
        out.extend(ABBR.get(w, w).split())
    return out


def norm(text: str | None) -> str:
    return " ".join(words(text))


def _hit(title_words: list[str], phrase: tuple[str, ...]) -> bool:
    return bool(phrase) and all(any(t.startswith(p) for t in title_words) for p in phrase)


# ------------------------------------------------------------------ config

def _file() -> dict:
    try:
        return yaml.safe_load((config.CONFIG_DIR / "designations.yml").read_text(
            encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _item(key: str, raw: dict) -> dict | None:
    label = str(raw.get("label") or "").strip()
    anyp = [str(p).strip() for p in raw.get("any") or [] if str(p).strip()]
    if not label or not anyp:
        return None
    return {"key": key, "label": label[:80], "any": anyp,
            "not": [str(p).strip() for p in raw.get("not") or [] if str(p).strip()]}


def defaults() -> dict[str, list[dict]]:
    """product key -> its designations, as the file ships them."""
    raw = _file()
    lib = raw.get("library") or {}
    out: dict[str, list[dict]] = {}
    for prod, keys in (raw.get("products") or {}).items():
        items = []
        for k in keys or []:
            it = _item(k, lib[k]) if isinstance(k, str) and k in lib else None
            if it:
                items.append(it)
        out[prod] = items
    return out


_CACHE: dict = {"t": 0.0, "lists": None}
CHECK_S = 60.0


def _load() -> dict:
    """The file's lists, read again at most once a minute."""
    if _CACHE["lists"] is not None and time.monotonic() - _CACHE["t"] < CHECK_S:
        return _CACHE["lists"]
    _CACHE.update(t=time.monotonic(), lists=defaults())
    return _CACHE["lists"]


def for_product(product: str | None) -> list[dict]:
    """The designations shown for a product, or the general set."""
    lists = _load()
    return lists.get(product or GENERAL) or lists.get(GENERAL) or []


def tag(product: str, key: str) -> str:
    return f"{product}:{key}"


def label_of(t: str) -> str:
    prod, _, key = t.partition(":")
    return next((d["label"] for d in for_product(prod) if d["key"] == key), key)


# ------------------------------------------------------------------ matching

def matcher():
    """Every product's designations compiled once: [(tags, any, not)]. A
    designation several products share is one rule carrying all their tags,
    so each title is tested against it once."""
    lists = _load()
    rules: dict[tuple, list[str]] = {}
    for prod, items in lists.items():
        for d in items:
            sig = (tuple(tuple(words(p)) for p in d["any"]),
                   tuple(tuple(words(p)) for p in d["not"]))
            rules.setdefault(sig, []).append(tag(prod, d["key"]))
    return [(tags, list(anyp), list(notp)) for (anyp, notp), tags in rules.items()]


def tags_for(title: str | None, compiled=None) -> list[str]:
    tw = words(title)
    if not tw:
        return []
    out: list[str] = []
    for tags, anyp, notp in compiled if compiled is not None else matcher():
        if any(_hit(tw, p) for p in anyp) and not any(_hit(tw, p) for p in notp):
            out.extend(tags)
    return out


def typed_sql(text: str, column: str = "title_norm") -> tuple[str, list]:
    """A title typed by hand, as SQL on the normalised title: commas separate
    alternatives, and each word must start a word of the title."""
    alts = []
    args: list = []
    for part in (text or "").split(","):
        ws = words(part)[:8]
        if not ws:
            continue
        conds = []
        for w in ws:
            conds.append(f"(' ' || COALESCE({column}, '')) LIKE ?")
            args.append(f"% {w}%")   # words() leaves letters and digits only
        alts.append("(" + " AND ".join(conds) + ")")
    if not alts:
        return "", []
    return "(" + " OR ".join(alts[:6]) + ")", args


def matches_typed(title: str | None, text: str) -> bool:
    tw = words(title)
    return any(_hit(tw, tuple(words(p))) for p in (text or "").split(",") if words(p))
