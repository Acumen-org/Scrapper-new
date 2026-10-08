"""Retired: email candidates now come only from the email hunter.

This job used to write one guessed address per person into contact_point and
leave it there unverified; on the live server that came to some 160,000
addresses nobody could trust. prospect/hunt.py (scripts/hunt_emails.py)
replaced it: it asks the firm's mail server about each likely address and
stores one only when the server confirms it.

What is left here is the name, for the callers that still use it:

  generate_for_firm()   the firm page's "find emails" button. It now puts the
                        firm at the front of the hunter's queue (people given
                        up on are looked at again) and writes no address.
  main()                one short hunting slice, for anyone who still runs
                        `python -m scripts.infer_emails` by hand.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import contacts, db, hunt, verify  # noqa: E402


def generate_for_firm(conn, crd, firm_pat=None, fallback=None) -> int:
    """Queue one firm for the email hunter. Returns how many people at it were
    put back in line. firm_pat and fallback are accepted for the old callers
    and unused: the hunter learns the firm's pattern itself."""
    contacts.init(conn)
    verify.init(conn)
    n = hunt.queue_firm(conn, str(crd), reset=True)
    try:
        from prospect import jobs
        jobs.init(conn)
        jobs.request_run(conn, "email_hunt")
    except Exception:
        conn.rollback()
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--crd")
    ap.add_argument("--seconds", type=float, default=300)
    args = ap.parse_args()
    conn = db.connect()
    contacts.init(conn)
    verify.init(conn)
    try:
        stats = hunt.run(conn, limit=args.limit, seconds=args.seconds,
                         crd=args.crd.strip() if args.crd else None)
    except hunt.NoEngine as e:
        conn.close()
        print(str(e)[:300], file=sys.stderr)
        return 2
    conn.close()
    print(hunt.summary(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
