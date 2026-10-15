"""Today's custodian names from each firm's current ADV, read only when new.

The Custodian refresh job (scripts/autopilot_slice.py, firm_refresh) downloads
the current ADV Part 1 PDF for firms that report custody or private funds and
keeps the custodian names it lists. This module holds its table, the rule for
when a firm is due again, and the reader.

A firm is read again only when there is something new to read:

  - it has never been read;
  - the weekly SEC feed shows a filing newer than the one last read;
  - the last good read is 180 days old, a safety net; or
  - the last attempt failed and its retry time has come: a day after the first
    failure, a week after the second, a month after any later one. A firm that
    files again is tried at once, whatever its wait.

The PDF's text is read with pdfium, which takes well under a second where
pdfplumber took half a minute and timed out on long filings. pdfplumber reads
the filing only when pdfium's text shows the custodian question without an
answer, or is not readable as an ADV at all.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS firm_refresh (
    crd         TEXT PRIMARY KEY,
    fetched_at  TEXT NOT NULL,
    pdf_bytes   INTEGER,
    custodians  TEXT,               -- pipe separated, deduped, as filed today
    status      TEXT NOT NULL       -- ok | fetch_failed | parse_failed
);
"""

# Columns added after the table first shipped.
#   filing_date  the newest ADV filing the last attempt covers: the feed's date
#                when it was read (reads from before this column count as
#                covering filings up to three days before the read)
#   fails        failed attempts in a row; 0 after a good read
#   next_try_at  when a failed firm may be tried again
COLUMNS = (("detail", "TEXT"), ("last_success_at", "TEXT"), ("filing_date", "TEXT"),
           ("fails", "INTEGER DEFAULT 0"), ("next_try_at", "TEXT"))

STALE_DAYS = 180                # a good read is redone after this, filing or not
RETRY_DAYS = (1, 7, 30)         # waits after the first, second and later failures
# IAPD can serve a new filing's PDF a day or so after the filing date. A read
# from before filing_date was kept is trusted for filings three days older.
LEGACY_MARGIN_DAYS = 3

# One rule, written once: the slice's queue and the job's backlog count must
# agree, or the job either spins on nothing or stops with work left.
_DUE_WHERE = """(f.q5k3='Y' OR f.q7b='Y')
      AND (r.crd IS NULL
           OR f.filing_date > r.filing_date
           OR (r.status = 'ok' AND r.fetched_at < {stale})
           OR (r.status != 'ok' AND COALESCE(r.next_try_at, r.fetched_at) <= {now}))"""

# Firms never read first, best firms first among them; then the longest waiting.
DUE_SQL = f"""SELECT f.crd, f.filing_date, COALESCE(r.fails, 0) AS fails
    FROM firm_current f
    LEFT JOIN firm_scope s ON s.crd=f.crd
    LEFT JOIN firm_refresh r ON r.crd=f.crd
    WHERE {_DUE_WHERE.format(stale='?', now='?')}
    ORDER BY (r.crd IS NOT NULL), r.fetched_at NULLS FIRST,
             s.priority DESC NULLS LAST LIMIT ?"""

# For the job registry (prospect/jobs.py); jobs._fill supplies the times.
BACKLOG_SQL = ("SELECT COUNT(*) n FROM firm_current f LEFT JOIN firm_refresh r ON r.crd=f.crd"
               " WHERE " + _DUE_WHERE.format(stale=f"'{{ago:{STALE_DAYS}}}'", now="'{now}'"))


def now_iso(t: datetime | None = None) -> str:
    return (t or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def due_params(limit: int, t: datetime | None = None) -> tuple:
    t = t or datetime.now(timezone.utc)
    return (now_iso(t - timedelta(days=STALE_DAYS)), now_iso(t), limit)


def retry_after(fails: int) -> timedelta:
    """How long a firm waits after its n-th failure in a row (n >= 1)."""
    return timedelta(days=RETRY_DAYS[min(max(fails, 1), len(RETRY_DAYS)) - 1])


def retry_words(fails: int) -> str:
    days = retry_after(fails).days
    return "tomorrow" if days == 1 else f"in {days} days"


def init(conn) -> None:
    from .db import add_column
    conn.executescript(SCHEMA)
    for col, decl in COLUMNS:
        add_column(conn, "firm_refresh", col, decl)
    conn.execute("UPDATE firm_refresh SET last_success_at=fetched_at"
                 " WHERE status='ok' AND last_success_at IS NULL")
    # Good reads from before filing_date was kept: they cover every filing
    # made a few days before the read, so only a later filing re-reads them.
    legacy = conn.execute("SELECT crd, fetched_at FROM firm_refresh"
                          " WHERE status='ok' AND filing_date IS NULL").fetchall()
    if legacy:
        conn.executemany("UPDATE firm_refresh SET filing_date=? WHERE crd=?",
                         [(covered_through(r["fetched_at"]), r["crd"]) for r in legacy])
    conn.commit()


def covered_through(read_at: str | None) -> str | None:
    """The filing date a read with no feed date is trusted to cover."""
    try:
        day = datetime.fromisoformat(read_at[:10]).date()
    except (TypeError, ValueError):
        return None
    return (day - timedelta(days=LEGACY_MARGIN_DAYS)).isoformat()


# ---------------------------------------------------------------- the reader

CUSTODIAN_RE = re.compile(r"Legal name of custodian:[ \t]*(?:\n[ \t]*)?([^\n]+)", re.I)
_LABEL = re.compile(r"Legal name of custodian", re.I)
# Every ADV Part 1 the SEC renders carries these; text without them is text
# pdfium could not decode, not a filing without custodians.
_ADV_MARKS = ("FORM ADV", "Schedule D")


def _clean(text: str) -> str:
    # pdfium ends lines with \r\n and pdfplumber with \n; the pattern wants \n.
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


def names_in(text: str, names: list[str] | None = None) -> list[str]:
    """Custodian legal names in a page's text, in order, without repeats."""
    names = [] if names is None else names
    for name in CUSTODIAN_RE.findall(_clean(text)):
        name = " ".join(name.split())
        if name and name not in names:
            names.append(name)
    return names


def pdfium_pages(pdf: bytes) -> list[str]:
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf)
    try:
        out = []
        for i in range(len(doc)):
            page = doc[i]
            textpage = page.get_textpage()
            try:
                out.append(textpage.get_text_range())
            finally:
                textpage.close()
                page.close()
        return out
    finally:
        doc.close()


def pdfplumber_pages(pdf: bytes):
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        for page in doc.pages:
            yield page.extract_text() or ""
            # pdfplumber otherwise retains every page's layout and chars.
            page.close()


def needs_second_reader(text: str, names: list[str]) -> bool:
    """Whether pdfium's text may be hiding names pdfplumber would find."""
    if names:
        return False
    if _LABEL.search(text):
        return True                  # the question is there; its answer was not read
    return not all(mark.lower() in text.lower() for mark in _ADV_MARKS)


def read_names(pdf: bytes) -> tuple[list[str], str]:
    """(custodian names, the reader that found them) for one ADV PDF."""
    try:
        pages = pdfium_pages(pdf)
    except Exception:
        pages = None                 # pdfium refused the file; pdfplumber may not
    if pages is not None:
        names: list[str] = []
        for text in pages:
            names_in(text, names)
        if not needs_second_reader(_clean("\n".join(pages)), names):
            return names, "pdfium"
    names = []
    for text in pdfplumber_pages(pdf):
        names_in(text, names)
    return names, "pdfplumber"
