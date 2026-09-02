"""Extract filed contact details from cached brochure PDFs.

A Part 2A brochure's cover page is required to carry the firm's contact
information, and in practice the first pages usually name a person, a phone
number, and often an email address. Sampled before building: of 25 cached
brochures, 24 carried a phone and 13 an email in the first two pages. These are
details the firm itself filed, not guesses, which makes them the only kind of
contact data this system is allowed to present as real.

Reads only PDFs already cached by scripts.brochures; fetches nothing. Scored
firms (tier A, the intersection, tier C) are processed first so the lists the
team actually calls from fill in before the long tail.

    python -m scripts.extract_brochure_contacts [--limit N]
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import db, runlog, config  # noqa: E402

PAGES = 3  # cover page, table of contents, and one page of slack

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\(\d{3}\)\s?|\d{3}[-.\s])\d{3}[-.\s]\d{4}")

# Addresses that appear on cover pages but belong to the regulator or the
# platform, not the firm.
SKIP_EMAIL = ("sec.gov", "finra.org", "adviserinfo")


def norm_phone(raw: str) -> str:
    d = re.sub(r"\D", "", raw)
    return f"({d[:3]}) {d[3:6]}-{d[6:10]}" if len(d) == 10 else raw.strip()


def context_line(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            return " ".join(line.split())[:120]
    return ""


def extract(pdf_path: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(emails, phones) as (value, context) pairs from the first pages."""
    import pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        # NUL bytes from embedded fonts are rejected by Postgres text columns.
        text = "\n".join((p.extract_text() or "") for p in pdf.pages[:PAGES]).replace("\x00", "")
    emails, phones = [], []
    for m in EMAIL_RE.findall(text):
        low = m.lower().strip(".")
        if any(s in low for s in SKIP_EMAIL) or low in (e for e, _ in emails):
            continue
        emails.append((low, context_line(text, m)))
    seen = set()
    for m in PHONE_RE.findall(text):
        p = norm_phone(m)
        if p not in seen:
            seen.add(p)
            phones.append((p, context_line(text, m.strip())))
    return emails[:4], phones[:3]


def todo_query(limit: int) -> str:
    # Scored firms first: tier A rank order, then intersection, then tier C
    # score, then everything else with a cached brochure.
    return f"""
        SELECT b.crd, b.pdf_path FROM brochure b
        LEFT JOIN tier_a_rank ta ON ta.crd = b.crd
        LEFT JOIN (SELECT crd, MAX(total_score) sc FROM tier_c_score GROUP BY crd) tc
               ON tc.crd = b.crd
        WHERE b.status='ok'
          AND b.crd NOT IN (SELECT crd FROM contact_scan)
        ORDER BY (ta.crd IS NULL), ta.rank, (tc.crd IS NULL), tc.sc DESC
        LIMIT {int(limit)}"""


SCAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS contact_scan (
    crd        TEXT PRIMARY KEY,      -- brochure scanned for contact details
    scanned_at TEXT NOT NULL,
    n_email    INTEGER NOT NULL,
    n_phone    INTEGER NOT NULL,
    status     TEXT NOT NULL          -- ok | unreadable
);
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()

    cfg = config.load()
    conn = db.connect()
    conn.executescript(SCAN_SCHEMA)

    with runlog.Run(conn, "brochure_contacts", "extract", cfg.stamp) as run:
        rows = conn.execute(todo_query(args.limit)).fetchall()
        if not rows:
            print("nothing left to scan")
            run.skip("all cached brochures scanned")
            return 0
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        got_e = got_p = bad = 0
        for r in rows:
            try:
                emails, phones = extract(r["pdf_path"])
                status = "ok"
            except Exception:
                emails, phones, status = [], [], "unreadable"
                bad += 1
            for val, ctx in emails:
                conn.execute("""INSERT OR IGNORE INTO firm_contact_info
                    (crd, kind, value, source, context, found_at)
                    VALUES (?,?,?,?,?,?)""",
                             (r["crd"], "email", val, "brochure", ctx, now))
            for val, ctx in phones:
                conn.execute("""INSERT OR IGNORE INTO firm_contact_info
                    (crd, kind, value, source, context, found_at)
                    VALUES (?,?,?,?,?,?)""",
                             (r["crd"], "phone", val, "brochure", ctx, now))
            conn.execute("INSERT OR REPLACE INTO contact_scan VALUES (?,?,?,?,?)",
                         (r["crd"], now, len(emails), len(phones), status))
            got_e += bool(emails)
            got_p += bool(phones)
            conn.commit()
        print(f"scanned {len(rows)}: {got_e} with email, {got_p} with phone, "
              f"{bad} unreadable")
        run.note(f"scanned={len(rows)} email={got_e} phone={got_p} bad={bad}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
