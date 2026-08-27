"""Enrich loaded archive data in place.

Three backfills, all additive. Nothing here overwrites a filed value: every
derived column sits alongside the original string or JSON it was derived from,
so a mapping decision can be reversed by amending config and re-running.

  1. Filing dates onto the crosswalk. The triggers order filings in time and
     cannot be computed without them.
  2. Custodian entity ids onto 5K3, from the maintained alias table, plus a
     review list of unmapped names above the configured row threshold.
  3. Owners and Minimum Investment onto 7B1, lifted out of raw_json. Both speak
     to whether a firm's clients can clear a private placement threshold.

    python -m scripts.enrich [--step dates|custodians|funds]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, custodians, db, runlog, snapshot  # noqa: E402

def _open_text(path):
    """The crosswalk member is stored inflated (ingest_schedule_d writes it that
    way), while the weekly feeds are gzipped. Sniff rather than assume, so
    either form reads correctly."""
    with open(path, "rb") as fh:
        magic = fh.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", encoding="utf-8-sig", errors="replace")
    return open(path, "rt", encoding="utf-8-sig", errors="replace")


DATE_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y")


def parse_date(s: str | None) -> str | None:
    """Return ISO date, or None. The archive stamps MM/DD/YYYY with a time.

    Parsed rather than string-compared: MM/DD/YYYY sorts by month first, which
    silently reorders years and corrupts anything recency-based.
    """
    s = (s or "").strip()
    if not s:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def add_column(conn, table: str, col: str, decl: str) -> bool:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col in cols:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()
    return True


# ------------------------------------------------------------------ 1. dates

def step_dates(conn, cfg) -> None:
    added = add_column(conn, "filing_crd", "filing_date", "TEXT")
    done = conn.execute(
        "SELECT COUNT(*) c FROM filing_crd WHERE filing_date IS NOT NULL").fetchone()["c"]
    if done and not added:
        print(f"filing dates already backfilled ({done:,})")
        return

    row = conn.execute(
        "SELECT rel_path FROM snapshot WHERE source_key='adv_filing_crosswalk'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    path = config.SNAPSHOT_DIR / row["rel_path"]
    print(f"reading dates from {path.name}")

    batch, n, unparsed = [], 0, 0
    with _open_text(path) as fh:
        for rec in csv.DictReader(fh):
            fid = (rec.get("FilingID") or "").strip()
            if not fid:
                continue
            d = parse_date(rec.get("DateSubmitted")) or parse_date(rec.get("Execution Date"))
            if d is None:
                unparsed += 1
                continue
            batch.append((d, fid))
            if len(batch) >= 20000:
                conn.executemany("UPDATE filing_crd SET filing_date=? WHERE filing_id=?", batch)
                n += len(batch); batch.clear()
    if batch:
        conn.executemany("UPDATE filing_crd SET filing_date=? WHERE filing_id=?", batch)
        n += len(batch)
    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS ix_filing_date ON filing_crd (crd, filing_date)")
    conn.commit()
    covered = conn.execute(
        "SELECT COUNT(*) c FROM filing_crd WHERE filing_date IS NOT NULL").fetchone()["c"]
    rng = conn.execute(
        "SELECT MIN(filing_date) a, MAX(filing_date) b FROM filing_crd"
        " WHERE filing_date IS NOT NULL").fetchone()
    print(f"  dated {covered:,} filings ({unparsed:,} unparsable), span {rng['a']} .. {rng['b']}")


# ------------------------------------------------------------- 2. custodians

def step_custodians(conn, cfg) -> None:
    add_column(conn, "sched_d_5k3", "custodian_entity", "TEXT")
    add_column(conn, "sched_d_5k3", "custodian_canonical", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_5k3_entity ON sched_d_5k3 (custodian_entity)")
    conn.commit()

    table = custodians.load()
    print(f"alias table: {len(table.entities)} entities, "
          f"{len(table.migrations)} dated migrations")

    rows = conn.execute(
        "SELECT custodian_business_name n, custodian_legal_name l, COUNT(*) c"
        " FROM sched_d_5k3 GROUP BY 1,2").fetchall()
    updates, unmapped = [], {}
    for r in rows:
        name = r["n"] or r["l"]
        eid = table.match(r["n"]) or table.match(r["l"])
        if eid is None:
            if name:
                unmapped[name] = unmapped.get(name, 0) + r["c"]
            continue
        updates.append((eid, table.canonical_name(eid), r["n"], r["l"]))

    for i in range(0, len(updates), 5000):
        conn.executemany(
            "UPDATE sched_d_5k3 SET custodian_entity=?, custodian_canonical=?"
            " WHERE custodian_business_name IS NOT DISTINCT FROM ? AND custodian_legal_name IS NOT DISTINCT FROM ?",
            updates[i:i + 5000])
        conn.commit()

    total = conn.execute("SELECT COUNT(*) c FROM sched_d_5k3").fetchone()["c"]
    mapped = conn.execute(
        "SELECT COUNT(*) c FROM sched_d_5k3 WHERE custodian_entity IS NOT NULL").fetchone()["c"]
    print(f"  mapped {mapped:,} / {total:,} rows ({mapped/total*100:.1f}%)")

    review = sorted(((n, c) for n, c in unmapped.items() if c >= table.review_threshold),
                    key=lambda kv: -kv[1])
    out = config.CONFIG_DIR / "custodians_review.txt"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("# Unmapped custodian names above the review threshold.\n")
        fh.write("# Add to config/custodians.yml or leave: the tail is one-off\n")
        fh.write("# trust companies that never appear again.\n")
        fh.write(f"# threshold: {table.review_threshold} rows\n\n")
        for n, c in review:
            fh.write(f"{c:>7}  {n}\n")
    tail = sum(c for n, c in unmapped.items() if c < table.review_threshold)
    print(f"  review list: {len(review)} names above {table.review_threshold} rows -> {out.name}")
    print(f"  unmapped tail below threshold: {len(unmapped)-len(review):,} names, {tail:,} rows")


# ------------------------------------------------------------------ 3. funds

def step_funds(conn, cfg) -> None:
    add_column(conn, "sched_d_7b1", "owners", "INTEGER")
    done = conn.execute(
        "SELECT COUNT(*) c FROM sched_d_7b1 WHERE owners IS NOT NULL").fetchone()["c"]
    if done:
        print(f"owners already backfilled ({done:,})")
    else:
        conn.execute(
            "UPDATE sched_d_7b1 SET owners ="
            " CAST(NULLIF(json_extract(raw_json,'$.Owners'),'') AS INTEGER)")
        conn.commit()
    r = conn.execute(
        "SELECT COUNT(*) n, SUM(owners IS NOT NULL) o,"
        " SUM(minimum_investment IS NOT NULL) m FROM sched_d_7b1").fetchone()
    print(f"  7B1 rows {r['n']:,}   owners {r['o']:,}   minimum_investment {r['m']:,}")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_7b1_cap ON sched_d_7b1 (owners, gross_asset_value)")
    conn.commit()


STEPS = {"dates": step_dates, "custodians": step_custodians, "funds": step_funds}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", choices=list(STEPS), default=None)
    args = ap.parse_args()
    cfg = config.load()
    conn = db.connect()
    todo = [args.step] if args.step else list(STEPS)
    for name in todo:
        print(f"\n--- {name} ---")
        with runlog.Run(conn, f"enrich:{name}", "enrich", cfg.stamp) as run:
            STEPS[name](conn, cfg)
    print(f"\nconfig {cfg.stamp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
