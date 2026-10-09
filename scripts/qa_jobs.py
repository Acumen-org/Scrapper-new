"""Edge cases of the background jobs, against a real scratch database.

The worker, its lanes and the watchdog, driven with stand-in jobs
(scripts/qa_child.py) so every outcome can be made to happen on demand:
lanes overlapping, a failing job backing off, a slice stopped mid-run, a
paused job, a job waiting on a requirement, a lane whose database
connection breaks, the watchdog's repairs and alerts, the mail brake, the
website shards, the brochure reader, and a worker restart that must spare
the process that asks for it.

Point it at a scratch database, never the live one: it writes rows.

    BELLWETHER_DSN=postgresql://... python -m scripts.qa_jobs

Exits nonzero on any failure.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BELLWETHER_SCHEDULER", "0")

from prospect import db, health, jobs, procs  # noqa: E402
from scripts import autopilot  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {name}{(': ' + detail) if detail and not ok else ''}", flush=True)
    if not ok:
        FAILED.append(name)


def iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def now() -> datetime:
    return datetime.now(timezone.utc)


J = jobs.Job
ORIGINAL = list(jobs.JOBS)


def registry(*items: jobs.Job) -> None:
    jobs.JOBS[:] = list(items)
    jobs.BY_KIND.clear()
    jobs.BY_KIND.update({j.kind: j for j in jobs.JOBS})


def reset(c) -> None:
    c.execute("DELETE FROM auto_task WHERE kind LIKE 'qa%'")
    c.execute("DELETE FROM job_run WHERE kind LIKE 'qa%'")
    c.execute("DELETE FROM job_alert")
    c.execute("DELETE FROM job_event")
    c.execute("DELETE FROM run_log WHERE source_key='qa_child'")
    c.commit()
    jobs.init(c)


def run_lanes(lanes, seconds) -> None:
    """Run the given lanes' loops for a while, then stop them, so no lane
    from one check runs into the next."""
    started = jobs.now_iso()
    autopilot.STOP.clear()
    threads = [threading.Thread(target=autopilot.lane_loop, args=(lane, started), daemon=True)
               for lane in lanes]
    for t in threads:
        t.start()
    time.sleep(seconds)
    autopilot.STOP.set()
    for t in threads:
        t.join(60)


def state(c, kind):
    return jobs.states(c).get(kind) or {}


def main() -> int:
    autopilot.IDLE_SLEEP_S = 0.5
    c = db.connect()
    from prospect import hunt, verify as _verify
    _verify.init(c)
    hunt.init(c)
    jobs.init(c)
    c.executescript(autopilot.LANE_SCHEMA)
    db.add_column(c, "worker_lane", "slice_pid", "INTEGER")
    health.init(c)
    c.commit()

    # 1. Two lanes overlap: two 3-second jobs finish together, not one after the other.
    registry(J("qa_a", "A", "", "scripts.qa_child", ("sleep", "3"), lane="mail", every_hours=24),
             J("qa_b", "B", "", "scripts.qa_child", ("sleep", "3"), lane="web", every_hours=24))
    reset(c)
    run_lanes(("mail", "web"), 9)
    runs = {r["kind"]: r for r in c.execute("SELECT * FROM job_run WHERE kind IN ('qa_a','qa_b')")}
    gap = abs((datetime.fromisoformat(runs["qa_a"]["started_at"])
               - datetime.fromisoformat(runs["qa_b"]["started_at"])).total_seconds()) if len(runs) == 2 else 99
    check("lanes run side by side", len(runs) == 2 and gap < 2, f"runs={len(runs)} start gap={gap}s")
    check("each slice leaves a job_run row with its time",
          all(r["status"] == "ok" and 2.5 <= r["seconds"] < 10 for r in runs.values()),
          str([(r["seconds"], r["status"]) for r in runs.values()]))

    # 2. A failing job backs off 15 minutes, then an hour.
    registry(J("qa_fail", "Fail", "", "scripts.qa_child", ("fail",), lane="search"))
    reset(c)
    run_lanes(("search",), 4)
    s = state(c, "qa_fail")
    wait = (datetime.fromisoformat(s["next_run_at"]) - now()).total_seconds() / 60 if s.get("next_run_at") else 0
    check("a failure backs off about 15 minutes", s.get("fails") == 1 and 13 < wait < 16,
          f"fails={s.get('fails')} wait={wait:.1f}")
    c.execute("UPDATE auto_task SET next_run_at=? WHERE kind='qa_fail'", (iso(now() - timedelta(minutes=1)),))
    c.commit()
    run_lanes(("search",), 4)
    s = state(c, "qa_fail")
    wait = (datetime.fromisoformat(s["next_run_at"]) - now()).total_seconds() / 60
    check("a second failure backs off about an hour", s.get("fails") == 2 and 55 < wait < 61,
          f"fails={s.get('fails')} wait={wait:.1f}")

    # 3. A slice stopped mid-run: its run_log row is closed, not left "running".
    registry(J("qa_hang", "Hang", "", "scripts.qa_child", ("runlog", "60"), lane="data", timeout_s=4))
    reset(c)
    run_lanes(("data",), 9)
    s = state(c, "qa_hang")
    rows = c.execute("SELECT status, slice_id FROM run_log WHERE source_key='qa_child'").fetchall()
    check("a timed-out slice is recorded as a timeout", s.get("last_status") == "timeout",
          str(s.get("last_status")))
    check("its run_log row is closed as interrupted, by slice id",
          rows and all(r["status"] == "interrupted" and r["slice_id"] for r in rows),
          str([dict(r) for r in rows]))

    # 4. Paused: skipped with work waiting; Run now runs it once and it stays paused.
    registry(J("qa_paused", "Paused", "", "scripts.qa_child", ("ok",), lane="mail",
               backlog_sql="SELECT 5 n"))
    reset(c)
    jobs.set_paused(c, "qa_paused", True)
    run_lanes(("mail",), 3)
    check("a paused job does not run", (state(c, "qa_paused").get("runs") or 0) == 0)
    jobs.request_run(c, "qa_paused")
    run_lanes(("mail",), 4)
    s = state(c, "qa_paused")
    check("Run now runs a paused job once, and it stays paused",
          s.get("runs") == 1 and s.get("desired_state") == "paused", str(s.get("runs")))

    # 5. A requirement not met: the job waits, says why, and never runs.
    registry(J("qa_needs", "Needs AI", "", "scripts.qa_child", ("ok",), lane="web",
               needs="ai:research"))
    reset(c)
    run_lanes(("web",), 3)
    s = state(c, "qa_needs")
    check("a job waiting on a requirement says why and does not run",
          (s.get("runs") or 0) == 0 and "AI" in (s.get("message") or ""), str(s.get("message")))

    # 6. A lane whose database calls fail starts over and carries on.
    registry(J("qa_after", "After", "", "scripts.qa_child", ("ok",), lane="search", every_hours=24))
    reset(c)
    real = jobs.states
    hits = {"n": 0}

    def flaky(conn):
        hits["n"] += 1
        if hits["n"] == 1:
            raise RuntimeError("connection reset by peer")
        return real(conn)

    jobs.states = flaky
    autopilot.LOOP_ERROR_SLEEP_S = 0.3
    try:
        run_lanes(("search",), 5)
    finally:
        jobs.states = real
    check("a lane survives a database error", (state(c, "qa_after").get("runs") or 0) >= 1)

    # 7. The watchdog: stuck slice, overdue job, quota failure; then all clear.
    registry(J("qa_stuck", "Stuck", "", "scripts.qa_child", ("ok",), lane="web", timeout_s=60),
             J("qa_late", "Late", "", "scripts.qa_child", ("ok",), lane="mail"),
             J("qa_quota", "Quota", "", "scripts.qa_child", ("ok",), lane="search"))
    reset(c)
    old = iso(now() - timedelta(hours=7))
    c.execute("UPDATE auto_task SET running_since=? WHERE kind='qa_stuck'", (old,))
    c.execute("UPDATE auto_task SET next_run_at=?, last_run_at=? WHERE kind='qa_late'", (old, old))
    c.execute("UPDATE auto_task SET fails=3, last_status='failed', last_output='HTTP 429: quota'"
              " WHERE kind='qa_quota'")
    c.commit()
    restarts = []
    alerts = health.run(c, restart_worker=lambda: restarts.append(1))
    check("a stuck slice gets the worker restarted", restarts == [1])
    check("an overdue job is moved to the front", state(c, "qa_late").get("force") == 1)
    check("seven hours overdue is reported", "qa_late" in alerts)
    check("a quota failure names Settings, AI",
          alerts.get("qa_quota", {}).get("href") == "/settings/ai", str(alerts.get("qa_quota")))
    c.execute("UPDATE auto_task SET running_since=NULL, fails=0, last_status='ok', last_output='fine',"
              " next_run_at=?, force=0 WHERE kind LIKE 'qa%'", (iso(now() + timedelta(hours=1)),))
    c.commit()
    check("alerts clear once healthy", not health.run(c, restart_worker=lambda: None))

    # 8. Ghost runs: older than the limit, or anything from before a fresh start.
    c.execute("INSERT INTO run_log (source_key, stage, started_at, status, config_stamp)"
              " VALUES ('qa_child','test',?, 'running','qa')", (iso(now() - timedelta(minutes=10)),))
    c.commit()
    check("a recent running row survives the 3-hour sweep", health.close_ghost_runs(c) == 0)
    check("but is closed at a fresh start", health.close_ghost_runs(c, minutes=2) >= 1)

    # 9. The mail brake: halves the pace while servers say "try later" a lot.
    from prospect import verify
    c.execute("DELETE FROM email_attempt WHERE crd='qa'")
    for i in range(60):
        c.execute("INSERT INTO email_attempt (crd, person_key, address, status, checked_at)"
                  " VALUES ('qa', 'p', ?, ?, ?)", (f"qa{i}@example.invalid",
                                                  "retry" if i % 2 else "invalid", iso(now())))
    c.commit()
    verify._BRAKE["t"] = -1e9
    slow = verify._brake_factor()
    c.execute("UPDATE email_attempt SET status='invalid' WHERE crd='qa'")
    c.commit()
    verify._BRAKE["t"] = -1e9
    normal = verify._brake_factor()
    c.execute("DELETE FROM email_attempt WHERE crd='qa'")
    c.commit()
    check("the mail pace halves while servers ask us to slow down", (slow, normal) == (0.5, 1.0),
          f"{slow}, {normal}")

    # 10. Website shards: the shard query runs and the shards cover each firm once.
    from scripts import web_enrich
    try:
        parts = [web_enrich.todo(c, 50, (k, 3)) for k in range(3)]
        everyone = web_enrich.todo(c, 150)
        seen = [r["crd"] for p in parts for r in p]
        check("website shards split the firms without overlap",
              len(seen) == len(set(seen)) and set(seen) <= {r["crd"] for r in everyone})
    except Exception as e:
        c.rollback()
        check("website shards split the firms without overlap", False, repr(e))

    # 11. The brochure reader: form fields, a blank template, a scan with no engine.
    from prospect import ocr
    from scripts.qa_requirements import _form_pdf
    d = Path(tempfile.mkdtemp())
    (d / "form.pdf").write_bytes(_form_pdf(b"We write covered calls for clients. " * 10))
    (d / "blank.pdf").write_bytes(_form_pdf(b"x"))
    ocr.init(c)
    for crd, name in (("990001", "form.pdf"), ("990002", "blank.pdf")):
        c.execute("DELETE FROM brochure WHERE crd=?", (crd,))
        c.execute("INSERT INTO brochure (crd, version_id, fetched_at, pdf_path, pages, text_chars, status)"
                  " VALUES (?, 1, ?, ?, 1, 0, 'ok')", (crd, iso(now()), str(d / name)))
    c.commit()
    r = subprocess.run([sys.executable, "-m", "scripts.ocr_brochures", "--limit", "5"],
                       cwd=str(Path(__file__).resolve().parent.parent), capture_output=True, text=True,
                       env=dict(os.environ), timeout=120)
    got = {x["crd"]: (x["ocr_status"], x["ocr_method"]) for x in c.execute(
        "SELECT crd, ocr_status, ocr_method FROM brochure WHERE crd IN ('990001','990002')")}
    check("a form-field brochure is read and tagged", got.get("990001") == ("ok", "form"),
          f"{got} {r.stderr[-300:]}")
    check("a blank template is marked, not retried", got.get("990002") == ("empty", "empty"), str(got))
    tags = [t["tag"] for t in c.execute("SELECT tag FROM brochure_tag WHERE crd='990001' AND present=1")]
    check("its text reaches the tags", "covered_calls" in tags, str(tags))
    c.execute("DELETE FROM brochure_tag WHERE crd IN ('990001','990002')")
    c.execute("DELETE FROM brochure WHERE crd IN ('990001','990002')")
    c.commit()

    # 12. Restarting the worker stops it and spares the process asking.
    from prospect import config, webapp
    fake = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **procs.group_kwargs())
    pidfile = config.DATA_DIR / "autopilot.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(fake.pid))
    webapp.restart_worker()
    time.sleep(1)
    check("a worker restart stops the worker", fake.poll() is not None)
    check("and the process that asked lives on", procs.is_alive(os.getpid()))
    pidfile.unlink(missing_ok=True)

    registry(*ORIGINAL)
    reset(c)
    c.close()
    print(f"\n{'ALL JOB CHECKS PASS' if not FAILED else str(len(FAILED)) + ' FAILED: ' + ', '.join(FAILED)}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)      # lane threads are daemons looping for ever
