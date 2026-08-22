"""Ingest the historical Schedule D archive (2011-11-05 to 2024-12-31).

Schedule D is absent from the weekly feed, so this archive is the only bulk
source for custodian names and private fund detail. It is retained permanently:
thirteen years of history is the corpus for backtesting trigger definitions, not
a one time bootstrap.

Order matters. The Schedule D tables key on Filing ID and carry no CRD, so the
crosswalk loads first; without it nothing here can be attached to a firm.

    python -m scripts.ingest_schedule_d [--limit N]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, guard, runlog, snapshot  # noqa: E402

CROSSWALK_KEY = "adv_filing_crosswalk"
ARCHIVE_KEY = "schedule_d_archive"

# CSV fields in this archive run long (fund names, address blobs).
csv.field_size_limit(1 << 24)


def _int(v: str | None) -> int | None:
    if not v:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _reader(fh) -> csv.DictReader:
    return csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace"))


# ------------------------------------------------------------------ crosswalk

def fetch_crosswalk_member(cfg, src) -> bytes:
    """Range-fetch one member out of the 701 MB part 1 archive and inflate it.

    Pulling 95 MB instead of 701 MB. The offsets come from the archive's central
    directory and are asserted here: if the archive is rebuilt they move, and a
    wrong offset must fail loudly rather than quietly inflating garbage.
    """
    rng = src["range_fetch"]
    off, comp = rng["local_offset"], rng["compressed_bytes"]
    req = urllib.request.Request(src["url"], headers={
        "User-Agent": cfg.http["user_agent"],
        "Range": f"bytes={off}-{off + comp + 1024}",
    })
    raw = urllib.request.urlopen(req, timeout=cfg.http["timeout_seconds"]).read()
    if raw[:4] != b"PK\x03\x04":
        raise guard.SchemaViolation(
            f"{src['member']}: byte offset {off} is not a zip local file header. "
            "The upstream archive has been rebuilt and the configured offset is stale."
        )
    nlen = int.from_bytes(raw[26:28], "little")
    elen = int.from_bytes(raw[28:30], "little")
    name = raw[30:30 + nlen].decode("utf-8", "replace").split("/")[-1]
    if name != src["member"]:
        raise guard.SchemaViolation(
            f"offset {off} resolves to '{name}', expected '{src['member']}'. "
            "Configured offsets are stale."
        )
    body = raw[30 + nlen + elen:]
    return zlib.decompressobj(-zlib.MAX_WBITS).decompress(body)


def load_crosswalk(conn, cfg, run: runlog.Run) -> int:
    src = cfg.source(CROSSWALK_KEY)
    existing = conn.execute("SELECT COUNT(*) c FROM filing_crd").fetchone()["c"]
    if existing:
        print(f"crosswalk already loaded: {existing:,} rows")
        return existing

    print(f"range-fetching {src['member']} ...")
    blob = fetch_crosswalk_member(cfg, src)
    print(f"  inflated {len(blob):,} bytes")

    dest = snapshot.snapshot_path(CROSSWALK_KEY, "2024-12-31", src["member"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)
    snap_id, _ = snapshot.register(conn, CROSSWALK_KEY, "2024-12-31", dest, cfg.stamp)

    rdr = csv.DictReader(io.StringIO(blob.decode("utf-8-sig", errors="replace")))
    cols = list(rdr.fieldnames or [])
    guard.record_columns(conn, CROSSWALK_KEY, src["member"], cols, snap_id)
    guard.require_columns(cols, src["required_columns"], src["member"])

    fid_c, crd_c = src["columns"]["filing_id"], src["columns"]["crd"]
    rows, skipped = 0, 0
    batch = []
    for rec in rdr:
        fid, crd = (rec.get(fid_c) or "").strip(), (rec.get(crd_c) or "").strip()
        if not fid or not crd:
            skipped += 1
            continue
        batch.append((fid, crd, snap_id))
        if len(batch) >= 20000:
            conn.executemany("INSERT OR REPLACE INTO filing_crd VALUES (?,?,?)", batch)
            rows += len(batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT OR REPLACE INTO filing_crd VALUES (?,?,?)", batch)
        rows += len(batch)
    conn.commit()
    print(f"  crosswalk: {rows:,} filing to CRD pairs ({skipped:,} rows lacked one side)")
    run.note(f"crosswalk {rows} pairs")
    return rows


# --------------------------------------------------------------- Schedule D

TARGETS = {
    "sched_d_5k3": {
        "table": "sched_d_5k3",
        "fields": ["custodian_legal_name", "custodian_business_name", "city",
                   "state", "country", "is_related_person", "assets_held"],
        "ints": {"assets_held"},
    },
    "sched_d_7b1": {
        "table": "sched_d_7b1",
        "fields": ["fund_id", "fund_name", "fund_type", "fund_type_other",
                   "gross_asset_value", "minimum_investment", "state", "country",
                   "fund_of_funds"],
        "ints": {"gross_asset_value", "minimum_investment"},
    },
}


def load_table(conn, cfg, zf: zipfile.ZipFile, member_path: str, spec: dict,
               snap_id: int, limit: int | None, crosswalk: dict[str, str]) -> tuple[int, int]:
    key = spec["key"]
    target = TARGETS[key]
    colmap = spec["columns"]

    with zf.open(member_path) as fh:
        rdr = _reader(fh)
        cols = list(rdr.fieldnames or [])
        guard.record_columns(conn, ARCHIVE_KEY, spec["member"], cols, snap_id)
        guard.require_columns(cols, spec["required_columns"], spec["member"])

        crosswalk_hits = 0
        rows = 0
        fid_c = colmap["filing_id"]
        out_cols = ["snapshot_id", "filing_id", "crd"] + target["fields"] + ["raw_json"]
        sql = (f"INSERT INTO {target['table']} ({','.join(out_cols)}) "
               f"VALUES ({','.join('?' * len(out_cols))})")
        batch = []
        for rec in rdr:
            fid = (rec.get(fid_c) or "").strip()
            crd = crosswalk.get(fid) if fid else None
            if crd:
                crosswalk_hits += 1
            vals = [snap_id, fid or None, crd]
            for f in target["fields"]:
                raw = rec.get(colmap.get(f, ""), "")
                vals.append(_int(raw) if f in target["ints"] else (raw or None))
            vals.append(json.dumps({k: v for k, v in rec.items() if v}))
            batch.append(vals)
            rows += 1
            if len(batch) >= 10000:
                conn.executemany(sql, batch)
                conn.commit()
                batch.clear()
                print(f"    {rows:,} rows ...", end="\r")
            if limit and rows >= limit:
                break
        if batch:
            conn.executemany(sql, batch)
            conn.commit()
    return rows, crosswalk_hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="rows per table, for smoke tests")
    args = ap.parse_args()

    cfg = config.load()
    config.ensure_dirs()
    conn = db.connect()
    db.init(conn)
    src = cfg.source(ARCHIVE_KEY)

    snap = conn.execute(
        "SELECT * FROM snapshot WHERE source_key=? ORDER BY id DESC LIMIT 1",
        (ARCHIVE_KEY,)).fetchone()
    if snap is None:
        print(f"no {ARCHIVE_KEY} snapshot held; capture it first", file=sys.stderr)
        return 1
    archive = config.SNAPSHOT_DIR / snap["rel_path"]
    snap_id = int(snap["id"])

    with runlog.Run(conn, ARCHIVE_KEY, "ingest", cfg.stamp) as run:
        load_crosswalk(conn, cfg, run)
        crosswalk = {r["filing_id"]: r["crd"]
                     for r in conn.execute("SELECT filing_id, crd FROM filing_crd")}
        print(f"crosswalk in memory: {len(crosswalk):,} pairs")

        zf = zipfile.ZipFile(archive)
        names = {n.split("/")[-1]: n for n in zf.namelist()}
        total_rows = 0
        summary = []

        for spec in src["tables"]:
            member = spec["member"]
            path = names.get(member)
            if path is None:
                raise guard.SchemaViolation(
                    f"{member} absent from {archive.name}. Archive layout changed upstream."
                )
            existing = conn.execute(
                f"SELECT COUNT(*) c FROM {TARGETS[spec['key']]['table']}").fetchone()["c"]
            if existing:
                print(f"{spec['key']}: already loaded ({existing:,} rows), skipping")
                total_rows += existing
                summary.append((spec["key"], existing, None))
                continue

            print(f"{spec['key']}: loading {member} ...")
            rows, hits = load_table(conn, cfg, zf, path, spec, snap_id, args.limit, crosswalk)
            pct = (hits / rows * 100) if rows else 0.0
            print(f"  {rows:,} rows, {hits:,} resolved to a CRD ({pct:.1f}%)")
            total_rows += rows
            summary.append((spec["key"], rows, pct))

        run.rows_out = total_rows
        run.check_row_delta(total_rows, warn_pct=100.0)
        run.note("; ".join(
            f"{k}={n}" + (f" crd={p:.1f}%" if p is not None else "") for k, n, p in summary))

    print(f"\nconfig {cfg.stamp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
