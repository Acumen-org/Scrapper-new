"""Backfill website signals from pages the crawler already cached.

The crawler records signals as it goes (see prospect/websignals). This reads
every page still held in the web cache once, so firms crawled before signals
existed get them too, with no network use at all.

    python -m scripts.web_signals
"""

from __future__ import annotations

import gzip
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, runlog, websignals  # noqa: E402


def main() -> int:
    cfg = config.load()
    conn = db.connect()
    conn.executescript(websignals.SCHEMA)
    conn.commit()
    try:
        pages = conn.execute("SELECT url, crd, cache_path FROM web_page"
                             " WHERE cache_path IS NOT NULL").fetchall()
    except Exception:
        print("no web cache yet")
        return 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    found = read = 0
    with runlog.Run(conn, "web_signals", "derive", cfg.stamp) as run:
        for i, p in enumerate(pages, 1):
            try:
                html = gzip.decompress(Path(p["cache_path"]).read_bytes()).decode(
                    "utf-8", "replace")
            except OSError:
                continue
            read += 1
            found += websignals.record(conn, p["crd"], p["url"], html, now)
            if i % 500 == 0:
                conn.commit()
        conn.commit()
        n = conn.execute("SELECT COUNT(*) n FROM web_signal").fetchone()["n"]
        run.rows_out = n
        run.note(f"{read} cached pages read, {n} signals held")
        print(f"{read:,} cached pages read; {n:,} signals held")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
