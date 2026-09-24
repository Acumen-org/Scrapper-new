"""Derive firm_overlay from the ADV to 13F match.

Nine call sites read this table and nothing wrote it, so every query that
reached for the intersection came back empty or failed outright. It is a
projection of adv_13f_match rather than a source in its own right: a firm is
flagged when its match was accepted automatically, so a reviewer rejecting or
still holding a match keeps it out of the working lists that read this.

Rebuilt whole on each run. The input is itself rebuilt weekly, and an overlay
that accumulated stale flags would quietly widen the working list every week.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from prospect import config, db, runlog  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS firm_overlay (
    crd      TEXT PRIMARY KEY,
    phh_13f  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_overlay_13f ON firm_overlay (phh_13f);
"""


def main() -> int:
    cfg = config.load()
    conn = db.connect()
    conn.executescript(SCHEMA)
    conn.commit()

    with runlog.Run(conn, "firm_overlay", "derive", cfg.stamp) as run:
        conn.execute("DELETE FROM firm_overlay")
        conn.execute("""
            INSERT INTO firm_overlay (crd, phh_13f)
            SELECT DISTINCT crd, 1 FROM adv_13f_match
            WHERE status='auto' AND crd IS NOT NULL""")
        conn.commit()

        n = conn.execute(
            "SELECT COUNT(*) n FROM firm_overlay WHERE phh_13f=1").fetchone()["n"]
        run.rows_out = n
        run.note(f"{n} firms flagged phh_13f")
        print(f"firm_overlay: {n:,} firms also file 13F")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
