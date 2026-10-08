"""Read scanned brochures with Unlimited-OCR, a few per slice.

A brochure whose PDF has (almost) no text layer was filed as scanned images;
pdfplumber gets nothing from it, so its tags, its contacts and everything the
scores read from it were missing. This renders its pages, sends each one to
the OCR server set in Settings, Crawling (prospect/ocr.py), and then treats
the text like any other brochure's: saved, tagged, and queued again for the
contact reader. A brochure the server cannot read is marked failed and left;
a server that is down fails the slice, so the worker backs off and the
watchdog says so in Settings.

    python -m scripts.ocr_brochures [--limit 4]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from prospect import config, db, net, ocr, runlog  # noqa: E402
from scripts import brochures  # noqa: E402

TODO_SQL = """SELECT crd, version_id, pdf_path, pages FROM brochure
    WHERE status='ok' AND COALESCE(ocr_status,'') = ''
      AND COALESCE(text_chars,0) < 50 * GREATEST(COALESCE(pages,1),1)
    ORDER BY crd LIMIT ?"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pdf(row, fetch, contact) -> bytes | None:
    p = Path(row["pdf_path"] or "")
    if row["pdf_path"] and p.exists():
        return p.read_bytes()
    if row["version_id"]:
        return brochures.fetch_pdf(fetch, contact, row["version_id"])
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=4)
    args = ap.parse_args()
    if not ocr.configured():
        print("no OCR server set; scanned brochures wait")
        return 0
    cfg = config.load()
    tags_cfg = yaml.safe_load((config.CONFIG_DIR / "brochure_tags.yml").read_text(encoding="utf-8"))
    stamp = f"{cfg.stamp}|brochure_tags.v{int(tags_cfg['config_version'])}"
    conn = db.connect()
    ocr.init(conn)
    rows = conn.execute(TODO_SQL, (args.limit,)).fetchall()
    conn.commit()
    if not rows:
        print("no scanned brochures waiting")
        return 0
    fetch = net.Fetcher(cfg.http)
    done = failed = pages_read = 0
    with runlog.Run(conn, "brochure_ocr", "ocr", stamp) as run:
        for row in rows:
            crd = row["crd"]
            pdf = _pdf(row, fetch, cfg.http["user_agent"])
            if not pdf:
                conn.execute("UPDATE brochure SET ocr_status='failed', ocr_at=? WHERE crd=?",
                             (_now(), crd))
                conn.commit()
                failed += 1
                continue
            try:
                images = ocr.render_pages(pdf, int(tags_cfg["max_pages"]))
            except Exception:
                conn.execute("UPDATE brochure SET ocr_status='failed', ocr_at=? WHERE crd=?",
                             (_now(), crd))
                conn.commit()
                failed += 1
                continue
            # A server error stops the slice (and is reported); nothing is marked.
            text = "\n\n".join(ocr.read_page(img) for img in images).replace("\x00", "")
            pages_read += len(images)
            brochures.save_text(crd, text)
            tag_rows, neg_rows = brochures.tag_text(text, tags_cfg, stamp)
            brochures.write_tags(conn, crd, tag_rows, neg_rows)
            conn.execute("UPDATE brochure SET text_chars=?, ocr_status='ok', ocr_at=?, ocr_pages=?"
                         " WHERE crd=?", (len(text), _now(), len(images), crd))
            conn.commit()
            # The contact reader saw an empty brochure; let it read this one again.
            try:
                conn.execute("DELETE FROM contact_scan WHERE crd=?", (crd,))
                conn.commit()
            except Exception:
                conn.rollback()
            done += 1
            print(f"  {crd}: {len(images)} pages, {len(text):,} characters", flush=True)
        run.rows_out = done
        run.note(f"{done} scanned brochures read ({pages_read} pages), {failed} unreadable")
    conn.close()
    print(f"OCR read {done} scanned brochures ({pages_read} pages); {failed} could not be read")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
