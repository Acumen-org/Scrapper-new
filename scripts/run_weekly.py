"""The weekly cycle, as one command. Cron target and UI-button target alike.

Order matters and every step is idempotent:

  1. snapshot   capture this week's feed (skips when already held; a missed week
                is lost permanently upstream, so this always runs first)
  2. firms      parse the newest snapshot into the firm table
  3. diff       forward-looking triggers between the two newest firm snapshots
  4. rescore    tiers A and C from current data
  5. brochures  continue band coverage, a bounded slice per run
  6. cusip      re-verify the target CUSIP map when the last verification is
                more than 90 days old. Issuers rename and share classes change
                CUSIPs, and a ticker silently dropping to zero holders looks
                identical to a ticker nobody holds. A documented intention that
                nothing executes is not a control; this executes it.

Any step failing stops the run loudly. A silent partial pull that leaves stale
rows looking fresh is the worst failure this system can have.

    python -m scripts.run_weekly [--skip snapshot,brochures] [--brochure-slice N]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def run(mod: str, *args: str) -> None:
    cmd = [sys.executable, "-m", mod, *args]
    print(f"\n=== {mod} {' '.join(args)} ===")
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit(f"{mod} failed with exit code {r.returncode}; stopping the "
                         f"cycle so nothing downstream runs on partial data")


def cusip_due(conn, max_age_days: int = 90) -> bool:
    row = conn.execute(
        "SELECT MAX(finished_at) t FROM run_log WHERE source_key='cusip_map'"
        " AND status='ok'").fetchone()
    if not row or not row["t"]:
        return True
    age = (date.today() - date.fromisoformat(row["t"][:10])).days
    print(f"CUSIP map last verified {age} days ago"
          + (" (due)" if age > max_age_days else ""))
    return age > max_age_days


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip", default="", help="comma separated step names to skip")
    ap.add_argument("--brochure-slice", type=int, default=150)
    ap.add_argument("--web-slice", type=int, default=150)
    args = ap.parse_args()
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    if "snapshot" not in skip:
        run("scripts.snapshot_adv_feed")
        run("scripts.snapshot_adv_feed", "--source", "adv_state_feed")
    if "firms" not in skip:
        run("scripts.ingest_firms")
        run("scripts.ingest_firms", "--source", "adv_state_feed")
    # The static bulk archives and everything derived from them. All of
    # these are no-ops once held: the archives never change, the crosswalk
    # loads once, and enrich skips what it has already backfilled. They are
    # in the cycle so a fresh install builds its own schema rather than
    # needing a runbook.
    if "archive" not in skip:
        run("scripts.snapshot_archive", "--source", "schedule_d_archive")
        run("scripts.ingest_schedule_d")
        run("scripts.enrich")
        run("scripts.custodian_share")
        run("scripts.ingest_schedule_a")

    if "diff" not in skip:
        run("scripts.diff_snapshots")
        run("scripts.diff_snapshots", "--source", "adv_state_feed")
        run("scripts.build_firm_history")
    # After enrich (custodian_entity) and the diffs, before the rescore
    # that reads trigger priorities.
    if "triggers" not in skip:
        run("scripts.triggers")

    # The ADV-to-13F intersection, which the working lists and the review
    # queue both read.
    if "overlay" not in skip:
        run("scripts.ingest_13f_index")
        run("scripts.match_13f")
        run("scripts.build_overlay")

    if "rescore" not in skip:
        run("scripts.rank_tiers")
        run("scripts.segment_real_estate")
    if "brochures" not in skip:
        run("scripts.brochures", "--scope", "band",
            "--limit", str(args.brochure_slice))
    # Third-party websites, not SEC endpoints: slower, and a site being
    # down is normal rather than a failure. Sliced like the brochures so
    # the cycle keeps a predictable length.
    if "web" not in skip:
        run("scripts.web_enrich", "--limit", str(args.web_slice))

    if "cusip" not in skip:
        conn = db.connect()
        if cusip_due(conn):
            run("scripts.build_cusip_map", "--filings", "25")

    print("\nweekly cycle complete; check /health for flags")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
