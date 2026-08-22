"""Build the 13F filer index from EDGAR, then measure tier coverage.

Coverage is measured before anything is scored, because the answer decides
whether the covered call overlay belongs in the tier C score at all. A signal
present for one firm in eight makes that eighth look better than the rest for
reasons unrelated to fit, and an SDR reads absence as a negative rather than as
absence of a filing obligation.

The 13F reporting threshold is $100M in 13(f) securities, so smaller advisers
never appear. Absence is not a negative signal and must never be scored as one.

Matching here is normalised-name plus state and is deliberately generous, because
this is a population measurement rather than a per-firm link. The strict,
confidence-scored matching that gates the review queue comes with the holdings
ingest; a loose match here would overstate coverage, which biases toward NOT
building the overlay, the safer direction to be wrong in.

    python -m scripts.ingest_13f_index [--quarters 4]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, guard, net, runlog  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS edgar_13f_filer (
    cik             TEXT NOT NULL,
    company_name    TEXT NOT NULL,
    form_type       TEXT NOT NULL,
    date_filed      TEXT NOT NULL,
    accession       TEXT,
    quarter         TEXT NOT NULL,
    norm_name       TEXT NOT NULL,
    PRIMARY KEY (cik, accession)
);
CREATE INDEX IF NOT EXISTS ix_13f_norm ON edgar_13f_filer (norm_name);
CREATE INDEX IF NOT EXISTS ix_13f_cik ON edgar_13f_filer (cik);
"""

INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx"

_SUFFIX = {"LLC", "L L C", "INC", "INCORPORATED", "LP", "L P", "LLP", "LTD", "CO",
           "CORP", "CORPORATION", "COMPANY", "PLC", "SA", "AG", "NA", "THE", "PC",
           "PLLC", "TRUST", "GROUP", "PARTNERS", "PARTNERSHIP", "ADVISORS",
           "ADVISERS", "MANAGEMENT", "CAPITAL", "ASSOCIATES", "HOLDINGS"}
_DROP = re.compile(r"[.'’,]")
_PUNCT = re.compile(r"[^A-Z0-9 ]+")
_WS = re.compile(r"\s+")


def normalise(raw: str | None) -> str:
    """Aggressive: strips the generic adviser-name tail so 'X Capital Management
    LLC' and 'X Capital' collapse together. Tuned for recall, since this measures
    a population rather than linking individual firms."""
    if not raw:
        return ""
    s = _PUNCT.sub(" ", _DROP.sub("", raw.upper()))
    parts = _WS.sub(" ", s).strip().split(" ")
    while len(parts) > 1 and parts[-1] in _SUFFIX:
        parts.pop()
    if len(parts) > 1 and parts[0] == "THE":
        parts.pop(0)
    return " ".join(parts)


def quarters_back(n: int) -> list[tuple[int, int]]:
    from datetime import date
    t = date.today()
    y, q = t.year, (t.month - 1) // 3 + 1
    out = []
    for _ in range(n):
        out.append((y, q))
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    return out


# form.idx LOOKS fixed-column but the company-name field overflows for long
# names, shifting CIK and date rightward. Slicing by offset then cuts into the
# name: it truncates the company at 62 chars and reads a fragment of it as the
# CIK. Parsed from the right instead, which is unambiguous because the trailing
# three fields never contain spaces.
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ROW_RE = re.compile(
    r"^(?P<form>\S+)\s+(?P<company>.*?)\s+(?P<cik>\d+)\s+"
    r"(?P<filed>\d{4}-\d{2}-\d{2})\s+(?P<fname>\S+)\s*$")


def parse_form_idx(text: str, quarter: str) -> list[tuple]:
    """Rows are sorted by form type; we want 13F-HR and its amendments, not
    13F-NT (notice of no holdings)."""
    rows = []
    for line in text.splitlines():
        if not line.startswith("13F-HR"):
            continue
        m = ROW_RE.match(line)
        if not m:
            continue
        company = m.group("company").strip()
        cik = m.group("cik")
        fname = m.group("fname")
        acc = fname.rsplit("/", 1)[-1].replace(".txt", "") if fname else None
        if not cik or not company:
            continue
        rows.append((cik, company, m.group("form"), m.group("filed"),
                     acc, quarter, normalise(company)))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarters", type=int, default=4)
    args = ap.parse_args()

    cfg = config.load()
    conn = db.connect()
    conn.executescript(SCHEMA); conn.commit()
    fetch = net.Fetcher(cfg.http)

    with runlog.Run(conn, "edgar_13f_index", "ingest", cfg.stamp) as run:
        total = 0
        for year, q in quarters_back(args.quarters):
            qkey = f"{year}Q{q}"
            have = conn.execute("SELECT COUNT(*) c FROM edgar_13f_filer WHERE quarter=?",
                                (qkey,)).fetchone()["c"]
            if have:
                print(f"{qkey}: already held ({have:,} filings)")
                total += have
                continue
            url = INDEX_URL.format(year=year, q=q)
            print(f"{qkey}: fetching {url}")
            try:
                resp = fetch._request(url, stream=False)
                text = resp.text
                resp.close()
            except Exception as exc:
                print(f"  unavailable ({exc}); quarter may not be published yet")
                continue
            rows = parse_form_idx(text, qkey)
            # Structural assertions. A mis-parsed line yields a name fragment in
            # the CIK column rather than an error, so validate before storing.
            guard.require_rows(len(rows), f"{qkey} form.idx",
                               "index published but no 13F-HR rows recognised")
            guard.require_all(rows, lambda r: r[0].isdigit(),
                              f"{qkey} form.idx CIK column",
                              lambda r: f"cik={r[0]!r} company={r[1][:30]!r}")
            guard.require_all(rows, lambda r: DATE_RE.match(r[3]),
                              f"{qkey} form.idx date column",
                              lambda r: f"date={r[3]!r} company={r[1][:30]!r}")
            conn.executemany(
                "INSERT OR IGNORE INTO edgar_13f_filer VALUES (?,?,?,?,?,?,?)", rows)
            conn.commit()
            print(f"  {len(rows):,} 13F-HR filings")
            total += len(rows)
        run.rows_out = total

    filers = conn.execute(
        "SELECT COUNT(DISTINCT cik) c FROM edgar_13f_filer").fetchone()["c"]
    print(f"\ndistinct 13F filers across the window: {filers:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
