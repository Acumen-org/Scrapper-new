"""Ask the AI to find published contact details for people every free source missed.

One bounded slice: the best-placed people still without a confirmed email, a
direct or mobile phone, or a LinkedIn profile, after the website crawl and the
email hunt. Every detail the AI reports is checked on the page it cites before
it is stored, and emails then go through the mail-server check (see
prospect/research.py). Within the AI daily limit; a person is researched at
most once in 60 days.

    python -m scripts.ai_research [--limit 8] [--seconds 1200]
    python -m scripts.ai_research --crd 123456       that firm's people, now
    add --force to research people again inside their 60 days

Prints one line of aggregate numbers, never an address or a name.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import ai, config, contacts, db, research, runlog, verify  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=8, help="people to research, at most")
    ap.add_argument("--seconds", type=float, help="time budget (1200, or 420 with --crd)")
    ap.add_argument("--crd", help="research one firm's people now")
    ap.add_argument("--force", action="store_true", help="ignore the 60-day wait")
    args = ap.parse_args()

    conn = db.connect()
    contacts.init(conn)
    verify.init(conn)
    ai.init(conn)
    research.init(conn)
    crd = args.crd.strip() if args.crd else None
    limit = max(args.limit, 25) if crd and args.limit == 8 else args.limit
    # With --crd this runs inside scripts/find_contacts, which allows it 600s.
    seconds = args.seconds if args.seconds is not None else (420 if crd else 1200)
    cfg = config.load()
    with runlog.Run(conn, "ai_research", "enrich", cfg.stamp) as run:
        stats = research.run(conn, limit=limit, seconds=seconds, crd=crd,
                             force=args.force)
        line = research.summary(stats)
        if stats.get("not_enabled"):
            run.skip(line)
        else:
            run.rows_in = stats.get("people", 0)
            run.rows_out = stats.get("emails", 0) + stats.get("phones", 0) + stats.get("linkedin", 0)
            run.note(line[:300])
    conn.close()
    print(line)
    # A stop that will repeat (a rejected key, an unknown model) fails the
    # slice, so the worker backs off and the Jobs screen shows the reason.
    return 1 if stats.get("stopped_for_error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
