"""Autopilot: background completeness jobs, always under user control.

Jobs run in small slices and re-read their desired state between slices, so
Pause in the UI takes effect within seconds and nothing has to be killed. One
worker process at a time (pid file); starting any job from the UI launches it.

  brochures     continue Part 2A coverage across the band
  firm_refresh  fetch current ADV Part 1 PDFs for the flagged firms (Q5K3 or
                Q7B yes) and extract current custodian names, refreshing data
                whose bulk source ends 2024-12-31
  email_verify  check queued candidate emails, one per ~10s, matching the pace
                the team already used by hand
  cusip_verify  re-verify the target security map when older than 90 days

    python -m scripts.autopilot
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, net, procs  # noqa: E402

PID_FILE = config.DATA_DIR / "autopilot.pid"
PDF_DIR = config.DATA_DIR / "adv_pdfs"

REFRESH_SCHEMA = """
CREATE TABLE IF NOT EXISTS firm_refresh (
    crd         TEXT PRIMARY KEY,
    fetched_at  TEXT NOT NULL,
    pdf_bytes   INTEGER,
    custodians  TEXT,               -- pipe separated, deduped, as filed today
    status      TEXT NOT NULL       -- ok | fetch_failed | parse_failed
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_task(conn, kind, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    conn.execute(f"UPDATE auto_task SET {cols}, updated_at=? WHERE kind=?",
                 (*kw.values(), now(), kind))
    conn.commit()


def desired(conn, kind) -> str:
    r = conn.execute("SELECT desired_state FROM auto_task WHERE kind=?",
                     (kind,)).fetchone()
    return r["desired_state"] if r else "paused"


# ------------------------------------------------------------------ jobs

def job_brochures(conn, cfg) -> bool:
    """One slice of band brochure coverage. Returns True when work remains."""
    band = conn.execute("SELECT COUNT(*) n FROM firm_current WHERE is_era=0"
                        " AND raum>=25e6 AND raum<500e6").fetchone()["n"]
    done = conn.execute("SELECT COUNT(*) n FROM brochure").fetchone()["n"]
    set_task(conn, "brochures", progress=done, total=band,
             message=f"{done:,} of {band:,} brochures processed")
    if done >= band:
        set_task(conn, "brochures", desired_state="paused",
                 message=f"complete: {done:,} of {band:,}")
        return False
    r = subprocess.run([sys.executable, "-m", "scripts.brochures",
                        "--scope", "band", "--limit", "25"],
                       cwd=config.ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        set_task(conn, "brochures", message=f"slice failed: {r.stdout[-160:]}")
    return True


CUSTODIAN_RE = re.compile(r"Legal name of custodian:\s*(.+)")


def job_firm_refresh(conn, cfg, fetch) -> bool:
    """Fetch current ADV Part 1 PDFs for flagged firms; extract custodians.

    The extraction pattern was verified against a live filing before this was
    written: 'Legal name of custodian:' lines carry the names as filed today.
    """
    conn.executescript(REFRESH_SCHEMA)
    todo = conn.execute("""
        SELECT crd FROM firm_current
        WHERE is_era=0 AND raum>=25e6 AND raum<500e6
          AND (q5k3='Y' OR q7b='Y')
          AND crd NOT IN (SELECT crd FROM firm_refresh)
        LIMIT 12""").fetchall()
    total = conn.execute("""SELECT COUNT(*) n FROM firm_current
        WHERE is_era=0 AND raum>=25e6 AND raum<500e6
          AND (q5k3='Y' OR q7b='Y')""").fetchone()["n"]
    done = conn.execute("SELECT COUNT(*) n FROM firm_refresh").fetchone()["n"]
    set_task(conn, "firm_refresh", progress=done, total=total,
             message=f"{done:,} of {total:,} flagged firms refreshed")
    if not todo:
        set_task(conn, "firm_refresh", desired_state="paused",
                 message=f"complete: {done:,} of {total:,}")
        return False

    import pdfplumber
    PDF_DIR.mkdir(parents=True, exist_ok=True)
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
        (PDF_DIR / f"{crd}.pdf").write_bytes(pdf)
        try:
            with pdfplumber.open(io.BytesIO(pdf)) as doc:
                text = "\n".join((p.extract_text() or "") for p in doc.pages)
            names = []
            for n in CUSTODIAN_RE.findall(text):
                n = " ".join(n.split())
                if n and n not in names:
                    names.append(n)
            conn.execute("INSERT OR REPLACE INTO firm_refresh VALUES (?,?,?,?,?)",
                         (crd, now(), len(pdf), "|".join(names) or None, "ok"))
        except Exception:
            conn.execute("INSERT OR REPLACE INTO firm_refresh VALUES (?,?,?,?,?)",
                         (crd, now(), len(pdf), None, "parse_failed"))
        conn.commit()
    return True


def job_contact_extract(conn, cfg) -> bool:
    """One slice of brochure contact extraction: filed emails and phones from
    the first pages of PDFs already on disk. No network at all."""
    total = conn.execute("SELECT COUNT(*) n FROM brochure WHERE status='ok'"
                         ).fetchone()["n"]
    try:
        done = conn.execute("SELECT COUNT(*) n FROM contact_scan").fetchone()["n"]
    except Exception:
        done = 0
    set_task(conn, "contact_extract", progress=done, total=total,
             message=f"{done:,} of {total:,} brochures scanned for contacts")
    if done >= total:
        set_task(conn, "contact_extract", desired_state="paused",
                 message=f"complete: {done:,} of {total:,}; more arrive as "
                         f"brochures download")
        return False
    r = subprocess.run([sys.executable, "-m", "scripts.extract_brochure_contacts",
                        "--limit", "40"], cwd=config.ROOT,
                       capture_output=True, text=True)
    if r.returncode != 0:
        set_task(conn, "contact_extract", message=f"slice failed: {r.stdout[-160:]}")
    return True


def job_web_enrich(conn, cfg) -> bool:
    """One slice of website contact enrichment: people, titles, emails, and
    phones from each firm's own site. Slices are small because each firm can
    take several polite seconds."""
    total = conn.execute("""SELECT COUNT(*) n FROM firm_current
        WHERE is_era=0 AND raum>=25e6 AND raum<500e6
          AND website IS NOT NULL AND website != ''""").fetchone()["n"]
    try:
        done = conn.execute("SELECT COUNT(*) n FROM web_enrich_state").fetchone()["n"]
    except Exception:
        done = 0
    set_task(conn, "web_enrich", progress=done, total=total,
             message=f"{done:,} of {total:,} firm websites read")
    if done >= total:
        set_task(conn, "web_enrich", desired_state="paused",
                 message=f"complete: {done:,} of {total:,}")
        return False
    r = subprocess.run([sys.executable, "-m", "scripts.web_enrich",
                        "--limit", "6"], cwd=config.ROOT,
                       capture_output=True, text=True)
    if r.returncode != 0:
        set_task(conn, "web_enrich", message=f"slice failed: {r.stdout[-160:]}")
    return True


def job_email_verify(conn, cfg) -> bool:
    """One queued candidate per slice, at the hand-dialed pace (10s)."""
    row = conn.execute("SELECT * FROM contact_email WHERE status='queued'"
                       " ORDER BY id LIMIT 1").fetchone()
    pending = conn.execute("SELECT COUNT(*) n FROM contact_email"
                           " WHERE status='queued'").fetchone()["n"]
    checked = conn.execute("SELECT COUNT(*) n FROM contact_email"
                           " WHERE status IN ('valid','invalid')").fetchone()["n"]
    set_task(conn, "email_verify", progress=checked, total=checked + pending,
             message=f"{pending:,} queued, {checked:,} checked")
    if row is None:
        set_task(conn, "email_verify", desired_state="paused",
                 message=f"queue empty; {checked:,} checked")
        return False
    import requests
    try:
        resp = requests.post("https://www.mailwarm.com/api/email-checker",
                             json={"email": row["email"]}, timeout=45,
                             headers={"content-type": "application/json",
                                      "User-Agent": "Mozilla/5.0"})
        data = resp.json() if resp.status_code == 200 else {}
        verdict = str(data.get("result") or data.get("status") or "").lower()
        # Catch-all domains accept every RCPT, so "deliverable" there proves
        # nothing: that is how a guessed @linkedin.com address once came back
        # valid. Anything hedged stays unverifiable rather than valid.
        if any(w in verdict for w in ("catch", "accept", "unknown", "risky")):
            status = "unverifiable"
        elif "valid" in verdict or "deliverable" in verdict:
            status = "valid"
        else:
            status = "invalid" if verdict else "error"
    except Exception:
        status = "error"
    conn.execute("UPDATE contact_email SET status=?, checked_at=? WHERE id=?",
                 (status, now(), row["id"]))
    conn.commit()
    time.sleep(10)
    return True


def job_cusip_verify(conn, cfg) -> bool:
    r = conn.execute("SELECT MAX(finished_at) t FROM run_log"
                     " WHERE source_key='cusip_map' AND status='ok'").fetchone()
    age = 999
    if r and r["t"]:
        age = (date.today() - date.fromisoformat(r["t"][:10])).days
    if age <= 90:
        set_task(conn, "cusip_verify", desired_state="paused", progress=1, total=1,
                 message=f"verified {age} days ago; due at 90")
        return False
    set_task(conn, "cusip_verify", message="re-verifying the CUSIP map now")
    subprocess.run([sys.executable, "-m", "scripts.build_cusip_map",
                    "--filings", "25"], cwd=config.ROOT, capture_output=True)
    set_task(conn, "cusip_verify", desired_state="paused", progress=1, total=1,
             message="re-verified; next due in 90 days")
    return False


JOBS = {"brochures": job_brochures, "firm_refresh": None,  # bound below
        "contact_extract": job_contact_extract,
        "web_enrich": job_web_enrich,
        "email_verify": job_email_verify, "cusip_verify": job_cusip_verify}


def main() -> int:
    # One worker at a time, claimed atomically. The previous read-then-write
    # check both raced and, because os.kill(pid, 0) terminates on Windows,
    # killed the very process it was checking for. See prospect.procs.
    if not procs.claim_pidfile(PID_FILE):
        print(f"autopilot already running (pid {PID_FILE.read_text().strip()})")
        return 0
    cfg = config.load()
    conn = db.connect()
    fetch = net.Fetcher(cfg.http)
    conn.executescript(REFRESH_SCHEMA)
    for k in JOBS:
        conn.execute("INSERT OR IGNORE INTO auto_task (kind) VALUES (?)", (k,))
    conn.commit()
    print("autopilot up; obeying desired_state per job")

    idle_cycles = 0
    try:
        while True:
            worked = False
            for kind in JOBS:
                if desired(conn, kind) != "running":
                    continue
                fn = JOBS[kind]
                if kind == "firm_refresh":
                    worked = job_firm_refresh(conn, cfg, fetch) or worked
                else:
                    worked = fn(conn, cfg) or worked
            if worked:
                idle_cycles = 0
            else:
                idle_cycles += 1
                if idle_cycles > 360:  # ~30 min with nothing to do: exit quietly
                    break
                time.sleep(5)
    finally:
        PID_FILE.unlink(missing_ok=True)
    print("autopilot idle exit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
