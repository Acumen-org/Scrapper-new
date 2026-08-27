"""Rank tier A for PHH and score tier C for AcuBooth.

Tier membership was settled by the HNW gate and private-fund presence. This ranks
within it. Every component is persisted as its own column: if the detail page
cannot explain why a firm scored 78, the score will not be trusted and the team
goes back to a spreadsheet.

Tier C carries the section 5 discipline in the schema itself. The Schwab
component is named for what it is, a 2026 pre-positioning signal, and
non-discretionary share is recorded for display and deliberately excluded from
the score: it is regulatory AUM, while the AcuBooth pool is held away and outside
RAUM entirely.

    python -m scripts.rank_tiers
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, runlog  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS tier_a_rank (
    crd                 TEXT PRIMARY KEY,
    rank                INTEGER,
    total_score         REAL,
    hnw_aum_score       REAL,
    fund_count_score    REAL,
    min_investment_score REAL,
    fund_type_score     REAL,
    hnw_aum             INTEGER,
    fund_count          INTEGER,
    max_min_investment  INTEGER,
    best_fund_type      TEXT,
    in_working_list     INTEGER NOT NULL DEFAULT 0,
    as_of_filing_date   TEXT,
    fund_gav_total      INTEGER,
    fund_raum_ratio     REAL,
    fund_source         TEXT,
    config_stamp        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ta_rank ON tier_a_rank (rank);

CREATE TABLE IF NOT EXISTS tier_c_score (
    crd                 TEXT PRIMARY KEY,
    rank                INTEGER,
    total_score         REAL,
    hnw_aum_score       REAL,
    hnw_clients_score   REAL,
    schwab_2026_score   REAL,
    capacity_score      REAL,
    hnw_aum             INTEGER,
    hnw_clients         INTEGER,
    schwab_share        REAL,
    schwab_as_of        TEXT,
    iar_count           INTEGER,
    nondisc_share       REAL,   -- display only, never scored: see section 5
    clients_per_rep     REAL,
    clients_per_rep_score REAL,
    config_stamp        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tc_rank ON tier_c_score (rank);
"""


def band(value, bands, default=0.0) -> float:
    """Bands are [threshold, score] descending. First threshold at or below wins."""
    if value is None:
        return default
    for threshold, score in bands:
        if value >= threshold:
            return float(score)
    return default


def build_sets(conn):
    conn.executescript("""
        DROP TABLE IF EXISTS t_fundfirm; DROP TABLE IF EXISTS t_refirm;
        CREATE TEMP TABLE t_fundfirm AS
            SELECT DISTINCT crd FROM sched_d_7b1 WHERE crd IS NOT NULL;
        CREATE UNIQUE INDEX ix_ff ON t_fundfirm(crd);
        CREATE TEMP TABLE t_refirm AS
            SELECT DISTINCT crd FROM sched_d_7b1
            WHERE crd IS NOT NULL AND fund_type='Real Estate Fund';
        CREATE UNIQUE INDEX ix_rf ON t_refirm(crd);
    """)


def gated(conn, g):
    """In-band registered advisers passing the HNW gate, with tier membership."""
    return conn.execute(f"""
        SELECT f.crd, f.legal_name, f.raum, f.raum_nondisc, f.hnw_aum, f.hnw_clients,
               f.iar_count,
               CASE WHEN f.raum>0 THEN 1.0*COALESCE(f.hnw_aum,0)/f.raum ELSE 0 END sh,
               ff.crd IS NOT NULL AS has_fund, rf.crd IS NOT NULL AS has_re
        FROM firm_current f
        LEFT JOIN t_fundfirm ff ON ff.crd=f.crd
        LEFT JOIN t_refirm  rf ON rf.crd=f.crd
        WHERE f.is_era=0 AND f.raum>=25e6 AND f.raum<500e6
          AND COALESCE(f.hnw_clients,0) >= {g['min_hnw_clients']}
          AND (CASE WHEN f.raum>0 THEN 1.0*COALESCE(f.hnw_aum,0)/f.raum ELSE 0 END
                 >= {g['min_hnw_share']}
               OR COALESCE(f.hnw_aum,0) >= {g['min_hnw_aum']})
    """).fetchall()


def fund_profile(conn, crds):
    """Latest-filing fund profile per firm: count, best type, highest minimum."""
    out = {}
    q = conn.execute("""
        WITH latest AS (SELECT s.crd AS crd, MAX(fc.filing_date) d FROM sched_d_7b1 s
                        JOIN filing_crd fc ON fc.filing_id=s.filing_id
                        WHERE s.crd IS NOT NULL GROUP BY s.crd)
        SELECT s.crd, l.d AS as_of, COUNT(DISTINCT s.fund_id) n,
               MAX(COALESCE(s.minimum_investment,0)) maxmin,
               GROUP_CONCAT(DISTINCT s.fund_type) types
        FROM sched_d_7b1 s
        JOIN filing_crd fc ON fc.filing_id=s.filing_id
        JOIN latest l ON l.crd=s.crd AND l.d=fc.filing_date
        GROUP BY s.crd, l.d""")
    for r in q:
        if r["crd"] in crds:
            out[r["crd"]] = r
    return out


def main() -> int:
    cfg = config.load()
    sc = yaml.safe_load((config.CONFIG_DIR / "scoring.yml").read_text(encoding="utf-8"))
    stamp = f"{cfg.stamp}|scoring.v{sc['config_version']}"
    conn = db.connect()
    conn.executescript(SCHEMA); conn.commit()
    build_sets(conn)

    g = sc["real_estate_segmentation"]["hnw_gate"]
    rows = gated(conn, g)
    tier_a = [r for r in rows if r["has_fund"] and not r["has_re"]]
    tier_c = [r for r in rows if not r["has_fund"]]
    print(f"gated universe {len(rows):,}   tier A {len(tier_a):,}   tier C {len(tier_c):,}")

    # ------------------------------------------------------------- tier A
    ta = sc["tier_a_ranking"]
    W, prof = ta["weights"], fund_profile(conn, {r["crd"] for r in tier_a})
    # Own-vehicle status, same instrument that produced the sponsor segment.
    src_by_crd, gav_by_crd = {}, {}
    for r in conn.execute("""
        WITH latest AS (SELECT s.crd AS crd, MAX(fc.filing_date) d FROM sched_d_7b1 s
          JOIN filing_crd fc ON fc.filing_id=s.filing_id WHERE s.crd IS NOT NULL
          GROUP BY s.crd)
        SELECT s.crd, f.raum, SUM(COALESCE(s.gross_asset_value,0)) gav
        FROM sched_d_7b1 s JOIN filing_crd fc ON fc.filing_id=s.filing_id
        JOIN latest l ON l.crd=s.crd AND l.d=fc.filing_date
        JOIN firm_current f ON f.crd=s.crd GROUP BY s.crd, f.raum"""):
        gav_by_crd[r["crd"]] = r["gav"]
        ratio = (r["gav"] / r["raum"]) if r["raum"] else None
        src_by_crd[r["crd"]] = ("unknown" if ratio is None else
                                "own_vehicles" if ratio >= 0.50 else
                                "mixed" if ratio >= 0.15 else "third_party")
    scored = []
    for r in tier_a:
        p = prof.get(r["crd"])
        n = p["n"] if p else 1
        mm = (p["maxmin"] or 0) if p else 0
        types = (p["types"] or "").split(",") if p else []
        s_aum = band(r["hnw_aum"] or 0, ta["hnw_aum_bands"])
        s_cnt = band(n, ta["fund_count_bands"])
        s_min = (band(mm, ta["min_investment_bands"]) if mm > 0
                 else ta["min_investment_unknown"])
        s_typ = max((ta["fund_type_scores"].get(t.strip(), 40) for t in types), default=40)
        total = (s_aum * W["hnw_aum"] + s_cnt * W["fund_count"]
                 + s_min * W["min_investment"] + s_typ * W["fund_type"]) / sum(W.values())
        src = src_by_crd.get(r["crd"], "unknown")
        if src == "own_vehicles":
            total -= ta["own_vehicle_penalty"]
        elif src == "mixed":
            total -= ta["mixed_vehicle_penalty"]
        best = max(types, key=lambda t: ta["fund_type_scores"].get(t.strip(), 0),
                   default="") if types else ""
        gav_total = gav_by_crd.get(r["crd"])
        ratio = (gav_total / r["raum"]) if (gav_total and r["raum"]) else None
        scored.append((r["crd"], total, s_aum, s_cnt, s_min, s_typ,
                       r["hnw_aum"], n, mm or None, best.strip(),
                       p["as_of"] if p else None, gav_total, ratio, src))
    scored.sort(key=lambda x: -x[1])
    cut = ta["working_list_size"]
    conn.execute("DELETE FROM tier_a_rank")
    conn.executemany(
        "INSERT INTO tier_a_rank (crd,rank,total_score,hnw_aum_score,fund_count_score,"
        "min_investment_score,fund_type_score,hnw_aum,fund_count,max_min_investment,"
        "best_fund_type,in_working_list,as_of_filing_date,fund_gav_total,"
        "fund_raum_ratio,fund_source,config_stamp)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(c, i + 1, t, a, fc, mi, ft, ha, n, mm, bt, 1 if i < cut else 0, ao,
          gt, rr, sr, stamp)
         for i, (c, t, a, fc, mi, ft, ha, n, mm, bt, ao, gt, rr, sr) in enumerate(scored)])
    conn.commit()
    print(f"tier A ranked, working list = top {cut}")

    # ------------------------------------------------------------- tier C
    tc = sc["tier_c_acubooth"]
    W2 = tc["weights"]
    prof2 = {r["crd"]: r for r in conn.execute(
        "SELECT crd, schwab_share_reported s, as_of_filing_date d FROM firm_custodian_profile")}
    out = []
    for r in tier_c:
        cp = prof2.get(r["crd"])
        share = cp["s"] if cp else None
        s_aum = band(r["hnw_aum"] or 0, tc["hnw_aum_bands"])
        s_cl = band(r["hnw_clients"] or 0, tc["hnw_client_bands"])
        s_sch = (share or 0) * 100.0        # pre-positioning only, see config caveat
        s_cap = band(r["iar_count"] or 0, tc["capacity_bands"])
        # Clients per rep, damped by average HNW assets per client. A high ratio
        # on a small book is a mass-affluent practice with accounts too small to
        # carry a 100-share option position.
        cpr = ((r["hnw_clients"] or 0) / r["iar_count"]) if r["iar_count"] else None
        per_client = ((r["hnw_aum"] or 0) / r["hnw_clients"]) if r["hnw_clients"] else 0
        s_cpr = band(cpr or 0, tc["clients_per_rep_bands"]) * band(
            per_client, tc["hnw_per_client_damping"], default=0.25)
        total = (s_aum * W2["hnw_aum"] + s_cl * W2["hnw_clients"]
                 + s_sch * W2["schwab_2026"] + s_cap * W2["capacity"]
                 + s_cpr * W2["clients_per_rep"]) / sum(W2.values())
        nd = (r["raum_nondisc"] / r["raum"]) if r["raum"] else None
        out.append((r["crd"], total, s_aum, s_cl, s_sch, s_cap, r["hnw_aum"],
                    r["hnw_clients"], share, cp["d"] if cp else None,
                    r["iar_count"], nd, cpr, s_cpr))
    out.sort(key=lambda x: -x[1])
    conn.execute("DELETE FROM tier_c_score")
    conn.executemany(
        "INSERT INTO tier_c_score (crd,rank,total_score,hnw_aum_score,hnw_clients_score,"
        "schwab_2026_score,capacity_score,hnw_aum,hnw_clients,schwab_share,schwab_as_of,"
        "iar_count,nondisc_share,clients_per_rep,clients_per_rep_score,config_stamp)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(c, i + 1, t, a, cl, sch, cap, ha, hc, sh, sd, iar, nd, cpr, scpr, stamp)
         for i, (c, t, a, cl, sch, cap, ha, hc, sh, sd, iar, nd, cpr, scpr) in enumerate(out)])
    conn.commit()
    print(f"tier C scored, {len(out):,} firms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
