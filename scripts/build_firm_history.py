"""Per-filing AUM and headcount history, from the retained Base_A archive.

The archive crosswalk member already held on disk carries 5F.(2)(c) regulatory
AUM and 5B.(1) rep count for every filing back to 2011, which turns the firm
page's single current number into a trajectory. Current-feed snapshots extend
the line weekly from here.

    python -m scripts.build_firm_history
"""

from __future__ import annotations

import csv
import gzip
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, guard, runlog  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS firm_history (
    crd         TEXT NOT NULL,
    filing_date TEXT NOT NULL,
    raum        INTEGER,
    iar_count   INTEGER,
    source      TEXT NOT NULL,      -- archive | feed
    PRIMARY KEY (crd, filing_date)
);
CREATE INDEX IF NOT EXISTS ix_fh_crd ON firm_history (crd, filing_date);
"""

DATE_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y")


def pdate(s):
    s = (s or "").strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            continue
    return None


def main() -> int:
    cfg = config.load()
    conn = db.connect()
    conn.executescript(SCHEMA); conn.commit()

    have = conn.execute("SELECT COUNT(*) n FROM firm_history WHERE source='archive'"
                        ).fetchone()["n"]
    with runlog.Run(conn, "firm_history", "derive", cfg.stamp) as run:
        if not have:
            row = conn.execute(
                "SELECT rel_path FROM snapshot WHERE source_key='adv_filing_crosswalk'"
                " ORDER BY id DESC LIMIT 1").fetchone()
            path = config.SNAPSHOT_DIR / row["rel_path"]
            print(f"streaming {path.name}")
            batch, n = [], 0
            with gzip.open(path, "rt", encoding="utf-8-sig", errors="replace") as fh:
                rdr = csv.DictReader(fh)
                guard.require_columns(list(rdr.fieldnames or []),
                                      ["1E1", "DateSubmitted", "5F2c", "5B1"],
                                      "Base_A history columns")
                for rec in rdr:
                    crd = (rec.get("1E1") or "").strip()
                    d = pdate(rec.get("DateSubmitted"))
                    if not crd or not d:
                        continue
                    def num(v):
                        try:
                            return int(float(v))
                        except (TypeError, ValueError):
                            return None
                    batch.append((crd, d, num(rec.get("5F2c")),
                                  num(rec.get("5B1")), "archive"))
                    if len(batch) >= 20000:
                        conn.executemany(
                            "INSERT OR REPLACE INTO firm_history VALUES (?,?,?,?,?)",
                            batch)
                        n += len(batch); batch.clear()
            if batch:
                conn.executemany(
                    "INSERT OR REPLACE INTO firm_history VALUES (?,?,?,?,?)", batch)
                n += len(batch)
            conn.commit()
            print(f"  archive filings stored: {n:,}")
            run.rows_out = n
        else:
            print(f"archive history already built ({have:,} rows)")

        # append every held feed snapshot as a point on the line (idempotent)
        conn.execute("""
            INSERT OR REPLACE INTO firm_history
            SELECT f.crd, f.filing_date, f.raum, f.iar_count, 'feed'
            FROM firm f WHERE f.filing_date IS NOT NULL""")
        conn.commit()
        pts = conn.execute("SELECT COUNT(*) n FROM firm_history").fetchone()["n"]
        firms = conn.execute("SELECT COUNT(DISTINCT crd) n FROM firm_history").fetchone()["n"]
        print(f"history: {pts:,} points across {firms:,} firms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
