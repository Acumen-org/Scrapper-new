"""Bring the People index up to date (prospect/people_index.py).

Run by the background worker every half hour, so addresses, phones and
LinkedIn profiles the contact jobs find show up on the People screen without
anyone asking. Most runs only check: when nothing the index reads has changed
they stop there, and when a few firms changed they rebuild just those firms'
rows. A full build runs at least daily and whenever the roster, designations
or product lists change; --full asks for one now.

    python -m scripts.build_people_index [--full]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, people_index, runlog  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="rebuild the whole index now")
    args = ap.parse_args()
    cfg = config.load()
    conn = db.connect()
    people_index.init(conn)
    with runlog.Run(conn, "people_index", "build", cfg.stamp) as run:
        info: dict = {}
        n, secs = people_index.build(conn, full=args.full, info=info)
        mode = info.get("mode", "")
        if mode == "busy":
            print("people index: another process is building it; nothing done")
            run.skip("another process is building the index")
            return 0
        # rows_out is what this run wrote, so the throughput figures show the
        # work done rather than the size of the table.
        run.rows_out = info.get("written", n)
        what = {"no roster": "no roster loaded yet",
                "unchanged": "nothing it reads changed",
                "refresh": f"{info.get('firms', 0):,} firms refreshed in place,"
                           f" {info.get('written', 0):,} rows written",
                "full": f"full build ({info.get('why', '')})"}.get(mode, mode)
        cov = people_index.coverage(conn)
        run.note(f"{mode}: {what}; rows={n} seconds={secs:.1f} email={cov.get('email', 0)}"
                 f" direct={cov.get('direct', 0)} linkedin={cov.get('linkedin', 0)}")
        print(f"people index: {what}; {n:,} rows, {secs:.1f}s; {cov.get('email', 0):,} with a"
              f" usable email, {cov.get('direct', 0):,} with a direct line,"
              f" {cov.get('linkedin', 0):,} with LinkedIn")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
