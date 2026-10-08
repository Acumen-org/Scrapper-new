"""What kind of firm each adviser is, and why Bellwether thinks so.

Not every registered adviser is a wealth manager. Charles Schwab, PIMCO,
Oaktree, Morgan Stanley and LPL all file Form ADV, and a list that ranks them
next to a twelve-person RIA is not doing its job. Every firm in firm_current is
therefore sorted into one firm type (the categories in config/industry.yml),
with a confidence from 0 to 100, a few plain reasons ("92% of assets are in
pooled vehicles", "parent Allianz SE owns 75% or more") and where the answer
came from: the rules here, Bellwether AI for firms the rules could not settle,
or a person who corrected it in Settings.

The rules, in the order they are tried (the first that applies decides):

  1  Exempt reporting adviser: the SEC feed says so, or a state feed shows an
     active exemption and no approved registration.
  2  Known name: the firm's own name matches a known entity in the knowledge
     base (Charles Schwab, PIMCO, LPL ...). The longest pattern wins, so
     "Morgan Stanley Investment Management" beats "Morgan Stanley". A group's
     fund or institutional arm is classed by what it does, except custodians,
     whose whole group counts as custodian.
  3  Who its clients are (Item 5.D, by assets, or by count when no assets are
     reported), when individuals are under half: pooled vehicles make a
     private fund manager; registered funds an asset manager; institutions an
     asset manager, or a consultant / OCIO when it consults for pension plans,
     endowments and governments; other advisers a sub-adviser (asset manager).
     This comes before licences, so an ETF issuer that is also a
     broker-dealer is still an asset manager.
  4  What the firm itself is (Item 6.A): a bank, a trust company or a
     broker-dealer. A broker-dealer controlled by a wirehouse, custodian,
     bank or insurer takes that type.
  5  Who controls it (Schedule A): a firm serving people that is 50% or more
     owned by a custodian, wirehouse, broker-dealer, bank or insurer takes
     that type. RIA groups and asset-manager owners are noted only.
  6  Wealth managers, by their own answers: internet adviser or thousands of
     small accounts per employee (robo); a thousand or more accounts per
     employee in wrap programs or chosen by other advisers (TAMP); a family
     office by name, or HNW clients averaging $20M+ (multi-family office);
     advisors who are broker-dealer reps or an affiliated broker-dealer
     (hybrid RIA); otherwise an independent RIA. Firms with no client
     breakdown are judged, at low confidence, by the services they list.
  7  Nothing on file to tell: unknown, which never excludes a firm anywhere.

A person's choice (source 'manual') always wins and survives every
reclassification; the rules' current answer is kept beside it so a screen can
say when the two disagree. Bellwether AI may settle firms the rules leave
below 50% confidence, never over a person's choice, within the daily AI limit.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import yaml

from . import config

# ---------------------------------------------------------------- categories


# Acumen's core market: wealth managers for individuals and families.
WEALTH = ("independent_ria", "hybrid_ria", "multi_family_office")


def _load_categories() -> dict[str, dict]:
    y = yaml.safe_load((config.CONFIG_DIR / "industry.yml").read_text(encoding="utf-8"))
    return {c["key"]: {"key": c["key"], "label": c["label"],
                       "short": c.get("short") or c["label"],
                       "description": c.get("description") or "",
                       "treatment": c.get("treatment") or "", "core": c["key"] in WEALTH}
            for c in y["categories"]}


# The shipped definitions: key -> {key, label, short, description, treatment,
# core}, in display order. Admins may reword them in Settings, Industry
# knowledge; categories() returns the words in force.
CATEGORIES: dict[str, dict] = _load_categories()
KEYS: list[str] = list(CATEGORIES)
BY_KEY: dict[str, dict] = CATEGORIES

# Every type the rules below can produce. Checked at import, so a category
# removed from the YAML cannot leave a rule pointing nowhere.
RULE_KEYS = {"independent_ria", "hybrid_ria", "multi_family_office", "consultant_ocio", "tamp", "robo",
             "asset_manager", "private_fund", "custodian", "wirehouse", "independent_bd",
             "bank_trust", "insurance", "era", "unknown"}
if not RULE_KEYS <= set(KEYS):
    raise RuntimeError("config/industry.yml is missing firm types: "
                       + ", ".join(sorted(RULE_KEYS - set(KEYS))))

# Types a known entity (or a person) can assign.
ASSIGNABLE = [k for k in KEYS if k not in ("unknown", "era")]
# Types decided by what the business does; a group name never overrides them.
INSTITUTIONAL = ("asset_manager", "private_fund", "consultant_ocio")
# An owner of 50%+ in one of these makes a wealth manager part of it.
PARENT_DECIDES = ("custodian", "wirehouse", "independent_bd", "bank_trust", "insurance")
# Below this the rules ask Bellwether AI, when it is allowed to.
AI_BELOW = 50

SIGNALS = {
    "serves_hnw": "Serves HNW individuals",
    "mass_affluent": "Mostly non-HNW individuals",
    "planning": "Offers financial planning",
    "dual_registered": "Advisors are broker-dealer reps",
    "affiliated_bd": "Affiliated broker-dealer",
    "commissions": "Earns commissions",
    "insurance_licensed": "Most advisors are insurance agents",
    "performance_fees": "Charges performance fees",
    "private_funds": "Advises private funds",
    "registered_funds": "Advises registered funds",
    "institutional": "Serves institutions",
    "wrap": "Runs wrap fee programs",
    "files_13f": "Files 13F",
    "in_group": "Part of a larger group",
    "bank_affiliate": "Related bank",
    "trust_affiliate": "Related trust company",
    "qualified_custodian": "Acts as qualified custodian",
    "internet_adviser": "Internet adviser",
}

SOURCES = {"rules": "Rules", "ai": "Bellwether AI", "manual": "Set by hand"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS firm_class (
    crd              TEXT PRIMARY KEY,
    category         TEXT NOT NULL,
    confidence       INTEGER NOT NULL,   -- 0-100
    evidence         TEXT NOT NULL,      -- JSON list of short reasons, as shown
    signals          TEXT,               -- JSON list of SIGNALS keys
    source           TEXT NOT NULL,      -- rules | ai | manual
    rules_category   TEXT,               -- what the rules say now, kept under ai/manual
    rules_confidence INTEGER,
    rules_evidence   TEXT,               -- JSON
    note             TEXT,               -- a person's note, or the AI's reason
    ai_hash          TEXT,               -- the input the AI last read
    updated_at       TEXT NOT NULL,
    overridden_by    TEXT,
    overridden_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_firm_class_cat ON firm_class (category);
"""


def init(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def categories(conn=None) -> list[dict]:
    """The categories with any wording an admin changed in Settings."""
    try:
        from . import knowledge
        over = knowledge.category_text(conn)
    except Exception:
        over = {}
    return [dict(c, **over.get(c["key"], {})) for c in CATEGORIES.values()]


def label(key: str | None, short: bool = False) -> str:
    c = next((c for c in categories() if c["key"] == key), None)
    if c is None:
        return "Not classified" if not key else str(key)
    return c["short"] if short else c["label"]


# ------------------------------------------------------------------ inputs

def _safe(conn, sql: str, args=()) -> list:
    try:
        return conn.execute(sql, args).fetchall()
    except Exception:
        conn.rollback()
        return []


def load_inputs(conn, crds: list[str] | None = None) -> dict[str, dict]:
    """Everything the rules read, per firm, in a few bulk queries."""
    one = crds is not None
    ph = ",".join("?" * len(crds)) if one else ""
    only = (lambda col: f" AND {col} IN ({ph})") if one else (lambda col: "")
    a = tuple(crds) if one else ()
    F: dict[str, dict] = {}
    for r in _safe(conn, f"""SELECT crd, legal_name, business_name, is_era, regulator,
            raum, raum_nondisc, clients_total, hnw_clients, hnw_aum, retail_clients,
            retail_aum, iar_count, total_employees, q7b, website
            FROM firm_current WHERE 1=1{only('crd')}""", a):
        d = dict(r)
        d.update(answers={}, owners=[], funds=None, files_13f=False)
        F[d["crd"]] = d
    if not F:
        return F
    for r in _safe(conn, f"SELECT crd, answers FROM firm_adv_profile WHERE 1=1{only('crd')}", a):
        if r["crd"] in F:
            try:
                F[r["crd"]]["answers"] = json.loads(r["answers"] or "{}")
            except ValueError:
                pass
    for r in _safe(conn, f"""SELECT crd, name, ownership_code, control_person FROM schedule_a
            WHERE is_individual=0 AND ownership_code IN ('D','E'){only('crd')}""", a):
        if r["crd"] in F:
            F[r["crd"]]["owners"].append({"name": r["name"], "code": r["ownership_code"],
                                          "control": r["control_person"] == "Y"})
    # Private funds on the latest Schedule D filing. The newest filing is the
    # one with the latest date, or the highest filing id where dates are
    # missing (ids only grow).
    for r in _safe(conn, f"""
            WITH latest AS (
                SELECT DISTINCT ON (s.crd) s.crd, s.filing_id FROM sched_d_7b1 s
                LEFT JOIN filing_crd fc ON fc.filing_id=s.filing_id
                WHERE s.crd IS NOT NULL{only('s.crd')}
                ORDER BY s.crd, fc.filing_date DESC NULLS LAST, LENGTH(s.filing_id) DESC,
                         s.filing_id DESC)
            SELECT s.crd, COUNT(DISTINCT s.fund_id) n,
                   SUM(COALESCE(s.gross_asset_value,0)) gav,
                   STRING_AGG(DISTINCT s.fund_type, '|') types
            FROM sched_d_7b1 s JOIN latest l ON l.crd=s.crd AND l.filing_id=s.filing_id
            GROUP BY s.crd""", a):
        if r["crd"] in F:
            F[r["crd"]]["funds"] = dict(r)
    for r in _safe(conn, f"""SELECT DISTINCT crd FROM adv_13f_match
            WHERE status IN ('auto','confirmed'){only('crd')}""", a):
        if r["crd"] in F:
            F[r["crd"]]["files_13f"] = True
    return F


# ------------------------------------------------------------------- rules

@dataclass
class Verdict:
    category: str
    confidence: int
    evidence: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)


def _num(a: dict, k: str) -> float:
    try:
        return float(a.get(k) or 0)
    except (TypeError, ValueError):
        return 0.0


def _y(a: dict, k: str) -> bool:
    return a.get(k) == "Y"


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


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


SUFFIXES = re.compile(r"\b(Se|Ag|Nv|Bv|Sa|Plc|Llp|Lp|Llc|Inc|Ltd|Gmbh|Spa)\b\.?")


def _nice(name: str) -> str:
    """An owner's name in title case, with company suffixes in capitals."""
    from .names import nice_name
    return SUFFIXES.sub(lambda m: m.group(0).upper(), nice_name(name))


INST_TYPES = {"C": "banks", "G": "pension plans", "H": "charities", "I": "governments",
              "K": "insurers", "L": "sovereign funds", "M": "companies"}


def client_mix(a: dict) -> dict | None:
    """Item 5.D as shares: by assets, or by client count when a firm reports
    no assets per type (planners, consultants). None when nothing is there."""
    vals = {x: _num(a, f"Q5D{x}3") for x in "ABCDEFGHIJKLMN"}
    basis = "assets"
    tot = sum(vals.values())
    if tot <= 0:
        vals = {x: _num(a, f"Q5D{x}1") for x in "ABCDEFGHIJKLMN"}
        tot = sum(vals.values())
        basis = "clients"
    if tot <= 0:
        return None
    s = {x: v / tot for x, v in vals.items()}
    inst_parts = sorted(((s[x], INST_TYPES[x]) for x in INST_TYPES if s[x] > 0), reverse=True)
    return {"basis": basis, "of": "of assets" if basis == "assets" else "of clients",
            "indiv": s["A"] + s["B"], "hnw": s["B"], "retail": s["A"],
            "regfund": s["D"] + s["E"], "pooled": s["F"],
            "inst": sum(s[x] for x in INST_TYPES), "inst_parts": inst_parts,
            "plans": s["G"] + s["H"] + s["I"],
            "advisers": s["J"], "other": s["N"]}


FAMILY_OFFICE_RE = re.compile(r"\bFAMILY OFFICES?\b|\bMULTI FAMILY\b|\bFAMILY WEALTH OFFICE\b")
CONSULT_RE = re.compile(r"\bOCIO\b|\bCONSULT|\bOUTSOURCED CHIEF\b")

CODE_WORDS = {"E": "75% or more", "D": "50 to 75%"}


def private_share(d: dict) -> float | None:
    """Private fund gross assets (Schedule D, to 2024) over today's assets, or
    None when no private fund is on file."""
    gav = float((d.get("funds") or {}).get("gav") or 0)
    raum = float(d.get("raum") or 0)
    return gav / raum if gav and raum else None


def _fund_line(d: dict) -> str | None:
    f = d.get("funds") or {}
    n = f.get("n") or 0
    if not n:
        return "Advises private funds (Item 7.B)" if d.get("q7b") == "Y" else None
    types = [t for t in (f.get("types") or "").split("|") if t]
    kinds = ", ".join(sorted({t.replace(" Fund", "").lower() for t in types}))[:80]
    gav = f" with {_money(f.get('gav'))} gross" if f.get("gav") else ""
    return f"Advises {n} private fund{'s' if n != 1 else ''}{' (' + kinds + ')' if kinds else ''}{gav} (Schedule D)"


def signals(d: dict, a: dict, mix: dict | None) -> list[str]:
    s = []
    if mix:
        if mix["hnw"] >= 0.4 or (d.get("hnw_clients") or 0) >= 25:
            s.append("serves_hnw")
        if mix["retail"] >= 0.5:
            s.append("mass_affluent")
        if mix["inst"] >= 0.25:
            s.append("institutional")
        if mix["regfund"] > 0.05 or _y(a, "Q2A5"):
            s.append("registered_funds")
    if _y(a, "Q5G1"):
        s.append("planning")
    staff, reps = _num(a, "Q5B1"), _num(a, "Q5B2")
    if reps >= 1:
        s.append("dual_registered")
    if _y(a, "Q7A1"):
        s.append("affiliated_bd")
    if _y(a, "Q5E5"):
        s.append("commissions")
    if staff and _num(a, "Q5B5") / staff >= 0.5:
        s.append("insurance_licensed")
    if _y(a, "Q5E6"):
        s.append("performance_fees")
    if d.get("q7b") == "Y" or (d.get("funds") or {}).get("n"):
        s.append("private_funds")
    if _num(a, "Q5I2A") + _num(a, "Q5I2C") > 0:
        s.append("wrap")
    if d.get("files_13f"):
        s.append("files_13f")
    if _y(a, "Q7A8"):
        s.append("bank_affiliate")
    if _y(a, "Q7A9"):
        s.append("trust_affiliate")
    if _y(a, "Q9D1") or _y(a, "Q9D2"):
        s.append("qualified_custodian")
    if _y(a, "Q2A11"):
        s.append("internet_adviser")
    return s


def _business(d: dict, a: dict, mix: dict | None, names_norm: str) -> Verdict | None:
    """Step 4: firms whose clients are funds or institutions rather than people."""
    if mix is None:
        if _y(a, "Q2A7"):
            return Verdict("consultant_ocio", 60,
                           ["Registers with the SEC as a pension consultant (Item 2.A)",
                            "Reports no assets it manages by client type"])
        return None
    of, indiv = mix["of"], mix["indiv"]
    if indiv >= 0.5:
        return None
    adjust = 0 if mix["basis"] == "assets" else -10
    if mix["pooled"] >= 0.5:
        ev = [f"{_pct(mix['pooled'])} {of} are in pooled vehicles (Item 5.D)"]
        line = _fund_line(d)
        share = private_share(d)
        if d.get("q7b") == "N":
            return Verdict("asset_manager", 75 + adjust, ev + [
                "Advises no private funds (Item 7.B), so the pooled vehicles are collective "
                "trusts or public funds outside the US"])
        if share is not None and share < 0.3:
            return Verdict("asset_manager", 70 + adjust, ev + [
                f"Private funds hold only {_pct(share)} of its assets (Schedule D); the rest "
                f"are collective trusts or public funds"])
        if line:
            ev.append(line)
        conf = (90 if mix["pooled"] >= 0.7 else 80) if share is not None else 70
        return Verdict("private_fund", conf + adjust, ev)
    if mix["regfund"] >= 0.3 and indiv < 0.35:
        ev = [f"{_pct(mix['regfund'])} {of} are registered funds: mutual funds, ETFs, BDCs (Item 5.D)"]
        if _y(a, "Q2A5"):
            ev.append("Registers as adviser to registered investment companies (Item 2.A)")
        return Verdict("asset_manager", (90 if mix["regfund"] >= 0.5 else 80) + adjust, ev)
    if mix["inst"] >= 0.5 and indiv < 0.35:
        kinds = ", ".join(f"{n} {_pct(v)}" for v, n in mix["inst_parts"][:3])
        ev = [f"{_pct(mix['inst'])} {of} are institutions ({kinds}) (Item 5.D)"]
        raum = d.get("raum") or 0
        nondisc = (d.get("raum_nondisc") or 0) / raum if raum else 0
        consult = None
        if mix["plans"] < 0.4 and not CONSULT_RE.search(names_norm):
            consult = None
        elif _y(a, "Q2A7"):
            consult = "Registers as a pension consultant (Item 2.A)"
        elif CONSULT_RE.search(names_norm):
            consult = "Its name says consulting or OCIO"
        elif _y(a, "Q5G6") and _y(a, "Q5G7") and nondisc >= 0.5:
            consult = (f"Consults on pensions and selects managers (Item 5.G); "
                       f"{_pct(nondisc)} of assets are advised without discretion")
        if consult:
            return Verdict("consultant_ocio", 75 + adjust, ev + [consult])
        return Verdict("asset_manager", 75 + adjust, ev)
    if mix["advisers"] >= 0.4 and indiv < 0.35:
        return Verdict("asset_manager", 65 + adjust,
                       [f"{_pct(mix['advisers'])} {of} come from other advisers, as a "
                        f"sub-adviser (Item 5.D)"])
    if indiv >= 0.35:
        return None
    parts = [("private_fund" if d.get("q7b") == "Y" else "asset_manager", mix["pooled"],
              "pooled vehicles"),
             ("asset_manager", mix["regfund"], "registered funds"),
             ("asset_manager", mix["inst"], "institutions"),
             ("asset_manager", mix["advisers"], "other advisers (sub-advisory)")]
    if sum(p[1] for p in parts) >= 0.5:
        cat, share, what = max(parts, key=lambda p: p[1])
        return Verdict(cat, 60 + adjust,
                       [f"Mostly funds and institutions; the largest share, {_pct(share)} "
                        f"{of}, is {what} (Item 5.D)",
                        f"Individuals are {_pct(indiv)} {of}"])
    return None


def _parent_hits(d: dict, kb) -> list[tuple]:
    """(match, owner) for each controlling owner that is a known entity,
    strongest first: 75%+ before 50-75%, named before generic."""
    hits = []
    for o in d.get("owners") or []:
        m = kb.parent(o["name"])
        if m:
            hits.append((m, o))
    hits.sort(key=lambda h: (h[1]["code"] != "E", not h[1].get("control"), -h[0].strength))
    return hits


def _lower(text: str) -> str:
    """A label inside a sentence: ordinary words lower case, acronyms kept."""
    return " ".join(w.lower() if w[:1].isupper() and w[1:].islower() else w
                    for w in (text or "").split())


def _owner_line(m, o) -> str:
    return (f"Parent {_nice(o['name'])} ({_lower(label(m.entity.category))}) owns "
            f"{CODE_WORDS.get(o['code'], 'a controlling stake')} (Schedule A)")


def classify(d: dict, kb) -> Verdict:
    """One firm's type from its inputs (load_inputs) and the known entities."""
    a = d.get("answers") or {}
    mix = client_mix(a)
    sig = signals(d, a, mix)
    names = [d.get("legal_name"), d.get("business_name")]
    from .knowledge import normalize
    names_norm = " | ".join(normalize(n) for n in names if n)
    hits = _parent_hits(d, kb)
    deciding = [(m, o) for m, o in hits if m.entity.category in PARENT_DECIDES]
    if any(m.entity.category not in PARENT_DECIDES for m, o in hits) or deciding:
        sig.append("in_group")

    def done(v: Verdict, extra: list[str] | None = None) -> Verdict:
        ev = list(v.evidence)
        for line in extra or []:
            if line and line not in ev and len(ev) < 4:
                ev.append(line)
        return Verdict(v.category, max(0, min(100, int(v.confidence))), ev[:4], sig)

    owner_note = _owner_line(*hits[0]) if hits else None

    # 1 Exempt reporting adviser, with the SEC or with a state
    if d.get("is_era"):
        return done(Verdict("era", 99, ["Exempt reporting adviser: files Form ADV without "
                                        "registering"]), [_fund_line(d), owner_note])
    states = state_era(a)
    if states:
        return done(Verdict("era", 95, [f"Exempt reporting adviser with {states} (state "
                                        f"filing), not registered"]), [_fund_line(d), owner_note])

    biz = _business(d, a, mix, names_norm)

    # 2 Known name
    m = kb.firm(names)
    if m:
        cat = m.entity.category
        if (cat != "custodian" and cat not in INSTITUTIONAL and biz is not None
                and biz.category in INSTITUTIONAL and mix and mix["indiv"] < 0.10):
            return done(biz, [f"Part of {m.entity.title} by name"])
        if m.generic:
            ev = [f"Name says it is a {_lower(label(cat))}"]
        else:
            ev = [f"Name matches known {_lower(label(cat))}: {m.pattern}"
                  + (f" ({m.entity.title})" if m.entity.title.lower() not in m.pattern.lower()
                     and len(m.entity.title) < 40 and not m.entity.title.startswith(("Other", "Large", "Any"))
                     else "")]
        if mix and cat in WEALTH + ("tamp", "robo"):
            ev.append(f"{_pct(mix['indiv'])} {mix['of']} are individuals and families (Item 5.D)")
        return done(Verdict(cat, m.entity.confidence, ev), [owner_note])

    # 3 Who its clients are: funds and institutions decide it, whatever else
    # the firm is licensed as (an ETF issuer or a buyout firm may also be a
    # registered broker-dealer).
    if biz is not None:
        also = []
        if _y(a, "Q6A1"):
            also.append("Also a registered broker-dealer (Item 6.A)")
        if _y(a, "Q6A7") or _y(a, "Q6A8"):
            also.append("Also a bank or trust company (Item 6.A)")
        return done(biz, also + [owner_note])

    # 4 What the firm itself is. A trust charter alone does not make a wealth
    # manager for families a bank: many large independent RIAs hold one to act
    # as trustee. A bank does.
    trust_only = _y(a, "Q6A8") and not _y(a, "Q6A7") and mix is not None and mix["indiv"] >= 0.5
    if (_y(a, "Q6A7") or _y(a, "Q6A8")) and not trust_only:
        what = "a bank" if _y(a, "Q6A7") else "a trust company"
        return done(Verdict("bank_trust", 92, [f"The firm is itself {what} (Item 6.A)"]),
                    [owner_note])
    if _y(a, "Q6A1"):
        ev = ["The firm is itself a registered broker-dealer (Item 6.A)"]
        staff, reps = _num(a, "Q5B1"), _num(a, "Q5B2")
        if reps:
            ev.append(f"{reps:,.0f} of its {staff:,.0f} advisory staff are registered reps "
                      f"(Item 5.B)")
        top = next(((m2, o) for m2, o in deciding
                     if m2.entity.category in ("wirehouse", "custodian", "bank_trust",
                                               "insurance")), None)
        if top:
            return done(Verdict(top[0].entity.category, 85, ev + [_owner_line(*top)]))
        return done(Verdict("independent_bd", 85, ev), [owner_note])

    # 5 Who controls it, for a firm serving people (or saying nothing about
    # its clients). A fund or institutional firm owned by an insurer is still
    # an asset manager; its owner is noted.
    if deciding and (mix is None or mix["indiv"] >= 0.35):
        m2, o = deciding[0]
        conf = 85 if o["code"] == "E" else 75
        if m2.generic:
            conf -= 10
        ev = [_owner_line(m2, o)]
        if mix:
            ev.append(f"{_pct(mix['indiv'])} {mix['of']} are individuals and families (Item 5.D)")
        return done(Verdict(m2.entity.category, conf, ev))

    # 6 Wealth managers
    if mix is None:
        return done(_services_only(d, a), [owner_note])
    if mix["indiv"] < 0.35:
        oth = a.get("Q5DN3Oth") or ""
        ev = [f"Individuals are only {_pct(mix['indiv'])} {mix['of']}; most clients are "
              f"'other'" + (f" ({oth.lower()[:60]})" if oth else "") + " (Item 5.D)"]
        if d.get("q7b") == "Y" or (d.get("funds") or {}).get("n"):
            return done(Verdict("private_fund", 55, ev), [_fund_line(d), owner_note])
        if _y(a, "Q5G1") or _y(a, "Q5G2"):
            return done(Verdict("independent_ria", 40, ev), [owner_note])
        return done(Verdict("asset_manager", 50, ev), [owner_note])
    return done(_wealth(d, a, mix, names_norm),
                ["Also acts as a trust company (Item 6.A)" if trust_only else None, owner_note])


def state_era(a: dict) -> str:
    """The states where a firm is an active exempt reporting adviser, when it
    holds no approved state registration; else ''."""
    era = [x.split(":")[0] for x in (a.get("_state_era") or "").split(",")
           if x.endswith(":ACTIVE")]
    if not era or "APPROVED" in (a.get("_state_rgstn") or ""):
        return ""
    return ", ".join(sorted(set(era))[:4])


def _services_only(d: dict, a: dict) -> Verdict:
    """No client breakdown at all (a new or planning-only firm): what it says
    it does is the only evidence, so confidence stays low."""
    none = "No assets or clients by type on file (Item 5.D)"
    if _y(a, "Q5G1") or _y(a, "Q5G2"):
        svc = "financial planning" if _y(a, "Q5G1") else "portfolio management for individuals"
        return Verdict("independent_ria", 55, [none, f"Offers {svc} (Item 5.G)"])
    if d.get("q7b") == "Y" or _y(a, "Q5G4"):
        return Verdict("private_fund", 55, [none, "Advises private funds or pooled vehicles "
                                                  "(Items 5.G, 7.B)"])
    if _y(a, "Q5G3"):
        return Verdict("asset_manager", 55, [none, "Manages registered funds (Item 5.G)"])
    if _y(a, "Q5G6") or _y(a, "Q2A7"):
        return Verdict("consultant_ocio", 50, [none, "Consults on pension plans (Items 2.A, 5.G)"])
    if _y(a, "Q5G5"):
        return Verdict("asset_manager", 45, [none, "Manages portfolios for institutions (Item 5.G)"])
    if any(_y(a, k) for k in ("Q5G8", "Q5G9", "Q5G10", "Q5G11")):
        return Verdict("unknown", 25, [none, "Publishes newsletters, ratings or seminars "
                                             "(Item 5.G)"])
    if "APPROVED" not in (a.get("_state_rgstn") or "") and "APPROVED" not in (a.get("_rgstn") or ""):
        return Verdict("unknown", 10, [none, "No approved registration in the current feed"])
    return Verdict("unknown", 15, [none])


def _wealth(d: dict, a: dict, mix: dict, names_norm: str) -> Verdict:
    """Step 6: which kind of wealth manager."""
    indiv, of = mix["indiv"], mix["of"]
    adjust = 0 if mix["basis"] == "assets" else -10
    who = (f"{_pct(indiv)} {of} are individuals and families, {_pct(mix['hnw'])} HNW "
           f"(Item 5.D)")
    clients = d.get("clients_total") or 0
    staff = _num(a, "Q5B1") or (d.get("iar_count") or 0)
    raum = d.get("raum") or 0
    per_staff = clients / staff if staff else None
    avg_acct = raum / clients if clients and raum else None
    if _y(a, "Q2A11"):
        return Verdict("robo", 85, ["Registers as an internet adviser (Item 2.A)", who])
    if per_staff and per_staff >= 3000 and avg_acct and avg_acct < 150_000:
        return Verdict("robo", 65, [f"{clients:,} clients for {staff:,.0f} advisory staff, "
                                    f"averaging {_money(avg_acct)} each", who])
    wrap = _num(a, "Q5I2A") + _num(a, "Q5I2B") + _num(a, "Q5I2C")
    wrap_share = wrap / raum if raum else 0
    if per_staff and per_staff >= 1000 and (wrap_share >= 0.5 or _y(a, "Q5G7")
                                            or mix["advisers"] >= 0.15):
        ev = [f"{clients:,} clients for {staff:,.0f} advisory staff, a platform's scale"]
        if wrap_share >= 0.2:
            ev.append(f"{_pct(wrap_share)} of assets are in wrap programs (Item 5.I)")
        elif _y(a, "Q5G7"):
            ev.append("Selects other advisers for clients (Item 5.G)")
        return Verdict("tamp", 75 if wrap_share >= 0.5 else 65, ev + [who])
    hnw_n = d.get("hnw_clients") or 0
    hnw_avg = (d.get("hnw_aum") or 0) / hnw_n if hnw_n else 0
    if FAMILY_OFFICE_RE.search(names_norm) and mix["hnw"] >= 0.3:
        return Verdict("multi_family_office", 85 + adjust, ["Calls itself a family office",
                                            f"HNW clients hold {_pct(mix['hnw'])} {of}"
                                            + (f", averaging {_money(hnw_avg)}" if hnw_avg else "")])
    if mix["hnw"] >= 0.6 and hnw_avg >= 20e6 and 5 <= hnw_n <= 600:
        return Verdict("multi_family_office", 65, [f"{hnw_n:,} HNW clients averaging {_money(hnw_avg)}, "
                                   f"{_pct(mix['hnw'])} of assets", who])
    reps = _num(a, "Q5B2")
    if reps >= 1 and staff and reps / staff >= 0.2:
        return Verdict("hybrid_ria", 85 + adjust,
                       [f"{reps:,.0f} of {staff:,.0f} advisory staff are registered reps of a "
                        f"broker-dealer (Item 5.B)", who])
    if _y(a, "Q6A2"):
        return Verdict("hybrid_ria", 80 + adjust,
                       ["The adviser is also a registered rep of a broker-dealer (Item 6.A)", who])
    if reps >= 1:
        return Verdict("hybrid_ria", 65 + adjust,
                       [f"{reps:,.0f} of {staff:,.0f} advisory staff are registered reps of a "
                        f"broker-dealer (Item 5.B)", who])
    if _y(a, "Q7A1"):
        return Verdict("hybrid_ria", 65 + adjust, ["Has a related broker-dealer (Item 7.A)", who])
    conf = 90 if indiv >= 0.7 else 80 if indiv >= 0.5 else 60
    ev = [who]
    if _y(a, "Q5G1"):
        ev.append("Offers financial planning (Item 5.G)")
    elif _y(a, "Q5G2"):
        ev.append("Manages portfolios for individuals (Item 5.G)")
    return Verdict("independent_ria", conf + adjust, ev)


# ------------------------------------------------------------------ persist

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _j(v) -> str:
    return json.dumps(v, separators=(",", ":"))


def _manual_evidence(row: dict, v: Verdict) -> list[str]:
    who = row.get("overridden_by") or "an admin"
    when = (row.get("overridden_at") or "")[:10]
    head = f"Set by {who}" + (f" on {when}" if when else "")
    if row.get("note"):
        head += f": {row['note']}"
    ev = [head]
    if v.category != row["category"]:
        ev.append(f"The rules say {label(v.category)} ({v.confidence}%): "
                  + (v.evidence[0] if v.evidence else ""))
    else:
        ev.extend(v.evidence[:2])
    return ev[:4]


def _merge(old: dict | None, v: Verdict, now: str) -> dict:
    """The row to store, given what is stored and what the rules say now. A
    person's choice is kept; an AI answer is kept while the rules remain
    unsure; otherwise the rules' answer is stored."""
    row = {"category": v.category, "confidence": v.confidence, "evidence": v.evidence,
           "signals": v.signals, "source": "rules", "rules_category": v.category,
           "rules_confidence": v.confidence, "rules_evidence": v.evidence,
           "note": None, "ai_hash": None, "overridden_by": None, "overridden_at": None}
    if old:
        row["ai_hash"] = old.get("ai_hash")
        if old["source"] == "manual":
            row.update(category=old["category"], confidence=100, source="manual",
                       note=old.get("note"), overridden_by=old.get("overridden_by"),
                       overridden_at=old.get("overridden_at"))
            row["evidence"] = _manual_evidence(old, v)
        elif old["source"] == "ai" and v.confidence < AI_BELOW:
            row.update(category=old["category"], confidence=old["confidence"], source="ai",
                       note=old.get("note"))
            row["evidence"] = ([f"Bellwether AI: {old.get('note') or 'read the filing summary'}"]
                               + v.evidence[:2])
    row["updated_at"] = now
    return row


COLS = ("crd", "category", "confidence", "evidence", "signals", "source", "rules_category",
        "rules_confidence", "rules_evidence", "note", "ai_hash", "updated_at",
        "overridden_by", "overridden_at")
UPSERT = (f"INSERT INTO firm_class ({', '.join(COLS)}) VALUES ({','.join('?' * len(COLS))})"
          " ON CONFLICT (crd) DO UPDATE SET "
          + ", ".join(f"{c}=excluded.{c}" for c in COLS[1:]))


def _values(crd: str, row: dict) -> tuple:
    out = []
    for c in COLS:
        v = crd if c == "crd" else row.get(c)
        if c in ("evidence", "signals", "rules_evidence"):
            v = _j(v or [])
        out.append(v)
    return tuple(out)


def _same(old: dict, row: dict) -> bool:
    for c in COLS[1:]:
        if c == "updated_at":
            continue
        a, b = old.get(c), row.get(c)
        if c in ("evidence", "signals", "rules_evidence"):
            b = _j(b or [])
        if (a if a is not None else None) != b:
            return False
    return True


def classify_all(conn, progress=None, kb=None) -> dict:
    """Classify every firm in firm_current and store the results. Returns
    counts: firms, rows written, firms whose type changed, and per category."""
    from . import knowledge
    init(conn)
    kb = kb or knowledge.matcher(conn, fresh=True)
    inputs = load_inputs(conn)
    existing = {r["crd"]: dict(r) for r in conn.execute("SELECT * FROM firm_class")}
    now = _now()
    writes, changed = [], 0
    by: dict[str, int] = {}
    for i, (crd, d) in enumerate(inputs.items(), 1):
        v = classify(d, kb)
        old = existing.get(crd)
        row = _merge(old, v, now)
        by[row["category"]] = by.get(row["category"], 0) + 1
        if old is None or old["category"] != row["category"]:
            changed += 1
        if old is None or not _same(old, row):
            writes.append(_values(crd, row))
        if progress and i % 10000 == 0:
            progress(i, len(inputs))
    for i in range(0, len(writes), 2000):
        conn.executemany(UPSERT, writes[i:i + 2000])
    gone = [c for c in existing if c not in inputs and existing[c]["source"] != "manual"]
    for i in range(0, len(gone), 500):
        part = gone[i:i + 500]
        conn.execute(f"DELETE FROM firm_class WHERE crd IN ({','.join('?' * len(part))})",
                     tuple(part))
    conn.commit()
    if progress:
        progress(len(inputs), len(inputs))
    return {"firms": len(inputs), "written": len(writes), "changed": changed,
            "removed": len(gone), "by_category": by}


def classify_one(conn, crd: str, store: bool = False) -> Verdict | None:
    """The rules' answer for one firm, optionally stored (respecting a
    person's choice and an AI answer like classify_all does)."""
    from . import knowledge
    d = load_inputs(conn, [crd]).get(crd)
    if d is None:
        return None
    v = classify(d, knowledge.matcher(conn))
    if store:
        old = conn.execute("SELECT * FROM firm_class WHERE crd=?", (crd,)).fetchone()
        conn.execute(UPSERT, _values(crd, _merge(dict(old) if old else None, v, _now())))
        conn.commit()
    return v


# ------------------------------------------------------------------- read

def _shape(r) -> dict:
    d = dict(r)
    for c in ("evidence", "signals", "rules_evidence"):
        try:
            d[c] = json.loads(d.get(c) or "[]")
        except ValueError:
            d[c] = []
    cat = next((c for c in categories() if c["key"] == d["category"]), None) or {}
    d["label"] = cat.get("label") or d["category"]
    d["short"] = cat.get("short") or d["label"]
    d["treatment"] = cat.get("treatment") or ""
    d["core"] = d["category"] in WEALTH
    d["signal_labels"] = [SIGNALS[s] for s in d.get("signals") or [] if s in SIGNALS]
    d["source_label"] = SOURCES.get(d.get("source"), d.get("source"))
    return d


def get(conn, crd: str) -> dict | None:
    """One firm's type, ready to show: category, label, short, confidence,
    evidence (list), signals and signal_labels, source and source_label,
    treatment, core (a wealth manager), overridden_by, note, rules_category.
    None when the firm has not been classified yet."""
    try:
        r = conn.execute("SELECT * FROM firm_class WHERE crd=?", (crd,)).fetchone()
    except Exception:
        conn.rollback()
        return None
    return _shape(r) if r else None


def get_many(conn, crds) -> dict[str, dict]:
    """get() for many firms at once (a list page's badges): crd -> dict."""
    crds = [c for c in dict.fromkeys(crds or []) if c]
    out: dict[str, dict] = {}
    for i in range(0, len(crds), 1000):
        part = crds[i:i + 1000]
        for r in _safe(conn, f"SELECT * FROM firm_class WHERE crd IN"
                             f" ({','.join('?' * len(part))})", tuple(part)):
            out[r["crd"]] = _shape(r)
    return out


def counts(conn) -> dict[str, dict]:
    """Per category: firms, SEC-registered firms, firms set by hand, average
    confidence."""
    out = {}
    for r in _safe(conn, """SELECT c.category, COUNT(*) n,
            COUNT(*) FILTER (WHERE f.regulator='SEC' AND COALESCE(f.is_era,0)=0) sec,
            COUNT(*) FILTER (WHERE c.source='manual') manual,
            COUNT(*) FILTER (WHERE c.source='ai') ai,
            AVG(c.confidence) conf
            FROM firm_class c LEFT JOIN firm_current f ON f.crd=c.crd
            GROUP BY c.category"""):
        out[r["category"]] = {"n": r["n"], "sec": r["sec"], "manual": r["manual"],
                              "ai": r["ai"], "conf": float(r["conf"] or 0)}
    return out


def overrides(conn) -> list[dict]:
    rows = _safe(conn, """SELECT c.*, f.legal_name FROM firm_class c
        LEFT JOIN firm_current f ON f.crd=c.crd WHERE c.source='manual'
        ORDER BY c.overridden_at DESC NULLS LAST""")
    return [_shape(r) for r in rows]


# ------------------------------------------------------------------ write

def set_override(conn, crd: str, category: str | None, who: str, note: str = "") -> dict | None:
    """A person's choice of type for one firm, or with category None (or '')
    a return to what the rules say. Survives every reclassification. The
    firm's product scores are recomputed at once, since a type can put a firm
    on a list or take it off."""
    crd = (crd or "").strip()
    if not conn.execute("SELECT 1 FROM firm_current WHERE crd=?", (crd,)).fetchone():
        raise ValueError("No firm with that CRD.")
    now = _now()
    old = conn.execute("SELECT * FROM firm_class WHERE crd=?", (crd,)).fetchone()
    old = dict(old) if old else None
    v = classify_one(conn, crd) or Verdict("unknown", 0, ["Not classified yet"])
    if category:
        if category not in ASSIGNABLE:
            raise ValueError("Choose one of the firm types.")
        base = {"category": category, "source": "manual", "note": (note or "").strip()[:300] or None,
                "overridden_by": (who or "admin")[:80], "overridden_at": now}
        row = _merge(base, v, now)
    else:
        row = _merge(None, v, now)
        if old and old.get("ai_hash"):
            row["ai_hash"] = old["ai_hash"]
    conn.execute(UPSERT, _values(crd, row))
    conn.commit()
    try:
        from . import products
        products.rescore_firm(conn, crd)
    except Exception:
        conn.rollback()
    return get(conn, crd)


def search(conn, q: str, limit: int = 25) -> list[dict]:
    """Firms by name or CRD with their type, for the override screen."""
    q = (q or "").strip()
    if not q:
        return []
    rows = _safe(conn, """SELECT f.crd, f.legal_name, f.business_name, f.city, f.state,
            f.raum, f.regulator, f.is_era FROM firm_current f
            WHERE f.crd = ? OR f.legal_name ILIKE ? OR f.business_name ILIKE ?
            ORDER BY (f.crd = ?) DESC, f.raum DESC NULLS LAST LIMIT ?""",
                 (q, f"%{q}%", f"%{q}%", q, limit))
    types = get_many(conn, [r["crd"] for r in rows])
    return [dict(r, firm_class=types.get(r["crd"])) for r in rows]


def firms_in(conn, category: str, limit: int = 50) -> list[dict]:
    """The largest firms of one type, for spot checks in Settings."""
    rows = _safe(conn, """SELECT f.crd, f.legal_name, f.city, f.state, f.raum, c.confidence,
            c.source, c.evidence FROM firm_class c JOIN firm_current f ON f.crd=c.crd
            WHERE c.category=? ORDER BY f.raum DESC NULLS LAST LIMIT ?""", (category, limit))
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["evidence"] = json.loads(d["evidence"] or "[]")
        except ValueError:
            d["evidence"] = []
        out.append(d)
    return out


# --------------------------------------------------------------------- AI

AI_SYSTEM = ("You classify US investment advisers for a sales team. Choose the one firm "
             "type that best fits the filing summary. Answer only from the summary; when it "
             "does not settle the question, answer unknown with low confidence.")


def _ai_schema() -> dict:
    return {"type": "object",
            "properties": {"category": {"type": "string", "enum": KEYS},
                           "confidence": {"type": "integer"},
                           "reason": {"type": "string"}},
            "required": ["category", "confidence", "reason"], "additionalProperties": False}


def ai_summary(d: dict, v: Verdict) -> str:
    """The facts the AI reads: names, size, client mix, services, other
    businesses, owners and what the rules saw. No contact data."""
    a = d.get("answers") or {}
    mix = client_mix(a)
    L = [f"Firm: {d.get('legal_name')}" + (f" (doing business as {d['business_name']})"
                                           if d.get("business_name") and d["business_name"] != d.get("legal_name") else ""),
         f"Registered with: {d.get('regulator')}; assets {_money(d.get('raum'))}; "
         f"clients {d.get('clients_total') or 0}; advisory staff {_num(a, 'Q5B1'):.0f}; "
         f"of whom broker-dealer reps {_num(a, 'Q5B2'):.0f}, insurance agents {_num(a, 'Q5B5'):.0f}"]
    if mix:
        L.append(f"Client mix ({mix['basis']}): individuals {_pct(mix['retail'])}, HNW "
                 f"{_pct(mix['hnw'])}, registered funds {_pct(mix['regfund'])}, pooled vehicles "
                 f"{_pct(mix['pooled'])}, institutions {_pct(mix['inst'])}, other advisers "
                 f"{_pct(mix['advisers'])}, other {_pct(mix['other'])}"
                 + (f" ({a.get('Q5DN3Oth')})" if a.get("Q5DN3Oth") else ""))
    svc = {"Q5G1": "financial planning", "Q5G2": "portfolios for individuals",
           "Q5G3": "portfolios for registered funds", "Q5G4": "pooled vehicles",
           "Q5G5": "portfolios for institutions", "Q5G6": "pension consulting",
           "Q5G7": "selecting other advisers", "Q5G8": "newsletters",
           "Q5G9": "security ratings", "Q5G11": "seminars"}
    L.append("Services: " + (", ".join(v2 for k, v2 in svc.items() if _y(a, k)) or "none stated")
             + (f"; other: {a.get('Q5G12Oth')}" if a.get("Q5G12Oth") else ""))
    biz = {"Q6A1": "broker-dealer", "Q6A2": "registered rep", "Q6A3": "commodity pool operator",
           "Q6A5": "real estate broker", "Q6A6": "insurance agent", "Q6A7": "bank",
           "Q6A8": "trust company", "Q6A12": "accountant", "Q6A13": "lawyer"}
    other = [v2 for k, v2 in biz.items() if _y(a, k)]
    if other:
        L.append("The firm itself is also: " + ", ".join(other))
    owners = d.get("owners") or []
    if owners:
        L.append("Controlling owners: " + "; ".join(
            f"{o['name']} ({CODE_WORDS.get(o['code'], o['code'])})" for o in owners[:4]))
    line = _fund_line(d)
    if line:
        L.append(line)
    if d.get("website"):
        L.append(f"Website: {d['website']}")
    L.append(f"Rules' best guess: {v.category} at {v.confidence}%: " + "; ".join(v.evidence[:3]))
    L.append("Firm types: " + "; ".join(f"{c['key']} = {c['label']}: {c['description'][:110]}"
                                        for c in categories()))
    return "\n".join(L)


def ai_pass(conn, limit: int = 40, who: str = "classify job", log=print) -> int:
    """Ask Bellwether AI about the firms the rules could not settle, best
    prospects first. Skips a firm whose facts it has already read, never
    touches a person's choice, and stops at the daily AI limit. Returns how
    many firms it classified."""
    from . import ai, knowledge
    if limit <= 0 or not ai.enabled("clean"):
        return 0
    rows = _safe(conn, """SELECT c.crd, c.ai_hash FROM firm_class c
        JOIN firm_current f ON f.crd=c.crd LEFT JOIN firm_scope s ON s.crd=c.crd
        WHERE c.source='rules' AND c.confidence < ? AND COALESCE(f.is_era,0)=0
        ORDER BY s.priority DESC NULLS LAST, f.raum DESC NULLS LAST LIMIT ?""",
                 (AI_BELOW, limit * 3))
    kb = knowledge.matcher(conn)
    done = 0
    for r in rows:
        if done >= limit or not ai.enabled("clean"):
            break
        d = load_inputs(conn, [r["crd"]]).get(r["crd"])
        if d is None:
            continue
        v = classify(d, kb)
        text = ai_summary(d, v)
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        if r["ai_hash"] == h:
            continue
        try:
            out = ai.complete(AI_SYSTEM, [{"role": "user", "content": text}], feature="clean",
                              tier="fast", schema=_ai_schema(), max_tokens=400, who=who)
        except Exception as e:
            log(f"  AI stopped: {e}")
            break
        cat = out.get("category") if isinstance(out, dict) else None
        reason = str((out or {}).get("reason") or "")[:240]
        try:
            conf = max(0, min(70, int((out or {}).get("confidence") or 0)))
        except (TypeError, ValueError):
            conf = 0
        if cat in ASSIGNABLE and conf >= 40:
            conn.execute("UPDATE firm_class SET category=?, confidence=?, source='ai', note=?,"
                         " ai_hash=?, evidence=?, updated_at=? WHERE crd=? AND source='rules'",
                         (cat, conf, reason, h, _j([f"Bellwether AI: {reason}"] + v.evidence[:2]),
                          _now(), r["crd"]))
            done += 1
        else:
            conn.execute("UPDATE firm_class SET ai_hash=? WHERE crd=?", (h, r["crd"]))
        conn.commit()
    return done


# ------------------------------------------------------------------ help

RULES_SUMMARY = [
    ("Exempt reporting advisers", "The SEC feed says the firm is exempt, or a state feed shows an "
                                  "active exemption and no approved registration."),
    ("Known names", "The firm's own name matches a known entity in Industry knowledge "
                    "(Charles Schwab, PIMCO, LPL ...). The longest pattern wins. A group's fund "
                    "or institutional arm is classed by what it does, except custodians."),
    ("Who its clients are", "Item 5.D, when individuals are under half: half or more in pooled "
                            "vehicles is a private fund manager; 30%+ in registered funds an "
                            "asset manager; half or more institutions an asset manager, or a "
                            "consultant / OCIO when it consults for pension plans, endowments "
                            "and governments; 40%+ from other advisers a sub-adviser (asset "
                            "manager)."),
    ("What the firm itself is", "Item 6.A: the firm is itself a bank, a trust company or a "
                                "broker-dealer. A broker-dealer controlled by a wirehouse, "
                                "custodian, bank or insurer takes that type."),
    ("Who controls it", "Schedule A: a firm serving people that is 50%+ owned by a custodian, "
                        "wirehouse, broker-dealer, bank or insurer takes that type. Owners that "
                        "are RIA groups or asset managers are noted but change nothing."),
    ("Wealth managers", "Internet adviser, or 3,000+ small accounts per advisory employee: "
                        "digital adviser. 1,000+ accounts per employee in wrap programs or "
                        "chosen by other advisers: TAMP. A family office by name, or HNW clients "
                        "averaging $20M+: multi-family office. Advisors who are broker-dealer "
                        "reps, or an affiliated broker-dealer: hybrid RIA. Otherwise "
                        "independent RIA."),
    ("Nothing to tell", "No client breakdown: judged by the services listed, at low "
                        "confidence, or not classified. Neither excludes a firm from any list."),
    ("People and AI", "A type set by hand always wins and survives reclassification. Bellwether "
                      "AI may settle firms the rules leave under 50% confidence, within the "
                      "daily AI limit, and never over a person's choice."),
]
