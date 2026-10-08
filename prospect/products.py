"""Product scoring: how every firm is judged against every product list.

The rules live in config/products.yml (gates, weights, points per level)
and in one small function per criterion here, which decides which level a firm
reaches and says why in plain words. Nothing in this module is a hidden
number: a threshold is either in the config or named in the evidence string
the firm page prints next to the points.

Three layers, deliberately separate:

  features   everything known about a firm, read in bulk from the store
             (load_features). One dict per firm, so scoring is pure Python
             over data and the same code scores one firm or forty thousand.
  evaluate   gates, disqualifiers, criteria, penalties and signals for one
             product and one firm, returning a Result that carries its own
             explanation (evaluate).
  persist    product_score (one row per firm per product it is scored for)
             and firm_scope (every firm on at least one list, with its best
             score), rebuilt whole by score_all and patched for one firm by
             rescore_firm after someone sets a manual level.

Manual levels: criteria marked `manual` accept a level set by an SDR on the
firm page (score_override). The manual level replaces the computed one for
that firm, and the evidence says who set it, when and why, so a number never
looks computed when a person chose it.

Missing data: every criterion says whether its level rests on a finding or on
data Bellwether does not have yet. Unknown criteria earn nothing, and each
score carries its coverage (the share of the weight that rests on real data)
and its potential (what it would be if every unknown came back at full
marks). A firm with half its data missing therefore shows a modest score, an
amber coverage figure and the list of what is missing, never a high number
built from the half that happened to be known.

One list per product, ranked by score; there are no tiers. The weights,
levels and thresholds come from config/products.yml, overlaid by whatever an
admin or the product's owner has changed on the Scoring screen (stored in
scoring_config, with every version kept in scoring_history).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from functools import lru_cache
import math

import yaml

from . import config

# ------------------------------------------------------------------ config

_CFG: dict | None = None
_CFG_STATE: dict = {"t": 0.0, "fp": None}
CFG_CHECK_S = 10.0

CONFIG_SCHEMA = """
CREATE TABLE IF NOT EXISTS scoring_config (
    product    TEXT PRIMARY KEY,     -- a product key, or '_global'
    body       TEXT NOT NULL,        -- JSON: the product's whole definition
    updated_by TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scoring_history (
    id         INTEGER PRIMARY KEY,
    product    TEXT NOT NULL,
    body       TEXT NOT NULL,
    note       TEXT,
    updated_by TEXT,
    updated_at TEXT NOT NULL
);
"""

GLOBAL_KEYS = ("major_custodians", "phh_focus_states")


def base_cfg() -> dict:
    """The shipped defaults, exactly as config/products.yml states them."""
    return yaml.safe_load((config.CONFIG_DIR / "products.yml").read_text(
        encoding="utf-8"))


def _overrides() -> tuple[dict, str | None]:
    """Edited product definitions from the database, and a fingerprint that
    changes whenever any of them does."""
    from . import db
    try:
        c = db.connect()
        try:
            rows = c.execute("SELECT product, body, updated_at FROM scoring_config"
                             ).fetchall()
        finally:
            c.close()
    except Exception:
        return {}, None
    out = {r["product"]: json.loads(r["body"]) for r in rows}
    fp = "|".join(sorted(f"{r['product']}@{r['updated_at']}" for r in rows))
    return out, fp


def _merge(base: dict, over: dict) -> dict:
    merged = json.loads(json.dumps(base))
    g = over.get("_global") or {}
    for k in GLOBAL_KEYS:
        if k in g:
            merged[k] = g[k]
    for key, body in over.items():
        if key in merged["products"]:
            body = json.loads(json.dumps(body))
            # A product edited before a gate or disqualifier shipped (the
            # firm-type rule, say) still gets it: the editor cannot remove
            # gates or disqualifiers, so a missing one was never a choice.
            for section in ("gates", "disqualifiers"):
                have = {g["key"] for g in body.get(section) or []}
                for g in merged["products"][key].get(section) or []:
                    if g["key"] not in have:
                        body.setdefault(section, []).append(json.loads(json.dumps(g)))
            merged["products"][key] = body
    # Preserve edited weights and points while expanding the legacy criterion.
    for cr in merged['products']['glynac']['criteria']:
        if cr['key'] == 'black_diamond':
            if cr.get('label') == 'Portfolio platform is Black Diamond':
                cr['label'] = 'Black Diamond, Salesforce or Redtail'
            for lv in cr.get('levels', []):
                if lv[1] == 'Orion, Tamarac, Addepar or Advyzon':
                    lv[1] = 'No confirmed supported portfolio or CRM system'
        if cr['key'] == 'm365' and cr.get('label') == 'Email runs on Microsoft 365':
            cr['label'] = 'Microsoft 365 or Dynamics'
    return merged


def cfg() -> dict:
    """The rules in force. Re-checks the database at most every few seconds, so
    a change saved on the Scoring screen reaches every process (both web
    workers and the background jobs) without a restart."""
    global _CFG
    import time as _t
    if _CFG is not None and _t.monotonic() - _CFG_STATE["t"] < CFG_CHECK_S:
        return _CFG
    over, fp = _overrides()
    _CFG_STATE["t"] = _t.monotonic()
    if _CFG is not None and fp == _CFG_STATE["fp"]:
        return _CFG
    merged = _merge(base_cfg(), over)
    try:
        _validate(merged)
    except ValueError:
        # A stored edit that no longer validates (say, a criterion removed
        # from the code) must not take every list down: fall back to the
        # shipped rules, which always validate.
        merged = base_cfg()
        _validate(merged)
    _CFG = merged
    _CFG_STATE["fp"] = fp
    return _CFG


def reload() -> None:
    _CFG_STATE["t"] = 0.0


def _validate(c: dict) -> None:
    """Fail loudly on a config that cannot mean what it says."""
    for key, p in c["products"].items():
        crits = [cr for cr in p["criteria"] if float(cr.get("weight", 0)) > 0]
        w = round(sum(float(cr["weight"]) for cr in crits), 6)
        if abs(w - 100) > 0.01:
            raise ValueError(f"{p.get('name', key)}: weights add to {w:g}, not 100")
        seen = set()
        for cr in p["criteria"]:
            if cr["key"] in seen:
                raise ValueError(f"{p.get('name', key)}: two factors share the key {cr['key']}")
            seen.add(cr["key"])
            point_values = [lv[0] for lv in cr.get("levels", [])]
            point_values += [b[1] for b in cr.get("bands", [])]
            if any(not math.isfinite(float(v)) or not 0 <= float(v) <= 100 for v in point_values):
                raise ValueError(f"{p.get('name', key)}: {cr['label']} points must be between 0 and 100")
            kind = cr.get("kind")
            if kind == "field":
                if cr.get("field") not in FIELDS:
                    raise ValueError(f"{p.get('name', key)}: unknown data field {cr.get('field')}")
                if not cr.get("bands"):
                    raise ValueError(f"{p.get('name', key)}: {cr['label']} needs bands")
            elif kind == "flag":
                if cr.get("field") not in FLAGS and not str(cr.get("field", "")).startswith(
                        ("tag:", "web:")):
                    raise ValueError(f"{p.get('name', key)}: unknown yes/no field {cr.get('field')}")
            elif cr["key"] not in CRITERIA:
                raise ValueError(f"{p.get('name', key)}: {cr['key']} has no evaluator")
        for g in p.get("gates", []) + p.get("disqualifiers", []):
            if g["key"] not in GATES:
                raise ValueError(f"{p.get('name', key)}: gate {g['key']} has no evaluator")
        for g in p.get("gates", []):
            if g["key"] == "firm_type":
                raise ValueError(f"{p.get('name', key)}: the firm-type rule is a disqualifier, "
                                 f"not a gate")
        for g in p.get("disqualifiers", []):
            if g["key"] == "firm_type":
                _check_firm_type_rule(p.get("name", key), g)


def save_product(key: str, body: dict, who: str, note: str = "") -> None:
    """Store an edited product definition after validating it in context."""
    from . import db
    if key != "_global" and key not in base_cfg()["products"]:
        raise ValueError("unknown product")
    over, _ = _overrides()
    over[key] = body
    _validate(_merge(base_cfg(), over))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    c = db.connect()
    try:
        c.executescript(CONFIG_SCHEMA)
        text = json.dumps(body, separators=(",", ":"))
        c.execute("INSERT INTO scoring_config (product, body, updated_by, updated_at)"
                  " VALUES (?,?,?,?) ON CONFLICT(product) DO UPDATE SET body=excluded.body,"
                  " updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                  (key, text, who or None, now))
        c.execute("INSERT INTO scoring_history (product, body, note, updated_by, updated_at)"
                  " VALUES (?,?,?,?,?)", (key, text, note or None, who or None, now))
        c.commit()
    finally:
        c.close()
    reload()
    request_rescore()


def reset_product(key: str, who: str) -> None:
    """Back to the shipped defaults for one product (history keeps the old)."""
    from . import db
    c = db.connect()
    try:
        c.executescript(CONFIG_SCHEMA)
        c.execute("DELETE FROM scoring_config WHERE product=?", (key,))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        c.execute("INSERT INTO scoring_history (product, body, note, updated_by, updated_at)"
                  " VALUES (?,?,?,?,?)", (key, "{}", "reset to defaults", who or None, now))
        c.commit()
    finally:
        c.close()
    reload()
    request_rescore()


def request_rescore(refresh_all: bool = False, conn=None) -> bool:
    """Rescore everything: queue the Scores job (scripts.score_products), which
    rescans every firm in firm_current against every product, not only the
    firms already on some list. With refresh_all, every firm's website, email
    platform and email checks are queued for a refresh first.

    Called after every scoring save and reset, by the Rescore all firms
    buttons and after a reclassification changes any firm's type. Only flags
    the job, so it is safe inside a request: the background worker picks it
    up within a minute (the caller in the web app may also call
    ensure_autopilot to start a stopped worker). Returns False when the job
    table is not there yet."""
    from . import db, jobs
    c = conn or db.connect()
    try:
        if refresh_all:
            jobs.request_full_refresh(c)
        jobs.request_run(c, "rescore")
        return True
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        return False
    finally:
        if conn is None:
            c.close()


# ------------------------------------------------------- firm-type rules
# Each product's firm_type disqualifier lists the firm types it sells to.
# The scoring editor reads firm_type_rule() and writes with set_firm_types()
# or, inside its own save handler, apply_firm_types().

def _check_firm_type_rule(name: str, g: dict) -> None:
    from . import firmtype
    allow = g.get("allow")
    if not isinstance(allow, list) or not allow:
        raise ValueError(f"{name}: choose at least one firm type the product sells to")
    bad = [c for c in allow if c not in firmtype.KEYS or c == "unknown"]
    if bad:
        raise ValueError(f"{name}: unknown firm type {bad[0]}")
    try:
        mc = float(g.get("min_confidence", 60))
    except (TypeError, ValueError):
        raise ValueError(f"{name}: firm-type confidence must be a number") from None
    if not 0 <= mc <= 100:
        raise ValueError(f"{name}: firm-type confidence must be between 0 and 100")


def firm_type_rule(key: str) -> dict | None:
    """A product's firm-type rule, for the scoring editor:
    {index, label, allow: [keys], min_confidence, off, categories: [{key, label,
    short, description, treatment, core, allowed}]}. None if the product has
    no such rule. 'unknown' is not offered: it can never disqualify."""
    from . import firmtype
    for i, g in enumerate(product(key).get("disqualifiers", [])):
        if g["key"] != "firm_type":
            continue
        allow = list(g.get("allow") or [])
        return {"index": i, "label": g.get("label", ""), "allow": allow,
                "min_confidence": int(float(g.get("min_confidence", 60))),
                "off": bool(g.get("off")),
                "categories": [dict(c, allowed=c["key"] in allow)
                               for c in firmtype.categories() if c["key"] != "unknown"]}
    return None


def apply_firm_types(body: dict, allow: list[str], min_confidence=None) -> dict:
    """Write the allowed firm types (and optionally the confidence needed to
    disqualify) into a product body in place, adding the rule if the body
    lacks it; validated. For the scoring editor's save handler, before
    save_product."""
    allow = [a for a in dict.fromkeys(allow or []) if a]
    rule = next((g for g in body.get("disqualifiers") or [] if g["key"] == "firm_type"), None)
    if rule is None:
        rule = {"key": "firm_type", "label": "Not a firm type this product sells to",
                "min_confidence": 60}
        body.setdefault("disqualifiers", []).insert(0, rule)
    rule["allow"] = allow
    if min_confidence not in (None, ""):
        try:
            mc = float(min_confidence)
        except (TypeError, ValueError):
            raise ValueError("Firm-type confidence must be a number") from None
        rule["min_confidence"] = int(mc) if mc == int(mc) else mc
    _check_firm_type_rule(body.get("name", "This product"), rule)
    return body


def set_firm_types(key: str, allow: list[str], who: str, min_confidence=None,
                   note: str = "") -> None:
    """Save which firm types a product sells to (validated, kept in history,
    and every firm rescored)."""
    body = json.loads(json.dumps(product(key)))
    apply_firm_types(body, allow, min_confidence)
    save_product(key, body, who, note or "firm types changed")


def history(key: str, limit: int = 12) -> list[dict]:
    from . import db
    try:
        c = db.connect()
        try:
            return [dict(r) for r in c.execute(
                "SELECT id, note, updated_by, updated_at FROM scoring_history"
                " WHERE product=? ORDER BY id DESC LIMIT ?", (key, limit))]
        finally:
            c.close()
    except Exception:
        return []


def edited(key: str) -> dict | None:
    """Who last changed a product's scoring, if anyone has."""
    from . import db
    try:
        c = db.connect()
        try:
            r = c.execute("SELECT updated_by, updated_at FROM scoring_config"
                          " WHERE product=?", (key,)).fetchone()
        finally:
            c.close()
    except Exception:
        return None
    return dict(r) if r else None


def product_keys() -> list[str]:
    return list(cfg()["order"])


def product(key: str) -> dict:
    return cfg()["products"][key]


def stamp() -> str:
    fp = _CFG_STATE.get("fp")
    edit = "" if not fp else "+edited:" + hashlib_short(fp)
    return f"{config.load().stamp}|products.v{cfg()['config_version']}{edit}"


def hashlib_short(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def level_label(crit: dict, points: float) -> str:
    """The config's description of the level a points value sits at."""
    for pts, label in sorted(crit.get("levels", []), key=lambda lv: -float(lv[0])):
        if points >= pts:
            return label
    lv = crit.get("levels") or [[0, ""]]
    return lv[-1][1]


@lru_cache(maxsize=1)
def _base_levels() -> dict:
    return {(key, c["key"]): tuple(float(lv[0]) for lv in c.get("levels", []))
            for key, p in base_cfg()["products"].items() for c in p["criteria"]}


def level_inputs(key: str, criterion: dict) -> list[float]:
    """Stable evaluator levels, independent of an admin's edited point values."""
    return list(criterion.get("level_inputs") or
                _base_levels().get((key, criterion["key"]), ()))


def configured_points(key: str, criterion: dict, points: float) -> float:
    """Apply edited levels to built-in evaluators, including continuous scores.

    Numeric bands and yes/no factors already read their configured points.
    Built-in evaluators return their original scale; piecewise interpolation
    preserves that scale by default and honours edits without changing evidence.
    """
    if criterion.get("bands") or criterion.get("kind") in ("field", "flag"):
        return points
    inputs = level_inputs(key, criterion)
    levels = criterion.get("levels") or []
    if not inputs or len(inputs) != len(levels):
        return points
    scale = dict(zip(inputs, (float(lv[0]) for lv in levels)))
    scale.setdefault(0.0, 0.0)
    scale.setdefault(100.0, 100.0)
    ordered = sorted(scale)
    for lo, hi in zip(ordered, ordered[1:]):
        if lo <= points <= hi:
            return scale[lo] + (scale[hi] - scale[lo]) * (points - lo) / (hi - lo)
    return points


def band(value, bands, default=0.0) -> float:
    """[threshold, points] descending; the first threshold at or below wins."""
    if value is None:
        return default
    for threshold, pts in bands:
        if value >= threshold:
            return float(pts)
    return default


# ---------------------------------------------------------------- features

TAG_LABELS: dict[str, str] = {}
TAG_PRODUCTS: dict[str, str] = {}
TICKER_PRODUCT: dict[str, str] = {}


def _load_vocab() -> None:
    if TAG_LABELS:
        return
    t = yaml.safe_load((config.CONFIG_DIR / "brochure_tags.yml").read_text(
        encoding="utf-8"))
    TAG_LABELS.update(t.get("labels", {}))
    TAG_PRODUCTS.update(t.get("products", {}))
    s = yaml.safe_load((config.CONFIG_DIR / "target_securities.yml").read_text(
        encoding="utf-8"))
    for tk, v in (s.get("securities") or {}).items():
        TICKER_PRODUCT[tk] = v.get("product")


def tag_label(tag: str) -> str:
    _load_vocab()
    return TAG_LABELS.get(tag, tag.replace("_", " ").capitalize())


def _safe(conn, sql: str, args=()) -> list:
    """A query over a table a fresh install may not have yet. Missing input is
    treated as no evidence, never as a crash: the score then says 'not found'."""
    try:
        return conn.execute(sql, args).fetchall()
    except Exception:
        conn.rollback()
        return []


def load_features(conn, crds: list[str] | None = None) -> dict[str, dict]:
    """Everything the scores read, per firm. `crds` limits it to some firms
    (the firm page and a manual override use one); None reads every firm."""
    _load_vocab()
    one = crds is not None
    ph = ",".join("?" * len(crds)) if one else ""
    only = (lambda col: f" AND {col} IN ({ph})") if one else (lambda col: "")
    a = tuple(crds) if one else ()

    F: dict[str, dict] = {}
    for r in _safe(conn, f"""SELECT crd, legal_name, regulator, is_era, raum,
            raum_disc, hnw_aum, hnw_clients, retail_aum, retail_clients,
            clients_total, iar_count, total_employees, q7b, state, country,
            registered_date, website, disciplinary
            FROM firm_current WHERE 1=1{only('crd')}""", a):
        d = dict(r)
        d.update(tags={}, funds=None, seg=None, cust=None, h13f={}, files_13f=False,
                 officers=[], owners=[], triggers=[], filings_12m=0, mail=None, web={},
                 status=None, overrides={}, extra=None, brochure=None, people=None,
                 reach={"personal": 0, "verified": 0}, web_scanned=False,
                 firm_class=None)
        F[d["crd"]] = d
    if not F:
        return F

    def each(sql, fn):
        for r in _safe(conn, sql, a):
            d = F.get(r["crd"])
            if d is not None:
                fn(d, r)

    each(f"SELECT * FROM firm_adv_extra WHERE 1=1{only('crd')}",
         lambda d, r: d.__setitem__("extra", dict(r)))
    each(f"""SELECT crd, status, date_submitted, tag_version FROM brochure
             WHERE 1=1{only('crd')}""",
         lambda d, r: d.__setitem__("brochure", dict(r)))
    each(f"""SELECT crd, tag, present, confidence, hits, best_phrase, best_snippet,
             section_item, distinct_phrases FROM brochure_tag WHERE 1=1{only('crd')}""",
         lambda d, r: d["tags"].__setitem__(r["tag"], dict(r)))
    each(f"""
        WITH latest AS (SELECT s.crd AS crd, MAX(fc.filing_date) d FROM sched_d_7b1 s
                        JOIN filing_crd fc ON fc.filing_id=s.filing_id
                        WHERE s.crd IS NOT NULL{only('s.crd')} GROUP BY s.crd)
        SELECT s.crd, l.d AS as_of, COUNT(DISTINCT s.fund_id) n,
               MAX(COALESCE(s.minimum_investment,0)) maxmin,
               SUM(COALESCE(s.gross_asset_value,0)) gav,
               STRING_AGG(DISTINCT s.fund_type, '|') types,
               MAX(CASE WHEN s.raw_json::json->>'Prime Brokers'='Y' THEN 1 ELSE 0 END) pb,
               MAX(CASE WHEN s.raw_json::json->>'Administrator'='Y' THEN 1 ELSE 0 END) adm,
               MAX(CASE WHEN s.raw_json::json->>'Annual Audit'='Y' THEN 1 ELSE 0 END) aud
        FROM sched_d_7b1 s
        JOIN filing_crd fc ON fc.filing_id=s.filing_id
        JOIN latest l ON l.crd=s.crd AND l.d=fc.filing_date
        GROUP BY s.crd, l.d""",
         lambda d, r: d.__setitem__("funds", dict(r)))
    each(f"SELECT * FROM re_segment WHERE 1=1{only('crd')}",
         lambda d, r: d.__setitem__("seg", dict(r)))
    each(f"SELECT * FROM firm_custodian_profile WHERE 1=1{only('crd')}",
         lambda d, r: d.__setitem__("cust", dict(r)))

    def hold(d, r):
        prev = d["h13f"].get(r["ticker"])
        if prev is None or r["quarter"] > prev["quarter"]:
            d["h13f"][r["ticker"]] = {"quarter": r["quarter"], "value": r["v"] or 0,
                                     "shares": r["sh"] or 0}
    each(f"""SELECT crd, ticker, quarter, MAX(value_usd) v, MAX(shares) sh
             FROM holding_13f WHERE crd IS NOT NULL AND ticker IS NOT NULL{only('crd')}
             GROUP BY crd, ticker, quarter""", hold)
    each(f"""SELECT DISTINCT crd FROM adv_13f_match
             WHERE status IN ('auto','confirmed'){only('crd')}""",
         lambda d, r: d.__setitem__("files_13f", True))
    each(f"""SELECT crd, name, title, control_person, as_of FROM schedule_a
             WHERE is_individual=1{only('crd')}""",
         lambda d, r: d["officers"].append(dict(r)))
    each(f"""SELECT crd, name FROM schedule_a WHERE is_individual=0
             AND ownership_code IN ('D','E'){only('crd')}""",
         lambda d, r: d["owners"].append(r["name"]))
    each(f"""SELECT crd, trigger_type, detected_date, description FROM trigger_event
             WHERE suppressed=0{only('crd')}""",
         lambda d, r: d["triggers"].append(dict(r)))
    cutoff = date.fromordinal(date.today().toordinal() - 365).isoformat()
    for r in _safe(conn, f"""SELECT crd, COUNT(DISTINCT filing_date) n FROM firm_history
            WHERE filing_date >= ?{only('crd')} GROUP BY crd""", (cutoff,) + a):
        if r["crd"] in F:
            F[r["crd"]]["filings_12m"] = r["n"]
    each(f"SELECT * FROM firm_mail_platform WHERE 1=1{only('crd')}",
         lambda d, r: d.__setitem__("mail", dict(r)))
    each(f"SELECT crd, signal, evidence, url FROM web_signal WHERE 1=1{only('crd')}",
         lambda d, r: d["web"].__setitem__(r["signal"], dict(r)))
    each(f"SELECT crd, status, owner FROM firm_status WHERE 1=1{only('crd')}",
         lambda d, r: d.__setitem__("status", dict(r)))
    each(f"""SELECT crd, product, criterion, points, note, set_by, set_at
             FROM score_override WHERE 1=1{only('crd')}""",
         lambda d, r: d["overrides"].__setitem__((r["product"], r["criterion"]),
                                                 dict(r)))
    each(f"SELECT * FROM firm_people_stats WHERE 1=1{only('crd')}",
         lambda d, r: d.__setitem__("people", dict(r)))
    each(f"""SELECT crd,
               COUNT(DISTINCT person_key) FILTER (WHERE person_key != '' AND is_role=0
                     AND verify_status NOT IN ('invalid','no_mail_server')
                     AND (confidence >= 60 OR verify_status='valid')) AS personal,
               COUNT(*) FILTER (WHERE verify_status='valid') AS verified
             FROM usable_contact_point WHERE kind='email'{only('crd')} GROUP BY crd""",
         lambda d, r: d.__setitem__("reach", {"personal": r["personal"] or 0,
                                              "verified": r["verified"] or 0}))
    each(f"SELECT crd FROM web_enrich_state WHERE status='ok'{only('crd')}",
         lambda d, r: d.__setitem__("web_scanned", True))

    def firm_class(d, r):
        try:
            ev = json.loads(r["evidence"] or "[]")
        except ValueError:
            ev = []
        d["firm_class"] = {"category": r["category"], "confidence": r["confidence"],
                           "source": r["source"], "evidence": ev}
    each(f"""SELECT crd, category, confidence, source, evidence FROM firm_class
             WHERE 1=1{only('crd')}""", firm_class)
    return F


# ------------------------------------------------------------ helpers

def _pct(v) -> str:
    return f"{v * 100:.0f}%"


def _money(v) -> str:
    if v is None:
        return "-"
    v = float(v)
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.0f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:,.0f}"


def _nice(s: str) -> str:
    from .names import nice_name
    return nice_name(s)


def has(d, tag) -> bool:
    t = d["tags"].get(tag)
    return bool(t and t["present"])


def said(d, tag) -> str:
    """The firm's own sentence for a tag, for evidence strings."""
    t = d["tags"].get(tag) or {}
    s = (t.get("best_snippet") or "").strip()
    return f'brochure: "{s}"' if s else f"brochure mentions {tag_label(tag).lower()}"


def brochure_read(d) -> bool:
    b = d["brochure"]
    return bool(b and b["status"] == "ok")


def not_found(d, what: str) -> str:
    if brochure_read(d):
        return f"No {what} in the brochure or filings"
    return f"No {what} in filings; brochure not read yet"


def unknown(pts, ev: str):
    """A level that rests on missing data rather than on a finding. The score
    counts it as zero and the screen shows it as missing, so a firm never
    looks strong because something about it is unknown."""
    return pts, ev, False


def negative(d, pts, what: str):
    """Nothing found. That is a finding once the brochure has been read, and
    missing data until then."""
    if brochure_read(d):
        return pts, not_found(d, what)
    return unknown(pts, not_found(d, what))


def hnw_share(d) -> float:
    return (d["hnw_aum"] or 0) / d["raum"] if d["raum"] else 0.0


def avg_hnw(d) -> float:
    return (d["hnw_aum"] or 0) / d["hnw_clients"] if d["hnw_clients"] else 0.0


def fund_count(d) -> int:
    """Private funds advised now. The current feed's Item 7.B answer is the
    authority; the archive count (to 2024) says how many when it agrees."""
    n = (d["funds"] or {}).get("n") or 0
    if d["q7b"] == "N":
        return 0
    if d["q7b"] == "Y":
        return max(n, 1)
    return n


def advises_private_funds(d) -> bool:
    return fund_count(d) > 0


def recency(detected: str) -> float:
    try:
        age = (date.today() - date.fromisoformat(detected[:10])).days
    except (TypeError, ValueError):
        return 0.02
    return max(0.02, 0.5 ** (max(age, 0) / 365.0))


_TRIGGER_PRODUCTS: dict = {}


def trigger_products() -> dict:
    """Which product each trigger type is a lead (or disqualifier) for."""
    if not _TRIGGER_PRODUCTS:
        s = yaml.safe_load((config.CONFIG_DIR / "scoring.yml").read_text(
            encoding="utf-8"))
        _TRIGGER_PRODUCTS.update(s.get("trigger_products") or {})
    return _TRIGGER_PRODUCTS


def lead_triggers(d, family: str) -> list[dict]:
    """Lead triggers relevant to a product family, freshest first."""
    TRIGGER_PRODUCTS = trigger_products()
    out = []
    for t in d["triggers"]:
        meta = TRIGGER_PRODUCTS.get(t["trigger_type"], {})
        if meta.get("kind") != "lead":
            continue
        if meta.get("product") not in ("BOTH", family.upper()) and family != "any":
            continue
        out.append(t)
    out.sort(key=lambda t: t["detected_date"], reverse=True)
    return out


CCO_RE = re.compile(r"CHIEF COMPLIANCE|\bCCO\b", re.I)
OTHER_ROLE_RE = re.compile(r"PRESIDENT|\bCEO\b|CHIEF EXECUTIVE|MANAGING|PRINCIPAL|OWNER"
                           r"|PARTNER|CHIEF OPERATING|\bCOO\b|\bCFO\b|CHIEF FINANCIAL"
                           r"|GENERAL COUNSEL|CHIEF INVESTMENT|\bCIO\b|FOUNDER"
                           r"|MEMBER|DIRECTOR|TREASURER|SECRETARY", re.I)
CIO_RE = re.compile(r"CHIEF INVESTMENT|\bCIO\b", re.I)

PLATFORMS = {"platform_black_diamond": "Black Diamond", "platform_orion": "Orion",
             "platform_tamarac": "Tamarac", "platform_addepar": "Addepar",
             "platform_advyzon": "Advyzon", "platform_salesforce": "Salesforce",
             "platform_redtail": "Redtail",
             # Read as soon as the brochure vocabulary or the website patterns
             # emit them; until then they are simply never found.
             "platform_dynamics": "Microsoft Dynamics", "platform_practifi": "Practifi",
             "platform_xlr8": "XLR8", "platform_salentica": "Salentica"}

# The four systems Glynac works with, and the products that count as each:
# Practifi, XLR8 and Salentica are CRMs built on Salesforce.
GLYNAC_SYSTEMS = {"Microsoft": ("Microsoft Dynamics",),
                  "Salesforce": ("Salesforce", "Practifi", "XLR8", "Salentica"),
                  "Redtail": ("Redtail",), "Black Diamond": ("Black Diamond",)}
OTHER_PLATFORMS = ("Orion", "Tamarac", "Addepar", "Advyzon")


def platform_evidence(d) -> dict[str, str]:
    """Reporting platform -> where it was seen (brochure or website)."""
    out = {}
    for sig, name in PLATFORMS.items():
        if has(d, sig):
            out[name] = said(d, sig)
        elif sig in d["web"]:
            out[name] = "website: " + (d["web"][sig]["evidence"] or "client login link")
    return out


def glynac_systems(d) -> dict[str, list[str]]:
    """Which of the four systems Glynac works with a firm is seen to use, each
    with what was found and where: Microsoft 365 from the public mail records,
    the rest from the brochure or the firm's website. A system not found is
    absent from the result, which means unknown, not 'does not use'."""
    out: dict[str, list[str]] = {}
    m = d.get("mail") or {}
    if m.get("platform") == "m365":
        out["Microsoft"] = [f"Microsoft 365 email ({m.get('evidence') or m.get('domain') or 'mail records'})"]
    plats = platform_evidence(d)
    for system, names in GLYNAC_SYSTEMS.items():
        for n in names:
            if n in plats:
                out.setdefault(system, []).append(f"{n} ({plats[n]})")
    return out


def status_level(d) -> str | None:
    s = (d["status"] or {}).get("status")
    return s


# ------------------------------------------------------------------ gates
# Each returns (passed, evidence). A disqualifier returns (triggered, evidence).

# A controlling owner (50%+ on Schedule A) that is a bank, insurer or
# wirehouse makes a firm captive rather than independent. Item 7.A alone is too
# blunt for this: a related insurance AGENCY is ordinary for a planner, and
# counting it would call five thousand independent firms captive.
INSTITUTION_RE = re.compile(
    r"\bBANK\b|BANCORP|BANCSHARES|INSURANCE|ASSURANCE|\bLIFE\b|MASSMUTUAL"
    r"|MORGAN STANLEY|MERRILL|\bUBS\b|WELLS FARGO|RAYMOND JAMES|AMERIPRISE"
    r"|EDWARD JONES|\bBAIRD\b|JPMORGAN|GOLDMAN SACHS|TRUST COMPANY", re.I)


def captive_reason(d) -> str | None:
    if (d["extra"] or {}).get("rel_bank"):
        return "Has a related bank (Item 7.A)"
    for name in d["owners"]:
        if INSTITUTION_RE.search(name or ""):
            return f"Controlled by {name.title()} (Schedule A)"
    return None


def g_sec_registered(d, g, key):
    if d["is_era"]:
        return False, "Exempt reporting adviser"
    if d["regulator"] != "SEC":
        return False, "State-registered, not SEC-registered"
    if (d["country"] or "United States") != "United States":
        return False, f"Based in {d['country']}"
    if key.startswith("phh"):
        why = captive_reason(d)
        if why:
            return False, why
    return True, "SEC-registered adviser"


def g_raum_range(d, g, key):
    r = d["raum"] or 0
    lo, hi = g.get("min", 0), g.get("max")
    ok = r >= lo and (hi is None or r < hi)
    return ok, f"{_money(r)} in assets"


def g_min_raum(d, g, key):
    r = d["raum"] or 0
    return r >= g["min"], f"{_money(r)} in assets"


def g_individual_share(d, g, key):
    if not d["raum"]:
        return False, "No regulatory assets reported"
    s = ((d["hnw_aum"] or 0) + (d["retail_aum"] or 0)) / d["raum"]
    return s >= g["min"], f"Individuals hold {_pct(s)} of assets"


def g_independent(d, g, key):
    why = captive_reason(d)
    if why:
        return False, why
    return True, "No bank, insurer or wirehouse control on file"


def g_supported_system(d, g, key):
    sy = glynac_systems(d)
    if sy:
        return True, "Compatible: " + "; ".join(v[0] for v in sy.values())
    # A portfolio or email vendor does not establish which CRM a firm uses.
    # Unknown compatibility stays eligible, with the missing factors worth zero.
    return True, ("Compatibility not confirmed yet; Glynac works with Microsoft 365 and "
                  "Dynamics, Salesforce (including Practifi and XLR8), Redtail and Black Diamond")


def g_no_private_funds(d, g, key):
    n = fund_count(d)
    return n == 0, ("Advises no private funds" if n == 0
                    else f"Advises {n} private fund{'s' if n > 1 else ''}")


def g_partner_type(d, g, key):
    if d["is_era"]:
        return False, "Exempt reporting adviser"
    for t in ("exchange_1031", "owner_clients", "real_estate_direct"):
        if has(d, t):
            return True, said(d, t)
    return False, not_found(d, "business or property owner clients")


def g_investment_property(d, g, key):
    for t in ("exchange_1031", "real_estate_direct", "private_real_estate",
              "net_lease_types"):
        if has(d, t):
            return True, said(d, t)
    oc = d["tags"].get("owner_clients") or {}
    if oc.get("present") and re.search(r"real estate|property",
                                       oc.get("best_phrase") or "", re.I):
        return True, said(d, "owner_clients")
    return False, "No investment real estate language in the brochure"


def g_family_office(d, g, key):
    if d["is_era"]:
        return False, "Exempt reporting adviser"
    if not d["clients_total"] or not d["raum"]:
        return False, "No client count reported"
    avg = d["raum"] / d["clients_total"]
    if avg < g["min_avg_client"]:
        return False, f"Average client {_money(avg)}"
    if hnw_share(d) < 0.5:
        return False, f"Average client {_money(avg)}, but HNW is only {_pct(hnw_share(d))} of assets"
    return True, f"Average client {_money(avg)}; HNW is {_pct(hnw_share(d))} of assets"


def g_check_capacity(d, g, key):
    return (d["raum"] or 0) >= g["min"], f"{_money(d['raum'])} in assets"


def g_bans_illiquid(d, g, key):
    for t in ("private_markets", "alternatives"):
        tg = d["tags"].get(t)
        if tg and not tg["present"]:
            return True, "Brochure states they do not use " + tag_label(t).lower()
    return False, ""


def g_re_competitor(d, g, key):
    s = d["seg"]
    if s and s["segment"] == "competitor":
        return True, (f"Real estate fund of {_money(s['total_gav'])} is "
                      f"{_pct(s['raum_ratio'] or 0)} of assets")
    return False, ""


def g_left_schwab(d, g, key):
    moves = [t for t in d["triggers"] if t["trigger_type"].startswith("custodian_change")]
    if not moves:
        return False, ""
    last = max(moves, key=lambda t: t["detected_date"])
    if last["trigger_type"] == "custodian_change_from_platform":
        return True, f"Left Schwab ({last['detected_date']})"
    return False, ""


def g_firm_type(d, g, key):
    """Disqualifier: the firm's type is not one this product sells to. A firm
    not classified yet, typed 'unknown', or classified below min_confidence by
    the rules or AI is never removed (missing data never disqualifies); a type
    set by hand always counts."""
    fc = d.get("firm_class")
    allow = g.get("allow") or []
    if not fc or not allow:
        return False, "Firm type not classified yet"
    cat = fc.get("category")
    from . import firmtype
    if cat in allow or cat == "unknown":
        return False, f"{firmtype.label(cat)}"
    manual = fc.get("source") == "manual"
    conf = int(fc.get("confidence") or 0)
    if not manual and conf < float(g.get("min_confidence", 60)):
        return False, f"Possibly {firmtype.label(cat).lower()} ({conf}%), too unsure to remove"
    why = (fc.get("evidence") or [""])[0]
    how = "set by hand" if manual else f"{conf}% confident"
    return True, f"{firmtype.label(cat)} ({how})" + (f": {why}" if why else "")


GATES = {
    "firm_type": g_firm_type,
    "sec_registered": g_sec_registered, "raum_range": g_raum_range,
    "min_raum": g_min_raum, "individual_share": g_individual_share,
    "independent": g_independent, "supported_system": g_supported_system,
    "no_private_funds": g_no_private_funds, "partner_type": g_partner_type,
    "investment_property": g_investment_property, "family_office": g_family_office,
    "check_capacity": g_check_capacity, "bans_illiquid": g_bans_illiquid,
    "re_competitor": g_re_competitor, "left_schwab": g_left_schwab,
}


# --------------------------------------------------------------- criteria
# Each returns (points 0-100, evidence).

def c_hnw_fit(d, c, key):
    if not d["raum"] or d["hnw_aum"] is None:
        return unknown(0, "Client mix not reported")
    s, a = hnw_share(d), avg_hnw(d)
    ev = f"HNW is {_pct(s)} of assets, {_money(a)} per HNW client"
    if s >= 0.60 and a >= 2e6:
        return 100, ev
    if s >= 0.40:
        return 75, ev
    if s >= 0.20:
        return 50, ev
    return 25, ev


def c_alts_use(d, c, key):
    n = fund_count(d)
    if n >= 2:
        return 100, f"Advises {n} private funds (Schedule D)"
    if n == 1:
        return 75, "Advises a private fund (Item 7.B)"
    if has(d, "private_markets"):
        return 75, said(d, "private_markets")
    t = d["tags"].get("alternatives")
    if t and t["present"]:
        return (45 if (t["hits"] or 0) >= 2 else 20), said(d, "alternatives")
    return negative(d, 0, "alternatives")


def c_central_process(d, c, key):
    cio = next((o for o in d["officers"] if CIO_RE.search(o["title"] or "")), None)
    ic = has(d, "investment_committee") or cio is not None
    ic_ev = said(d, "investment_committee") if has(d, "investment_committee") else (
        f"Chief investment officer on file ({_nice(cio['title'])})" if cio else "")
    if has(d, "model_portfolios") and ic:
        return 100, said(d, "model_portfolios")
    if has(d, "approved_managers") and ic:
        return 80, said(d, "approved_managers")
    if ic:
        return 55, ic_ev
    if has(d, "model_portfolios"):
        return 55, said(d, "model_portfolios")
    return negative(d, 25, "central investment process")


def c_portfolio_need(d, c, key):
    if has(d, "real_assets_income") and (has(d, "tax_management")
                                         or has(d, "private_markets")):
        return 80, said(d, "real_assets_income")
    if has(d, "real_assets_income"):
        return 55, said(d, "real_assets_income")
    return negative(d, 25, "stated need for income or real assets")


def phh_13f(d) -> tuple[int, float, list[str]]:
    names = [tk for tk in d["h13f"] if TICKER_PRODUCT.get(tk) == "phh"]
    top = max((d["h13f"][tk]["value"] for tk in names), default=0)
    return len(names), top, sorted(names)


def c_re_familiarity(d, c, key):
    s = d["seg"]
    if s and s["segment"] in ("prospect", "unraised", "ambiguous"):
        return 100, f"Advises a real estate fund of {_money(s['total_gav'])}"
    if has(d, "private_real_estate"):
        return 100, said(d, "private_real_estate")
    n, top, names = phh_13f(d)
    if n >= 3 or top >= 500_000:
        return 60, f"13F holds {', '.join(names)} (largest {_money(top)})"
    if n:
        return 30, f"13F holds {', '.join(names)}"
    if has(d, "real_estate_direct"):
        return 30, said(d, "real_estate_direct")
    return negative(d, 0, "real estate")


def c_ops_fit(d, c, key):
    majors = set(cfg()["major_custodians"])
    cust = (d["cust"] or {}).get("primary_canonical")
    private = advises_private_funds(d) or has(d, "private_markets")
    if cust in majors and private:
        return 100, f"Custodies at {cust} and already uses private investments"
    if cust in majors:
        return 60, f"Custodies at {cust}"
    if cust:
        return 30, f"Custodian {cust}; K-1 and private fund support unknown"
    return unknown(30, "Custodian not reported")


def _status_points(d, mapping: dict, default=None):
    s = status_level(d)
    if s in mapping:
        return mapping[s], f"Status in Bellwether: {s}"
    return default


def c_relationship(d, c, key):
    hit = _status_points(d, {"customer": 100, "qualified": 70, "meeting set": 70})
    if hit:
        return hit
    trig = lead_triggers(d, "phh")
    if not trig:
        return 0, "No recent trigger and no contact yet"
    recent = [t for t in trig if recency(t["detected_date"]) >= 0.25]
    base = 50 if len(recent) >= 2 else 20
    w = recency(trig[0]["detected_date"])
    return round(base * w, 1), (f"{trig[0]['description']} ({trig[0]['detected_date']})"
                                + (f", and {len(recent) - 1} more" if len(recent) > 1 else ""))


def c_repeat_potential(d, c, key):
    if d["iar_count"] is None and d["hnw_clients"] is None:
        return unknown(0, "Advisor and client counts not reported")
    iar, h = d["iar_count"] or 0, d["hnw_clients"] or 0
    ev = f"{iar:,} advisors, {h:,} HNW clients"
    if iar >= 10 or h >= 200:
        return 100, ev
    if iar >= 3 or h >= 50:
        return 40, ev
    return 0, ev


# 1031
def c_exchange_frequency(d, c, key):
    t = d["tags"].get("exchange_1031")
    if t and t["present"]:
        if (t["hits"] or 0) >= 5 or (t["distinct_phrases"] or 0) >= 3:
            return 60, said(d, "exchange_1031")
        return 28, said(d, "exchange_1031")
    if not brochure_read(d):
        return unknown(0, "Brochure not read yet")
    return 0, "No 1031 language in the brochure"


def c_owner_client_fit(d, c, key):
    if not d["raum"] or d["hnw_aum"] is None:
        return unknown(0, "Client mix not reported")
    s, a = hnw_share(d), avg_hnw(d)
    ev = f"HNW is {_pct(s)} of assets, {_money(a)} per HNW client"
    if a >= 5e6:
        return 100, ev
    if s >= 0.40:
        return 75, ev
    if s >= 0.20:
        return 50, ev
    if (d["hnw_clients"] or 0) > 0:
        return 25, ev
    return 0, ev


def c_intro_timing(d, c, key):
    if d["extra"] is None:
        return unknown(33, "Services (Item 5.G) not read yet")
    if d["extra"].get("svc_financial_planning"):
        return 67, "Offers financial planning (Item 5.G)"
    return 33, "No financial planning service reported"


def c_specialization(d, c, key):
    t = d["tags"].get("exchange_1031")
    if t and t["present"] and t["section_item"] in (4, 8):
        return 67, f"1031 work described in Item {t['section_item']} of the brochure"
    if not brochure_read(d):
        return unknown(33, "Brochure not read yet")
    return 33, "Generalist adviser"


def c_relationship_strength(d, c, key):
    hit = _status_points(d, {"customer": 100, "qualified": 60, "meeting set": 60,
                             "working": 30})
    return hit or (0, "No contact yet")


def c_referral_potential(d, c, key):
    tagged = has(d, "exchange_1031") or has(d, "owner_clients")
    if tagged and (d["hnw_clients"] or 0) >= 50:
        return 100, f"{d['hnw_clients']:,} HNW clients and owner or 1031 language"
    if tagged:
        return 50, said(d, "owner_clients" if has(d, "owner_clients") else "exchange_1031")
    if not brochure_read(d):
        return unknown(0, "Brochure not read yet")
    return 0, "No recurring owner client flow visible"


def _geo(d, hit_pts, miss_pts, unset_pts):
    focus = [s.upper() for s in cfg().get("phh_focus_states") or []]
    if not focus:
        return unknown(unset_pts, "PHH focus states not set in the scoring settings")
    if (d["state"] or "").upper() in focus:
        return hit_pts, f"Based in {d['state']}, a PHH focus state"
    return miss_pts, f"Based in {d['state']}, outside PHH focus states"


def c_overlap_1031(d, c, key):
    return _geo(d, 100, 0, 60)


# JV
def c_check_size(d, c, key):
    r = d["raum"] or 0
    avg = r / d["clients_total"] if d["clients_total"] else 0
    ev = f"{_money(r)} in assets, {_money(avg)} per client"
    if r >= 2e9 or avg >= 50e6:
        return 100, ev
    if r >= 5e8:
        return 75, ev
    return 40, ev


def c_direct_re(d, c, key):
    s = d["seg"]
    if s:
        return 100, f"Advises a real estate fund of {_money(s['total_gav'])}"
    if has(d, "private_real_estate"):
        return 67, said(d, "private_real_estate")
    if has(d, "real_estate_direct"):
        return 67, said(d, "real_estate_direct")
    n, _, names = phh_13f(d)
    if n:
        return 33, f"13F holds {', '.join(names)}"
    if has(d, "alternatives"):
        return 33, said(d, "alternatives")
    return negative(d, 0, "real estate")


def c_property_type(d, c, key):
    if has(d, "net_lease_types"):
        return 100, said(d, "net_lease_types")
    for t in ("private_real_estate", "real_estate_direct"):
        if has(d, t):
            return 33, said(d, t)
    return negative(d, 0, "property type")


def c_capability_gap(d, c, key):
    return unknown(33, "Not yet known; set it after the first conversation")


def c_geography_fit(d, c, key):
    return _geo(d, 100, 0, 50)


def c_governance_fit(d, c, key):
    return unknown(50, "Not yet known; set it after the first conversation")


def c_decision_speed(d, c, key):
    if advises_private_funds(d):
        return 100, "Already runs private funds, so it acts on live deals"
    return unknown(50, "Not yet known")


def c_jv_relationship(d, c, key):
    hit = _status_points(d, {"customer": 100, "qualified": 40, "meeting set": 40})
    return hit or (0, "No contact yet")


# AcuBooth
def c_hnw_assets(d, c, key):
    if d["hnw_aum"] is None:
        return unknown(0, "HNW assets not reported")
    return band(d["hnw_aum"] or 0, c["bands"]), f"{_money(d['hnw_aum'] or 0)} of HNW assets"


def c_hnw_clients(d, c, key):
    if d["hnw_clients"] is None:
        return unknown(0, "HNW clients not reported")
    return band(d["hnw_clients"] or 0, c["bands"]), f"{d['hnw_clients'] or 0:,} HNW clients"


def c_schwab_share(d, c, key):
    cp = d["cust"] or {}
    s = cp.get("schwab_share_reported")
    if s is None:
        return unknown(0, "No custodian reported, so the Schwab share is unknown")
    return round(s * 100, 1), (f"Schwab holds {_pct(s)} of reported custody "
                               f"(as of {cp.get('as_of_filing_date')}); positions for "
                               f"late 2026, not accounts sellable today")


def c_clients_per_advisor(d, c, key):
    iar = d["iar_count"] or 0
    if not iar:
        return unknown(20, "No advisors reported")
    cpr = (d["hnw_clients"] or 0) / iar
    per = avg_hnw(d)
    mult = band(per, c["damping"], default=0.25)
    pts = band(cpr, c["bands"]) * mult
    return round(pts, 1), (f"{cpr:.0f} HNW clients per advisor, {_money(per)} each"
                           + (f" (x{mult:.2f})" if mult < 1 else ""))


def c_advisors(d, c, key):
    if d["iar_count"] is None:
        return unknown(0, "Advisors not reported")
    return band(d["iar_count"] or 0, c["bands"]), f"{d['iar_count'] or 0:,} advisors"


# Glynac
def c_m365(d, c, key):
    """Microsoft: Microsoft 365 email (public mail records) or a Dynamics CRM
    (brochure or website). Email checked and elsewhere is a finding; email not
    checked yet, or no domain to check, is missing data."""
    sy = glynac_systems(d)
    if "Microsoft" in sy:
        return 100, "; ".join(sy["Microsoft"])
    m = d.get("mail")
    if not m:
        return unknown(0, "Email platform not checked yet; no Dynamics CRM seen")
    p, ev = m.get("platform"), m.get("evidence") or ""
    if p == "google":
        return 0, f"Email on Google Workspace ({ev}); no Dynamics CRM seen"
    if p == "other":
        return 0, f"Email not on Microsoft 365 ({ev}); no Dynamics CRM seen"
    if p == "none":
        return 0, ev or "The firm's domain receives no email"
    return unknown(0, ev or "Email provider not identifiable from the mail records")


def c_black_diamond(d, c, key):
    """Black Diamond, Salesforce (or a CRM built on it) or Redtail, seen in
    the brochure or on the website. Not seen is missing data, never a no:
    firms rarely name their CRM in public."""
    sy = glynac_systems(d)
    found = [line for s in ("Black Diamond", "Salesforce", "Redtail") for line in sy.get(s, [])]
    if found:
        return 100, "; ".join(found)
    others = [n for n in platform_evidence(d) if n in OTHER_PLATFORMS]
    return unknown(0, "Not confirmed yet: no Black Diamond, Salesforce (or Practifi, XLR8) or "
                      "Redtail in the brochure or on the website"
                   + (f"; reports on {', '.join(others)}" if others else ""))


GLYNAC_TRIGGER_POINTS = {"aum_jump": 80, "iar_growth": 65,
                         "custodian_change_to_platform": 65,
                         "custodian_change_other": 65,
                         "custodian_change_from_platform": 65,
                         "new_registration": 55}


def c_best_trigger(d, c, key):
    best, ev = 0.0, "No recent trigger"
    for t in d["triggers"]:
        base = GLYNAC_TRIGGER_POINTS.get(t["trigger_type"])
        if not base:
            continue
        v = base * recency(t["detected_date"])
        if v > best:
            best, ev = v, f"{t['description']} ({t['detected_date']})"
    return round(best, 1), ev


def c_advisor_count(d, c, key):
    n = d["iar_count"] or 0
    pts = 100 if n >= 25 else 70 if n >= 10 else 40 if n >= 5 else 10
    return pts, f"{n:,} advisors"


def c_marketing(d, c, key):
    x = d["extra"] or {}
    found = []
    if x.get("ad_testimonials") or x.get("ad_endorsements"):
        found.append("testimonials or endorsements")
    if x.get("ad_performance") or x.get("ad_hypothetical") or x.get("ad_predecessor"):
        found.append("performance in ads")
    if x.get("ad_ratings"):
        found.append("third-party ratings")
    if "publishes" in d["web"]:
        found.append("blog or newsletter")
    socials = json.loads(x.get("social_hosts") or "[]")
    if socials:
        found.append("social media (" + ", ".join(socials[:3]) + ")")
    if not found:
        if not x:
            return unknown(0, "Item 5.L not read yet")
        return 0, "No marketing flags in Item 5.L and no blog or social media found"
    return 20 * len(found), "Found: " + ", ".join(found)


def c_portfolio_complexity(d, c, key):
    pts, bits = 0, []
    if d["files_13f"]:
        pts += 40
        bits.append("files 13F")
    st = d["tags"].get("strategies") or {}
    if (st.get("distinct_phrases") or 0) >= 3:
        pts += 30
        bits.append(f"{st['distinct_phrases']} strategies in the brochure")
    if (d["filings_12m"] or 0) >= 2:
        pts += 30
        bits.append(f"ADV amended {d['filings_12m']} times in 12 months")
    return pts, ("; ".join(bits) if bits else "None found")


def c_compliance_owner(d, c, key):
    if not d["officers"]:
        return unknown(0, "No Schedule A roster on file")
    ccos = [o for o in d["officers"] if CCO_RE.search(o["title"] or "")]
    if not ccos:
        return 0, "No compliance officer named on Schedule A"
    o = ccos[0]
    stripped = CCO_RE.sub("", o["title"] or "")
    dual = bool(OTHER_ROLE_RE.search(stripped))
    title = _nice(o["title"] or "")
    if dual and (d["iar_count"] or 0) >= 10:
        return 100, f"Compliance officer is also {title}, with {d['iar_count']:,} advisors"
    if dual:
        return 60, f"Compliance officer is also {title}"
    return 60, f"Full-time compliance officer ({title})"


def c_assets_band(d, c, key):
    r = d["raum"] or 0
    pts = 100 if r >= 1e9 else 80 if r >= 5e8 else 60 if r >= 2.5e8 else 40
    return pts, f"{_money(r)} in assets"


def c_providers(d, c, key):
    if d["cust"] is None and d["funds"] is None:
        return unknown(0, "No custodian or fund service providers on file")
    n, bits = 0, []
    k = (d["cust"] or {}).get("reported_custodians") or 0
    if k:
        n += k
        bits.append(f"{k} custodian{'s' if k > 1 else ''}")
    f = d["funds"] or {}
    for col, label in (("pb", "prime broker"), ("adm", "fund administrator"),
                       ("aud", "fund auditor")):
        if f.get(col):
            n += 1
            bits.append(label)
    pts = 100 if n >= 5 else 60 if n >= 3 else 20 if n >= 1 else 0
    return pts, (", ".join(bits) if bits else "None reported")


# Factors an admin can add from the Scoring screen without code: any number
# in this catalogue scored by bands, or any yes/no below scored by two levels.
# The function returns None when the value is not known, which the score
# treats as missing data like any other criterion.

def _years_registered(d):
    try:
        y = int((d["registered_date"] or "")[:4])
    except ValueError:
        return None
    return date.today().year - y


def _ppl(field):
    return lambda d: (d["people"] or {}).get(field) if d["people"] else None


def _growth(d):
    p = d["people"]
    if not p or not p.get("headcount"):
        return None
    start = (p["headcount"] or 0) - (p.get("net_12m") or 0)
    return (p.get("net_12m") or 0) / start if start > 0 else None


def _avg_client(d):
    return d["raum"] / d["clients_total"] if d["raum"] and d["clients_total"] else None


FIELDS: dict[str, tuple[str, str, object]] = {
    "raum": ("Assets under management", "money", lambda d: d["raum"]),
    "hnw_aum": ("HNW assets", "money", lambda d: d["hnw_aum"]),
    "hnw_clients": ("HNW clients", "int", lambda d: d["hnw_clients"]),
    "hnw_share": ("HNW share of assets", "pct",
                  lambda d: hnw_share(d) if d["raum"] and d["hnw_aum"] is not None else None),
    "avg_client": ("Average client size", "money", _avg_client),
    "avg_hnw_client": ("Average HNW client size", "money",
                       lambda d: avg_hnw(d) if d["hnw_clients"] else None),
    "clients_total": ("Clients", "int", lambda d: d["clients_total"]),
    "iar_count": ("Advisors (Item 5.B)", "int", lambda d: d["iar_count"]),
    "total_employees": ("Employees", "int", lambda d: d["total_employees"]),
    "private_funds": ("Private funds advised", "int", fund_count),
    "filings_12m": ("ADV amendments in 12 months", "int", lambda d: d["filings_12m"]),
    "years_registered": ("Years registered", "int", _years_registered),
    "headcount": ("People on IAPD now", "int", _ppl("headcount")),
    "hires_12m": ("Advisors hired in 12 months", "int", _ppl("hires_12m")),
    "departures_12m": ("Advisors who left in 12 months", "int", _ppl("departures_12m")),
    "headcount_growth": ("Headcount growth, 12 months", "pct", _growth),
    "personal_emails": ("Named people with an email", "int",
                        lambda d: d["reach"]["personal"]),
    "verified_emails": ("Verified email addresses", "int", lambda d: d["reach"]["verified"]),
    "signals_90d": ("Signals in the last 90 days", "int",
                    lambda d: sum(1 for t in d["triggers"]
                                  if recency(t["detected_date"]) >= 0.84)),
}


def _mail_is(platform):
    def f(d):
        m = d["mail"]
        if not m or m["platform"] not in ("m365", "google", "other", "none"):
            return None
        return m["platform"] == platform
    return f


FLAGS: dict[str, tuple[str, object]] = {
    "m365": ("Email runs on Microsoft 365", _mail_is("m365")),
    "google": ("Email runs on Google Workspace", _mail_is("google")),
    "files_13f": ("Files 13F", lambda d: bool(d["files_13f"])),
    "financial_planning": ("Offers financial planning (Item 5.G)",
                           lambda d: None if d["extra"] is None
                           else bool(d["extra"].get("svc_financial_planning"))),
    "sec_registered": ("SEC-registered", lambda d: d["regulator"] == "SEC"),
    "independent": ("Independent (no bank, insurer or wirehouse control)",
                    lambda d: captive_reason(d) is None),
    "has_disclosure": ("Discloses a disciplinary event (Item 11)",
                       lambda d: None if d.get("disciplinary") is None
                       else d.get("disciplinary") == "Y"),
    "private_funds_any": ("Advises any private fund", advises_private_funds),
    "has_personal_email": ("Has a named person with an email",
                           lambda d: d["reach"]["personal"] > 0),
    "wealth_manager": ("Firm type is a wealth manager (independent or hybrid RIA, family office)",
                       lambda d: None if not d.get("firm_class")
                       or d["firm_class"]["category"] == "unknown"
                       else d["firm_class"]["category"] in ("independent_ria", "hybrid_ria",
                                                            "multi_family_office")),
}


def flag_value(d, field: str):
    if field.startswith("tag:"):
        tag = field[4:]
        if has(d, tag):
            return True
        return False if brochure_read(d) else None
    if field.startswith("web:"):
        sig = field[4:]
        if sig in d["web"]:
            return True
        return False if d["web_scanned"] else None
    return FLAGS[field][1](d)


def flag_label(field: str) -> str:
    if field.startswith("tag:"):
        return "Brochure mentions " + tag_label(field[4:]).lower()
    if field.startswith("web:"):
        return "Website shows " + field[4:].replace("_", " ")
    return FLAGS.get(field, (field,))[0]


def fmt_field(kind: str, v) -> str:
    if v is None:
        return "-"
    if kind == "money":
        return _money(v)
    if kind == "pct":
        return f"{v * 100:.0f}%"
    return f"{v:,.0f}" if isinstance(v, (int, float)) else str(v)


def c_field(d, c, key):
    label, kind, fn = FIELDS[c["field"]]
    v = fn(d)
    if v is None:
        return unknown(0, f"{label}: not known")
    return band(v, c["bands"]), f"{label}: {fmt_field(kind, v)}"


def c_flag(d, c, key):
    v = flag_value(d, c["field"])
    label = flag_label(c["field"])
    if v is None:
        return unknown(0, f"{label}: not known yet")
    return (float(c.get("yes", 100)), f"{label}: yes") if v else (
        float(c.get("no", 0)), f"{label}: no")


CRITERIA = {
    "hnw_fit": c_hnw_fit, "alts_use": c_alts_use, "central_process": c_central_process,
    "portfolio_need": c_portfolio_need, "re_familiarity": c_re_familiarity,
    "ops_fit": c_ops_fit, "relationship": c_relationship,
    "repeat_potential": c_repeat_potential,
    "exchange_frequency": c_exchange_frequency, "owner_client_fit": c_owner_client_fit,
    "intro_timing": c_intro_timing, "specialization": c_specialization,
    "relationship_strength": c_relationship_strength,
    "referral_potential": c_referral_potential, "overlap_1031": c_overlap_1031,
    "check_size": c_check_size, "direct_re": c_direct_re,
    "property_type": c_property_type, "capability_gap": c_capability_gap,
    "geography_fit": c_geography_fit, "governance_fit": c_governance_fit,
    "decision_speed": c_decision_speed, "jv_relationship": c_jv_relationship,
    "hnw_assets": c_hnw_assets, "hnw_clients": c_hnw_clients,
    "schwab_share": c_schwab_share, "clients_per_advisor": c_clients_per_advisor,
    "advisors": c_advisors,
    "m365": c_m365, "black_diamond": c_black_diamond, "best_trigger": c_best_trigger,
    "advisor_count": c_advisor_count, "marketing": c_marketing,
    "portfolio_complexity": c_portfolio_complexity,
    "compliance_owner": c_compliance_owner, "assets_band": c_assets_band,
    "providers": c_providers,
}


# ------------------------------------------------------ penalties, signals

def p_glynac_competitor(d):
    if has(d, "compliance_vendor"):
        return said(d, "compliance_vendor")
    if "compliance_vendor" in d["web"]:
        return "website: " + (d["web"]["compliance_vendor"]["evidence"] or "")
    return None


PENALTIES = {"glynac_competitor": p_glynac_competitor}


def talking_points(d, family: str) -> list[dict]:
    """The firm's own words and holdings, for the SDR's opening line."""
    out = []
    for tag, prod in TAG_PRODUCTS.items():
        if prod != family or not has(d, tag):
            continue
        t = d["tags"][tag]
        out.append({"kind": "brochure", "label": tag_label(tag),
                    "text": t["best_snippet"] or ""})
    names = [tk for tk in d["h13f"] if TICKER_PRODUCT.get(tk) == family]
    for tk in sorted(names, key=lambda k: -d["h13f"][k]["value"]):
        h = d["h13f"][tk]
        out.append({"kind": "13f", "label": tk,
                    "text": f"{_money(h['value'])} in {h['quarter']}"
                            + (f", {h['shares']:,} shares" if h["shares"] else "")})
    return out


def signals_for(d, key: str) -> list[dict]:
    fam = product(key)["family"].lower()
    fam = {"phh": "phh", "acubooth": "acubooth", "glynac": "glynac"}[fam]
    out = talking_points(d, fam)
    if key == "phh_fund" and (has(d, "owner_clients") or has(d, "exchange_1031")):
        out.append({"kind": "flag", "label": "Also a 1031 partner",
                    "text": "Serves business or property owners; see the PHH 1031 list"})
    if key == "acubooth":
        for t in lead_triggers(d, "acubooth")[:3]:
            out.append({"kind": "trigger", "label": "Trigger",
                        "text": f"{t['description']} ({t['detected_date']})"})
    return out


# -------------------------------------------------------------- evaluate

@dataclass
class Result:
    product: str
    status: str                      # scored | gated | disqualified
    reason: str = ""
    score: float = 0.0               # points earned on known data, 0 to 100
    coverage: float = 100.0          # share of the weight resting on known data
    potential: float = 0.0           # the score if every unknown scored full marks
    tier: str = ""                   # retired; kept so old callers do not break
    action: str = ""
    gates: list = field(default_factory=list)
    components: list = field(default_factory=list)
    penalties: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    pitch: str = ""

    @property
    def missing(self) -> list[str]:
        return [c["label"] for c in self.components if not c["known"]]

    def top_reasons(self, n=2) -> list[dict]:
        """The criteria contributing most, for a one-line 'why' on a list."""
        return sorted(self.components, key=lambda c: -c["contrib"])[:n]


def pitch_for(key: str, d, comps) -> str:
    if key == "glynac":
        by = {c["key"]: c for c in comps}
        if by.get("m365", {}).get("points") == 100:
            return "Lead with marketing compliance"
        if by.get("black_diamond", {}).get("points") == 100:
            return "Lead with portfolio and disclosure checks"
        return "Lead with vendor risk"
    return product(key).get("pitch", "")


def evaluate(key: str, d: dict) -> Result:
    p = product(key)
    gates = []
    for g in p.get("gates", []):
        ok, ev = GATES[g["key"]](d, g, key)
        gates.append({"label": g["label"], "passed": ok, "evidence": ev})
        if not ok:
            return Result(key, "gated", reason=f"{g['label']}: {ev}", gates=gates)
    for g in p.get("disqualifiers", []):
        if g.get("off"):
            continue                      # switched off on the Scoring screen
        hit, ev = GATES[g["key"]](d, g, key)
        if hit:
            gates.append({"label": g["label"], "passed": False, "evidence": ev,
                          "disqualifier": True})
            return Result(key, "disqualified", reason=f"{g['label']}: {ev}",
                          gates=gates)
    comps, total, known_w, unknown_w = [], 0.0, 0.0, 0.0
    for c in p["criteria"]:
        weight = float(c.get("weight", 0))
        if weight <= 0:
            continue                      # switched off on the Scoring screen
        kind = c.get("kind")
        fn = c_field if kind == "field" else c_flag if kind == "flag" else CRITERIA[c["key"]]
        out = fn(d, c, key)
        pts, ev = out[0], out[1]
        known = out[2] if len(out) > 2 else True
        pts = configured_points(key, c, float(pts))
        ov = d["overrides"].get((key, c["key"]))
        manual = None
        if ov is not None and c.get("manual"):
            manual = {"by": ov["set_by"], "at": (ov["set_at"] or "")[:10],
                      "note": ov["note"] or "", "computed": pts,
                      "computed_evidence": ev, "computed_known": known}
            pts = float(ov["points"])
            ev = level_label(c, pts)
            known = True                  # a person who knows has said so
        pts = max(0.0, min(100.0, float(pts)))
        contrib = pts * weight / 100.0 if known else 0.0
        total += contrib
        if known:
            known_w += weight
        else:
            unknown_w += weight
        comps.append({"key": c["key"], "label": c["label"], "weight": weight,
                      "points": round(pts, 1) if known else 0.0,
                      "contrib": round(contrib, 2), "evidence": ev,
                      "level": level_label(c, pts) if known else "Missing data",
                      "known": known, "manual": bool(c.get("manual")),
                      "override": manual})
    pens = []
    for pen in p.get("penalties", []):
        ev = PENALTIES[pen["key"]](d)
        if ev:
            total -= pen["points"]
            pens.append({"label": pen["label"], "points": pen["points"], "evidence": ev})
    total = round(max(0.0, total), 1)
    return Result(key, "scored", score=total, coverage=round(known_w, 1),
                  potential=round(min(100.0, total + unknown_w), 1), gates=gates,
                  components=comps, penalties=pens, signals=signals_for(d, key),
                  pitch=pitch_for(key, d, comps))


def evaluate_all(d: dict) -> dict[str, Result]:
    return {k: evaluate(k, d) for k in product_keys()}


# --------------------------------------------------------------- persist

SCHEMA = """
CREATE TABLE IF NOT EXISTS product_score (
    crd          TEXT NOT NULL,
    product      TEXT NOT NULL,
    status       TEXT NOT NULL,      -- scored | disqualified
    reason       TEXT,
    score        REAL,
    tier         TEXT,
    rank         INTEGER,
    raum         BIGINT,
    detail_json  TEXT,               -- components, penalties, signals, pitch
    computed_at  TEXT NOT NULL,
    config_stamp TEXT NOT NULL,
    PRIMARY KEY (crd, product)
);
CREATE INDEX IF NOT EXISTS ix_ps_rank ON product_score (product, status, rank);
CREATE INDEX IF NOT EXISTS ix_ps_crd ON product_score (crd);
CREATE TABLE IF NOT EXISTS firm_scope (
    crd          TEXT PRIMARY KEY,
    best_score   REAL NOT NULL,      -- highest score across products, 0-100
    best_product TEXT NOT NULL,
    best_tier    TEXT,
    products     TEXT NOT NULL,      -- comma separated product keys it is scored for
    -- Work order for every background job: the best score on any list.
    priority     REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_scope_best ON firm_scope (best_score DESC);
CREATE TABLE IF NOT EXISTS score_override (
    crd        TEXT NOT NULL,
    product    TEXT NOT NULL,
    criterion  TEXT NOT NULL,
    points     REAL NOT NULL,
    note       TEXT,
    set_by     TEXT,
    set_at     TEXT NOT NULL,
    PRIMARY KEY (crd, product, criterion)
);
"""


def init(conn) -> None:
    conn.executescript(SCHEMA)
    conn.executescript(CONFIG_SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(firm_scope)")}
    if "priority" not in have:
        conn.execute("ALTER TABLE firm_scope ADD COLUMN priority REAL NOT NULL DEFAULT 0")
    from .db import add_column
    for col, typ in (("coverage", "REAL"), ("potential", "REAL"), ("missing", "TEXT")):
        add_column(conn, "product_score", col, typ)
    add_column(conn, "firm_scope", "best_coverage", "REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_scope_pri ON firm_scope (priority DESC)")
    # Columns the scores read on tables other jobs own. An existing install
    # gets them here, at startup, rather than waiting for the brochure job.
    for table, col, typ in (("brochure", "tag_version", "INTEGER"),
                            ("brochure_tag", "distinct_phrases", "INTEGER")):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()
    # The scores read each firm's type, and the firm types read the industry
    # knowledge base, so both are created (and the knowledge seeded) here, at
    # startup, with the scoring tables. A failure must not stop the scores.
    for mod in ("firmtype", "knowledge"):
        try:
            __import__(f"prospect.{mod}", fromlist=["init"]).init(conn)
        except Exception:
            conn.rollback()


def _detail(r: Result) -> str:
    return json.dumps({"components": r.components, "penalties": r.penalties,
                       "signals": r.signals, "pitch": r.pitch, "gates": r.gates,
                       "coverage": r.coverage, "potential": r.potential},
                      separators=(",", ":"))


def _rows_for(crd: str, d: dict, results: dict[str, Result], now: str, st: str):
    for key, r in results.items():
        if r.status == "gated":
            continue
        scored = r.status == "scored"
        yield (crd, key, r.status, r.reason or None,
               r.score if scored else None, None, None, d["raum"],
               _detail(r), now, st,
               r.coverage if scored else None, r.potential if scored else None,
               "|".join(r.missing) if scored else None)


INSERT = ("INSERT INTO product_score (crd, product, status, reason, score, tier, rank,"
          " raum, detail_json, computed_at, config_stamp, coverage, potential, missing)"
          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def rerank(conn, key: str | None = None) -> None:
    """Rank within each product: score, then how much of it rests on known
    data, then size, then CRD so the order never shuffles between runs."""
    keys = [key] if key else product_keys()
    for k in keys:
        conn.execute("""UPDATE product_score p SET rank = r.rn FROM (
            SELECT crd, ROW_NUMBER() OVER (ORDER BY score DESC, coverage DESC NULLS LAST,
                                           raum DESC NULLS LAST, crd) rn
            FROM product_score WHERE product=? AND status='scored') r
            WHERE p.crd = r.crd AND p.product = ?""", (k, k))
        conn.execute("UPDATE product_score SET rank=NULL WHERE product=?"
                     " AND status!='scored'", (k,))


def _priority(r: Result) -> float:
    """Work order for every background job: the best score a firm has on any
    list. Coverage breaks ties, so a firm whose score is already well founded
    is enriched before an identical score resting on guesses."""
    return r.score + r.coverage / 1000.0


def _scope_row(crd: str, results: dict[str, Result]):
    scored = [r for r in results.values() if r.status == "scored"]
    if not scored:
        return None
    best = max(scored, key=_priority)
    return (crd, best.score, best.product, None,
            ",".join(r.product for r in scored), _priority(best), best.coverage)


def score_all(conn, progress=None) -> dict[str, int]:
    """Score every firm for every product and rebuild both tables."""
    init(conn)
    feats = load_features(conn)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    st = stamp()
    rows, scope = [], []
    counts: dict[str, int] = {k: 0 for k in product_keys()}
    for i, (crd, d) in enumerate(feats.items(), 1):
        res = evaluate_all(d)
        rows.extend(_rows_for(crd, d, res, now, st))
        s = _scope_row(crd, res)
        if s:
            scope.append(s)
        for k, r in res.items():
            if r.status == "scored":
                counts[k] += 1
        if progress and i % 5000 == 0:
            progress(i, len(feats))
    conn.execute("DELETE FROM product_score")
    for i in range(0, len(rows), 5000):
        conn.executemany(INSERT, rows[i:i + 5000])
    conn.execute("DELETE FROM firm_scope")
    for i in range(0, len(scope), 5000):
        conn.executemany("INSERT INTO firm_scope (crd, best_score, best_product,"
                         " best_tier, products, priority, best_coverage)"
                         " VALUES (?,?,?,?,?,?,?)", scope[i:i + 5000])
    rerank(conn)
    conn.commit()
    if progress:
        progress(len(feats), len(feats))
    return counts


def rescore_firm(conn, crd: str) -> dict[str, Result]:
    """Recompute one firm after a manual level or status change, in place.
    No init() here: this runs inside a request, and schema work belongs to
    startup, where it cannot queue behind readers."""
    feats = load_features(conn, [crd])
    d = feats.get(crd)
    if d is None:
        return {}
    res = evaluate_all(d)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute("DELETE FROM product_score WHERE crd=?", (crd,))
    rows = list(_rows_for(crd, d, res, now, stamp()))
    if rows:
        conn.executemany(INSERT, rows)
    conn.execute("DELETE FROM firm_scope WHERE crd=?", (crd,))
    s = _scope_row(crd, res)
    if s:
        conn.execute("INSERT INTO firm_scope (crd, best_score, best_product,"
                     " best_tier, products, priority, best_coverage)"
                     " VALUES (?,?,?,?,?,?,?)", s)
    for k in res:
        rerank(conn, k)
    conn.commit()
    return res


def set_override(conn, crd: str, key: str, criterion: str, points: float | None,
                 note: str, who: str) -> None:
    """Set (or with points None, clear) a manual level, then rescore the firm."""
    crit = next((c for c in product(key)["criteria"] if c["key"] == criterion), None)
    if crit is None or not crit.get("manual"):
        raise ValueError(f"{key}.{criterion} does not accept a manual level")
    if points is None:
        conn.execute("DELETE FROM score_override WHERE crd=? AND product=?"
                     " AND criterion=?", (crd, key, criterion))
    else:
        allowed = {float(p) for p, _ in crit["levels"]}
        if float(points) not in allowed:
            raise ValueError("not one of the defined levels")
        conn.execute("INSERT INTO score_override (crd, product, criterion, points,"
                     " note, set_by, set_at) VALUES (?,?,?,?,?,?,?)"
                     " ON CONFLICT(crd, product, criterion) DO UPDATE SET"
                     " points=excluded.points, note=excluded.note,"
                     " set_by=excluded.set_by, set_at=excluded.set_at",
                     (crd, key, criterion, float(points), note or None, who or None,
                      datetime.now(timezone.utc).isoformat(timespec="seconds")))
    conn.commit()
    rescore_firm(conn, crd)
