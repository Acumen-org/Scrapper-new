"""Product scoring: how every firm is judged against every product list.

The rules live in config/products.yml (gates, weights, points per level, tiers)
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
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import yaml

from . import config

# ------------------------------------------------------------------ config

_CFG: dict | None = None


def cfg() -> dict:
    global _CFG
    if _CFG is None:
        _CFG = yaml.safe_load((config.CONFIG_DIR / "products.yml").read_text(
            encoding="utf-8"))
        _validate(_CFG)
    return _CFG


def _validate(c: dict) -> None:
    """Fail loudly on a config that cannot mean what it says."""
    for key, p in c["products"].items():
        w = sum(cr["weight"] for cr in p["criteria"])
        if w != 100:
            raise ValueError(f"products.yml: {key} weights add to {w}, not 100")
        for cr in p["criteria"]:
            if cr["key"] not in CRITERIA:
                raise ValueError(f"products.yml: {key}.{cr['key']} has no evaluator")
        for g in p.get("gates", []) + p.get("disqualifiers", []):
            if g["key"] not in GATES:
                raise ValueError(f"products.yml: {key} gate {g['key']} has no evaluator")


def product_keys() -> list[str]:
    return list(cfg()["order"])


def product(key: str) -> dict:
    return cfg()["products"][key]


def stamp() -> str:
    return f"{config.load().stamp}|products.v{cfg()['config_version']}"


def tier_for(key: str, score: float) -> tuple[str, str]:
    for threshold, label, action in product(key)["tiers"]:
        if score >= threshold:
            return str(label), action
    t = product(key)["tiers"][-1]
    return str(t[1]), t[2]


def level_label(crit: dict, points: float) -> str:
    """The config's description of the level a points value sits at."""
    for pts, label in crit.get("levels", []):
        if points >= pts:
            return label
    lv = crit.get("levels") or [[0, ""]]
    return lv[-1][1]


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
            registered_date, website
            FROM firm_current WHERE 1=1{only('crd')}""", a):
        d = dict(r)
        d.update(tags={}, funds=None, seg=None, cust=None, h13f={}, files_13f=False,
                 officers=[], owners=[], triggers=[], filings_12m=0, mail=None, web={},
                 status=None, overrides={}, extra=None, brochure=None)
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
             "platform_advyzon": "Advyzon"}


def platform_evidence(d) -> dict[str, str]:
    """Reporting platform -> where it was seen (brochure or website)."""
    out = {}
    for sig, name in PLATFORMS.items():
        if has(d, sig):
            out[name] = said(d, sig)
        elif sig in d["web"]:
            out[name] = "website: " + (d["web"][sig]["evidence"] or "client login link")
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
    mail = (d["mail"] or {}).get("platform")
    plats = platform_evidence(d)
    other = [p for p in plats if p != "Black Diamond"]
    if mail == "google" and other and "Black Diamond" not in plats:
        return False, f"Google email and {other[0]} for portfolios"
    return True, "At least one system Glynac can work with, or not yet ruled out"


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


GATES = {
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
    return 0, not_found(d, "alternatives")


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
    return 25, not_found(d, "central investment process")


def c_portfolio_need(d, c, key):
    if has(d, "real_assets_income") and (has(d, "tax_management")
                                         or has(d, "private_markets")):
        return 80, said(d, "real_assets_income")
    if has(d, "real_assets_income"):
        return 55, said(d, "real_assets_income")
    return 25, not_found(d, "stated need for income or real assets")


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
    return 0, not_found(d, "real estate")


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
    return 30, "Custodian not reported"


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
    return 0, "No 1031 language in the brochure"


def c_owner_client_fit(d, c, key):
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
    if (d["extra"] or {}).get("svc_financial_planning"):
        return 67, "Offers financial planning (Item 5.G)"
    return 33, "No financial planning service reported"


def c_specialization(d, c, key):
    t = d["tags"].get("exchange_1031")
    if t and t["present"] and t["section_item"] in (4, 8):
        return 67, f"1031 work described in Item {t['section_item']} of the brochure"
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
    return 0, "No recurring owner client flow visible"


def _geo(d, hit_pts, miss_pts, unset_pts):
    focus = [s.upper() for s in cfg().get("phh_focus_states") or []]
    if not focus:
        return unset_pts, "PHH focus states not set"
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
    return 0, not_found(d, "real estate")


def c_property_type(d, c, key):
    if has(d, "net_lease_types"):
        return 100, said(d, "net_lease_types")
    for t in ("private_real_estate", "real_estate_direct"):
        if has(d, t):
            return 33, said(d, t)
    return 0, not_found(d, "property type")


def c_capability_gap(d, c, key):
    return 33, "Not yet known; set it after the first conversation"


def c_geography_fit(d, c, key):
    return _geo(d, 100, 0, 50)


def c_governance_fit(d, c, key):
    return 50, "Not yet known; set it after the first conversation"


def c_decision_speed(d, c, key):
    if advises_private_funds(d):
        return 100, "Already runs private funds, so it acts on live deals"
    return 50, "Not yet known"


def c_jv_relationship(d, c, key):
    hit = _status_points(d, {"customer": 100, "qualified": 40, "meeting set": 40})
    return hit or (0, "No contact yet")


# AcuBooth
def c_hnw_assets(d, c, key):
    return band(d["hnw_aum"] or 0, c["bands"]), f"{_money(d['hnw_aum'] or 0)} of HNW assets"


def c_hnw_clients(d, c, key):
    return band(d["hnw_clients"] or 0, c["bands"]), f"{d['hnw_clients'] or 0:,} HNW clients"


def c_schwab_share(d, c, key):
    cp = d["cust"] or {}
    s = cp.get("schwab_share_reported")
    if s is None:
        return 0, "No custodian reported"
    return round(s * 100, 1), (f"Schwab holds {_pct(s)} of reported custody "
                               f"(as of {cp.get('as_of_filing_date')}); positions for "
                               f"late 2026, not accounts sellable today")


def c_clients_per_advisor(d, c, key):
    iar = d["iar_count"] or 0
    if not iar:
        return 20, "No advisors reported"
    cpr = (d["hnw_clients"] or 0) / iar
    per = avg_hnw(d)
    mult = band(per, c["damping"], default=0.25)
    pts = band(cpr, c["bands"]) * mult
    return round(pts, 1), (f"{cpr:.0f} HNW clients per advisor, {_money(per)} each"
                           + (f" (x{mult:.2f})" if mult < 1 else ""))


def c_advisors(d, c, key):
    return band(d["iar_count"] or 0, c["bands"]), f"{d['iar_count'] or 0:,} advisors"


# Glynac
def c_m365(d, c, key):
    m = d["mail"]
    if not m:
        return 33, "Email platform not checked yet"
    p = m["platform"]
    if p == "m365":
        return 100, f"Microsoft 365: {m['evidence']}"
    if p == "google":
        return 0, f"Google Workspace: {m['evidence']}"
    if p == "other":
        return 0, f"Not Microsoft 365: {m['evidence']}"
    return 33, m["evidence"] or "Unknown"


def c_black_diamond(d, c, key):
    plats = platform_evidence(d)
    if "Black Diamond" in plats:
        return 100, plats["Black Diamond"]
    if plats:
        name = next(iter(plats))
        return 0, f"{name}: {plats[name]}"
    return 33, "Portfolio platform not found in the brochure or website"


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
        return 0, ("No marketing flags in Item 5.L and no blog or social media found"
                   if x else "Item 5.L not read yet")
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
        return 0, "No Schedule A roster on file"
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
    score: float = 0.0
    tier: str = "-"
    action: str = ""
    gates: list = field(default_factory=list)
    components: list = field(default_factory=list)
    penalties: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    pitch: str = ""

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
        hit, ev = GATES[g["key"]](d, g, key)
        if hit:
            gates.append({"label": g["label"], "passed": False, "evidence": ev,
                          "disqualifier": True})
            return Result(key, "disqualified", reason=f"{g['label']}: {ev}",
                          gates=gates)
    comps, total = [], 0.0
    for c in p["criteria"]:
        pts, ev = CRITERIA[c["key"]](d, c, key)
        ov = d["overrides"].get((key, c["key"]))
        manual = None
        if ov is not None and c.get("manual"):
            manual = {"by": ov["set_by"], "at": (ov["set_at"] or "")[:10],
                      "note": ov["note"] or "", "computed": pts,
                      "computed_evidence": ev}
            pts = float(ov["points"])
            ev = level_label(c, pts)
        pts = max(0.0, min(100.0, float(pts)))
        contrib = pts * c["weight"] / 100.0
        total += contrib
        comps.append({"key": c["key"], "label": c["label"], "weight": c["weight"],
                      "points": round(pts, 1), "contrib": round(contrib, 2),
                      "evidence": ev, "level": level_label(c, pts),
                      "manual": bool(c.get("manual")), "override": manual})
    pens = []
    for pen in p.get("penalties", []):
        ev = PENALTIES[pen["key"]](d)
        if ev:
            total -= pen["points"]
            pens.append({"label": pen["label"], "points": pen["points"], "evidence": ev})
    total = round(max(0.0, total), 1)
    tier, action = tier_for(key, total)
    return Result(key, "scored", score=total, tier=tier, action=action, gates=gates,
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
CREATE INDEX IF NOT EXISTS ix_ps_tier ON product_score (product, tier);
CREATE TABLE IF NOT EXISTS firm_scope (
    crd          TEXT PRIMARY KEY,
    best_score   REAL NOT NULL,      -- highest score across products, 0-100
    best_product TEXT NOT NULL,
    best_tier    TEXT,
    products     TEXT NOT NULL,      -- comma separated product keys it is scored for
    -- Work order for every background job: tier first, then score, so tier A
    -- on any product outranks tier B on another whatever the raw numbers.
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
    have = {r[1] for r in conn.execute("PRAGMA table_info(firm_scope)")}
    if "priority" not in have:
        conn.execute("ALTER TABLE firm_scope ADD COLUMN priority REAL NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_scope_pri ON firm_scope (priority DESC)")
    # Columns the scores read on tables other jobs own. An existing install
    # gets them here, at startup, rather than waiting for the brochure job.
    for table, col, typ in (("brochure", "tag_version", "INTEGER"),
                            ("brochure_tag", "distinct_phrases", "INTEGER")):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()


def _detail(r: Result) -> str:
    return json.dumps({"components": r.components, "penalties": r.penalties,
                       "signals": r.signals, "pitch": r.pitch, "gates": r.gates,
                       "action": r.action}, separators=(",", ":"))


def _rows_for(crd: str, d: dict, results: dict[str, Result], now: str, st: str):
    for key, r in results.items():
        if r.status == "gated":
            continue
        yield (crd, key, r.status, r.reason or None,
               r.score if r.status == "scored" else None,
               r.tier if r.status == "scored" else None, None, d["raum"],
               _detail(r), now, st)


INSERT = ("INSERT INTO product_score (crd, product, status, reason, score, tier, rank,"
          " raum, detail_json, computed_at, config_stamp)"
          " VALUES (?,?,?,?,?,?,?,?,?,?,?)")


def rerank(conn, key: str | None = None) -> None:
    """Rank within each product: score, then size, then CRD for a stable order."""
    keys = [key] if key else product_keys()
    for k in keys:
        conn.execute("""UPDATE product_score p SET rank = r.rn FROM (
            SELECT crd, ROW_NUMBER() OVER (ORDER BY score DESC, raum DESC NULLS LAST,
                                           crd) rn
            FROM product_score WHERE product=? AND status='scored') r
            WHERE p.crd = r.crd AND p.product = ?""", (k, k))
        conn.execute("UPDATE product_score SET rank=NULL WHERE product=?"
                     " AND status!='scored'", (k,))


TIER_WEIGHT = {"A": 300, "B": 200, "C": 100}


def _priority(r: Result) -> float:
    return TIER_WEIGHT.get(r.tier, 0) + r.score


def _scope_row(crd: str, results: dict[str, Result]):
    scored = [r for r in results.values() if r.status == "scored"]
    if not scored:
        return None
    best = max(scored, key=_priority)
    return (crd, best.score, best.product, best.tier,
            ",".join(r.product for r in scored), _priority(best))


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
                         " best_tier, products, priority) VALUES (?,?,?,?,?,?)",
                         scope[i:i + 5000])
    rerank(conn)
    conn.commit()
    return counts


def rescore_firm(conn, crd: str) -> dict[str, Result]:
    """Recompute one firm after a manual level or status change, in place."""
    init(conn)
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
                     " best_tier, products, priority) VALUES (?,?,?,?,?,?)", s)
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
    init(conn)
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
