"""Growth and hiring need: is a firm growing, and where will it need people?

The evidence behind the firm page's Growth tab and the ATS product list
(Acumen Talent Solutions), built for every firm by scripts/build_growth.py,
the "growth" job:

  team        registered people now and a year ago, who joined and who left,
              from the SEC's record of every registered person
  business    assets, clients, total staff and advisory staff at the latest
              filing against the filing about a year before it (Form ADV
              Items 5.F, 5.C(1), 5.A and 5.B(1); the record goes back to 2011)
  capacity    assets and clients per advisor, and support staff per advisor
              against firms of a similar size
  backfill    who left in the last year, how long they had been at the firm and
              how long in the industry
  succession  people with 30 or more years in the industry
  expansion   new branch cities, new state filings, a recent registration,
              several people hired from one firm, fund launches, custodian
              changes
  open roles  careers pages and job boards (prospect/openroles.py)

Every comparison carries the two dates it compares. A change over more than
fifteen months is also given as a yearly rate, and said to be one, so 40%
over three years is never read as a year's growth.
"""

from __future__ import annotations

import json
import statistics
from datetime import date, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS firm_growth (
    crd                 TEXT PRIMARY KEY,
    computed_at         TEXT NOT NULL,
    headcount           INTEGER,     -- people registered at the firm now
    headcount_prev      INTEGER,     -- a year ago
    team_growth         REAL,        -- fraction over the year
    joined_12m          INTEGER,
    left_12m            INTEGER,
    period_from         TEXT,        -- the earlier filing the figures below compare with
    period_days         INTEGER,
    aum_growth          REAL,        -- yearly rate, as a fraction
    clients_growth      REAL,
    staff_growth        REAL,
    advisors_growth     REAL,
    per_advisor_growth  REAL,        -- assets per advisor, yearly rate
    support_per_advisor REAL,
    peer_support        REAL,        -- the median for firms of a similar size
    departures_senior   INTEGER,
    veterans_30y        INTEGER,
    veterans_share      REAL,
    new_locations       INTEGER,
    new_states          INTEGER,
    registered_date     TEXT,
    open_roles          INTEGER,
    careers_status      TEXT,
    detail              TEXT         -- JSON, everything the Growth tab shows
);
CREATE TABLE IF NOT EXISTS firm_state (
    crd         TEXT NOT NULL,
    state       TEXT NOT NULL,
    kind        TEXT NOT NULL,       -- notice (an SEC adviser's notice filing) | registration
    status      TEXT,
    filed_date  TEXT,
    PRIMARY KEY (crd, state, kind)
);
CREATE TABLE IF NOT EXISTS growth_meta (
    k   TEXT PRIMARY KEY,
    v   TEXT
);
"""

YEAR = 365
MIN_GAP_DAYS = 300            # an "a year ago" filing is at least this much older
ANNUALISE_AFTER_DAYS = 456    # beyond fifteen months a change is also given per year
VETERAN_YEARS = 30
SENIOR_TENURE_YEARS = 5       # a departure after this long at the firm is a senior one
SENIOR_INDUSTRY_YEARS = 15    # or after this long in the industry
LIFT_OUT_MIN = 3              # this many people from one firm in a year
ACTIVE_STATUSES_OUT = ("TERMINATED", "WITHDRAWN", "CANCELLED", "REVOKED", "DENIED")

# The kinds of signal (scripts/triggers.py) that show a firm taking on more.
GROWTH_TRIGGERS = {
    "first_private_fund": "First private fund",
    "first_real_estate_fund": "First real estate fund",
    "custodian_change_to_platform": "Custodian change",
    "custodian_change_from_platform": "Custodian change",
    "custodian_change_other": "Custodian change",
    "aum_jump": "Assets jumped",
    "iar_growth": "Advisor headcount grew",
    "new_registration": "Newly registered",
    "reregistration_or_gap": "Registered again",
}

AUM_BANDS = ((0, 100e6, "under $100M"), (100e6, 500e6, "$100M to $500M"),
             (500e6, 1e9, "$500M to $1B"), (1e9, 5e9, "$1B to $5B"), (5e9, float("inf"), "$5B and up"))


def init(conn) -> None:
    from . import db
    conn.executescript(SCHEMA)
    try:
        if conn.execute("SELECT to_regclass('firm_history') t").fetchone()["t"]:
            db.add_column(conn, "firm_history", "employees", "INTEGER")
            db.add_column(conn, "firm_history", "clients", "INTEGER")
    except Exception:
        conn.rollback()
    conn.commit()


def change(now, prev, days: int | None) -> dict | None:
    """{now, prev, change, annual} for one figure between two filings, or None
    when either is unknown or the earlier one is zero."""
    try:
        now, prev = float(now), float(prev)
    except (TypeError, ValueError):
        return None
    if prev <= 0 or now < 0 or not days or days <= 0:
        return None
    total = now / prev - 1
    if days > ANNUALISE_AFTER_DAYS:
        annual = (now / prev) ** (YEAR / days) - 1 if now > 0 else -1.0
    else:
        annual = total
    return {"now": now, "prev": prev, "change": round(total, 4), "annual": round(annual, 4)}


def band(aum) -> str:
    v = float(aum or 0)
    for lo, hi, label in AUM_BANDS:
        if lo <= v < hi:
            return label
    return AUM_BANDS[0][2]


def _years_between(a: str | None, b: str | None) -> float | None:
    try:
        d1, d2 = date.fromisoformat((a or "")[:10]), date.fromisoformat((b or "")[:10])
    except ValueError:
        return None
    return (d2 - d1).days / YEAR


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _safe(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except Exception:
        conn.rollback()
        return []


def load(conn, crd: str) -> dict | None:
    """The firm's growth row with its detail decoded, for the firm page."""
    rows = _safe(conn, "SELECT * FROM firm_growth WHERE crd=?", (crd,))
    if not rows:
        return None
    d = dict(rows[0])
    try:
        d["detail"] = json.loads(d.get("detail") or "{}")
    except ValueError:
        d["detail"] = {}
    return d


def history(conn, crd: str) -> list[dict]:
    """Every filing with assets, advisors, staff and clients, oldest first."""
    return [dict(r) for r in _safe(conn, """SELECT filing_date, raum, iar_count, employees, clients
        FROM firm_history WHERE crd=? ORDER BY filing_date""", (crd,))]


# ------------------------------------------------------------------ build

def compute_all(conn) -> dict:
    """Recompute firm_growth for every firm. Returns counts for the job log."""
    today = date.today()
    iso = today.isoformat()
    cut12 = (today - timedelta(days=YEAR)).isoformat()
    cut24 = (today - timedelta(days=2 * YEAR)).isoformat()
    vet_cut = date(today.year - VETERAN_YEARS, today.month, min(today.day, 28)).isoformat()

    firms: dict[str, dict] = {}
    rows = _safe(conn, """SELECT f.crd, f.raum, f.iar_count, f.total_employees, f.clients_total,
            f.registered_date, f.filing_date, p.answers
            FROM firm_current f LEFT JOIN firm_adv_profile p ON p.crd = f.crd""")
    if not rows:
        rows = _safe(conn, """SELECT crd, raum, iar_count, total_employees, clients_total,
                registered_date, filing_date, NULL AS answers FROM firm_current""")
    for r in rows:
        try:
            ans = json.loads(r["answers"] or "{}")
        except ValueError:
            ans = {}
        firms[r["crd"]] = {"crd": r["crd"], "raum": r["raum"], "advisors": r["iar_count"],
                           "staff": r["total_employees"], "clients": _int(ans.get("Q5C1")),
                           "q5b2": _int(ans.get("Q5B2")), "q5b5": _int(ans.get("Q5B5")),
                           "registered": r["registered_date"], "filed": r["filing_date"]}

    # The filing about a year before the latest one.
    prev = {r["crd"]: dict(r) for r in _safe(conn, """
        WITH last AS (SELECT crd, MAX(filing_date) d FROM firm_history GROUP BY crd)
        SELECT DISTINCT ON (h.crd) h.crd, h.filing_date, h.raum, h.iar_count, h.employees,
               h.clients
        FROM firm_history h JOIN last l ON l.crd = h.crd
        WHERE h.filing_date <= to_char(l.d::date - ?::int, 'YYYY-MM-DD')
        ORDER BY h.crd, h.filing_date DESC""", (MIN_GAP_DAYS,))}

    stats = {r["crd"]: dict(r) for r in _safe(conn, "SELECT * FROM firm_people_stats")}

    # Industry experience: the first registration anyone holds on record.
    have_people = bool(_safe(conn, "SELECT 1 FROM person_employment LIMIT 1"))
    vets, vet_names, departures, lifts, locs = {}, {}, {}, {}, {}
    if have_people:
        conn.execute("DROP TABLE IF EXISTS g_first")
        conn.execute("""CREATE TEMP TABLE g_first AS
            SELECT indvl_pk, MIN(NULLIF(start_date, '')) fs FROM person_employment
            GROUP BY indvl_pk""")
        conn.execute("CREATE INDEX ON g_first (indvl_pk)")
        conn.commit()
        for r in _safe(conn, """SELECT e.org_pk crd, COUNT(DISTINCT e.indvl_pk) n,
                COUNT(DISTINCT e.indvl_pk) FILTER (WHERE g.fs <= ?) v30
                FROM person_employment e JOIN g_first g ON g.indvl_pk = e.indvl_pk
                WHERE e.kind = 'current' AND e.org_pk != '' GROUP BY e.org_pk""", (vet_cut,)):
            vets[r["crd"]] = (r["n"] or 0, r["v30"] or 0)
        for r in _safe(conn, """SELECT crd, name, fs FROM (
                SELECT e.org_pk crd, p.name, g.fs,
                       ROW_NUMBER() OVER (PARTITION BY e.org_pk ORDER BY g.fs, p.name) rn
                FROM (SELECT DISTINCT org_pk, indvl_pk FROM person_employment
                      WHERE kind = 'current' AND org_pk != '') e
                JOIN g_first g ON g.indvl_pk = e.indvl_pk
                JOIN person p ON p.indvl_pk = e.indvl_pk
                WHERE g.fs <= ?) x WHERE rn <= 6""", (vet_cut,)):
            vet_names.setdefault(r["crd"], []).append({"name": r["name"], "since": (r["fs"] or "")[:4]})
        for r in _safe(conn, """SELECT ev.crd, ev.event_date, ev.other_org_pk, ev.other_org_name,
                p.name, g.fs,
                (SELECT MAX(pe.start_date) FROM person_employment pe
                  WHERE pe.indvl_pk = ev.indvl_pk AND pe.org_pk = ev.crd AND pe.start_date != ''
                    AND pe.start_date <= ev.event_date) firm_start
                FROM people_event ev JOIN person p ON p.indvl_pk = ev.indvl_pk
                LEFT JOIN g_first g ON g.indvl_pk = ev.indvl_pk
                WHERE ev.kind = 'left' AND ev.event_date >= ?""", (cut12,)):
            tenure = _years_between(r["firm_start"], r["event_date"])
            industry = _years_between(r["fs"], r["event_date"])
            departures.setdefault(r["crd"], []).append({
                "name": r["name"], "date": (r["event_date"] or "")[:10],
                "tenure": round(tenure, 1) if tenure is not None else None,
                "industry": round(industry, 1) if industry is not None else None,
                "to": r["other_org_name"], "to_crd": r["other_org_pk"],
                "senior": bool((tenure or 0) >= SENIOR_TENURE_YEARS
                               or (industry or 0) >= SENIOR_INDUSTRY_YEARS)})
        for r in _safe(conn, """SELECT crd, other_org_pk, MAX(other_org_name) org,
                COUNT(DISTINCT indvl_pk) n FROM people_event
                WHERE kind = 'joined' AND event_date >= ? AND COALESCE(other_org_pk, '') != ''
                GROUP BY crd, other_org_pk HAVING COUNT(DISTINCT indvl_pk) >= ?""",
                         (cut12, LIFT_OUT_MIN)):
            lifts.setdefault(r["crd"], []).append({"org": r["org"], "org_crd": r["other_org_pk"],
                                                   "n": r["n"]})
        for r in _safe(conn, """SELECT org_pk crd, UPPER(TRIM(city)) city, state,
                MIN(start_date) first_start, COUNT(DISTINCT indvl_pk) n
                FROM person_employment
                WHERE kind = 'current' AND org_pk != '' AND COALESCE(city, '') != ''
                  AND start_date != ''
                GROUP BY 1, 2, 3"""):
            locs.setdefault(r["crd"], []).append(dict(r))
        conn.execute("DROP TABLE IF EXISTS g_first")
        conn.commit()

    states, new_states = {}, {}
    for r in _safe(conn, "SELECT crd, state, kind, status, filed_date FROM firm_state"):
        if (r["status"] or "").upper() in ACTIVE_STATUSES_OUT:
            continue
        states[r["crd"]] = states.get(r["crd"], 0) + 1
        if (r["filed_date"] or "") >= cut12:
            new_states.setdefault(r["crd"], []).append(
                {"state": r["state"], "date": (r["filed_date"] or "")[:10], "kind": r["kind"]})

    events = {}
    types = tuple(GROWTH_TRIGGERS)
    ph = ",".join("?" * len(types))
    for r in _safe(conn, f"""SELECT crd, trigger_type, detected_date, description FROM trigger_event
            WHERE suppressed = 0 AND detected_date >= ? AND trigger_type IN ({ph})
            ORDER BY detected_date DESC""", (cut24, *types)):
        events.setdefault(r["crd"], []).append({
            "type": r["trigger_type"], "label": GROWTH_TRIGGERS[r["trigger_type"]],
            "date": (r["detected_date"] or "")[:10], "text": r["description"]})

    careers, postings = {}, {}
    for r in _safe(conn, "SELECT * FROM firm_careers"):
        careers[r["crd"]] = dict(r)
    # One entry per title: the firm's own tracking system, then its site, then
    # the job boards, which often repeat what the firm already lists.
    seen_titles: set[tuple[str, str]] = set()
    for r in _safe(conn, """SELECT crd, title, function, url, source, host, first_seen, last_seen
            FROM firm_job_posting WHERE active = 1
            ORDER BY CASE source WHEN 'ats' THEN 0 WHEN 'website' THEN 1 ELSE 2 END,
                     first_seen DESC"""):
        key = (r["crd"], (r["title"] or "").lower())
        if key in seen_titles:
            continue
        seen_titles.add(key)
        postings.setdefault(r["crd"], []).append(dict(r))

    # Support staff per advisor, and its median among firms of a similar size.
    by_band: dict[str, list[float]] = {}
    for f in firms.values():
        adv, staff = f["advisors"] or 0, f["staff"] or 0
        if adv >= 1 and staff >= adv:
            f["support"] = staff - adv
            f["spa"] = (staff - adv) / adv
            by_band.setdefault(band(f["raum"]), []).append(f["spa"])
        else:
            f["support"] = staff - adv if staff and staff >= adv else None
            f["spa"] = None
    peer = {b: statistics.median(v) for b, v in by_band.items() if len(v) >= 20}

    out_rows = []
    counts = {"firms": 0, "growing_team": 0, "with_open_roles": 0, "compared": 0}
    for crd, f in firms.items():
        detail: dict = {}
        p = prev.get(crd)
        days = None
        if p and f["filed"]:
            try:
                days = (date.fromisoformat(f["filed"][:10])
                        - date.fromisoformat(p["filing_date"][:10])).days
            except ValueError:
                days = None
        detail["period"] = {"from": p["filing_date"][:10] if p else None,
                            "to": (f["filed"] or "")[:10] or None, "days": days}
        aum = change(f["raum"], p and p["raum"], days)
        clients = change(f["clients"], p and p["clients"], days)
        staff = change(f["staff"], p and p["employees"], days)
        advisors = change(f["advisors"], p and p["iar_count"], days)
        per_adv = None
        if p and f["advisors"] and p["iar_count"]:
            per_adv = change((f["raum"] or 0) / f["advisors"], (p["raum"] or 0) / p["iar_count"], days)
        clients_per = None
        if p and f["advisors"] and p["iar_count"] and f["clients"] is not None and p["clients"]:
            clients_per = change(f["clients"] / f["advisors"], p["clients"] / p["iar_count"], days)
        detail.update(aum=aum, clients=clients, staff=staff, advisors=advisors,
                      aum_per_advisor=per_adv, clients_per_advisor=clients_per)
        if aum:
            counts["compared"] += 1
        b = band(f["raum"])
        detail["staff_mix"] = {"total": f["staff"], "advisory": f["advisors"],
                               "reps": f["q5b2"], "insurance": f["q5b5"],
                               "support": f["support"], "support_per_advisor": f["spa"],
                               "peer": peer.get(b), "band": b}
        s = stats.get(crd) or {}
        head = s.get("headcount")
        net = s.get("net_12m")
        head_prev = (head - net) if head is not None and net is not None else None
        team_growth = (net / head_prev) if head_prev and head_prev > 0 and net is not None else None
        detail["team"] = {"now": head, "prev": head_prev, "joined": s.get("hires_12m"),
                          "left": s.get("departures_12m"), "joined_prev": s.get("hires_prev_12m"),
                          "left_prev": s.get("departures_prev_12m"), "growth": team_growth}
        if (net or 0) > 0:
            counts["growing_team"] += 1
        # Senior departures first, each group newest first.
        deps = sorted(departures.get(crd, []), key=lambda x: x["date"], reverse=True)
        deps.sort(key=lambda x: not x["senior"])
        detail["departures"] = deps[:12]
        senior = sum(1 for x in departures.get(crd, []) if x["senior"])
        n_people, v30 = vets.get(crd, (head or 0, 0))
        share = (v30 / n_people) if n_people else None
        detail["veterans"] = {"n30": v30, "of": n_people, "share": share,
                              "names": vet_names.get(crd, [])}
        new_locs = []
        mine = locs.get(crd, [])
        if any((x["first_start"] or "") < cut12 for x in mine):
            new_locs = sorted(({"city": x["city"], "state": x["state"],
                                "since": (x["first_start"] or "")[:10], "people": x["n"]}
                               for x in mine if (x["first_start"] or "") >= cut12),
                              key=lambda x: x["since"], reverse=True)
        detail["locations"] = new_locs[:10]
        detail["states"] = {"total": states.get(crd, 0),
                            "new": sorted(new_states.get(crd, []), key=lambda x: x["date"],
                                          reverse=True)[:15]}
        reg_years = _years_between(f["registered"], iso)
        detail["registered"] = {"date": (f["registered"] or "")[:10] or None,
                                "years": round(reg_years, 1) if reg_years is not None else None}
        detail["lift_outs"] = lifts.get(crd, [])
        detail["events"] = events.get(crd, [])[:10]
        cr = careers.get(crd) or {}
        roles = postings.get(crd, [])
        by_fn: dict[str, int] = {}
        for x in roles:
            by_fn[x["function"] or "Other"] = by_fn.get(x["function"] or "Other", 0) + 1
        # The postings themselves are read live by the firm page and the
        # scores; the counts here are as of this build.
        detail["roles"] = {"status": cr.get("status"), "count": len(roles), "by_function": by_fn}
        if roles:
            counts["with_open_roles"] += 1
        counts["firms"] += 1
        out_rows.append((
            crd, iso, head, head_prev, team_growth, s.get("hires_12m"), s.get("departures_12m"),
            detail["period"]["from"], days,
            aum and aum["annual"], clients and clients["annual"], staff and staff["annual"],
            advisors and advisors["annual"], per_adv and per_adv["annual"],
            f["spa"], peer.get(b), senior, v30, share, len(new_locs),
            len(detail["states"]["new"]), (f["registered"] or "")[:10] or None,
            len(roles), cr.get("status"), json.dumps(detail, default=str)))

    conn.execute("DELETE FROM firm_growth")
    sql = ("INSERT INTO firm_growth (crd, computed_at, headcount, headcount_prev, team_growth,"
           " joined_12m, left_12m, period_from, period_days, aum_growth, clients_growth,"
           " staff_growth, advisors_growth, per_advisor_growth, support_per_advisor,"
           " peer_support, departures_senior, veterans_30y, veterans_share, new_locations,"
           " new_states, registered_date, open_roles, careers_status, detail)"
           " VALUES (" + ",".join("?" * 25) + ")")
    for i in range(0, len(out_rows), 2000):
        conn.executemany(sql, out_rows[i:i + 2000])
    conn.commit()
    return counts
