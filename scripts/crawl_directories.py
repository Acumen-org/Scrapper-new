"""Read the directories and websites added on the Enrichment screen.

    python -m scripts.crawl_directories                 every source that is due
    python -m scripts.crawl_directories --source-id 3   one source, now
    python -m scripts.crawl_directories --probe-id 3    test one source and give a verdict

A source is due when it has never been read or its schedule says so. Each run
is bounded by the source's page budget and a time limit, so one large
directory cannot hold up every other job; what is left is read next time.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, contacts, db, directory, runlog  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-id", type=int)
    ap.add_argument("--probe-id", type=int)
    args = ap.parse_args()

    conn = db.connect()
    directory.init(conn)
    contacts.init(conn)

    if args.probe_id:
        r = directory.probe(conn, args.probe_id)
        print(f"probe {args.probe_id}: {r.get('verdict')}: {r.get('explanation')}")
        # A source that reads generically is read straight away, so the
        # records appear while the person who added it is still looking.
        if r.get("verdict") == "generic":
            out = directory.crawl(conn, args.probe_id)
            print(out.get("message", ""))
        return 0

    todo = ([directory.get_source(conn, args.source_id)] if args.source_id
            else directory.due_sources(conn))
    todo = [s for s in todo if s]
    conn.commit()
    if not todo:
        print("no directory is due")
        return 0
    cfg = config.load()
    with runlog.Run(conn, "directories", "crawl", cfg.stamp) as run:
        n = 0
        for s in todo:
            if s.get("needs_adapter") and not args.source_id:
                # Re-test a source that needed an adapter once its schedule
                # comes round: the site may have changed, or a browser may now
                # be installed.
                r = directory.probe(conn, s["id"])
                if r.get("verdict") != "generic":
                    directory.update_source(conn, s["id"], next_run_at=_later(s))
                    print(f"{s['name']}: still needs an adapter")
                    continue
            out = directory.crawl(conn, s["id"])
            n += out.get("records", 0)
            print(f"{s['name']}: {out.get('message', '')}")
        run.rows_out = n
    return 0


def _later(s) -> str:
    from datetime import datetime, timedelta, timezone
    days = int(s.get("schedule_days") or 7) or 30
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
