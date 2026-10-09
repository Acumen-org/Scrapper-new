"""The background worker: runs every job in prospect/jobs.py, by itself.

One worker process at a time (pid file, claimed atomically). Inside it, one
thread per lane (prospect.jobs.LANES): mail servers, websites, search and AI,
and the data jobs. Lanes run side by side, so a forty-minute website slice no
longer holds up the email hunt; within a lane the jobs take turns, a job with
a backlog due again straight away and a caught-up job after its cadence.
Paused jobs are skipped unless someone pressed Run now.

Every slice is a separate Python process in a process group of its own, with
a time limit. When the limit passes the whole group is stopped, so a headless
browser or a shard the slice started goes with it instead of living on. The
slice's run_log rows carry its id (BELLWETHER_SLICE), and any it could not
close itself are closed when it ends. Each slice leaves a job_run row: how
long it took, how it ended, the most memory it used.

Each lane writes a heartbeat (worker_lane) before and after every slice and
while it idles, with the pid of the slice it is running. prospect/health.py,
run by the web app every few minutes, reads those beats and the job states:
a stuck slice or a frozen lane gets the worker restarted, a job that keeps
failing or stops making progress is put in front of an admin in Settings.
When the worker is asked to stop, it stops its running slices first.

The web app starts this worker at boot and checks every few minutes that it is
alive, so nobody has to remember to start it.

    python -m scripts.autopilot
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, jobs, procs  # noqa: E402

PID_FILE = config.DATA_DIR / "autopilot.pid"
IDLE_SLEEP_S = 20
LOOP_ERROR_SLEEP_S = 30     # after a database hiccup, before the lane starts over

LANE_SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_lane (
    lane      TEXT PRIMARY KEY,
    beat_at   TEXT NOT NULL,      -- last sign of life
    job       TEXT,               -- the job running now, if any
    started   TEXT,               -- when the worker process started
    pid       INTEGER
);
"""

RUNNING: dict[str, int] = {}      # lane -> pid of the slice it is running
STOP = threading.Event()          # set only by tests, to end their lane loops
_RUNNING_LOCK = threading.Lock()


def run_slice(job: jobs.Job, slice_id: str, on_start=None) -> tuple[str, str, float | None]:
    """(status, last line of output, peak memory in MB or None) for one slice."""
    cmd = [sys.executable, "-m", job.module, *job.args]
    env = dict(os.environ, BELLWETHER_SLICE=slice_id)
    try:
        proc = subprocess.Popen(cmd, cwd=str(config.ROOT), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, **procs.group_kwargs())
    except OSError as exc:
        return "failed", str(exc)[:300], None
    if on_start:
        on_start(proc.pid)
    out, err = [], []
    readers = [threading.Thread(target=lambda s=s, b=b: b.append(s.read()), daemon=True)
               for s, b in ((proc.stdout, out), (proc.stderr, err))]
    for t in readers:
        t.start()
    timed_out = threading.Event()

    def stop() -> None:
        timed_out.set()
        procs.kill_group(proc.pid)

    timer = threading.Timer(job.timeout_s, stop)
    timer.daemon = True
    timer.start()
    peak = None
    try:
        if hasattr(os, "wait4"):
            # wait4 reports the slice's own peak memory as it is reaped.
            _, status, usage = os.wait4(proc.pid, 0)
            proc.returncode = os.waitstatus_to_exitcode(status)
            peak = round(usage.ru_maxrss / 1024, 1)      # KB on Linux
        else:
            proc.wait()
    finally:
        timer.cancel()
    # Whatever the slice started and left behind (a browser, a shard) goes too.
    procs.kill_group(proc.pid)
    for t in readers:
        t.join(5)
    for pipe in (proc.stdout, proc.stderr):
        try:
            pipe.close()
        except OSError:
            pass
    stdout =(out[0] if out else b"").decode("utf-8", "replace").strip().splitlines()
    stderr = (err[0] if err else b"").decode("utf-8", "replace").strip().splitlines()
    last = stdout[-1] if stdout else ""
    if timed_out.is_set():
        return "timeout", f"stopped after {job.timeout_s // 60} minutes; resumes next slice", peak
    if proc.returncode != 0:
        return "failed", (stderr[-1] if stderr else last)[:300], peak
    return "ok", last[:300], peak


def beat(conn, lane: str, job: str | None, started: str, slice_pid: int | None = None) -> None:
    conn.execute("INSERT INTO worker_lane (lane, beat_at, job, started, pid, slice_pid)"
                 " VALUES (?,?,?,?,?,?) ON CONFLICT (lane) DO UPDATE SET beat_at=excluded.beat_at,"
                 " job=excluded.job, started=excluded.started, pid=excluded.pid,"
                 " slice_pid=excluded.slice_pid",
                 (lane, jobs.now_iso(), job, started, os.getpid(), slice_pid))
    conn.commit()


def lane_loop(lane: str, started: str) -> None:
    """Run the lane's jobs in turn, for ever. A crash in the loop itself is
    logged and the loop starts over with a fresh connection."""
    while not STOP.is_set():
        conn = None
        try:
            conn = db.connect()
            beat(conn, lane, None, started)
            while not STOP.is_set():
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
                    t0, began = time.monotonic(), jobs.now_iso()
                    slice_id = uuid.uuid4().hex[:12]
                    conn.execute("UPDATE auto_task SET running_since=? WHERE kind=?",
                                 (began, job.kind))
                    conn.commit()

                    def started_slice(pid, job=job):
                        with _RUNNING_LOCK:
                            RUNNING[lane] = pid
                        beat(conn, lane, job.kind, started, pid)

                    status, msg, peak = run_slice(job, slice_id, started_slice)
                    with _RUNNING_LOCK:
                        RUNNING.pop(lane, None)
                    secs = round(time.monotonic() - t0, 1)
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
                    # Rows the slice opened but could not close (it was stopped).
                    conn.execute("UPDATE run_log SET status='interrupted', finished_at=?,"
                                 " message=COALESCE(NULLIF(message,''), ?)"
                                 " WHERE slice_id=? AND status='running'",
                                 (now, f"slice ended: {status}", slice_id))
                    conn.execute("INSERT INTO job_run (kind, lane, started_at, seconds, status,"
                                 " peak_mb, output) VALUES (?,?,?,?,?,?,?)",
                                 (job.kind, lane, began, secs, status, peak, (msg or "")[:300]))
                    conn.commit()
                    beat(conn, lane, None, started)
                    print(f"[{lane}] {job.kind}: {status} in {secs:.0f}s {msg}", flush=True)
                if not worked:
                    beat(conn, lane, None, started)
                    STOP.wait(IDLE_SLEEP_S)
        except Exception:
            print(f"[{lane}] lane loop error, restarting it:\n{traceback.format_exc()}", flush=True)
            STOP.wait(LOOP_ERROR_SLEEP_S)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def _stop(signum, frame) -> None:
    """Asked to stop (a restart, a deploy): stop the running slices first, so
    none of them lives on after the worker that was watching it."""
    with _RUNNING_LOCK:
        pids = list(RUNNING.values())
    for pid in pids:
        procs.kill_group(pid)
    raise SystemExit(0)


def main() -> int:
    if not procs.claim_pidfile(PID_FILE):
        print(f"autopilot already running (pid {PID_FILE.read_text().strip()})")
        return 0
    conn = db.connect()
    jobs.init(conn)
    conn.executescript(LANE_SCHEMA)
    db.add_column(conn, "worker_lane", "slice_pid", "INTEGER")
    # A worker that died mid-slice leaves running_since set, and its run_log
    # rows say "running" for ever; close both so no screen shows a ghost.
    conn.execute("UPDATE auto_task SET running_since=NULL")
    from prospect import health
    health.close_ghost_runs(conn)
    conn.execute("DELETE FROM worker_lane")
    conn.commit()
    conn.close()
    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is not None:
            signal.signal(sig, _stop)
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
