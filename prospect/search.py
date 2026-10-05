"""Find firms by any combination of what Bellwether knows about them.

One query builder for every place that filters firms by more than a name: the
Bellwether AI questions ("RIAs in Texas on Black Diamond that hired this
year"), and anything else that wants the same reach. A model never writes SQL
here: it fills in the filter spec below, every value is bound as a parameter,
and every column name comes from this file.
"""

from __future__ import annotations

from . import products

SORTS = {
    "score": "score DESC NULLS LAST, f.raum DESC NULLS LAST",
    "aum": "f.raum DESC NULLS LAST",
    "hires": "hires_12m DESC NULLS LAST, f.raum DESC NULLS LAST",
    "growth": "net_12m DESC NULLS LAST, f.raum DESC NULLS LAST",
    "advisors": "f.iar_count DESC NULLS LAST",
    "signal": "last_signal DESC NULLS LAST",
    "name": "f.legal_name",
}

PLATFORM_SIGNAL = {"Black Diamond": "platform_black_diamond", "Orion": "platform_orion",
                   "Tamarac": "platform_tamarac", "Addepar": "platform_addepar",
                   "Advyzon": "platform_advyzon"}


def _nullable(t: dict) -> dict:
    return {"anyOf": [t, {"type": "null"}]}


def filter_schema() -> dict:
    """The JSON schema the AI planner fills in. Every field is required and
    nullable, which strict structured output needs."""
    props = {
        "product": _nullable({"type": "string", "enum": products.product_keys()}),
        "min_score": _nullable({"type": "number"}),
        "min_coverage": _nullable({"type": "number"}),
        "states": _nullable({"type": "array", "items": {"type": "string"}}),
        "city": _nullable({"type": "string"}),
        "name": _nullable({"type": "string"}),
        "min_aum": _nullable({"type": "number"}),
        "max_aum": _nullable({"type": "number"}),
        "min_hnw_share": _nullable({"type": "number"}),
        "min_advisors": _nullable({"type": "integer"}),
        "max_advisors": _nullable({"type": "integer"}),
        "mail_platform": _nullable({"type": "string", "enum": ["m365", "google", "other"]}),
        "reporting_platform": _nullable({"type": "string", "enum": list(PLATFORM_SIGNAL)}),
        "custodian": _nullable({"type": "string"}),
        "brochure_tags": _nullable({"type": "array", "items": {"type": "string"}}),
        "has_personal_email": _nullable({"type": "boolean"}),
        "has_verified_email": _nullable({"type": "boolean"}),
        "min_hires_12m": _nullable({"type": "integer"}),
        "min_departures_12m": _nullable({"type": "integer"}),
        "signal_types": _nullable({"type": "array", "items": {"type": "string"}}),
        "signal_days": _nullable({"type": "integer"}),
        "registration": _nullable({"type": "string", "enum": ["SEC", "STATE"]}),
        "private_funds": _nullable({"type": "boolean"}),
        "files_13f": _nullable({"type": "boolean"}),
        "owner": _nullable({"type": "string"}),
        "status": _nullable({"type": "string"}),
    }
    return {"type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


def _has_table(c, name: str) -> bool:
    try:
        r = c.execute("SELECT to_regclass(?) t", (name,)).fetchone()
        return bool(r and r["t"])
    except Exception:
        c.rollback()
        return False


def run(c, spec: dict, sort: str = "score", limit: int = 25,
        me: str = "") -> tuple[list[dict], int, list[str]]:
    """(rows, total matching, plain-English description of the filters)."""
    spec = {k: v for k, v in (spec or {}).items() if v not in (None, "", [])}
    where, args, said = ["f.is_era = 0"], [], []
    product = spec.get("product") if spec.get("product") in products.product_keys() else None
    people = _has_table(c, "firm_people_stats")

    if product:
        p = products.product(product)
        said.append(f"on the {p['name']} list")
        where.append("p.status = 'scored'")
        if spec.get("min_score") is not None:
            where.append("p.score >= ?")
            args.append(float(spec["min_score"]))
            said.append(f"scoring {float(spec['min_score']):.0f}+")
        if spec.get("min_coverage") is not None:
            where.append("p.coverage >= ?")
            args.append(float(spec["min_coverage"]))
            said.append(f"with {float(spec['min_coverage']):.0f}%+ of the score on known data")
    if spec.get("states"):
        st = [s.strip().upper()[:2] for s in spec["states"] if s and s.strip()]
        if st:
            where.append(f"f.state IN ({','.join('?' * len(st))})")
            args += st
            said.append("in " + ", ".join(st))
    if spec.get("city"):
        where.append("f.city ILIKE ?")
        args.append(f"%{spec['city']}%")
        said.append(f"in {spec['city']}")
    if spec.get("name"):
        where.append("(f.legal_name ILIKE ? OR f.business_name ILIKE ?)")
        args += [f"%{spec['name']}%"] * 2
        said.append(f"named like {spec['name']}")
    if spec.get("min_aum") is not None:
        where.append("f.raum >= ?")
        args.append(float(spec["min_aum"]))
        said.append(f"with {_money(spec['min_aum'])}+ in assets")
    if spec.get("max_aum") is not None:
        where.append("f.raum <= ?")
        args.append(float(spec["max_aum"]))
        said.append(f"under {_money(spec['max_aum'])}")
    if spec.get("min_hnw_share") is not None:
        v = float(spec["min_hnw_share"])
        v = v / 100 if v > 1 else v
        where.append("f.raum > 0 AND COALESCE(f.hnw_aum,0)::float / f.raum >= ?")
        args.append(v)
        said.append(f"HNW {v * 100:.0f}%+ of assets")
    if spec.get("min_advisors") is not None:
        where.append("f.iar_count >= ?")
        args.append(int(spec["min_advisors"]))
        said.append(f"{int(spec['min_advisors'])}+ advisors")
    if spec.get("max_advisors") is not None:
        where.append("f.iar_count <= ?")
        args.append(int(spec["max_advisors"]))
        said.append(f"at most {int(spec['max_advisors'])} advisors")
    if spec.get("mail_platform"):
        where.append("EXISTS (SELECT 1 FROM firm_mail_platform m WHERE m.crd=f.crd AND m.platform=?)")
        args.append(spec["mail_platform"])
        said.append({"m365": "on Microsoft 365", "google": "on Google Workspace",
                     "other": "on another mail provider"}[spec["mail_platform"]])
    if spec.get("reporting_platform") in PLATFORM_SIGNAL:
        sig = PLATFORM_SIGNAL[spec["reporting_platform"]]
        where.append("(EXISTS (SELECT 1 FROM web_signal w WHERE w.crd=f.crd AND w.signal=?)"
                     " OR EXISTS (SELECT 1 FROM brochure_tag b WHERE b.crd=f.crd AND b.tag=?"
                     " AND b.present=1))")
        args += [sig, sig]
        said.append(f"using {spec['reporting_platform']}")
    if spec.get("custodian"):
        where.append("EXISTS (SELECT 1 FROM firm_custodian_profile cp WHERE cp.crd=f.crd"
                     " AND cp.primary_canonical ILIKE ?)")
        args.append(f"%{spec['custodian']}%")
        said.append(f"custodying at {spec['custodian']}")
    for tag in spec.get("brochure_tags") or []:
        where.append("EXISTS (SELECT 1 FROM brochure_tag b WHERE b.crd=f.crd AND b.tag=?"
                     " AND b.present=1)")
        args.append(tag)
        said.append(f"brochure mentions {products.tag_label(tag).lower()}")
    if spec.get("has_personal_email"):
        where.append("EXISTS (SELECT 1 FROM usable_contact_point x WHERE x.crd=f.crd AND x.kind='email'"
                     " AND x.person_key != '' AND x.is_role=0 AND x.confidence >= 60"
                     " AND x.verify_status NOT IN ('invalid','no_mail_server'))")
        said.append("with a named person's email")
    if spec.get("has_verified_email"):
        where.append("EXISTS (SELECT 1 FROM usable_contact_point x WHERE x.crd=f.crd AND x.kind='email'"
                     " AND x.verify_status='valid')")
        said.append("with a verified email")
    if people and spec.get("min_hires_12m") is not None:
        where.append("ps.hires_12m >= ?")
        args.append(int(spec["min_hires_12m"]))
        said.append(f"that hired {int(spec['min_hires_12m'])}+ in 12 months")
    if people and spec.get("min_departures_12m") is not None:
        where.append("ps.departures_12m >= ?")
        args.append(int(spec["min_departures_12m"]))
        said.append(f"that lost {int(spec['min_departures_12m'])}+ in 12 months")
    if spec.get("signal_types") or spec.get("signal_days"):
        days = int(spec.get("signal_days") or 90)
        cond = "t.detected_date >= (CURRENT_DATE - ?::int)::text"
        targs: list = [days]
        if spec.get("signal_types"):
            cond += f" AND t.trigger_type IN ({','.join('?' * len(spec['signal_types']))})"
            targs += list(spec["signal_types"])
        where.append(f"EXISTS (SELECT 1 FROM trigger_event t WHERE t.crd=f.crd"
                     f" AND t.suppressed=0 AND {cond})")
        args += targs
        said.append(f"with a signal in the last {days} days")
    if spec.get("registration") in ("SEC", "STATE"):
        where.append("f.regulator = ?")
        args.append(spec["registration"])
        said.append("SEC-registered" if spec["registration"] == "SEC" else "state-registered")
    if spec.get("private_funds") is not None:
        where.append("f.q7b = ?")
        args.append("Y" if spec["private_funds"] else "N")
        said.append("advising private funds" if spec["private_funds"] else "with no private funds")
    if spec.get("files_13f") is not None:
        cond = ("EXISTS (SELECT 1 FROM adv_13f_match a WHERE a.crd=f.crd"
                " AND a.status IN ('auto','confirmed'))")
        where.append(cond if spec["files_13f"] else f"NOT {cond}")
        said.append("filing 13F" if spec["files_13f"] else "not filing 13F")
    if spec.get("owner"):
        o = spec["owner"]
        if o.lower() == "me" and me:
            where.append("s.owner = ?")
            args.append(me)
            said.append("owned by you")
        elif o.lower() in ("unclaimed", "nobody", "none"):
            where.append("(s.owner IS NULL OR s.owner = '')")
            said.append("unclaimed")
        else:
            where.append("s.owner ILIKE ?")
            args.append(f"%{o}%")
            said.append(f"owned by {o}")
    if spec.get("status"):
        where.append("s.status = ?")
        args.append(spec["status"])
        said.append(f"status {spec['status']}")

    score_col = "p.score" if product else "sc.best_score"
    cov_col = "p.coverage" if product else "sc.best_coverage"
    join_p = (f"JOIN product_score p ON p.crd=f.crd AND p.product='{product}'"
              if product else "")
    join_ps = "LEFT JOIN firm_people_stats ps ON ps.crd=f.crd" if people else ""
    ps_cols = ("ps.headcount, ps.hires_12m, ps.departures_12m, ps.net_12m"
               if people else "NULL headcount, NULL hires_12m, NULL departures_12m, NULL net_12m")
    base = f"""FROM firm_current f {join_p}
        LEFT JOIN firm_scope sc ON sc.crd=f.crd
        LEFT JOIN firm_status s ON s.crd=f.crd
        {join_ps}
        WHERE {' AND '.join(where)}"""
    try:
        total = c.execute(f"SELECT COUNT(*) n {base}", args).fetchone()["n"]
        order = SORTS.get(sort, SORTS["score"])
        if sort in ("hires", "growth") and not people:
            order = SORTS["score"]
        rows = c.execute(f"""
            SELECT f.crd, f.legal_name, f.city, f.state, f.raum, f.iar_count,
                   {score_col} AS score, {cov_col} AS coverage, sc.best_product,
                   s.owner, s.status, {ps_cols},
                   (SELECT MAX(t.detected_date) FROM trigger_event t WHERE t.crd=f.crd
                     AND t.suppressed=0) AS last_signal
            {base} ORDER BY {order} LIMIT ?""", args + [max(1, min(int(limit), 200))]).fetchall()
    except Exception:
        c.rollback()
        raise
    return [dict(r) for r in rows], int(total), said


def _money(v) -> str:
    v = float(v)
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"
