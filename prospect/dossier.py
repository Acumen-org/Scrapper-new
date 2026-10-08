"""Everything Bellwether knows about one firm, as compact plain text.

This is what Bellwether AI reads before it answers a question about a firm or
writes a brief: the filing facts, the scores with what is missing, the people
and who joined or left, every way to reach them and how sure we are, what
changed, what the firm says about itself, and what the team has noted. Each
section is labelled so an answer can say where a fact came from, and so a
model is never asked to remember anything it was not shown.

Built from the same tables the firm page reads; no model is involved here.
"""

from __future__ import annotations

import hashlib
import json

from . import products
from .names import nice_name


def _money(v) -> str:
    if v is None:
        return "unknown"
    v = float(v)
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.0f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}K"
    return f"${v:,.0f}"


def _rows(c, sql, args=()):
    try:
        return c.execute(sql, args).fetchall()
    except Exception:
        c.rollback()
        return []


def build(c, crd: str, max_people: int = 40) -> str:
    f = _rows(c, "SELECT * FROM firm_current WHERE crd=?", (crd,))
    if not f:
        return ""
    f = f[0]
    L: list[str] = []
    name = nice_name(f["legal_name"])
    L.append(f"FIRM: {name} (CRD {crd})")
    if f["business_name"] and f["business_name"] != f["legal_name"]:
        L.append(f"Also known as: {nice_name(f['business_name'])}")
    L.append(f"Location: {nice_name(f['city'] or '')}, {f['state'] or ''} {f['country'] or ''}".strip())
    L.append(f"Registration: {f['firm_type'] or ''} with {f['regulator'] or ''}, since "
             f"{f['registered_date'] or 'unknown'}; last ADV filed {f['filing_date'] or 'unknown'}")
    L.append(f"Website: {f['website'] or 'none on file'}; main phone: {f['phone'] or 'none'}")
    hs = (f["hnw_aum"] or 0) / f["raum"] * 100 if f["raum"] else 0
    L.append(f"Assets (RAUM): {_money(f['raum'])}; discretionary {_money(f['raum_disc'])}")
    L.append(f"Clients: {f['clients_total'] or 0} total; HNW {f['hnw_clients'] or 0} holding "
             f"{_money(f['hnw_aum'])} ({hs:.0f}% of assets); other individuals "
             f"{f['retail_clients'] or 0} holding {_money(f['retail_aum'])}")
    L.append(f"Advisors (Item 5.B): {f['iar_count'] or 0}; employees {f['total_employees'] or 0}")
    if f["disciplinary"] == "Y":
        L.append("Disclosures: the firm reports a disciplinary event (Item 11)")
    L.extend(_firm_type(c, crd))

    hist = _rows(c, """SELECT filing_date, raum FROM firm_history WHERE crd=? AND raum IS NOT NULL
                       ORDER BY filing_date""", (crd,))
    if len(hist) >= 2:
        a, b = hist[0], hist[-1]
        L.append(f"Asset history: {_money(a['raum'])} in {a['filing_date'][:4]} to "
                 f"{_money(b['raum'])} in {b['filing_date'][:4]} across {len(hist)} filings")

    feats = products.load_features(c, [crd]).get(crd)
    if feats:
        res = products.evaluate_all(feats)
        L.append("")
        L.append("PRODUCT FIT (score out of 100 earned on known data; coverage = share of "
                 "the scoring backed by data)")
        for r in sorted(res.values(), key=lambda r: -r.score):
            p = products.product(r.product)
            if r.status != "scored":
                L.append(f"- {p['name']}: not on the list ({r.reason})")
                continue
            L.append(f"- {p['name']}: score {r.score:.0f}, coverage {r.coverage:.0f}%, "
                     f"could reach {r.potential:.0f}")
            for comp in sorted(r.components, key=lambda x: -x["weight"]):
                tag = "" if comp["known"] else " [MISSING DATA]"
                L.append(f"    {comp['label']} (weight {comp['weight']:g}): "
                         f"{comp['points']:.0f} pts{tag}. {comp['evidence']}")
        mail = feats.get("mail") or {}
        if mail:
            L.append(f"Email platform: {mail.get('platform')} ({mail.get('evidence')})")
        systems = products.glynac_systems(feats)
        L.append("Systems Glynac works with, as seen: "
                 + ("; ".join(f"{k}: {', '.join(v)}" for k, v in systems.items())
                    if systems else "none confirmed yet (unknown, not absent)"))
        plats = products.platform_evidence(feats)
        if plats:
            L.append("Reporting platform: " + "; ".join(f"{k} ({v})" for k, v in plats.items()))
        cust = feats.get("cust") or {}
        if cust:
            L.append(f"Primary custodian: {cust.get('primary_canonical')}; Schwab share of "
                     f"reported custody {cust.get('schwab_share_reported')}; as of "
                     f"{cust.get('as_of_filing_date')}")
        if feats.get("funds"):
            fu = feats["funds"]
            L.append(f"Private funds: {fu.get('n')} ({fu.get('types')}), gross assets "
                     f"{_money(fu.get('gav'))}, as of {fu.get('as_of')}")
        tps = []
        for fam in ("phh", "acubooth", "glynac"):
            tps += products.talking_points(feats, fam)
        if tps:
            L.append("")
            L.append("IN THEIR OWN WORDS (brochure phrases and 13F holdings)")
            for tp in tps[:12]:
                L.append(f"- {tp['label']}: {tp['text'][:220]}")

    # People
    try:
        from . import people
        roster = people.roster(c, crd)
        st = people.stats(c, crd)
        mv = people.movements(c, crd, days=730)
    except Exception:
        c.rollback()
        roster, st, mv = [], None, {"joined": [], "left": []}
    if roster or st:
        L.append("")
        L.append("PEOPLE (SEC individual registrations and Schedule A)")
        if st:
            L.append(f"Headcount on IAPD: {st.get('headcount')}; joined in 12 months "
                     f"{st.get('hires_12m')}, left {st.get('departures_12m')}; average tenure "
                     f"{(st.get('avg_tenure_years') or 0):.1f} years; CFPs {st.get('cfp_count')}")
        for p in roster[:max_people]:
            bits = [p["name"]]
            if p.get("title"):
                bits.append(p["title"])
            if p.get("since"):
                bits.append(f"since {p['since'][:4]}")
            if p.get("prior_firm"):
                bits.append(f"previously {nice_name(p['prior_firm'])}")
            if p.get("designations"):
                bits.append("/".join(p["designations"][:3]))
            if p.get("has_disclosure"):
                bits.append("has a disclosure on record")
            L.append("- " + ", ".join(bits))
        for kind, label in (("joined", "Joined"), ("left", "Left")):
            for m in mv.get(kind, [])[:8]:
                other = nice_name(m.get("other_org_name") or "")
                arrow = "from" if kind == "joined" else "to"
                L.append(f"{label} {m['date']}: {m['name']}"
                         + (f" ({arrow} {other})" if other else ""))
    else:
        offs = _rows(c, """SELECT name, title FROM schedule_a WHERE crd=? AND is_individual=1
                           ORDER BY name LIMIT 25""", (crd,))
        if offs:
            L.append("")
            L.append("OFFICERS (Schedule A)")
            for o in offs:
                L.append(f"- {o['name']}: {o['title'] or ''}")

    # Contacts
    cps = _rows(c, """SELECT person_name, title, kind, value, label, is_role, source,
                             confidence, verify_status FROM usable_contact_point WHERE crd=?
                      ORDER BY (person_key=''), confidence DESC LIMIT 60""", (crd,))
    if cps:
        L.append("")
        L.append("CONTACT DETAILS (source; confidence 0-100; verification)")
        for r in cps:
            who = r["person_name"] or ("shared inbox" if r["is_role"] else "firm")
            L.append(f"- {who}: {r['kind']} {r['value']} ({r['source']}, {r['confidence']}, "
                     f"{r['verify_status']})")

    trig = _rows(c, """SELECT detected_date, trigger_type, description FROM trigger_event
                       WHERE crd=? AND suppressed=0 ORDER BY detected_date DESC LIMIT 12""",
                 (crd,))
    if trig:
        L.append("")
        L.append("WHAT CHANGED (signals)")
        for t in trig:
            L.append(f"- {t['detected_date']}: {t['description']}")

    st = _rows(c, "SELECT status, owner FROM firm_status WHERE crd=?", (crd,))
    note = _rows(c, "SELECT note FROM firm_note WHERE crd=?", (crd,))
    if st or note:
        L.append("")
        L.append("OUR TEAM'S NOTES")
        if st:
            L.append(f"Status: {st[0]['status'] or 'not set'}; owner: {st[0]['owner'] or 'nobody'}")
        if note and note[0]["note"]:
            L.append(f"Note: {note[0]['note'][:1200]}")
    return "\n".join(L)


def _firm_type(c, crd: str) -> list[str]:
    """What kind of firm this is, how sure Bellwether is and why, and what that
    means for Acumen's products."""
    try:
        from . import firmtype
        t = firmtype.get(c, crd)
    except Exception:
        c.rollback()
        return []
    if not t:
        return ["Firm type: not classified yet"]
    how = {"manual": "set by hand", "ai": "Bellwether AI", "rules": "rules"}.get(
        t.get("source"), t.get("source"))
    out = [f"Firm type: {t['label']} ({t['confidence']}% confident, {how}). Why: "
           + "; ".join(t.get("evidence") or [])[:420]]
    if t.get("treatment"):
        out.append(f"What the type means for Acumen: {t['treatment']}")
    if t.get("signal_labels"):
        out.append("Traits: " + ", ".join(t["signal_labels"]))
    return out


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def compact_json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)
