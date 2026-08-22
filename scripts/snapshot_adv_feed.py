"""Step 1: capture this week's SEC adviser feed, immutably.

Runs before anything else and depends on no other decision. The upstream manifest
exposes only the current file and there is no archive, so every week we do not
capture is signal lost permanently.

The capture also validates: it parses the snapshot it just wrote, asserts the
structure the config declares, counts records, and compares that count against
the previous capture. A structural change fails the run. A volume change flags it.

    python -m scripts.snapshot_adv_feed [--force] [--source adv_feed|adv_state_feed]

The same machinery captures both weekly compilation feeds: the SEC-registered
firm feed and the state-registered one. They share a manifest and differ only
in structure, which each source's config declares.
"""

from __future__ import annotations

import argparse
import gzip
import sys
from pathlib import Path

from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, guard, net, runlog, snapshot  # noqa: E402


def pick_file(manifest: dict, prefix: str) -> dict:
    matches = [f for f in manifest.get("files", []) if f.get("name", "").startswith(prefix)]
    if not matches:
        names = [f.get("name") for f in manifest.get("files", [])]
        raise guard.SchemaViolation(
            f"manifest has no file starting '{prefix}'. Present: {names}. "
            "The SEC has changed the feed layout."
        )
    if len(matches) > 1:
        raise guard.SchemaViolation(
            f"manifest has {len(matches)} files starting '{prefix}', expected one: "
            f"{[m['name'] for m in matches]}"
        )
    return matches[0]


def validate_and_count(path: Path, expected: dict) -> tuple[int, dict[str, int]]:
    """Stream the captured feed, assert structure, return counts.

    Asserts against the first record only: a restructure is global, and parsing
    23,000 records twice buys nothing. Counts are taken over the whole file.
    """
    root_el = expected["root_element"]
    rec_el = expected["record_element"]
    total = 0
    by_type: dict[str, int] = {}
    checked = False

    with gzip.open(path, "rb") as fh:
        ctx = etree.iterparse(fh, events=("start", "end"))
        for event, el in ctx:
            if event == "start" and el.tag == root_el and total == 0:
                continue
            if event != "end" or el.tag != rec_el:
                continue
            if not checked:
                guard.assert_xml_record(el, expected, f"{path.name} first <{rec_el}>")
                checked = True
            total += 1
            rgstn = el.find("Rgstn")
            ftype = rgstn.get("FirmType") if rgstn is not None else None
            by_type[ftype or "unknown"] = by_type.get(ftype or "unknown", 0) + 1
            el.clear()
            while el.getprevious() is not None:
                del el.getparent()[0]

    if not checked:
        raise guard.SchemaViolation(
            f"{path.name}: no <{rec_el}> records found under <{root_el}>. "
            "Feed is empty or restructured."
        )
    return total, by_type


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="re-download even if this publication date is already held")
    ap.add_argument("--source", default="adv_feed",
                    choices=["adv_feed", "adv_state_feed"])
    args = ap.parse_args()
    source_key = args.source

    cfg = config.load()
    config.ensure_dirs()
    src = cfg.source(source_key)
    fetcher = net.Fetcher(cfg.http)

    conn = db.connect()
    db.init(conn)

    with runlog.Run(conn, source_key, "snapshot", cfg.stamp) as run:
        manifest = fetcher.json(src["manifest_url"])
        wanted = src["wanted"][0]
        entry = pick_file(manifest, wanted["filename_prefix"])
        filename = entry["name"]
        published = entry.get("date", "")
        print(f"manifest: {filename}  published {published}  ({entry.get('size')})")

        held = snapshot.held_for_date(conn, source_key, published)
        dest = snapshot.snapshot_path(source_key, published, filename)

        if held is not None and dest.exists() and not args.force:
            print(f"already held: snapshot {held['id']} at {held['rel_path']}")
            run.skip(f"{published} already captured, no action")
            return 0

        print(f"downloading -> {dest}")
        written = fetcher.download(src["base_url"] + filename, dest)
        print(f"  {written:,} bytes")
        run.rows_in = written

        snap_id, was_new = snapshot.register(conn, source_key, published, dest, cfg.stamp)
        print(f"snapshot id {snap_id} ({'new' if was_new else 'already registered'})")

        print("validating structure and counting records ...")
        total, by_type = validate_and_count(dest, src["expected_structure"])
        print(f"  {total:,} records")
        for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]):
            print(f"    {k:<12} {v:,}")

        rc = src.get("row_count", {})
        run.check_row_delta(total, rc.get("warn_pct_change", 5.0), rc.get("min_plausible"))
        run.note(f"{filename}; " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))

        if run.flagged:
            print(f"\nFLAGGED: {run.message}")
        print(f"\nconfig {cfg.stamp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
