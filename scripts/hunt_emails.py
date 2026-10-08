"""Find a confirmed email for everyone at the best firms, one bounded slice at a time.

Each run takes the in-scope firms whose turn has come, best first, and for
each person without a usable email asks the firm's mail server about their
likely addresses until one is confirmed or all are turned away (see
prospect/hunt.py for the rules). Only a confirmed address is ever stored.
The background worker runs it every few minutes; nothing to do is a quick
exit.

    python -m scripts.hunt_emails [--limit 300] [--seconds 540]
    python -m scripts.hunt_emails --crd 123456 [--seconds 120]   one firm, now
    add --engine auto|reacher|native to override the Settings choice

Prints one line of aggregate numbers. Never an address or a name: the output
lands in job logs and CI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, contacts, db, hunt, runlog, verify  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=300, help="people to hunt in this run, at most")
    ap.add_argument("--seconds", type=float, default=540, help="time budget for this run")
    ap.add_argument("--crd", help="hunt one firm now, whatever its turn")
    ap.add_argument("--engine", choices=("auto", "reacher", "native"))
    ap.add_argument("--workers", type=int, default=hunt.WORKERS)
    args = ap.parse_args()

    conn = db.connect()
    contacts.init(conn)
    verify.init(conn)
    hunt.init(conn)
    cfg = config.load()
    crd = args.crd.strip() if args.crd else None
    no_engine = None
    with runlog.Run(conn, "email_hunt", "verify", cfg.stamp) as run:
        try:
            stats = hunt.run(conn, limit=args.limit, seconds=args.seconds, crd=crd,
                             engine=args.engine, workers=args.workers)
            line = hunt.summary(stats)
            run.rows_in = stats.get("people", 0)
            run.rows_out = stats.get("found", 0)
            run.note(line[:300])
        except hunt.NoEngine as e:
            no_engine = str(e)[:300]
            run.skip(no_engine)
    conn.close()
    if no_engine:
        # Exit non-zero so the background worker backs off for an hour and the
        # Jobs screen shows why, instead of re-running an empty slice.
        print(no_engine, file=sys.stderr)
        return 2
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
