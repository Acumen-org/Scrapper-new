"""Autopilot: background completeness jobs, always under user control.

Jobs run in small slices and re-read their desired state between slices, so
Pause in the UI takes effect within seconds and nothing has to be killed. One
worker process at a time (pid file); starting any job from the UI launches it.

  brochures     continue Part 2A coverage across the band
  firm_refresh  fetch current ADV Part 1 PDFs for the flagged firms (Q5K3 or
                Q7B yes) and extract current custodian names, refreshing data
                whose bulk source ends 2024-12-31
  contact_extract  read filed emails and phones off cached brochure pages
  web_enrich       read each firm's own website for people, titles and contacts
  email_verify     syntax and mail-domain checks on guessed addresses, locally
                   via DNS: no account, no key, no cost
  cusip_verify     re-verify the target security map when older than 90 days

Nothing here talks to a paid service. Every job reaches only the SEC, an
adviser's own website, or a DNS resolver.

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

from prospect import config, db, mailcheck, net, procs  # noqa: E402

PID_FILE = config.DATA_DIR / "autopilot.pid"

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

    # The PDF is parsed in memory and never opened again: the custodian names
    # are what we wanted and they go straight into firm_refresh. Writing each
    # one to disk built up megabytes that nothing ever read.
    import pdfplumber
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
            with pdfplumber.open(io.BytesIO(pdf)) as doc:
                # NUL bytes from embedded fonts are rejected by Postgres.
                text = "\n".join((p.extract_text() or "") for p in doc.pages).replace("\x00", "")
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


DONE_STATES = ("bad_syntax", "no_mail_server", "domain_accepts_mail")


def job_infer_emails(conn, cfg) -> bool:
    """One slice of decision-maker email inference across the scored lists.

    Builds guesses from the pattern each firm uses for its own people, checks
    them against the mail domain, and stops when every officer on the scored
    lists has an address. Cheap: one subprocess slice, all local."""
    total = conn.execute("""SELECT COUNT(*) n FROM schedule_a s
        JOIN firm_current f ON f.crd=s.crd
        WHERE s.is_individual=1 AND f.is_era=0 AND f.raum>=25e6 AND f.raum<500e6
          AND (s.crd IN (SELECT crd FROM tier_a_rank)
               OR s.crd IN (SELECT crd FROM tier_c_score WHERE rank<=1000)
               OR s.crd IN (SELECT crd FROM firm_overlay WHERE phh_13f=1))
        """).fetchone()["n"]
    done = conn.execute("SELECT COUNT(*) n FROM contact_email").fetchone()["n"]
    set_task(conn, "infer_emails", progress=min(done, total), total=total,
             message=f"{done:,} decision-maker addresses inferred")
    r = subprocess.run([sys.executable, "-m", "scripts.infer_emails",
                        "--limit", "800"], cwd=config.ROOT,
                       capture_output=True, text=True)
    made = 0
    for line in (r.stdout or "").splitlines():
        if line.startswith("generated"):
            try:
                made = int(line.split()[1].replace(",", ""))
            except (IndexError, ValueError):
                made = 0
    if r.returncode != 0:
        set_task(conn, "infer_emails", message=f"slice failed: {r.stdout[-160:]}")
        return True
    if made == 0:
        set_task(conn, "infer_emails", desired_state="paused",
                 message="complete: every scored-list officer has an address")
        return False
    return True


def job_email_verify(conn, cfg) -> bool:
    """Syntax and mail-domain checks on queued candidates, locally and free.

    This used to POST each address to an undocumented mailwarm.com endpoint. It
    returned the same verdict for a real address and a fabricated one at the
    same firm, so it distinguished nothing while relying on someone else's
    service. Now every check is a syntax test and a DNS MX lookup done here: no
    account, no key, no quota, and fast enough to do a batch per slice instead
    of one address every ten seconds.
    """
    pending = conn.execute("SELECT COUNT(*) n FROM contact_email"
                           " WHERE status='queued'").fetchone()["n"]
    checked = conn.execute(
        "SELECT COUNT(*) n FROM contact_email WHERE status IN"
        " ('bad_syntax','no_mail_server','domain_accepts_mail')").fetchone()["n"]
    set_task(conn, "email_verify", progress=checked, total=checked + pending,
             message=f"{pending:,} queued, {checked:,} checked")
    if not pending:
        set_task(conn, "email_verify", desired_state="paused",
                 message=f"queue empty; {checked:,} checked")
        return False

    rows = conn.execute("SELECT id, email FROM contact_email"
                        " WHERE status='queued' ORDER BY id LIMIT 40").fetchall()
    # One MX answer per domain: candidates come in threes per person and whole
    # teams share a domain, so caching turns hundreds of lookups into a handful.
    seen: dict[str, tuple[str, str]] = {}
    for r in rows:
        dom = (r["email"] or "").rsplit("@", 1)[-1].lower()
        if dom in seen:
            status, _ = seen[dom]
            if not mailcheck.valid_syntax(r["email"]):
                status = "bad_syntax"
        else:
            status, why = mailcheck.check(r["email"])
            if status != "unknown":       # never cache a resolver failure
                seen[dom] = (status, why)
        # 'unknown' means DNS was unreachable, so it stays queued for retry
        # rather than being recorded as a finding.
        if status == "unknown":
            continue
        conn.execute("UPDATE contact_email SET status=?, checked_at=? WHERE id=?",
                     (status, now(), r["id"]))
    conn.commit()
    # Re-read after the batch: the counts above were taken before any work, so
    # leaving them would show a progress bar one slice behind reality.
    left = conn.execute("SELECT COUNT(*) n FROM contact_email"
                        " WHERE status='queued'").fetchone()["n"]
    done = conn.execute(
        "SELECT COUNT(*) n FROM contact_email WHERE status IN"
        " ('bad_syntax','no_mail_server','domain_accepts_mail')").fetchone()["n"]
    set_task(conn, "email_verify", progress=done, total=done + left,
             message=f"{left:,} queued, {done:,} checked")
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
        "infer_emails": job_infer_emails,
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
