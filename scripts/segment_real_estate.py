"""Segment real estate fund advisers into prospects, competitors and pre-launch.

Item 7.B reports funds the adviser advises, so the flag alone means opposite
things at different scales. The RAUM ratio is the primary discriminant because it
is scale-invariant; absolute gross asset value and investor count are secondary
and catch the cases where the ratio is unavailable.

Zero gross asset value is a segment, not a gap: a real estate fund that has not
raised is a firm that has just decided to be in this business.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, runlog  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS re_segment (
    crd                 TEXT PRIMARY KEY,
    segment             TEXT NOT NULL,
    as_of_filing_date   TEXT NOT NULL,
    fund_count          INTEGER,
    total_gav           INTEGER,
    total_owners        INTEGER,
    min_investment      INTEGER,
    raum                INTEGER,
    raum_ratio          REAL,
    first_seen          TEXT,      -- earliest archive appearance of any RE fund
    rationale           TEXT NOT NULL,
    config_stamp        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reseg ON re_segment (segment, raum_ratio DESC);
"""


def classify(cfg_seg: dict, gav: int, owners: int, raum: int | None,
             first_seen: str | None, today: date,
             hnw_clients: int | None, hnw_aum: int | None) -> tuple[str, str]:
    p, s, u = cfg_seg["primary"], cfg_seg["secondary"], cfg_seg["unraised"]
    g = cfg_seg["hnw_gate"]

    # Gate first. PHH is sold to advisers FOR their high net worth clients, so a
    # firm without any is not a prospect at any fund size. This catches sponsors
    # whose whole book sits in pooled vehicles, which the RAUM ratio cannot see.
    share = (hnw_aum or 0) / raum if raum else 0.0
    aum = hnw_aum or 0
    passes = ((hnw_clients or 0) >= g["min_hnw_clients"]
              and (share >= g["min_hnw_share"] or aum >= g["min_hnw_aum"]))
    if not passes:
        return (g["fail_segment"],
                f"no qualifying high net worth book ({hnw_clients or 0} HNW clients, "
                f"${aum/1e6:,.0f}M, {share:.0%} of RAUM): fund sponsor rather than an "
                "adviser with clients to place")

    if gav == 0:
        months = None
        if first_seen:
            fy, fm, _ = (int(x) for x in first_seen.split("-"))
            months = (today.year - fy) * 12 + (today.month - fm)
        recent = months is not None and months <= u["recent_formation_months"]
        if owners <= u["max_owners"]:
            return ("unraised", f"zero gross asset value with {owners} investors"
                    + (f", first filed {months} months ago" if months is not None else "")
                    + ": fund exists but has not raised")
        return ("ambiguous", f"zero gross asset value but {owners} investors reported; "
                             "figures inconsistent, needs a human look")

    ratio = (gav / raum) if raum else None
    if ratio is not None:
        if ratio >= p["raum_ratio_competitor"]:
            return ("competitor", f"real estate fund assets are {ratio:.0%} of the "
                                  f"adviser's own RAUM: the fund is the business")
        if ratio < p["raum_ratio_prospect"]:
            # Ratio says accommodation. Let an absolute cut override only if the
            # fund is very large in its own right.
            if gav >= s["gav_competitor"] or owners >= s["owners_competitor"]:
                return ("competitor", f"fund is only {ratio:.0%} of RAUM but ${gav/1e6:,.0f}M "
                                      f"with {owners} investors: institutional in its own right")
            return ("prospect", f"real estate fund is {ratio:.0%} of RAUM "
                                f"(${gav/1e6:,.1f}M, {owners} investors): client accommodation, "
                                "proven appetite, limited capacity")
        return ("ambiguous", f"fund is {ratio:.0%} of RAUM: between accommodation and business")

    # No RAUM to compare against; fall back to absolute cuts.
    if gav >= s["gav_competitor"] or owners >= s["owners_competitor"]:
        return ("competitor", f"no RAUM on file; ${gav/1e6:,.0f}M fund with {owners} investors")
    if gav < s["gav_prospect"] and owners < s["owners_prospect"]:
        return ("prospect", f"no RAUM on file; small fund ${gav/1e6:,.1f}M, {owners} investors")
    return ("ambiguous", "no RAUM on file and fund size is mid-range")


def main() -> int:
    cfg = config.load()
    seg_cfg = yaml.safe_load((config.CONFIG_DIR / "scoring.yml").read_text())
    stamp = f"{cfg.stamp}|scoring.v{seg_cfg['config_version']}"
    conn = db.connect()
    conn.executescript(SCHEMA); conn.commit()
    today = date.today()

    with runlog.Run(conn, "re_segment", "derive", stamp) as run:
        conn.execute("DELETE FROM re_segment"); conn.commit()
        rows = conn.execute("""
            WITH latest AS (
              SELECT s.crd, MAX(fc.filing_date) d FROM sched_d_7b1 s
              JOIN filing_crd fc ON fc.filing_id=s.filing_id
              WHERE s.fund_type='Real Estate Fund' GROUP BY 1),
            first_seen AS (
              SELECT s.crd, MIN(fc.filing_date) f FROM sched_d_7b1 s
              JOIN filing_crd fc ON fc.filing_id=s.filing_id
              WHERE s.fund_type='Real Estate Fund' GROUP BY 1)
            SELECT s.crd, l.d AS as_of, fs.f AS first_seen,
                   COUNT(DISTINCT s.fund_id) funds,
                   SUM(COALESCE(s.gross_asset_value,0)) gav,
                   SUM(COALESCE(s.owners,0)) owners,
                   MIN(NULLIF(s.minimum_investment,0)) mininv,
                   f.raum, f.legal_name, f.hnw_clients, f.hnw_aum
            FROM sched_d_7b1 s
            JOIN filing_crd fc ON fc.filing_id=s.filing_id
            JOIN latest l ON l.crd=s.crd AND l.d=fc.filing_date
            JOIN first_seen fs ON fs.crd=s.crd
            JOIN firm_current f ON f.crd=s.crd
            WHERE s.fund_type='Real Estate Fund'
              AND f.is_era=0 AND f.raum>=25e6 AND f.raum<500e6
            GROUP BY s.crd""").fetchall()

        out = []
        for r in rows:
            gav, own = int(r["gav"] or 0), int(r["owners"] or 0)
            ratio = (gav / r["raum"]) if r["raum"] else None
            seg, why = classify(seg_cfg["real_estate_segmentation"], gav, own,
                                r["raum"], r["first_seen"], today,
                                r["hnw_clients"], r["hnw_aum"])
            out.append((r["crd"], seg, r["as_of"], r["funds"], gav, own, r["mininv"],
                        r["raum"], ratio, r["first_seen"], why, stamp))
        conn.executemany("INSERT INTO re_segment VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", out)
        conn.commit()
        run.rows_out = len(out)

    print(f"segmented {len(out)} in-band real estate fund advisers\n")
    prev = {"prospect": 66, "competitor": 83, "ambiguous": 35, "unraised": 6, "sponsor": 0}
    print(f"  {'segment':<12} {'now':>5} {'was':>5}  {'delta':>6}")
    for s in ("prospect", "competitor", "ambiguous", "unraised", "sponsor"):
        n = conn.execute("SELECT COUNT(*) c FROM re_segment WHERE segment=?", (s,)).fetchone()["c"]
        d = n - prev[s]
        print(f"  {s:<12} {n:>5} {prev[s]:>5}  {d:>+6}")

    print("\n  unraised segment detail:")
    for r in conn.execute("""SELECT crd, total_owners o, first_seen, rationale
        FROM re_segment WHERE segment='unraised' ORDER BY first_seen DESC LIMIT 8"""):
        print(f"    CRD {r['crd']:<9} {r['o']:>3} investors  first filed {r['first_seen']}")

    print("\n  strongest prospects by RAUM ratio (lowest = most accommodation-like):")
    for r in conn.execute("""SELECT s.crd, f.legal_name n, s.total_gav g, s.total_owners o,
        s.raum_ratio rr, s.min_investment mi FROM re_segment s JOIN firm_current f ON f.crd=s.crd
        WHERE s.segment='prospect' AND s.total_gav>0 ORDER BY s.raum_ratio LIMIT 8"""):
        mi = f"${r['mi']/1e3:,.0f}k min" if r["mi"] else "no min"
        print(f"    {(r['n'] or '')[:36]:<38} {r['rr']:>5.1%} of RAUM  "
              f"${r['g']/1e6:>6.1f}M  {r['o']:>3} inv  {mi}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
