"""Convert the Schwab flag from a boolean to a share.

Caveat that must travel with this number: Schedule D 5.K(3) lists only custodians
holding ten percent or more of SMA assets, so the denominator is the sum of
*reported* custodians, not the firm's whole separately managed account book. The
share is therefore an upper bound on Schwab concentration. The column is named
for what it is and the UI must show the caveat, the same rule as
est_avg_client_size.

The 2026 labelling from section 5 stands regardless of what the share says: this
identifies the late-2026 institutional opportunity, never a sellable book today.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, custodians, db, runlog  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS firm_custodian_profile (
    crd                     TEXT PRIMARY KEY,
    as_of_filing_date       TEXT NOT NULL,   -- archive value: show this in the UI
    primary_entity          TEXT,
    primary_canonical       TEXT,
    reported_custodians     INTEGER,
    schwab_assets           INTEGER,
    reported_total_assets   INTEGER,
    -- share of REPORTED custodian assets, not of total SMA assets
    schwab_share_reported   REAL,
    config_stamp            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fcp_share ON firm_custodian_profile (schwab_share_reported DESC);
"""


def main() -> int:
    cfg = config.load()
    conn = db.connect()
    conn.executescript(SCHEMA); conn.commit()
    table = custodians.load()

    with runlog.Run(conn, "custodian_share", "derive", cfg.stamp) as run:
        conn.execute("DELETE FROM firm_custodian_profile"); conn.commit()

        # Latest filing per firm that carries custodian rows.
        latest = {r["crd"]: r["d"] for r in conn.execute("""
            SELECT s.crd, MAX(fc.filing_date) d
            FROM sched_d_5k3 s JOIN filing_crd fc ON fc.filing_id=s.filing_id
            WHERE s.crd IS NOT NULL AND fc.filing_date IS NOT NULL
            GROUP BY s.crd""")}

        rows = conn.execute("""
            SELECT s.crd, fc.filing_date d, s.custodian_entity e,
                   SUM(COALESCE(s.assets_held,0)) amt, COUNT(*) n
            FROM sched_d_5k3 s JOIN filing_crd fc ON fc.filing_id=s.filing_id
            WHERE s.crd IS NOT NULL AND fc.filing_date IS NOT NULL
            GROUP BY s.crd, fc.filing_date, s.custodian_entity""").fetchall()

        agg: dict[str, dict] = {}
        for r in rows:
            if latest.get(r["crd"]) != r["d"]:
                continue
            a = agg.setdefault(r["crd"], {"d": r["d"], "tot": 0, "schwab": 0,
                                          "n": 0, "best": (0, None)})
            # TD Ameritrade counts as Schwab once the transition completed.
            eff = table.effective_entity(r["e"], r["d"])
            a["tot"] += r["amt"]; a["n"] += r["n"]
            if eff == "schwab":
                a["schwab"] += r["amt"]
            if r["amt"] > a["best"][0]:
                a["best"] = (r["amt"], eff or r["e"])

        out = []
        for crd, a in agg.items():
            share = (a["schwab"] / a["tot"]) if a["tot"] else None
            out.append((crd, a["d"], a["best"][1], table.canonical_name(a["best"][1]),
                        a["n"], a["schwab"] or None, a["tot"] or None, share, cfg.stamp))
        conn.executemany(
            "INSERT INTO firm_custodian_profile VALUES (?,?,?,?,?,?,?,?,?)", out)
        conn.commit()
        run.rows_out = len(out)
        print(f"profiled {len(out):,} firms")

    print("\nin-band registered firms, Schwab share of reported custodian assets")
    print("  (upper bound: denominator counts only custodians at 10%+ of SMA assets)")
    for lo, hi, lab in ((0.999, 1.01, "100%, Schwab only"), (0.75, 0.999, "75 to 99%"),
                        (0.50, 0.75, "50 to 75%"), (0.25, 0.50, "25 to 50%"),
                        (0.0001, 0.25, "under 25%")):
        r = conn.execute("""
            SELECT COUNT(*) c FROM firm_custodian_profile p JOIN firm_current f ON f.crd=p.crd
            WHERE f.is_era=0 AND f.raum>=25e6 AND f.raum<500e6
              AND p.schwab_share_reported>=? AND p.schwab_share_reported<?""",
            (lo, hi)).fetchone()
        print(f"    {lab:<20} {r['c']:>5,}")
    r = conn.execute("""
        SELECT COUNT(*) c FROM firm_custodian_profile p JOIN firm_current f ON f.crd=p.crd
        WHERE f.is_era=0 AND f.raum>=25e6 AND f.raum<500e6
          AND COALESCE(p.schwab_share_reported,0)=0""").fetchone()
    print(f"    {'no Schwab':<20} {r['c']:>5,}")
    stale = conn.execute("""
        SELECT MIN(as_of_filing_date) a, MAX(as_of_filing_date) b
        FROM firm_custodian_profile""").fetchone()
    print(f"\n  as-of dates span {stale['a']} .. {stale['b']} (archive only; every firm has")
    print("  filed at least one amendment since, so treat all of it as a baseline)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
