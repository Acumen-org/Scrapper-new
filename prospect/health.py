"""Keeps the background jobs healthy, and says so when it cannot.

The web app calls run() every few minutes (from its scheduler thread). It
looks at the worker's lanes and every job, repairs what a machine can repair,
and records what needs a person:

  fixed by itself    a slice stuck past its time limit, or a lane that stopped
                     beating, gets the worker restarted; a job that is overdue
                     is nudged to run; the weekly SEC pull is started when the
                     feed is late; runs left "running" by a restart are closed.
  needs a person     a job that keeps failing (with the reason, in words, and
                     where to fix it), an AI provider out of quota or credit, a
                     job that ran all day without getting anything done, a
                     worker that keeps getting stuck.

Alerts live in job_alert and are shown in Settings (the Jobs tab and the
overview) with a count on the Settings link. Each repair and each alert that
opens or clears is written to job_event, so the Jobs tab can say what
Bellwether fixed by itself. Nothing here sends anything anywhere.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from . import jobs

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_alert (
    kind        TEXT PRIMARY KEY,     -- a job kind, or worker / weekly
    level       TEXT NOT NULL,        -- bad | warn
    title       TEXT NOT NULL,
    detail      TEXT,
    href        TEXT,
    since       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_event (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    action      TEXT NOT NULL,        -- fixed | alert | resolved
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS ix_job_event_at ON job_event (at);
CREATE TABLE IF NOT EXISTS job_progress (
    kind        TEXT NOT NULL,
    at          TEXT NOT NULL,        -- one row per job per hour
    done        INTEGER,
    PRIMARY KEY (kind, at)
);
"""

OVERDUE_NUDGE_MIN = 120      # lanes take turns, so a job may wait a while; past this it is nudged
OVERDUE_ALERT_MIN = 360      # still waiting after a nudge: a person should know
STUCK_GRACE_MIN = 10         # past a slice's own time limit, it is stuck
FAILS_ALERT = 3              # failures in a row before anyone is told
RESTARTS_ALERT = 3           # worker restarts in an hour before anyone is told
FEED_LATE_DAYS = 9           # the SEC publishes weekly
GHOST_HOURS = 3              # no slice runs this long; a "running" row this old is a ghost

QUOTA_RE = re.compile(r"\b429\b|quota|rate limit|insufficient (ai )?credit|\b402\b|top up", re.I)
KEY_RE = re.compile(r"\b401\b|\b403\b|rejected the key|key was rejected|invalid api key", re.I)
NO_ENGINE_RE = re.compile(r"no mailbox check can run", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _age_min(stamp: str | None) -> float | None:
    return jobs._age_minutes(stamp)


def init(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def close_ghost_runs(conn, minutes: float | None = None) -> int:
    """run_log rows a restart left saying "running" for ever: older than
    GHOST_HOURS, or than `minutes` when the caller knows better (the web app
    at startup, when nothing from before can still be running)."""
    age = timedelta(minutes=minutes) if minutes is not None else timedelta(hours=GHOST_HOURS)
    cutoff = _iso(_now() - age)
    cur = conn.execute("UPDATE run_log SET status='interrupted', finished_at=?,"
                       " message=COALESCE(NULLIF(message,''), 'stopped when the worker restarted')"
                       " WHERE status='running' AND started_at < ?", (_iso(_now()), cutoff))
    conn.commit()
    return getattr(cur, "rowcount", 0) or 0


def event(conn, kind: str, action: str, detail: str) -> None:
    conn.execute("INSERT INTO job_event (at, kind, action, detail) VALUES (?,?,?,?)",
                 (_iso(_now()), kind, action, detail[:400]))
    conn.commit()


def _label(kind: str) -> str:
    j = jobs.BY_KIND.get(kind)
    return j.label if j else {"worker": "Background worker", "weekly": "Weekly SEC cycle"}.get(kind, kind)


def _reason(text: str) -> tuple[str, str, str]:
    """(level, what is wrong in words, where to fix it) for a job's last output."""
    t = text or ""
    if QUOTA_RE.search(t):
        return ("bad", "The AI provider refused for quota or credit. Top up the account, or "
                "choose another model, in Settings, AI.", "/settings/ai")
    if KEY_RE.search(t):
        return "bad", "The AI provider rejected the key. Check it in Settings, AI.", "/settings/ai"
    if NO_ENGINE_RE.search(t):
        return ("bad", "No mailbox check can run: the Reacher server does not answer and port 25 "
                "is closed. Check Settings, Verification.", "/settings/verify")
    return "bad", f"Its last run said: {t[:200] or 'nothing'}", "/settings/jobs"


def _record_progress(conn, kind: str, done: int | None) -> int | None:
    """Store this hour's progress figure and return the one from about a day ago."""
    if done is None:
        return None
    hour = _now().replace(minute=0, second=0, microsecond=0)
    conn.execute("INSERT INTO job_progress (kind, at, done) VALUES (?,?,?)"
                 " ON CONFLICT (kind, at) DO NOTHING", (kind, _iso(hour), done))
    r = conn.execute("SELECT done FROM job_progress WHERE kind=? AND at <= ?"
                     " ORDER BY at DESC LIMIT 1", (kind, _iso(hour - timedelta(hours=23)))).fetchone()
    return r["done"] if r else None


def evaluate(conn, *, restart_worker=None, start_weekly=None) -> dict[str, dict]:
    """Repair what can be repaired; return the alerts that remain, by kind."""
    alerts: dict[str, dict] = {}
    now = _now()
    tv = jobs.tag_version()
    st = jobs.states(conn)

    # 1. A slice stuck past its own limit, or a lane that stopped beating:
    #    restart the worker. The slices are its children and go with it.
    stuck = []
    for j in jobs.JOBS:
        age = _age_min((st.get(j.kind) or {}).get("running_since"))
        if age is not None and age > j.timeout_s / 60 + STUCK_GRACE_MIN:
            stuck.append(f"{j.label} had been running {age:.0f} minutes, past its {j.timeout_s // 60} minute limit")
    try:
        lanes = {r["lane"]: dict(r) for r in conn.execute("SELECT * FROM worker_lane")}
    except Exception:
        conn.rollback()
        lanes = {}
    for lane, row in lanes.items():
        limit = max([j.timeout_s for j in jobs.lane_jobs(lane)] or [1800]) / 60 + STUCK_GRACE_MIN
        age = _age_min(row.get("beat_at"))
        if age is not None and age > limit:
            stuck.append(f"the {jobs.LANE_LABEL.get(lane, lane)} lane went quiet {age:.0f} minutes ago")
    if stuck and restart_worker:
        restart_worker()
        event(conn, "worker", "fixed", "Restarted the background worker: " + "; ".join(stuck))
    restarts = conn.execute("SELECT COUNT(*) n FROM job_event WHERE kind='worker' AND action='fixed'"
                            " AND at >= ?", (_iso(now - timedelta(hours=1)),)).fetchone()["n"]
    if restarts >= RESTARTS_ALERT:
        alerts["worker"] = {"level": "bad", "title": "The background worker keeps getting stuck",
                            "detail": f"Bellwether restarted it {restarts} times in the last hour. "
                                      "The Jobs list below shows which job was running each time.",
                            "href": "/settings/jobs"}

    # 2. Each job: failing, refused by a provider, overdue, or not moving.
    for j in jobs.JOBS:
        s = st.get(j.kind) or {}
        if s.get("desired_state") == "paused":
            continue
        ok, why = jobs.requirement(j)
        backlog = jobs.count(conn, j.backlog_sql, tv)
        if not ok:
            continue
        if j.kind == "brochure_ocr":
            try:
                waiting = conn.execute("SELECT COUNT(*) n FROM brochure WHERE ocr_status='waiting'"
                                       ).fetchone()["n"]
            except Exception:
                conn.rollback()
                waiting = 0
            if waiting:
                alerts[j.kind] = {"level": "warn",
                                  "title": f"{waiting:,} scanned brochure{'s' if waiting != 1 else ''} "
                                           "need an OCR engine",
                                  "detail": "They were filed as scanned images. This server has no "
                                            "Tesseract; it is installed with the app image, so a redeploy "
                                            "brings it back. They are read by themselves once it is there.",
                                  "href": "/settings/crawl"}
                continue
        out = s.get("last_output") or ""
        fails = int(s.get("fails") or 0)
        if fails >= FAILS_ALERT or (s.get("last_status") == "ok" and QUOTA_RE.search(out)
                                     and "stopped" in out.lower()):
            level, what, href = _reason(out)
            nxt = s.get("next_run_at")
            when = ""
            if nxt:
                mins = -(_age_min(nxt) or 0)
                when = (f" Bellwether tries again in {mins / 60:.0f} hours." if mins >= 90 else
                        f" Bellwether tries again in {max(mins, 1):.0f} minutes.")
            alerts[j.kind] = {"level": level,
                              "title": f"{j.label} " + ("keeps failing" if fails >= FAILS_ALERT
                                                         else "is being refused"),
                              "detail": what + when, "href": href}
            continue
        if not s.get("running_since"):
            overdue = -1.0
            if backlog and s.get("last_status") not in ("failed", "timeout"):
                overdue = _age_min(s.get("last_run_at")) or 0
            elif s.get("next_run_at"):
                overdue = _age_min(s.get("next_run_at")) or 0
            if overdue > OVERDUE_NUDGE_MIN and not s.get("force"):
                conn.execute("UPDATE auto_task SET force=1 WHERE kind=?", (j.kind,))
                conn.commit()
                event(conn, j.kind, "fixed", f"{j.label} was {overdue / 60:.1f} hours overdue; "
                                             "moved it to the front of its lane")
            if overdue > OVERDUE_ALERT_MIN:
                alerts[j.kind] = {"level": "warn", "title": f"{j.label} has not run for "
                                                            f"{overdue / 60:.0f} hours",
                                  "detail": "It is due and was moved to the front of its lane. If "
                                            "this stays, the worker may be busy with a long job.",
                                  "href": "/settings/jobs"}
                continue
        # Not moving: ran several times today with work waiting, got nothing done.
        done = jobs.count(conn, j.done_sql, tv)
        before = _record_progress(conn, j.kind, done)
        recent = jobs.count(conn, j.recent_sql, tv)
        # "Busy" means it has been running in turn lately, so silence is not a
        # quiet schedule. Jobs with a daily figure are judged on it; the rest
        # on their progress count against the one stored a day ago.
        busy = (s.get("runs") or 0) >= 3 and (_age_min(s.get("last_run_at")) or 1e9) < 120
        stalled_recent = bool(backlog) and bool(j.recent_sql) and recent == 0
        stalled_done = (bool(backlog) and not j.recent_sql and before is not None
                        and done is not None and done <= before)
        if busy and (stalled_recent or stalled_done):
            what = (f"no {j.recent_label} in the last day" if j.recent_sql
                    else "its progress has not moved in a day")
            alerts[j.kind] = {"level": "warn", "title": f"{j.label} is running but not moving",
                              "detail": f"It ran today with {backlog:,} waiting, but {what}. "
                                        f"Its last run said: {out[:160] or 'nothing'}",
                              "href": "/settings/jobs"}

    # 3. The weekly SEC pull: start it when the feed is late, and say so if it stays late.
    try:
        r = conn.execute("SELECT MAX(captured_at) t FROM snapshot WHERE source_key='adv_feed'").fetchone()
        last = r["t"] if r else None
    except Exception:
        conn.rollback()
        last = None
    if last:
        age_days = (_age_min(last) or 0) / 1440
        if age_days > FEED_LATE_DAYS:
            busy = conn.execute("SELECT COUNT(*) n FROM run_log WHERE status='running'"
                                " AND started_at >= ?", (_iso(now - timedelta(hours=GHOST_HOURS)),)).fetchone()["n"]
            if not busy and start_weekly:
                start_weekly()
                event(conn, "weekly", "fixed", f"The SEC feed was {age_days:.0f} days old; started the weekly pull")
            alerts["weekly"] = {"level": "warn", "title": f"The SEC feed is {age_days:.0f} days old",
                                "detail": "The weekly pull normally keeps it under a week. Bellwether "
                                          "started it again; if this stays, the SEC site may be refusing it.",
                                "href": "/settings/system"}
    return alerts


def run(conn, *, restart_worker=None, start_weekly=None) -> dict[str, dict]:
    """One watchdog pass: repairs, then the alert list brought up to date."""
    init(conn)
    close_ghost_runs(conn)
    alerts = evaluate(conn, restart_worker=restart_worker, start_weekly=start_weekly)
    now = _iso(_now())
    have = {r["kind"]: dict(r) for r in conn.execute("SELECT * FROM job_alert")}
    for kind, a in alerts.items():
        if kind in have:
            conn.execute("UPDATE job_alert SET level=?, title=?, detail=?, href=?, updated_at=?"
                         " WHERE kind=?", (a["level"], a["title"], a["detail"], a["href"], now, kind))
        else:
            conn.execute("INSERT INTO job_alert (kind, level, title, detail, href, since, updated_at)"
                         " VALUES (?,?,?,?,?,?,?)",
                         (kind, a["level"], a["title"], a["detail"], a["href"], now, now))
            event(conn, kind, "alert", a["title"])
    for kind in have:
        if kind not in alerts:
            conn.execute("DELETE FROM job_alert WHERE kind=?", (kind,))
            event(conn, kind, "resolved", f"{have[kind]['title']}: cleared")
    conn.execute("DELETE FROM job_progress WHERE at < ?", (_iso(_now() - timedelta(days=3)),))
    try:
        conn.execute("DELETE FROM job_run WHERE started_at < ?", (_iso(_now() - timedelta(days=14)),))
    except Exception:
        conn.rollback()
    conn.execute("DELETE FROM job_event WHERE at < ?", (_iso(_now() - timedelta(days=30)),))
    conn.commit()
    return alerts


def alerts(conn) -> list[dict]:
    """Open alerts, worst first, for Settings."""
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM job_alert ORDER BY CASE level WHEN 'bad' THEN 0 ELSE 1 END, since")]
    except Exception:
        conn.rollback()
        return []


def recent_fixes(conn, hours: int = 24) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM job_event WHERE action IN ('fixed','resolved') AND at >= ?"
            " ORDER BY at DESC LIMIT 12", (_iso(_now() - timedelta(hours=hours)),))]
    except Exception:
        conn.rollback()
        return []
