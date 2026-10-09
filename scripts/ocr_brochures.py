"""Read the brochures that came back without a text layer, a few per slice.

pdfplumber reads almost every brochure exactly. The few it reads as (nearly)
empty are mostly the SEC's fillable Form ADV template, where the firm's text
sits in form fields, and a handful of scans (prospect/ocr.py has the detail).
This reads each one the right way (form fields, else OCR on its image pages),
then treats the text like any other brochure's: saved, tagged, and queued
again for the contact reader. A blank template is marked as such and left.

A scan with no OCR engine on the machine is marked 'waiting' and picked up
again once an engine is there. One brochure that cannot be read never stops
the others.

    python -m scripts.ocr_brochures [--limit 6]
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

THIN = "COALESCE(text_chars,0) < 50 * GREATEST(COALESCE(pages,1),1)"
TODO_SQL = f"""SELECT crd, version_id, pdf_path, pages FROM brochure
    WHERE status='ok' AND COALESCE(ocr_status,'') = '' AND {THIN}
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


def _mark(conn, crd: str, status: str, method: str | None = None) -> None:
    conn.execute("UPDATE brochure SET ocr_status=?, ocr_method=?, ocr_at=? WHERE crd=?",
                 (status, method, _now(), crd))
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=6)
    args = ap.parse_args()
    cfg = config.load()
    tags_cfg = yaml.safe_load((config.CONFIG_DIR / "brochure_tags.yml").read_text(encoding="utf-8"))
    stamp = f"{cfg.stamp}|brochure_tags.v{int(tags_cfg['config_version'])}"
    conn = db.connect()
    ocr.init(conn)
    if ocr.engine():
        # Scans that were waiting for an engine get their turn now there is one.
        conn.execute("UPDATE brochure SET ocr_status=NULL WHERE ocr_status='waiting'")
        conn.commit()
    rows = conn.execute(TODO_SQL, (args.limit,)).fetchall()
    conn.commit()
    if not rows:
        print("no brochures without a text layer waiting")
        return 0
    fetch = net.Fetcher(cfg.http)
    counts: dict[str, int] = {}
    pages_read = 0
    with runlog.Run(conn, "brochure_ocr", "ocr", stamp) as run:
        for row in rows:
            crd = row["crd"]
            pdf = _pdf(row, fetch, cfg.http["user_agent"])
            if not pdf:
                _mark(conn, crd, "failed")
                counts["unavailable"] = counts.get("unavailable", 0) + 1
                continue
            try:
                text, method, pages = ocr.read_pdf(pdf, int(tags_cfg["max_pages"]))
            except ocr.OCRError as e:
                status = "waiting" if not ocr.engine() else "failed"
                _mark(conn, crd, status)
                counts[status] = counts.get(status, 0) + 1
                print(f"  {crd}: {status} ({e})", flush=True)
                continue
            except Exception as e:      # a malformed PDF must not stop the rest
                _mark(conn, crd, "failed")
                counts["failed"] = counts.get("failed", 0) + 1
                print(f"  {crd}: failed ({type(e).__name__})", flush=True)
                continue
            if method == "empty":
                _mark(conn, crd, "empty", "empty")
                counts["empty"] = counts.get("empty", 0) + 1
                print(f"  {crd}: blank template, nothing to read", flush=True)
                continue
            pages_read += pages
            brochures.save_text(crd, text)
            tag_rows, neg_rows = brochures.tag_text(text, tags_cfg, stamp)
            brochures.write_tags(conn, crd, tag_rows, neg_rows)
            conn.execute("UPDATE brochure SET text_chars=?, ocr_status='ok', ocr_method=?, ocr_at=?,"
                         " ocr_pages=? WHERE crd=?", (len(text), method, _now(), pages, crd))
            conn.commit()
            # The contact reader saw an empty brochure; let it read this one again.
            try:
                conn.execute("DELETE FROM contact_scan WHERE crd=?", (crd,))
                conn.commit()
            except Exception:
                conn.rollback()
            counts[method] = counts.get(method, 0) + 1
            print(f"  {crd}: {method}, {len(text):,} characters", flush=True)
        run.rows_out = sum(v for k, v in counts.items() if k in ("form", "tesseract", "unlimited-ocr"))
        note = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        run.note(f"{note}; {pages_read} scanned pages read")
    conn.close()
    print(f"brochures without a text layer: {note}; {pages_read} scanned pages read")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
