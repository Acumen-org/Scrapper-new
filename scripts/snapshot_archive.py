"""Capture the SEC Form ADV historical bulk archives.

The weekly feeds carry current firm state only. Schedule D (private funds,
custodians) and the FilingID-to-CRD crosswalk exist solely in the pre-2025 FOIA
archives, which are static: captured once, retained permanently, and never
re-fetched unless the upstream file changes.

snapshot_adv_feed handles the two weekly feeds and is manifest-driven. These
archives have no manifest -- a fixed URL and a known coverage window -- so they
get their own capture path rather than bending that one out of shape.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from prospect import config, db, net, runlog, snapshot  # noqa: E402

ARCHIVES = ["adv_filing_crosswalk", "schedule_d_archive", "schedule_a_b"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="schedule_d_archive", choices=ARCHIVES)
    ap.add_argument("--force", action="store_true",
                    help="re-download even when a snapshot is already held")
    args = ap.parse_args()
    source_key = args.source

    cfg = config.load()
    config.ensure_dirs()
    src = cfg.source(source_key)
    fetcher = net.Fetcher(cfg.http)

    conn = db.connect()
    db.init(conn)

    # These archives are static, so the coverage end date is the publication
    # date: it is what actually distinguishes one capture from the next if the
    # SEC ever extends the window.
    published = str(src.get("coverage_end") or src.get("coverage_start") or "static")
    filename = src["url"].rsplit("/", 1)[-1]

    with runlog.Run(conn, source_key, "snapshot", cfg.stamp) as run:
        held = snapshot.latest(conn, source_key)
        dest = snapshot.snapshot_path(source_key, published, filename)

        if held is not None and dest.exists() and not args.force:
            print(f"already held: snapshot {held['id']} at {held['rel_path']}")
            run.skip(f"{published} already captured, no action")
            return 0

        print(f"downloading {src['url']}")
        print(f"  -> {dest}")
        written = fetcher.download(src["url"], dest)
        print(f"  {written:,} bytes")
        run.rows_in = written

        snap_id, was_new = snapshot.register(
            conn, source_key, published, dest, cfg.stamp)
        print(f"snapshot id {snap_id} ({'new' if was_new else 'already registered'})")
        run.note(f"{filename} ({written:,} bytes), coverage ending {published}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
