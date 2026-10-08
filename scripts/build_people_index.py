"""Rebuild the People index (prospect/people_index.py).

Run by the background worker every half hour, so addresses, phones and
LinkedIn profiles the contact jobs find show up on the People screen without
anyone asking. Takes well under a minute on the full roster.

    python -m scripts.build_people_index
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, people_index, runlog  # noqa: E402


def main() -> int:
    cfg = config.load()
    conn = db.connect()
    with runlog.Run(conn, "people_index", "build", cfg.stamp) as run:
        n, secs = people_index.build(conn)
        run.rows_out = n
        cov = people_index.coverage(conn)
        run.note(f"rows={n} seconds={secs:.1f} email={cov.get('email', 0)}"
                 f" direct={cov.get('direct', 0)} linkedin={cov.get('linkedin', 0)}")
        print(f"people index: {n:,} rows in {secs:.1f}s; {cov.get('email', 0):,} with a usable email,"
              f" {cov.get('direct', 0):,} with a direct line, {cov.get('linkedin', 0):,} with LinkedIn")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
