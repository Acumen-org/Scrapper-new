"""Industry knowledge: what Bellwether knows about the adviser industry and
about Acumen's own purpose, kept where an admin can read and change it.

Five kinds of item, one table (knowledge_item):

  category  the firm types the classifier assigns (prospect/firmtype.py). The
            keys are fixed by the rules; the label, description and how
            Bellwether treats each type are editable.
  entity    known names: custodians, wirehouses, broker-dealers, banks,
            insurers, asset managers, private fund managers, TAMPs, robo-
            advisers, consultants and RIA groups. Each carries name patterns
            matched against a firm's own names and against the companies that
            control it. Adding "Apex Clearing" as a custodian here changes how
            every firm with that name or that owner is classified at the next
            run.
  glossary  industry terms (RIA, ERA, RAUM, Form ADV, 13F, TAMP, OCIO ...).
  product   what Acumen sells and to whom (PHH, AcuBooth, Glynac).
  fact      how Bellwether works, so Bellwether AI can explain itself.

The shipped content is config/industry.yml. It is loaded into the table on
first start and whenever an item in the file changes, but an item an admin has
edited (updated_by is no longer 'seed') is never overwritten: the person's
version wins, and the file stays the reset point.

Readers: the classifier (matcher), the scores (through each firm's type), the
Settings screens, and Bellwether AI (ai_context, a compact digest added to its
system prompt).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import yaml

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_item (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,      -- category | entity | glossary | product | fact
    key         TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT,
    data        TEXT,               -- JSON: fields particular to the kind
    active      INTEGER NOT NULL DEFAULT 1,
    updated_by  TEXT,               -- 'seed' until a person edits the item
    updated_at  TEXT NOT NULL,
    seed_hash   TEXT,               -- the shipped version last applied
    UNIQUE (kind, key)
);
CREATE INDEX IF NOT EXISTS ix_knowledge_kind ON knowledge_item (kind, active);
"""

SEED = "seed"

KINDS = {
    "category": "Firm types",
    "entity": "Known entities",
    "product": "Acumen and its products",
    "glossary": "Glossary",
    "fact": "How Bellwether works",
}

KIND_HELP = {
    "category": "The types every firm is sorted into. The rules decide the type; the words "
                "here are what people and Bellwether AI read.",
    "entity": "Names that settle a firm's type. Patterns match the firm's own names; owner "
              "patterns match companies that own 50% or more of it. The longest matching "
              "pattern wins. Changes apply at the next reclassification.",
    "product": "What Acumen sells and to whom, in plain words, for Bellwether AI.",
    "glossary": "Industry terms Bellwether AI uses and explains.",
    "fact": "How Bellwether itself works, so its AI can explain scores and data.",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ seed

def seed_items() -> list[dict]:
    """The shipped items from config/industry.yml, in the table's shape."""
    y = yaml.safe_load((config.CONFIG_DIR / "industry.yml").read_text(encoding="utf-8"))
    out: list[dict] = []
    for c in y.get("categories") or []:
        out.append({"kind": "category", "key": c["key"], "title": c["label"],
                    "body": c.get("description") or "",
                    "data": {"short": c.get("short") or c["label"],
                             "treatment": c.get("treatment") or ""}})
    for e in y.get("entities") or []:
        data = {"category": e["category"], "patterns": list(e.get("patterns") or []),
                "parents": list(e.get("parents") or [])}
        if e.get("confidence"):
            data["confidence"] = int(e["confidence"])
        out.append({"kind": "entity", "key": e["key"], "title": e["title"],
                    "body": e.get("note") or "", "data": data})
    for kind, section in (("glossary", "glossary"), ("product", "products"),
                          ("fact", "facts")):
        for g in y.get(section) or []:
            out.append({"kind": kind, "key": g["key"], "title": g["title"],
                        "body": g.get("body") or "", "data": {}})
    return out


def _hash(item: dict) -> str:
    text = json.dumps([item["title"], item["body"], item["data"]], sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def init(conn) -> None:
    conn.executescript(SCHEMA)
    seed(conn)
    conn.commit()


def seed(conn) -> int:
    """Load shipped items that are new or changed, never touching an item a
    person has edited. Returns how many rows were written."""
    have = {(r["kind"], r["key"]): dict(r) for r in conn.execute(
        "SELECT kind, key, updated_by, seed_hash FROM knowledge_item")}
    n = 0
    now = _now()
    for it in seed_items():
        h = _hash(it)
        row = have.get((it["kind"], it["key"]))
        data = json.dumps(it["data"], separators=(",", ":"))
        if row is None:
            conn.execute("INSERT INTO knowledge_item (kind, key, title, body, data, active,"
                         " updated_by, updated_at, seed_hash) VALUES (?,?,?,?,?,1,?,?,?)"
                         " ON CONFLICT (kind, key) DO NOTHING",
                         (it["kind"], it["key"], it["title"], it["body"], data, SEED, now, h))
            n += 1
        elif row["updated_by"] == SEED and row["seed_hash"] != h:
            conn.execute("UPDATE knowledge_item SET title=?, body=?, data=?, updated_at=?,"
                         " seed_hash=? WHERE kind=? AND key=? AND updated_by=?",
                         (it["title"], it["body"], data, now, h, it["kind"], it["key"], SEED))
            n += 1
    # A shipped item taken out of the file goes too, unless a person edited it.
    shipped = {(it["kind"], it["key"]) for it in seed_items()}
    for (kind, key), row in have.items():
        if (kind, key) not in shipped and row["updated_by"] == SEED:
            conn.execute("DELETE FROM knowledge_item WHERE kind=? AND key=? AND updated_by=?",
                         (kind, key, SEED))
            n += 1
    return n


# ------------------------------------------------------------------ read

def _row(r) -> dict:
    d = dict(r)
    try:
        d["data"] = json.loads(d.get("data") or "{}")
    except ValueError:
        d["data"] = {}
    d["edited"] = d.get("updated_by") not in (None, SEED)
    return d


def items(conn, kind: str | None = None, active_only: bool = False) -> list[dict]:
    """Items for Settings and the readers below, in a stable order."""
    sql = "SELECT * FROM knowledge_item WHERE 1=1"
    args: list = []
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if active_only:
        sql += " AND active=1"
    sql += " ORDER BY kind, id"
    try:
        return [_row(r) for r in conn.execute(sql, tuple(args)).fetchall()]
    except Exception:
        conn.rollback()
        return [dict(it, id=None, active=1, updated_by=SEED, updated_at=None, edited=False)
                for it in seed_items() if not kind or it["kind"] == kind]


def get(conn, item_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM knowledge_item WHERE id=?", (int(item_id),)).fetchone()
    return _row(r) if r else None


def category_text(conn=None) -> dict[str, dict]:
    """Edited label, short label, description and treatment per category key,
    for overlaying the shipped definitions."""
    rows = _cached_rows(conn)
    return {r["key"]: {"label": r["title"], "description": r["body"] or "",
                       "short": (r["data"] or {}).get("short") or r["title"],
                       "treatment": (r["data"] or {}).get("treatment") or ""}
            for r in rows if r["kind"] == "category" and r.get("active", 1)}


# A short-lived in-process copy, so a page that formats a hundred badges or a
# classifier run reads the table once. Re-checked every few seconds so an edit
# in Settings reaches every process without a restart.
_CACHE: dict = {"t": 0.0, "rows": None}
CACHE_S = 15.0


def _cached_rows(conn=None) -> list[dict]:
    if _CACHE["rows"] is not None and time.monotonic() - _CACHE["t"] < CACHE_S:
        return _CACHE["rows"]
    own = conn is None
    try:
        if own:
            from . import db
            conn = db.connect()
        rows = items(conn, active_only=True)
    except Exception:
        rows = [dict(it, id=None, active=1, updated_by=SEED, edited=False)
                for it in seed_items()]
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    _CACHE.update(t=time.monotonic(), rows=rows)
    return rows


def invalidate() -> None:
    _CACHE.update(t=0.0, rows=None)


# ------------------------------------------------------------- matching

def normalize(name: str | None) -> str:
    """Capitals, '&' as AND, punctuation as spaces, single spaces. Patterns and
    names go through the same function, so "J.P. Morgan" matches "J P MORGAN"."""
    s = (name or "").upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return " ".join(s.split())


@dataclass
class Entity:
    key: str
    title: str
    category: str
    confidence: int
    note: str = ""
    patterns: list = field(default_factory=list)   # (text, compiled regex or None)
    parents: list = field(default_factory=list)


@dataclass
class Match:
    entity: Entity
    pattern: str        # as an admin wrote it
    matched: str        # the name it matched
    strength: int       # longer and more specific is stronger

    @property
    def generic(self) -> bool:
        return self.pattern.startswith("re:")


def _compile(pats: list[str]) -> list[tuple[str, object]]:
    out = []
    for p in pats or []:
        p = str(p).strip()
        if not p:
            continue
        if p.startswith("re:"):
            try:
                out.append((p, re.compile(p[3:])))
            except re.error:
                continue
        else:
            norm = normalize(p)
            if norm:
                out.append((p, norm))
    return out


def check_pattern(p: str) -> str | None:
    """An error message for a pattern that cannot work, else None."""
    p = (p or "").strip()
    if p.startswith("re:"):
        try:
            re.compile(p[3:])
        except re.error as e:
            return f'"{p}" is not a valid regular expression ({e})'
        return None
    if len(normalize(p)) < 3:
        return f'"{p}" is too short to match safely'
    return None


class Matcher:
    """Known entities compiled for matching firm and owner names."""

    def __init__(self, entities: list[Entity]):
        self.entities = entities

    def _scan(self, name: str, which: str) -> Match | None:
        norm = normalize(name)
        if not norm:
            return None
        padded = f" {norm} "
        best: Match | None = None
        for e in self.entities:
            for text, pat in getattr(e, which):
                if isinstance(pat, str):
                    if f" {pat} " not in padded:
                        continue
                    strength = 1000 + len(pat)       # a named entity beats a generic rule
                else:
                    m = pat.search(norm)
                    if not m:
                        continue
                    strength = len(m.group(0))
                if best is None or strength > best.strength:
                    best = Match(e, text, name, strength)
        return best

    def firm(self, names: list[str | None]) -> Match | None:
        """The strongest entity matching any of a firm's own names."""
        best = None
        for n in names:
            if not n:
                continue
            m = self._scan(n, "patterns")
            if m and (best is None or m.strength > best.strength):
                best = m
        return best

    def parent(self, name: str | None) -> Match | None:
        return self._scan(name or "", "parents")


def _entities_from(rows: list[dict]) -> list[Entity]:
    out = []
    for r in rows:
        if r["kind"] != "entity" or not r.get("active", 1):
            continue
        d = r.get("data") or {}
        cat = d.get("category")
        if not cat:
            continue
        out.append(Entity(key=r["key"], title=r["title"], category=cat,
                          confidence=int(d.get("confidence") or 95), note=r.get("body") or "",
                          patterns=_compile(d.get("patterns") or []),
                          parents=_compile(d.get("parents") or [])))
    return out


def matcher(conn=None, fresh: bool = False) -> Matcher:
    """The known entities in force. `fresh` skips the in-process cache, for a
    classifier run that must see an edit made a second ago."""
    if fresh:
        invalidate()
    return Matcher(_entities_from(_cached_rows(conn)))


# ------------------------------------------------------------------ write

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s[:48] or "item"


def _lines(v) -> list[str]:
    if isinstance(v, list):
        vals = v
    else:
        vals = re.split(r"[\r\n]+", v or "")
    return [x.strip() for x in vals if x and x.strip()]


def save_item(conn, item_id: int | None, kind: str, title: str, body: str,
              data: dict | None, who: str, key: str | None = None) -> int:
    """Create or update one item after validating it. Returns its id.

    For entities, `data` carries category, patterns and parents (lists or one
    per line) and an optional confidence."""
    from . import firmtype
    if kind not in KINDS:
        raise ValueError("Unknown kind of item.")
    title = (title or "").strip()[:160]
    body = (body or "").strip()[:2000]
    if not title:
        raise ValueError("Give it a title.")
    data = dict(data or {})
    old = get(conn, item_id) if item_id else None
    if item_id and not old:
        raise ValueError("That item no longer exists.")
    if old and old["kind"] != kind:
        raise ValueError("An item cannot change its kind.")
    if kind == "category":
        if not old:
            raise ValueError("Firm types come from the classifier's rules; edit an existing one.")
        data = {"short": (data.get("short") or title).strip()[:40],
                "treatment": (data.get("treatment") or "").strip()[:400]}
    elif kind == "entity":
        cat = data.get("category")
        if cat not in firmtype.ASSIGNABLE:
            raise ValueError("Choose the firm type this entity belongs to.")
        pats, parents = _lines(data.get("patterns")), _lines(data.get("parents"))
        if not pats and not parents:
            raise ValueError("Give at least one name pattern or owner pattern.")
        for p in pats + parents:
            err = check_pattern(p)
            if err:
                raise ValueError(err)
        conf = data.get("confidence")
        try:
            conf = int(conf) if conf not in (None, "") else None
        except (TypeError, ValueError):
            raise ValueError("Confidence must be a whole number from 30 to 99.") from None
        if conf is not None and not 30 <= conf <= 99:
            raise ValueError("Confidence must be a whole number from 30 to 99.")
        data = {"category": cat, "patterns": pats[:200], "parents": parents[:200]}
        if conf is not None:
            data["confidence"] = conf
    else:
        data = {}
    text = json.dumps(data, separators=(",", ":"))
    now = _now()
    who = (who or "admin").strip() or "admin"
    if who == SEED:
        who = "admin"
    if old:
        conn.execute("UPDATE knowledge_item SET title=?, body=?, data=?, updated_by=?,"
                     " updated_at=? WHERE id=?", (title, body, text, who, now, old["id"]))
        new_id = old["id"]
    else:
        base = _slug(key or title)
        k, i = base, 2
        while conn.execute("SELECT 1 FROM knowledge_item WHERE kind=? AND key=?",
                           (kind, k)).fetchone():
            k, i = f"{base}_{i}", i + 1
        cur = conn.execute("INSERT INTO knowledge_item (kind, key, title, body, data, active,"
                           " updated_by, updated_at) VALUES (?,?,?,?,?,1,?,?) RETURNING id",
                           (kind, k, title, body, text, who, now))
        new_id = cur.lastrowid
    conn.commit()
    invalidate()
    return int(new_id)


def set_active(conn, item_id: int, active: bool, who: str) -> None:
    """Switch an item off (or back on). Firm types cannot be switched off."""
    it = get(conn, item_id)
    if not it:
        raise ValueError("That item no longer exists.")
    if it["kind"] == "category":
        raise ValueError("Firm types cannot be switched off.")
    conn.execute("UPDATE knowledge_item SET active=?, updated_by=?, updated_at=? WHERE id=?",
                 (1 if active else 0, (who or "admin") if who != SEED else "admin", _now(),
                  it["id"]))
    conn.commit()
    invalidate()


def reset_item(conn, item_id: int) -> bool:
    """Put a shipped item back to the version in config/industry.yml."""
    it = get(conn, item_id)
    if not it:
        return False
    shipped = next((s for s in seed_items()
                    if s["kind"] == it["kind"] and s["key"] == it["key"]), None)
    if shipped is None:
        return False
    conn.execute("UPDATE knowledge_item SET title=?, body=?, data=?, active=1, updated_by=?,"
                 " updated_at=?, seed_hash=? WHERE id=?",
                 (shipped["title"], shipped["body"],
                  json.dumps(shipped["data"], separators=(",", ":")), SEED, _now(),
                  _hash(shipped), it["id"]))
    conn.commit()
    invalidate()
    return True


def delete_item(conn, item_id: int) -> None:
    """Remove an item a person added. Shipped items can only be switched off
    or reset, so the file's version is never lost."""
    it = get(conn, item_id)
    if not it:
        return
    if it["kind"] == "category" or is_shipped(it):
        raise ValueError("Shipped items can be switched off or reset, not deleted.")
    conn.execute("DELETE FROM knowledge_item WHERE id=?", (it["id"],))
    conn.commit()
    invalidate()


def is_shipped(item: dict) -> bool:
    return any(s["kind"] == item["kind"] and s["key"] == item["key"] for s in seed_items())


# ------------------------------------------------------------------ AI

def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 3].rstrip() + "..."


def _first(text: str, n: int) -> str:
    """The opening sentence (or two, while they fit) of a text, at most n
    characters, so a digest entry ends on a full stop rather than mid-word."""
    text = " ".join((text or "").split())
    out = ""
    for sent in re.split(r"(?<![A-Z]\.)(?<=[.!?])\s+", text):
        if len(out) + len(sent) + 1 > n:
            break
        out = f"{out} {sent}".strip()
    return out or _clip(text, n)


def ai_context(conn=None, max_chars: int = 9000, names: bool = False) -> str:
    """A compact digest for Bellwether AI's system prompt: what Acumen sells,
    the firm types with the lists each may appear on, well-known names by
    type (only with names=True: models know Schwab is a custodian, and each
    firm's own type is in its dossier), the glossary and how Bellwether works.
    About 2,000 tokens: each entry keeps its opening sentences rather than the
    digest being cut short."""
    rows = _cached_rows(conn)
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["kind"], []).append(r)
    L: list[str] = []
    if by.get("product"):
        L.append("ACUMEN AND WHAT IT SELLS")
        for r in by["product"]:
            L.append(f"- {r['title']}: {_first(r['body'], 380)}")
    from . import firmtype
    order = {k: i for i, k in enumerate(firmtype.KEYS)}
    cats = sorted(by.get("category") or [], key=lambda r: order.get(r["key"], 99))
    if cats:
        sells = _product_reach()
        L.append("FIRM TYPES (every firm has one, with a confidence and reasons; "
                 "lists = the product lists it may appear on)")
        for r in cats:
            on = sells.get(r["key"]) or ("all" if r["key"] == "unknown" else "none")
            L.append(f"- {r['title']}: {_first(r['body'], 150)} Lists: {on}.")
    groups: dict[str, list[str]] = {}
    for r in by.get("entity") or []:
        d = r.get("data") or {}
        names = [p for p in d.get("patterns") or [] if not str(p).startswith("re:")]
        if d.get("category") and names:
            groups.setdefault(d["category"], []).extend(names[:3])
    if groups and names:
        L.append("WELL-KNOWN NAMES BY TYPE (a firm named like these is usually that type)")
        for cat in sorted(groups, key=lambda k: order.get(k, 99)):
            L.append(f"- {firmtype.label(cat)}: {', '.join(groups[cat][:12])}")
    if by.get("glossary"):
        L.append("GLOSSARY")
        L.append("; ".join(f"{r['title']}: {_first(r['body'], 150)}" for r in by["glossary"]))
    if by.get("fact"):
        L.append("HOW BELLWETHER WORKS")
        for r in by["fact"]:
            L.append(f"- {r['title']}: {_first(r['body'], 340)}")
    text = "\n".join(L)
    return text if len(text) <= max_chars else text[:max_chars - 3].rstrip() + "..."


def _product_reach() -> dict[str, str]:
    """Category key -> the product lists that may include it, from the
    firm-type rule in the scoring config."""
    try:
        from . import products
        out: dict[str, list[str]] = {}
        for k in products.product_keys():
            rule = products.firm_type_rule(k)
            if not rule or rule.get("off"):
                continue
            for c in rule["allow"]:
                out.setdefault(c, []).append(products.product(k)["name"])
        return {c: ", ".join(v) for c, v in out.items()}
    except Exception:
        return {}
