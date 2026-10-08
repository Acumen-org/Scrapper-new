"""The background worker: runs every job in prospect/jobs.py, by itself.

One worker process at a time (pid file, claimed atomically). Inside it, one
thread per lane (prospect.jobs.LANES): mail servers, websites, search and AI,
and the data jobs. Lanes run side by side, so a forty-minute website slice no
longer holds up the email hunt; within a lane the jobs take turns, a job with
a backlog due again straight away and a caught-up job after its cadence.
Paused jobs are skipped unless someone pressed Run now.

Every slice is a separate Python process with a time limit. A website that
never answers or a PDF that sends the parser into a spin costs one slice, not
the worker; and the worker's own memory never grows with what the jobs read.

Each lane writes a heartbeat (worker_lane) before and after every slice and
while it idles. prospect/health.py, run by the web app every few minutes,
reads those beats and the job states: a stuck slice or a frozen lane gets the
worker restarted, a job that keeps failing or stops making progress is put in
front of an admin in Settings.

The web app starts this worker at boot and checks every few minutes that it is
alive, so nobody has to remember to start it.

    python -m scripts.autopilot
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, jobs, procs  # noqa: E402

PID_FILE = config.DATA_DIR / "autopilot.pid"
IDLE_SLEEP_S = 20

LANE_SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_lane (
    lane      TEXT PRIMARY KEY,
    beat_at   TEXT NOT NULL,      -- last sign of life
    job       TEXT,               -- the job running now, if any
    started   TEXT,               -- when the worker process started
    pid       INTEGER
);
"""


def run_slice(job: jobs.Job) -> tuple[str, str]:
    """(status, last line of output) for one slice of one job."""
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


def beat(conn, lane: str, job: str | None, started: str) -> None:
    conn.execute("INSERT INTO worker_lane (lane, beat_at, job, started, pid) VALUES (?,?,?,?,?)"
                 " ON CONFLICT (lane) DO UPDATE SET beat_at=excluded.beat_at, job=excluded.job,"
                 " started=excluded.started, pid=excluded.pid",
                 (lane, jobs.now_iso(), job, started, os.getpid()))
    conn.commit()


def lane_loop(lane: str, started: str) -> None:
    """Run the lane's jobs in turn, for ever. A crash in the loop itself is
    logged and the loop starts over with a fresh connection."""
    while True:
        conn = None
        try:
            conn = db.connect()
            beat(conn, lane, None, started)
            while True:
                worked = False
                tv = jobs.tag_version()
                # Run now, and a nudge from the watchdog, put a job first in line.
                states = jobs.states(conn)
                order = sorted(jobs.lane_jobs(lane),
                               key=lambda j: not (states.get(j.kind) or {}).get("force"))
                for job in order:
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
                    conn.execute("UPDATE auto_task SET running_since=? WHERE kind=?",
                                 (jobs.now_iso(), job.kind))
                    conn.commit()
                    beat(conn, lane, job.kind, started)
                    status, msg = run_slice(job)
                    worked = True
                    left = jobs.count(conn, job.backlog_sql, tv)
                    fails = 0 if status == "ok" else int(st.get("fails") or 0) + 1
                    # A failing job waits longer each time; one that worked goes
                    # straight back in the queue while it has a backlog.
                    nxt = jobs.schedule_next(job, left) if status == "ok" else jobs.retry_at(fails)
                    now = jobs.now_iso()
                    conn.execute(
                        "UPDATE auto_task SET running_since=NULL, last_run_at=?, next_run_at=?,"
                        " last_status=?, message=?, last_output=?, runs=COALESCE(runs,0)+1,"
                        " force=0, fails=?, ok_at=CASE WHEN ?='ok' THEN ? ELSE ok_at END,"
                        " updated_at=? WHERE kind=?",
                        (now, nxt, status, msg or None, msg or None, fails, status, now, now,
                         job.kind))
                    conn.commit()
                    beat(conn, lane, None, started)
                    print(f"[{lane}] {job.kind}: {status} {msg}", flush=True)
                if not worked:
                    beat(conn, lane, None, started)
                    time.sleep(IDLE_SLEEP_S)
        except Exception:
            print(f"[{lane}] lane loop error, restarting it:\n{traceback.format_exc()}", flush=True)
            time.sleep(30)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def main() -> int:
    if not procs.claim_pidfile(PID_FILE):
        print(f"autopilot already running (pid {PID_FILE.read_text().strip()})")
        return 0
    conn = db.connect()
    jobs.init(conn)
    conn.executescript(LANE_SCHEMA)
    # A worker that died mid-slice leaves running_since set, and its run_log
    # rows say "running" for ever; close both so no screen shows a ghost.
    conn.execute("UPDATE auto_task SET running_since=NULL")
    from prospect import health
    health.close_ghost_runs(conn)
    conn.execute("DELETE FROM worker_lane")
    conn.commit()
    conn.close()
    started = jobs.now_iso()
    print("autopilot up, lanes: " + ", ".join(jobs.LANES), flush=True)
    try:
        threads = []
        for lane in jobs.LANES:
            t = threading.Thread(target=lane_loop, args=(lane, started), daemon=True,
                                 name=f"lane-{lane}")
            t.start()
            threads.append(t)
        while True:
            time.sleep(60)
            for i, t in enumerate(threads):
                if not t.is_alive():   # cannot happen short of a bug; start it again
                    lane = jobs.LANES[i]
                    threads[i] = threading.Thread(target=lane_loop, args=(lane, started),
                                                  daemon=True, name=f"lane-{lane}")
                    threads[i].start()
    finally:
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
