"""Compute triggers retroactively across the archive, and report base rates.

Two triggers, both computable today from thirteen years of Schedule D rather
than waiting for two forward snapshots.

  custodian_change    A firm changing custodian is re-papering its book and
                      re-evaluating its whole vendor stack. Custodian history
                      starts 2018, when Item 5.K was added to Form ADV.

  first_private_fund  A firm's first private fund, and separately its first real
                      estate fund. The latter is a PHH lead on its own.

Known custodian consolidations are suppressed. A firm moved from TD Ameritrade to
Schwab during the 2023-24 transition was moved by its custodian, not by choice,
and firing on those would flood the queue and teach the team to ignore it. The
suppression is a dated rule in config/custodians.yml, not a hardcode, because
Pershing and LPL will consolidate the same way.

    python -m scripts.triggers [--rebuild]
"""

from __future__ import annotations

import argparse
import collections
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, custodians, db, runlog  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS trigger_event (
    id              INTEGER PRIMARY KEY,
    crd             TEXT    NOT NULL,
    trigger_type    TEXT    NOT NULL,
    detected_date   TEXT    NOT NULL,
    before_value    TEXT,
    after_value     TEXT,
    description     TEXT    NOT NULL,
    suppressed      INTEGER NOT NULL DEFAULT 0,
    suppression_rule TEXT,
    -- Recency decays rather than filtering: a fixed window needs an argument
    -- about where to put it, a weight lets the queue sort itself.
    age_days        INTEGER,
    recency_weight  REAL,
    -- Negative for disqualifying events (a firm leaving the platform).
    direction_weight REAL,
    priority        REAL,
    config_stamp    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_trig_crd ON trigger_event (crd, trigger_type);
CREATE INDEX IF NOT EXISTS ix_trig_date ON trigger_event (detected_date DESC);
CREATE INDEX IF NOT EXISTS ix_trig_live ON trigger_event (suppressed, trigger_type, detected_date DESC);
-- The per-firm EXISTS and per-row counts seek on crd first. Without this index
-- the planner picks the suppressed= index and scans every trigger per firm,
-- which measured 85 seconds on the firm list.
CREATE INDEX IF NOT EXISTS ix_trig_crd_supp ON trigger_event (crd, suppressed);
"""

CUSTODIAN_HISTORY_START = "2018-01-01"  # Item 5.K did not exist before the 2016 amendments


def custodian_changes(conn, table, cc_cfg, stamp) -> list[tuple]:
    """Primary custodian per filing, then diff consecutive filings per firm.

    Primary is the entity holding the most assets on that filing. Firms commonly
    list several custodians, so comparing whole sets would fire on any minor
    addition; the dominant relationship is what actually moves.
    """
    rows = conn.execute("""
        SELECT s.crd, fc.filing_date AS d, s.custodian_entity AS e,
               SUM(COALESCE(s.assets_held,0)) AS amt
        FROM sched_d_5k3 s
        JOIN filing_crd fc ON fc.filing_id = s.filing_id
        WHERE s.custodian_entity IS NOT NULL
          AND s.crd IS NOT NULL
          AND fc.filing_date >= ?
        GROUP BY s.crd, fc.filing_date, s.custodian_entity
    """, (CUSTODIAN_HISTORY_START,)).fetchall()

    per_filing: dict[tuple[str, str], tuple[int, str]] = {}
    for r in rows:
        k = (r["crd"], r["d"])
        cur = per_filing.get(k)
        if cur is None or r["amt"] > cur[0]:
            per_filing[k] = (r["amt"], r["e"])

    by_firm: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for (crd, d), (_amt, e) in per_filing.items():
        by_firm[crd].append((d, e))

    platform = cc_cfg["platform_entity"]
    out = []
    for crd, seq in by_firm.items():
        seq.sort()
        for (d0, e0), (d1, e1) in zip(seq, seq[1:]):
            if e0 == e1:
                continue
            rule = table.is_migration(e0, e1, d1)
            n0 = table.canonical_name(e0) or e0
            n1 = table.canonical_name(e1) or e1

            # Same event, opposite routing. Moving onto the platform is an
            # AcuBooth prospect; moving off it disqualifies the firm outright,
            # since AcuBooth runs on Schwab retail accounts only.
            if e1 == platform:
                ttype, dirkey = "custodian_change_to_platform", "to_platform"
                desc = f"Moved primary custodian to {n1} from {n0}: rebuilding on platform"
            elif e0 == platform:
                ttype, dirkey = "custodian_change_from_platform", "from_platform"
                desc = f"Moved primary custodian off {n0} to {n1}: no longer on platform"
            else:
                ttype, dirkey = "custodian_change_other", "other"
                desc = f"Changed primary custodian from {n0} to {n1}"
            if rule:
                desc = f"Custodian moved {n0} to {n1} in a platform consolidation"
            out.append((crd, ttype, d1, e0, e1, desc, 1 if rule else 0, rule,
                        cc_cfg["direction_weights"][dirkey], stamp))
    return out


def first_funds(conn, stamp) -> list[tuple]:
    """First private fund, and first real estate fund, per firm."""
    out = []
    for ttype, where, label in (
        ("first_private_fund", "", "first private fund"),
        ("first_real_estate_fund", " AND s.fund_type='Real Estate Fund'", "first real estate fund"),
    ):
        rows = conn.execute(f"""
            SELECT s.crd, MIN(fc.filing_date) AS d, COUNT(DISTINCT s.fund_id) AS n
            FROM sched_d_7b1 s
            JOIN filing_crd fc ON fc.filing_id = s.filing_id
            WHERE s.crd IS NOT NULL AND fc.filing_date IS NOT NULL{where}
            GROUP BY s.crd
        """).fetchall()
        for r in rows:
            name = conn.execute(f"""
                SELECT s.fund_name FROM sched_d_7b1 s
                JOIN filing_crd fc ON fc.filing_id=s.filing_id
                WHERE s.crd=? AND fc.filing_date=?{where}
                LIMIT 1""", (r["crd"], r["d"])).fetchone()
            fn = (name["fund_name"] if name else None) or "unnamed fund"
            out.append((r["crd"], ttype, r["d"], None, fn,
                        f"Reported its {label}: {fn}", 0, None, 1.0, stamp))
    return out


def apply_weights(conn, rec_cfg, today) -> None:
    """Age-decay every event and combine with its direction weight.

    Archive events are all more than eighteen months old by construction, so they
    weight low. That is the correct signal, not a bug: the archive is a base-rate
    and backtesting corpus, and the live queue starts filling with the second
    weekly snapshot.
    """
    half, floor = rec_cfg["half_life_days"], rec_cfg["floor_weight"]
    conn.execute("""
        UPDATE trigger_event
           SET age_days = CAST(julianday(?) - julianday(detected_date) AS INTEGER)
    """, (today,))
    conn.execute("""
        UPDATE trigger_event
           SET recency_weight = MAX(?, POWER(0.5, age_days / ?)),
               priority = MAX(?, POWER(0.5, age_days / ?)) * COALESCE(direction_weight, 1.0)
         WHERE age_days IS NOT NULL
    """, (floor, float(half), floor, float(half)))
    conn.commit()


def base_rates(conn) -> None:
    print("\n" + "=" * 72)
    print("BASE RATES (archive, retroactive)")
    print("=" * 72)

    print("\ncustodian_change, events per year")
    print(f"  {'year':<6} {'live':>7} {'suppressed':>11} {'firms w/ data':>14} {'live rate':>10}")
    rows = conn.execute("""
        SELECT substr(detected_date,1,4) y,
               SUM(suppressed=0) live, SUM(suppressed=1) supp
        FROM trigger_event WHERE trigger_type LIKE 'custodian_change%'
        GROUP BY 1 ORDER BY 1""").fetchall()
    for r in rows:
        pool = conn.execute("""
            SELECT COUNT(DISTINCT s.crd) c FROM sched_d_5k3 s
            JOIN filing_crd fc ON fc.filing_id=s.filing_id
            WHERE substr(fc.filing_date,1,4)=? AND s.custodian_entity IS NOT NULL""",
            (r["y"],)).fetchone()["c"]
        rate = (r["live"] / pool * 100) if pool else 0
        print(f"  {r['y']:<6} {r['live']:>7,} {r['supp']:>11,} {pool:>14,} {rate:>9.1f}%")

    print("\n  suppressed by rule:")
    for r in conn.execute("""
        SELECT suppression_rule k, COUNT(*) c, COUNT(DISTINCT crd) f
        FROM trigger_event WHERE suppressed=1 GROUP BY 1 ORDER BY c DESC"""):
        print(f"    {r['k']}: {r['c']:,} events across {r['f']:,} firms")

    for t in ("first_private_fund", "first_real_estate_fund"):
        print(f"\n{t}, events per year")
        for r in conn.execute("""
            SELECT substr(detected_date,1,4) y, COUNT(*) c
            FROM trigger_event WHERE trigger_type=? GROUP BY 1 ORDER BY 1""", (t,)):
            print(f"  {r['y']}  {r['c']:>6,}")


def live_now(conn) -> None:
    """What these triggers would surface today, restricted to scoreable firms."""
    print("\n" + "=" * 72)
    print("LIVE QUEUE, in-band registered firms only (ERAs excluded)")
    print("=" * 72)
    for t, window in (("custodian_change_to_platform", "2022-01-01"),
                      ("custodian_change_from_platform", "2022-01-01"),
                      ("custodian_change_other", "2022-01-01"),
                      ("first_private_fund", "2023-01-01"),
                      ("first_real_estate_fund", "2023-01-01")):
        r = conn.execute("""
            SELECT COUNT(DISTINCT t.crd) c FROM trigger_event t
            JOIN firm_current f ON f.crd = t.crd
            WHERE t.trigger_type=? AND t.suppressed=0 AND t.detected_date>=?
              AND f.is_era=0 AND f.raum>=25e6 AND f.raum<500e6""",
            (t, window)).fetchone()
        print(f"  {t:<24} since {window}:  {r['c']:>5,} firms")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()
    cfg = config.load()
    conn = db.connect()
    conn.executescript(SCHEMA); conn.commit()
    table = custodians.load()
    sc = yaml.safe_load((config.CONFIG_DIR / "scoring.yml").read_text())

    have = conn.execute("SELECT COUNT(*) c FROM trigger_event").fetchone()["c"]
    if have and not args.rebuild:
        print(f"{have:,} trigger events already computed; use --rebuild to recompute")
    else:
        with runlog.Run(conn, "triggers", "compute", cfg.stamp) as run:
            conn.execute("DELETE FROM trigger_event"); conn.commit()
            print("computing custodian changes ...")
            ev = custodian_changes(conn, table, sc["custodian_change"], cfg.stamp)
            print(f"  {len(ev):,} transitions")
            print("computing first funds ...")
            ev += first_funds(conn, cfg.stamp)
            conn.executemany(
                "INSERT INTO trigger_event (crd,trigger_type,detected_date,before_value,"
                "after_value,description,suppressed,suppression_rule,"
                "direction_weight,config_stamp) VALUES (?,?,?,?,?,?,?,?,?,?)", ev)
            conn.commit()
            print(f"  {len(ev):,} events total")
            run.rows_out = len(ev)
            apply_weights(conn, sc["trigger_recency"], date.today().isoformat())

    base_rates(conn)
    live_now(conn)
    print(f"\nconfig {cfg.stamp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
