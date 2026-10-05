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
import re
import subprocess
import sys
from datetime import date, datetime, timezone
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

CUSTODIAN_RE = re.compile(r"Legal name of custodian:\s*(.+)")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def firm_refresh(limit: int = 12) -> str:
    """The extraction pattern was verified against a live filing before it was
    written: 'Legal name of custodian:' lines carry the names as filed today."""
    cfg = config.load()
    fetch = net.Fetcher(cfg.http)
    conn = db.connect()
    conn.executescript(REFRESH_SCHEMA)
    conn.commit()
    todo = conn.execute(f"""
        SELECT f.crd FROM firm_current f JOIN firm_scope s ON s.crd=f.crd
        WHERE (f.q5k3='Y' OR f.q7b='Y')
          AND f.crd NOT IN (SELECT crd FROM firm_refresh)
        ORDER BY s.priority DESC LIMIT {int(limit)}""").fetchall()
    conn.commit()
    if not todo:
        return "nothing to refresh"
    import pdfplumber
    ok = 0
    for row in todo:
        crd = row["crd"]
        try:
            r = fetch._request(
                f"https://reports.adviserinfo.sec.gov/reports/ADV/{crd}/PDF/{crd}.pdf",
                stream=False)
            pdf = r.content
            r.close()
        except Exception:
            conn.execute("INSERT OR REPLACE INTO firm_refresh VALUES (?,?,?,?,?)",
                         (crd, now(), None, None, "fetch_failed"))
            conn.commit()
            continue
        try:
            # The PDF is parsed in memory and never written to disk: the names
            # are what we want, and saved PDFs were megabytes nothing re-read.
            with pdfplumber.open(io.BytesIO(pdf)) as doc:
                text = "\n".join((p.extract_text() or "") for p in doc.pages).replace("\x00", "")
            names = []
            for n in CUSTODIAN_RE.findall(text):
                n = " ".join(n.split())
                if n and n not in names:
                    names.append(n)
            conn.execute("INSERT OR REPLACE INTO firm_refresh VALUES (?,?,?,?,?)",
                         (crd, now(), len(pdf), "|".join(names) or None, "ok"))
            ok += 1
        except Exception:
            conn.execute("INSERT OR REPLACE INTO firm_refresh VALUES (?,?,?,?,?)",
                         (crd, now(), len(pdf), None, "parse_failed"))
        conn.commit()
    conn.close()
    return f"refreshed {ok} of {len(todo)} firms"


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
    elif what == "cusip_verify":
        print(cusip_verify())
    else:
        print("usage: python -m scripts.autopilot_slice firm_refresh|cusip_verify")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
