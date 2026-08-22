"""Owners and executive officers with their real titles, from Schedule A/B.

The individual feed gives names of registered reps but no positions; Schedule A
is where a firm names the people who run and own it, with titles as filed:
"MANAGING MEMBER & CHIEF COMPLIANCE OFFICER", "PRESIDENT", "CEO". This is the
only bulk source of who is in charge at each firm, which is exactly what a call
list needs.

Range-fetched out of the 701MB part 1 archive like the crosswalk was (72MB
instead of 701MB), snapshotted immutably, and reduced to each firm's LATEST
filing so the table holds the roster as last amended. Archive ends 2024-12-31;
every row carries the filing date it was true on.

    python -m scripts.ingest_schedule_a
"""

from __future__ import annotations

import csv
import io
import sys
import urllib.request
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, guard, runlog, snapshot  # noqa: E402

SOURCE_KEY = "schedule_a_b"

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule_a (
    crd            TEXT NOT NULL,
    name           TEXT NOT NULL,      -- as filed: LAST, FIRST, MIDDLE
    is_individual  INTEGER NOT NULL,   -- DE/FE/I column: I = 1
    title          TEXT,               -- Title or Status, as filed
    since          TEXT,               -- Status Acquired (MM/YYYY)
    ownership_code TEXT,               -- NA,A,B,C,D,E ownership brackets
    control_person TEXT,               -- Y/N
    as_of          TEXT,               -- date of the filing this roster is from
    PRIMARY KEY (crd, name, title)
);
CREATE INDEX IF NOT EXISTS ix_scha_crd ON schedule_a (crd);
"""

REQUIRED = ["FilingID", "Full Legal Name", "DE/FE/I", "Title or Status",
            "Ownership Code", "Control Person"]


def fetch_member(cfg, src) -> bytes:
    """Same range-fetch-and-assert pattern the crosswalk uses."""
    rng = src["range_fetch"]
    off, comp = rng["local_offset"], rng["compressed_bytes"]
    req = urllib.request.Request(src["url"], headers={
        "User-Agent": cfg.http["user_agent"],
        "Range": f"bytes={off}-{off + comp + 1024}",
    })
    raw = urllib.request.urlopen(req, timeout=cfg.http["timeout_seconds"]).read()
    if raw[:4] != b"PK\x03\x04":
        raise guard.SchemaViolation(
            f"{src['member']}: offset {off} is not a zip local header; the "
            "upstream archive was rebuilt and the configured offset is stale.")
    nlen = int.from_bytes(raw[26:28], "little")
    elen = int.from_bytes(raw[28:30], "little")
    name = raw[30:30 + nlen].decode("utf-8", "replace").split("/")[-1]
    if name != src["member"]:
        raise guard.SchemaViolation(
            f"offset {off} resolves to '{name}', expected '{src['member']}'.")
    return zlib.decompressobj(-zlib.MAX_WBITS).decompress(raw[30 + nlen + elen:])


def main() -> int:
    cfg = config.load()
    conn = db.connect()
    db.init(conn)
    conn.executescript(SCHEMA)

    with runlog.Run(conn, SOURCE_KEY, "ingest", cfg.stamp) as run:
        have = conn.execute("SELECT COUNT(*) n FROM schedule_a").fetchone()["n"]
        if have:
            print(f"schedule_a already loaded ({have:,} rows)")
            run.skip(f"already loaded {have} rows")
            return 0

        src = cfg.source(SOURCE_KEY)
        print(f"range-fetching {src['member']} ...")
        blob = fetch_member(cfg, src)
        print(f"  inflated {len(blob):,} bytes")
        dest = snapshot.snapshot_path(SOURCE_KEY, "2024-12-31", src["member"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        snap_id, _ = snapshot.register(conn, SOURCE_KEY, "2024-12-31", dest, cfg.stamp)

        fmap = {r["filing_id"]: (r["crd"], r["filing_date"]) for r in conn.execute(
            "SELECT filing_id, crd, filing_date FROM filing_crd")}
        print(f"  crosswalk: {len(fmap):,} filings")

        rdr = csv.DictReader(io.StringIO(blob.decode("utf-8-sig", errors="replace")))
        guard.require_columns(list(rdr.fieldnames or []), REQUIRED, src["member"])
        guard.record_columns(conn, SOURCE_KEY, src["member"], list(rdr.fieldnames), snap_id)

        # One pass, keeping only each firm's newest filing. FilingIDs are
        # assigned in submission order, so a bigger id is a newer roster.
        latest: dict[str, int] = {}
        rows: dict[str, list] = {}
        n = 0
        for rec in rdr:
            n += 1
            try:
                fid = int(rec["FilingID"])
            except (TypeError, ValueError):
                continue
            hit = fmap.get(str(fid))
            if hit is None:
                continue
            crd, fdate = hit
            if fid < latest.get(crd, 0):
                continue
            if fid > latest.get(crd, 0):
                latest[crd] = fid
                rows[crd] = []
            name = " ".join((rec.get("Full Legal Name") or "").split())
            if not name:
                continue
            rows[crd].append((
                crd, name,
                1 if (rec.get("DE/FE/I") or "").strip().upper() == "I" else 0,
                " ".join((rec.get("Title or Status") or "").split()) or None,
                (rec.get("Status Acquired") or "").strip() or None,
                (rec.get("Ownership Code") or "").strip() or None,
                (rec.get("Control Person") or "").strip() or None,
                fdate))

        guard.require_rows(len(rows), f"{src['member']} firms with a roster",
                           "every adviser files Schedule A; zero means the "
                           "crosswalk join or the parse broke")
        flat = [t for rs in rows.values() for t in rs]
        conn.executemany(
            "INSERT OR REPLACE INTO schedule_a VALUES (?,?,?,?,?,?,?,?)", flat)
        conn.commit()
        run.rows_in = n
        run.rows_out = len(flat)
        ind = sum(1 for t in flat if t[2] == 1)
        print(f"  {n:,} archive rows -> latest roster for {len(rows):,} firms, "
              f"{len(flat):,} people/entities ({ind:,} individuals)")
        run.note(f"{len(rows)} firms, {len(flat)} roster rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
