"""Bring Bellwether up to date with Cadence (prospect/cadence.py sync).

Run by the background worker every 10 minutes in a lane of its own. Each run
lifts a hold Cadence no longer warrants (it answers ping again), puts back
claims a dead process left, fails requests whose key is older than Cadence's
7 days, resends what is due, then reads every status change since the last
run, following nextCursor to the end. An opt-out it reads becomes a
do-not-contact here. It does nothing unless CADENCE_API_URL and
CADENCE_API_KEY are set and pushing is on in Settings, Cadence.

Its output holds counts only: never a contact's details, never the key.

    python -m scripts.cadence_sync
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import cadence, config, db, runlog  # noqa: E402


def main() -> int:
    cfg = config.load()
    conn = db.connect()
    cadence.init(conn)
    with runlog.Run(conn, "cadence_sync", "sync", cfg.stamp) as run:
        why = cadence.problem()
        if why:
            print(f"cadence: nothing to do; {why}")
            run.skip(why)
            return 0
        out = cadence.sync(conn)
        run.rows_out = out.get("changed", 0)
        line = (f"resent {out.get('sent', 0)}, unstuck {out.get('unstuck', 0)},"
                f" expired {out.get('expired', 0)}, pages {out.get('pages', 0)},"
                f" statuses {out.get('read', 0)}, changed {out.get('changed', 0)},"
                f" opted out {out.get('opted_out', 0)}")
        if out.get("held"):
            line += f"; queue held ({out['held']})"
        if out.get("error"):
            line += f"; status read failed ({out['error']})"
        run.note(line)
        print(f"cadence: {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
