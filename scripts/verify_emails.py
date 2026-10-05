"""Check email addresses against their mail servers, in slices.

Every email in contact_point gets a mailbox-level check (prospect.verify):
personal addresses before shared inboxes, firms that score highest on a product
list first, so a partial run always covers the people who get called. One run
is one slice; the background worker calls it again for the next slice.

Picked for a slice: never checked, queued from a firm page, last checked more
than 90 days ago, or "unknown" more than 7 days ago. The last one is there
because unknown is usually temporary (greylisting, a server that was down),
and without a shorter retry those rows would wait three months for a second
try.

    python -m scripts.verify_emails [--limit 60]          one bulk slice
    python -m scripts.verify_emails --crd 123456          every email at one firm
    python -m scripts.verify_emails --emails a@x.com,b@y.com   ad hoc, stores nothing
    python -m scripts.verify_emails --status              which engine would run, and why
    add --engine auto|reacher|native|dns to override the Settings choice
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, contacts, db, runlog, verify  # noqa: E402

RECHECK_DAYS = 90
RETRY_UNKNOWN_DAYS = 7


def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def todo(conn, limit: int) -> list[int]:
    args = (_ago(RECHECK_DAYS), _ago(RETRY_UNKNOWN_DAYS), limit)
    due = """cp.kind = 'email'
          AND (cp.verify_status IN ('unverified', 'queued')
               OR cp.verified_at < ?
               OR (cp.verify_status = 'unknown' AND cp.verified_at < ?))"""
    try:
        rows = conn.execute(f"""
            SELECT cp.id FROM contact_point cp
            LEFT JOIN firm_scope s ON s.crd = cp.crd
            WHERE {due}
            ORDER BY (cp.verify_status = 'queued') DESC, cp.is_role,
                     (s.crd IS NULL), s.priority DESC, cp.confidence DESC, cp.id
            LIMIT ?""", args).fetchall()
    except Exception:
        # No product lists built yet on this install: plain order still works.
        conn.rollback()
        rows = conn.execute(f"""
            SELECT cp.id FROM contact_point cp WHERE {due}
            ORDER BY (cp.verify_status = 'queued') DESC, cp.is_role,
                     cp.confidence DESC, cp.id
            LIMIT ?""", args).fetchall()
    return [r["id"] for r in rows]


def summary(res: dict) -> str:
    parts = [f"{s} {res[s]}" for s in verify.STATUSES if res.get(s)]
    return (f"checked {res.get('checked', 0)} via {res.get('engine')}: "
            + (", ".join(parts) if parts else "nothing to check"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--emails", help="comma separated addresses to check without storing")
    ap.add_argument("--crd", help="verify every email held for one firm")
    ap.add_argument("--status", action="store_true", help="print the engine status as JSON")
    ap.add_argument("--engine", choices=("auto",) + verify.ENGINES)
    args = ap.parse_args()

    if args.status:
        print(json.dumps(verify.engine_status(refresh=True), indent=2))
        return 0

    if args.emails:
        emails = [e.strip() for e in args.emails.split(",") if e.strip()]
        print(json.dumps(verify.check_many(emails, args.engine), indent=2))
        return 0

    conn = db.connect()
    contacts.init(conn)
    verify.init(conn)

    if args.crd:
        # The firm page button: everything at one firm, people before inboxes.
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM contact_point WHERE crd=? AND kind='email'"
            " ORDER BY is_role, id", (args.crd.strip(),)).fetchall()]
        conn.commit()
        print(summary(verify.verify_contacts(conn, ids, args.engine)))
        return 0

    ids = todo(conn, args.limit)
    conn.commit()
    cfg = config.load()
    with runlog.Run(conn, "verify_emails", "verify", cfg.stamp) as run:
        run.rows_in = len(ids)
        if not ids:
            run.skip("nothing due for a check")
            line = "checked 0: nothing due for a check"
        else:
            res = verify.verify_contacts(conn, ids, args.engine)
            run.rows_out = res.get("checked", 0)
            line = summary(res)
            run.note(line)
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
