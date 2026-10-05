"""The background worker: runs every job in prospect/jobs.py, by itself.

One worker process at a time (pid file, claimed atomically). It loops over the
jobs in registry order and runs one slice of each job that is due: a job with
a backlog is due again straight away, so backlogs drain round-robin with no job
starving the others; a job that is caught up is due again after its cadence.
Paused jobs are skipped unless someone pressed Run now.

Every slice is a separate Python process with a time limit. A website that
never answers or a PDF that sends the parser into a spin costs one slice, not
the worker; and the worker's own memory never grows with what the jobs read.

The web app starts this worker at boot and checks every few minutes that it is
alive, so nobody has to remember to start it.

    python -m scripts.autopilot
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, jobs, procs  # noqa: E402

PID_FILE = config.DATA_DIR / "autopilot.pid"
IDLE_SLEEP_S = 20


def run_slice(conn, job: jobs.Job) -> tuple[str, str]:
    """(status, last line of output) for one slice of one job."""
    conn.execute("UPDATE auto_task SET running_since=? WHERE kind=?",
                 (jobs.now_iso(), job.kind))
    conn.commit()
    cmd = [sys.executable, "-m", job.module, *job.args]
    try:
        r = subprocess.run(cmd, cwd=str(config.ROOT), capture_output=True, text=True,
                           timeout=job.timeout_s, encoding="utf-8", errors="replace")
        out = (r.stdout or "").strip().splitlines()
        err = (r.stderr or "").strip().splitlines()
        last = out[-1] if out else ""
        if r.returncode != 0:
            return "failed", (err[-1] if err else last)[:300]
        return "ok", last[:300]
    except subprocess.TimeoutExpired:
        return "timeout", f"stopped after {job.timeout_s // 60} minutes; resumes next slice"
    except OSError as exc:
        return "failed", str(exc)[:300]


def main() -> int:
    if not procs.claim_pidfile(PID_FILE):
        print(f"autopilot already running (pid {PID_FILE.read_text().strip()})")
        return 0
    conn = db.connect()
    jobs.init(conn)
    # A worker that died mid-slice leaves running_since set; clear it so the
    # screen does not show a job as running forever.
    conn.execute("UPDATE auto_task SET running_since=NULL")
    conn.commit()
    print("autopilot up", flush=True)
    try:
        while True:
            worked = False
            tv = jobs.tag_version()
            for job in jobs.JOBS:
                st = jobs.states(conn).get(job.kind, {})
                ok, why = jobs.requirement(job)
                if not ok:
                    if st.get("message") != why:
                        conn.execute("UPDATE auto_task SET message=?, force=0 WHERE kind=?",
                                     (why, job.kind))
                        conn.commit()
                    continue
                backlog = jobs.count(conn, job.backlog_sql, tv)
                if not jobs.due(job, st, backlog):
                    continue
                status, msg = run_slice(conn, job)
                worked = True
                left = jobs.count(conn, job.backlog_sql, tv)
                if status != "ok":
                    # A failing job backs off for an hour instead of spinning.
                    nxt = jobs.schedule_next(jobs.Job(job.kind, "", "", "", every_hours=1), 0)
                else:
                    nxt = jobs.schedule_next(job, left)
                conn.execute(
                    "UPDATE auto_task SET running_since=NULL, last_run_at=?, next_run_at=?,"
                    " last_status=?, message=?, last_output=?, runs=COALESCE(runs,0)+1,"
                    " force=0, updated_at=? WHERE kind=?",
                    (jobs.now_iso(), nxt, status, msg or None, msg or None, jobs.now_iso(),
                     job.kind))
                conn.commit()
                print(f"{job.kind}: {status} {msg}", flush=True)
            if not worked:
                time.sleep(IDLE_SLEEP_S)
    finally:
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
