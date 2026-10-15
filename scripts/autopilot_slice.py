"""One slice of the two jobs that have no script of their own.

  firm_refresh   fetch current ADV Part 1 PDFs for firms that report custody
                 or private funds (Q5K3 or Q7B yes) and read today's custodian
                 names, refreshing data whose bulk source ends 2024-12-31. A
                 firm is read again only after it files a new ADV, after 180
                 days, or when a failed read's wait is over
                 (prospect/custodian_refresh.py has the rules and the reader)
  cusip_verify   re-verify the target security map when older than 90 days

These used to live inside the worker loop. They are separate processes now,
like every other job, so the worker only schedules.

    python -m scripts.autopilot_slice firm_refresh
    python -m scripts.autopilot_slice cusip_verify
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, net  # noqa: E402
from prospect import custodian_refresh as refresh  # noqa: E402

# Kept under their old names for the checks that import them.
REFRESH_SCHEMA = refresh.SCHEMA
DUE_SQL = refresh.DUE_SQL

FILING_TIMEOUT_S = 110      # one filing, download and reading together
SLICE_BUDGET_S = 120        # no new filing is started after this


def refresh_one(crd: str) -> dict:
    """Run in an isolated process: pdfium is native code, so a malformed PDF
    that hangs or crashes it is stopped and recorded as a failure, and cannot
    end the slice or come back first in line every slice."""
    if not crd.isdigit():
        raise ValueError("CRD must be numeric")
    cfg = config.load()
    fetch = net.Fetcher(dict(cfg.http, timeout_seconds=25, retries=2))
    pdf = bytearray()
    try:
        with fetch._request(
                f"https://reports.adviserinfo.sec.gov/reports/ADV/{crd}/PDF/{crd}.pdf",
                stream=True) as response:
            for chunk in response.iter_content(65536):
                pdf.extend(chunk)
                if len(pdf) > 40 * 1024 * 1024:
                    return {"status": "fetch_failed", "detail": "PDF exceeds 40 MB"}
    except net.FetchError:
        return {"status": "fetch_failed", "detail": "SEC filing unavailable"}
    finally:
        fetch.session.close()
    try:
        names, reader = refresh.read_names(bytes(pdf))
        detail = f"{len(names)} custodian names in current filing"
        if reader != "pdfium":
            detail += f", read by {reader}"
        return {"status": "ok", "pdf_bytes": len(pdf), "custodians": "|".join(names) or None,
                "detail": detail}
    except Exception as exc:
        return {"status": "parse_failed", "pdf_bytes": len(pdf),
                "detail": f"PDF could not be read ({type(exc).__name__})"}


def read_filing(crd: str) -> dict:
    """One filing in its own process, stopped at FILING_TIMEOUT_S."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "scripts.autopilot_slice", "firm_refresh_one", crd],
            cwd=config.ROOT, capture_output=True, text=True, encoding="utf-8",
            timeout=FILING_TIMEOUT_S, check=True)
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {"status": "parse_failed", "detail": f"Filing exceeded {FILING_TIMEOUT_S} seconds"}
    except (subprocess.CalledProcessError, ValueError):
        return {"status": "parse_failed", "detail": "Filing reader stopped"}


def save(conn, row, data: dict) -> None:
    """Record one attempt. A failure keeps the last good names and waits a
    day, then a week, then a month before the next try."""
    ok = data["status"] == "ok"
    fails = 0 if ok else int(row["fails"] or 0) + 1
    t = datetime.now(timezone.utc)
    detail = data.get("detail") or ""
    retry_at = None
    if not ok:
        retry_at = refresh.now_iso(t + refresh.retry_after(fails))
        detail += f"; retry {refresh.retry_words(fails)}"
    # The filing this attempt covers. With no date in the feed, a read covers
    # what was filed a few days before it, as for reads from before the column.
    covers = row["filing_date"] or refresh.covered_through(refresh.now_iso(t))
    conn.execute("""INSERT INTO firm_refresh
        (crd, fetched_at, pdf_bytes, custodians, status, detail, last_success_at,
         filing_date, fails, next_try_at) VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(crd) DO UPDATE SET fetched_at=excluded.fetched_at,
        pdf_bytes=COALESCE(excluded.pdf_bytes, firm_refresh.pdf_bytes),
        custodians=CASE WHEN excluded.status='ok' THEN excluded.custodians
            ELSE firm_refresh.custodians END,
        status=excluded.status, detail=excluded.detail,
        last_success_at=COALESCE(excluded.last_success_at, firm_refresh.last_success_at),
        filing_date=excluded.filing_date, fails=excluded.fails,
        next_try_at=excluded.next_try_at""",
        (row["crd"], refresh.now_iso(t), data.get("pdf_bytes"), data.get("custodians"),
         data["status"], detail, refresh.now_iso(t) if ok else None, covers, fails, retry_at))
    conn.commit()


def firm_refresh(limit: int = 300, budget_s: float = SLICE_BUDGET_S) -> str:
    """Read due filings one after another until the slice's budget is used,
    so the data lane's other jobs get their turn soon after."""
    conn = db.connect()
    try:
        refresh.init(conn)
        todo = conn.execute(DUE_SQL, refresh.due_params(limit)).fetchall()
        conn.commit()
        started, ok, attempted = time.monotonic(), 0, 0
        for row in todo:
            if time.monotonic() - started >= budget_s:
                break
            data = read_filing(row["crd"])
            save(conn, row, data)
            attempted += 1
            ok += data["status"] == "ok"
            print(f"Custodian filing {row['crd']}: {data['status']} ({data.get('detail', '')})",
                  flush=True)
        if not attempted:
            return "Updated 0 of 0 filings; nothing new filed and no retry due"
        return (f"Updated {ok} of {attempted} filings; {attempted - ok} to retry after 1, 7 or 30 days"
                f" ({time.monotonic() - started:.0f} s)")
    finally:
        conn.close()


def cusip_verify() -> str:
    conn = db.connect()
    r = conn.execute("SELECT MAX(finished_at) t FROM run_log"
                     " WHERE source_key='cusip_map' AND status='ok'").fetchone()
    conn.close()
    age = 999
    if r and r["t"]:
        age = (date.today() - date.fromisoformat(r["t"][:10])).days
    if age <= 90:
        return f"verified {age} days ago; due at 90"
    subprocess.run([sys.executable, "-m", "scripts.build_cusip_map", "--filings", "25"],
                   cwd=config.ROOT, capture_output=True)
    return "re-verified; next due in 90 days"


def main() -> int:
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    if what == "firm_refresh":
        print(firm_refresh())
    elif what == "firm_refresh_one":
        print(json.dumps(refresh_one(sys.argv[2])))
    elif what == "cusip_verify":
        print(cusip_verify())
    else:
        print("usage: python -m scripts.autopilot_slice firm_refresh|cusip_verify")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
