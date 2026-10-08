"""One slice of the two jobs that have no script of their own.

  firm_refresh   fetch current ADV Part 1 PDFs for firms that report custody
                 or private funds (Q5K3 or Q7B yes) and read today's custodian
                 names, refreshing data whose bulk source ends 2024-12-31
  cusip_verify   re-verify the target security map when older than 90 days

These used to live inside the worker loop. They are separate processes now,
like every other job, so the worker only schedules.

    python -m scripts.autopilot_slice firm_refresh
    python -m scripts.autopilot_slice cusip_verify
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, net  # noqa: E402

REFRESH_SCHEMA = """
CREATE TABLE IF NOT EXISTS firm_refresh (
    crd         TEXT PRIMARY KEY,
    fetched_at  TEXT NOT NULL,
    pdf_bytes   INTEGER,
    custodians  TEXT,               -- pipe separated, deduped, as filed today
    status      TEXT NOT NULL       -- ok | fetch_failed | parse_failed
);
"""

CUSTODIAN_RE = re.compile(r"Legal name of custodian:[ \t]*(?:\n[ \t]*)?([^\n]+)", re.I)

# A failed source stays retryable; successful filings are refreshed monthly.
DUE_SQL = """SELECT f.crd FROM firm_current f
    LEFT JOIN firm_scope s ON s.crd=f.crd
    LEFT JOIN firm_refresh r ON r.crd=f.crd
    WHERE (f.q5k3='Y' OR f.q7b='Y')
      AND (r.crd IS NULL OR (r.status != 'ok' AND r.fetched_at < ?)
           OR r.fetched_at < ?)
    ORDER BY (r.crd IS NOT NULL), r.fetched_at NULLS FIRST,
             s.priority DESC NULLS LAST LIMIT ?"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def refresh_one(crd: str) -> dict:
    """Run in an isolated process: a slow/large PDF cannot end the whole slice."""
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
        return {"status": "fetch_failed", "detail": "SEC filing unavailable; retry scheduled"}
    finally:
        fetch.session.close()
    try:
        import pdfplumber
        names = []
        with pdfplumber.open(io.BytesIO(pdf)) as doc:
            for page in doc.pages:
                text = (page.extract_text() or "").replace("\x00", "")
                for name in CUSTODIAN_RE.findall(text):
                    name = " ".join(name.split())
                    if name and name not in names:
                        names.append(name)
                # pdfplumber otherwise retains every page's layout and chars.
                page.close()
        return {"status": "ok", "pdf_bytes": len(pdf), "custodians": "|".join(names) or None,
                "detail": f"{len(names)} custodian names in current filing"}
    except Exception as exc:
        return {"status": "parse_failed", "pdf_bytes": len(pdf),
                "detail": f"PDF could not be read ({type(exc).__name__}); retry scheduled"}


def firm_refresh(limit: int = 4, seconds: int = 480) -> str:
    conn = db.connect()
    conn.executescript(REFRESH_SCHEMA)
    db.add_column(conn, "firm_refresh", "detail", "TEXT")
    db.add_column(conn, "firm_refresh", "last_success_at", "TEXT")
    conn.execute("UPDATE firm_refresh SET last_success_at=fetched_at WHERE status='ok' AND last_success_at IS NULL")
    conn.commit()
    try:
        t = datetime.now(timezone.utc)
        todo = conn.execute(DUE_SQL, ((t - timedelta(days=1)).isoformat(),
                                     (t - timedelta(days=30)).isoformat(), limit)).fetchall()
        conn.commit()
        started, ok, attempted = time.monotonic(), 0, 0
        for row in todo:
            remaining = seconds - (time.monotonic() - started)
            if remaining < 10:
                break
            crd = row["crd"]
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "scripts.autopilot_slice", "firm_refresh_one", crd],
                    cwd=config.ROOT, capture_output=True, text=True, encoding="utf-8",
                    timeout=min(110, remaining), check=True)
                data = json.loads(result.stdout)
            except subprocess.TimeoutExpired:
                data = {"status": "parse_failed", "detail": "Filing exceeded 110 seconds; retry scheduled"}
            except (subprocess.CalledProcessError, ValueError):
                data = {"status": "parse_failed", "detail": "Filing reader stopped; retry scheduled"}
            conn.execute("""INSERT INTO firm_refresh
                (crd, fetched_at, pdf_bytes, custodians, status, detail, last_success_at) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(crd) DO UPDATE SET fetched_at=excluded.fetched_at,
                pdf_bytes=COALESCE(excluded.pdf_bytes, firm_refresh.pdf_bytes),
                custodians=CASE WHEN excluded.status='ok' THEN excluded.custodians
                    ELSE firm_refresh.custodians END,
                status=excluded.status, detail=excluded.detail,
                last_success_at=COALESCE(excluded.last_success_at, firm_refresh.last_success_at)""",
                (crd, now(), data.get("pdf_bytes"), data.get("custodians"), data["status"], data.get("detail"),
                 now() if data['status'] == 'ok' else None))
            conn.commit()
            attempted += 1
            ok += data["status"] == "ok"
            print(f"Custodian filing {crd}: {data['status']} ({data.get('detail', '')})", flush=True)
        return f"Updated {ok} of {attempted} filings; {attempted - ok} scheduled for retry in 24 hours"
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
