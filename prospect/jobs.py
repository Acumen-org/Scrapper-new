"""Background jobs: what runs, how often, and what it is doing now.

Every job runs by itself. Nobody has to press Start: a job is on from the
moment Bellwether starts, works through its backlog in short slices (best
firms first), and once caught up checks back on its own cadence for new work.
An admin can still pause a job, or press Run now to make it go immediately.

The registry here is the single description of the jobs. The background worker
(scripts/autopilot.py) reads it to decide what to run next, and Settings, Jobs
reads it to show the state of each one. Each slice is a separate Python
process with a time limit, so a stuck website or a bad PDF can never wedge the
worker or leak memory into the web server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS auto_task (
    kind          TEXT PRIMARY KEY,
    desired_state TEXT NOT NULL DEFAULT 'running',
    progress      INTEGER DEFAULT 0,
    total         INTEGER DEFAULT 0,
    message       TEXT,
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS firm_refresh_request (
    crd TEXT PRIMARY KEY,
    requested_at TEXT NOT NULL
);
"""

EXTRA_COLUMNS = (("last_run_at", "TEXT"), ("next_run_at", "TEXT"), ("last_status", "TEXT"),
                 ("running_since", "TEXT"), ("runs", "INTEGER DEFAULT 0"),
                 ("force", "INTEGER DEFAULT 0"), ("last_output", "TEXT"))


@dataclass(frozen=True)
class Job:
    kind: str
    label: str
    blurb: str
    module: str
    args: tuple = ()
    every_hours: float = 24.0        # cadence once the backlog is clear
    backlog_sql: str | None = None   # rows still to do; > 0 means keep going
    done_sql: str | None = None      # for the progress bar
    total_sql: str | None = None
    timeout_s: int = 1800
    needs: str = ""                  # "", "ai:<feature>"
    group: str = "enrich"
    extra: dict = field(default_factory=dict)


SCOPE = "SELECT crd FROM firm_scope"

JOBS: list[Job] = [
    Job("people", "People and hiring",
        "Loads the SEC's weekly record of every registered rep: where they work, since "
        "when, where they were before, exams and disclosures, and who joined or left each "
        "firm. Also part of the weekly cycle; this catches a new install up at once.",
        "scripts.ingest_people", (), every_hours=24, timeout_s=3600,
        backlog_sql="SELECT CASE WHEN EXISTS (SELECT 1 FROM person) THEN 0 ELSE 1 END n",
        done_sql="SELECT COUNT(*) n FROM firm_people_stats", group="filings"),
    Job("people_index", "People screen",
        "Folds the roster, officer titles and every usable email, phone and LinkedIn profile "
        "into the table the People screen reads, so new contacts appear there within half an "
        "hour and the screen stays instant.",
        "scripts.build_people_index", (), every_hours=0.5, timeout_s=900,
        backlog_sql="SELECT CASE WHEN to_regclass('people_index') IS NULL THEN 1 ELSE 0 END n",
        group="contacts"),
    Job("brochures", "Brochures",
        "Downloads each firm's Part 2A brochure and reads it for what the firm does, in "
        "its own words. Best-scored firms first; failed downloads retry after a day.",
        "scripts.brochures", ("--scope", "scored", "--limit", "25"), every_hours=24,
        backlog_sql=f"SELECT COUNT(*) n FROM firm_scope s WHERE NOT EXISTS (SELECT 1 FROM brochure b WHERE b.crd=s.crd)",
        done_sql="SELECT COUNT(*) n FROM brochure b JOIN firm_scope s ON s.crd=b.crd",
        total_sql="SELECT COUNT(*) n FROM firm_scope", group="filings"),
    Job("brochure_retag", "Brochure re-read",
        "Re-reads brochures already held when the vocabulary grows, from saved text.",
        "scripts.brochures", ("--retag", "--limit", "200", "--workers", "2"), every_hours=24,
        backlog_sql="SELECT COUNT(*) n FROM brochure WHERE status='ok' AND COALESCE(tag_version,1) < {tag_version}",
        done_sql="SELECT COUNT(*) n FROM brochure WHERE status='ok' AND COALESCE(tag_version,1) >= {tag_version}",
        total_sql="SELECT COUNT(*) n FROM brochure WHERE status='ok'", group="filings"),
    Job("contact_extract", "Brochure contacts",
        "Reads the emails and phone numbers firms printed in their own brochures.",
        "scripts.extract_brochure_contacts", ("--limit", "40"), every_hours=12,
        backlog_sql="SELECT GREATEST((SELECT COUNT(*) FROM brochure WHERE status='ok') - (SELECT COUNT(*) FROM contact_scan), 0) n",
        done_sql="SELECT COUNT(*) n FROM contact_scan",
        total_sql="SELECT COUNT(*) n FROM brochure WHERE status='ok'", group="contacts"),
    Job("web_enrich", "Firm websites",
        "Reads each firm's own website (team, bio, contact pages and vCards) for the "
        "people who work there, their titles, emails and direct lines. Re-reads a site "
        "after the interval set in Settings.",
        "scripts.web_enrich", ("--limit", "6"), every_hours=6, timeout_s=2400,
        backlog_sql="SELECT COUNT(*) n FROM firm_current f LEFT JOIN web_enrich_state w ON w.crd=f.crd LEFT JOIN firm_refresh_request r ON r.crd=f.crd WHERE f.website IS NOT NULL AND f.website != '' AND (w.crd IS NULL OR w.scanned_at < '{recrawl_cutoff}' OR r.requested_at > w.scanned_at)",
        done_sql="SELECT COUNT(*) n FROM web_enrich_state",
        total_sql="SELECT COUNT(*) n FROM firm_current WHERE website IS NOT NULL AND website != ''",
        group="contacts"),
    Job("directories", "Directories and sources",
        "Crawls the directories and websites added on the Enrichment screen, each on its "
        "own schedule, and matches what it finds to firms and people.",
        "scripts.crawl_directories", (), every_hours=1, timeout_s=3600,
        backlog_sql="SELECT COUNT(*) n FROM directory_source WHERE status != 'paused' AND (next_run_at IS NULL OR next_run_at <= '{now}')",
        group="contacts"),
    # Backlog: the stamp scripts/ingest_offices.py records (STAMP_SQL there;
    # keep the two identical) no longer matches the roster and feeds.
    Job("offices", "Office phones",
        "Gives everyone a line: the phone of the office their registration places them "
        "in, from the offices each firm files on Form ADV, else the firm's main number. "
        "Also stores each firm's LinkedIn page and the adviser profiles firms list on "
        "Form ADV. Redone whenever the roster or the firm filings change.",
        "scripts.ingest_offices", (), every_hours=168, timeout_s=1800,
        backlog_sql="SELECT CASE WHEN EXISTS (SELECT 1 FROM office_phone_state WHERE k='attach' AND stamp = (SELECT '1/' || (SELECT COALESCE(MAX(id), 0) FROM snapshot WHERE source_key IN ('adv_feed','adv_state_feed','ia_indvl_feed'))::text || '/' || (SELECT COUNT(*) FROM firm_office)::text || '/' || (SELECT COUNT(*) FROM firm_social)::text)) THEN 0 ELSE 1 END n",
        group="contacts"),
    Job("contact_search", "Web search",
        "Finds published personal emails, phones and LinkedIn profiles. Keeps searching "
        "while any channel is missing, including people with a LinkedIn profile already. "
        "Repeat interval is set in Crawling. Search engines are rate limited and source pages checked.",
        "scripts.search_contacts", ("--limit", "40", "--seconds", "420"), every_hours=0.5,
        timeout_s=900,
        backlog_sql=None,
        done_sql="SELECT COUNT(*) n FROM contact_search_state x JOIN person_employment e ON e.org_pk=x.crd AND x.person_key='i:'||e.indvl_pk WHERE e.kind='current'",
        total_sql="SELECT COUNT(*) n FROM person_employment WHERE kind='current'",
        group="contacts"),
    # The email hunt replaced "infer_emails", which wrote unconfirmed guesses
    # into contact_point. Its firm-page button now queues a firm here instead.
    Job("email_hunt", "Email hunt",
        "Looks for confirmed personal emails across all firms, prioritising product fit. Asks each firm's mail "
        "server about a person's likely addresses one after another, without sending "
        "anything, and keeps only an address the server confirms. A bounced guess moves on "
        "to the next pattern, a 'try later' is retried, and when every pattern bounces the "
        "person is looked at again after 60 days.",
        "scripts.hunt_emails", ("--limit", "300", "--seconds", "540"), every_hours=0.25,
        timeout_s=900,
        backlog_sql="SELECT COUNT(*) n FROM firm_current f LEFT JOIN email_hunt_firm h ON h.crd=f.crd LEFT JOIN firm_refresh_request r ON r.crd=f.crd WHERE h.crd IS NULL OR h.next_try_at IS NULL OR h.next_try_at <= '{now}' OR r.requested_at > h.checked_at",
        done_sql="SELECT COUNT(*) n FROM email_hunt_firm h JOIN firm_current f ON f.crd=h.crd",
        total_sql="SELECT COUNT(*) n FROM firm_current", group="contacts"),
    Job("email_verify", "Email verification",
        "Checks every published address with the mail server that would receive it, "
        "without sending anything, and re-checks after 90 days. Personal addresses first. "
        "Guessed addresses are the email hunt's.",
        "scripts.verify_emails", ("--limit", "60"), every_hours=1, timeout_s=1800,
        backlog_sql="SELECT COUNT(*) n FROM contact_point WHERE kind='email' AND source NOT IN ('pattern','ai_web') AND verify_status IN ('unverified','queued')",
        done_sql="SELECT COUNT(*) n FROM contact_point WHERE kind='email' AND source NOT IN ('pattern','ai_web') AND verify_status NOT IN ('unverified','queued')",
        total_sql="SELECT COUNT(*) n FROM contact_point WHERE kind='email' AND source NOT IN ('pattern','ai_web')", group="contacts"),
    Job("mail_platform", "Email platform",
        "Tells Microsoft 365 from Google from public mail records, re-checked every 90 days.",
        "scripts.mail_platform", ("--limit", "400"), every_hours=24,
        backlog_sql="SELECT COUNT(*) n FROM firm_current f LEFT JOIN firm_mail_platform m ON m.crd=f.crd LEFT JOIN firm_refresh_request r ON r.crd=f.crd WHERE m.crd IS NULL OR r.requested_at > m.checked_at",
        done_sql="SELECT COUNT(*) n FROM firm_mail_platform m JOIN firm_scope s ON s.crd=m.crd",
        total_sql="SELECT COUNT(*) n FROM firm_scope", group="filings"),
    Job("firm_refresh", "Custodian refresh",
        "Reads today's custodian names from the current ADV for firms that report "
        "custody, since the bulk custodian archive ends December 2024.",
        "scripts.autopilot_slice", ("firm_refresh",), every_hours=24, timeout_s=600,
        backlog_sql="SELECT COUNT(*) n FROM firm_current f LEFT JOIN firm_refresh r ON r.crd=f.crd WHERE (f.q5k3='Y' OR f.q7b='Y') AND (r.crd IS NULL OR (r.status!='ok' AND r.fetched_at < to_char(NOW()-INTERVAL '1 day', 'YYYY-MM-DD\"T\"HH24:MI:SS')) OR r.fetched_at < to_char(NOW()-INTERVAL '30 days', 'YYYY-MM-DD\"T\"HH24:MI:SS'))",
        done_sql="SELECT COUNT(*) n FROM firm_refresh WHERE status='ok'",
        group="filings"),
    Job("classify", "Firm types",
        "Sorts every firm into a type (independent RIA, custodian, wirehouse, asset "
        "manager, private fund manager and the rest) from its Form ADV answers, its owners "
        "and the known names in Industry knowledge. Types set by hand are kept. When any "
        "type changes, the scores are recomputed.",
        "scripts.classify_firms", ("--ai-limit", "40"), every_hours=24, timeout_s=1800,
        backlog_sql="SELECT COUNT(*) n FROM firm_current f WHERE NOT EXISTS (SELECT 1 FROM firm_class c WHERE c.crd=f.crd)",
        done_sql="SELECT COUNT(*) n FROM firm_class c JOIN firm_current f ON f.crd=c.crd",
        total_sql="SELECT COUNT(*) n FROM firm_current", group="filings"),
    Job("rescore", "Scores",
        "Recomputes every product list from the latest data. Takes seconds.",
        "scripts.score_products", (), every_hours=3, timeout_s=1200, group="system"),
    Job("cusip_verify", "Security map check",
        "Re-checks the 13F security identifiers against real filings every 90 days.",
        "scripts.autopilot_slice", ("cusip_verify",), every_hours=24, group="filings"),
    Job("ai_briefs", "AI firm briefs",
        "Writes a short brief for the best firms on each list and refreshes it when "
        "their data changes. Uses the AI provider in Settings, within its daily limit.",
        "scripts.ai_jobs", ("briefs", "--limit", "15"), every_hours=12, needs="ai:brief",
        group="ai"),
    Job("ai_clean", "AI clean-up",
        "Tidies people's titles and sorts them into roles where the rules could not.",
        "scripts.ai_jobs", ("clean", "--limit", "80"), every_hours=12, needs="ai:clean",
        group="ai"),
    Job("ai_research", "AI contact research",
        "For the best-placed people still missing a confirmed email, a direct phone or a "
        "LinkedIn profile after every free source, asks the AI provider to find what they "
        "or their firm published, then re-reads each cited page itself and keeps only what "
        "is really there. Emails it finds still need the mail server's confirmation. "
        "Repeat interval is set in Crawling; the AI daily limit still applies.",
        "scripts.ai_research", ("--limit", "8"), every_hours=1, timeout_s=1500,
        needs="ai:research", group="ai"),
]
BY_KIND = {j.kind: j for j in JOBS}

GROUP_LABEL = {"filings": "SEC filings", "contacts": "Contacts", "ai": "AI",
               "system": "System"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init(conn) -> None:
    conn.executescript(SCHEMA)
    from .db import add_column
    for col, typ in EXTRA_COLUMNS:
        add_column(conn, "auto_task", col, typ)
    for j in JOBS:
        conn.execute("INSERT INTO auto_task (kind, desired_state) VALUES (?, 'running')"
                     " ON CONFLICT (kind) DO NOTHING", (j.kind,))
    # Jobs used to start paused and wait for a click. They now all run by
    # themselves, so the old default is swept away exactly once (the marker
    # row records it); anything an admin pauses from now on stays paused.
    done = conn.execute("SELECT 1 FROM auto_task WHERE kind='_jobs_v2'").fetchone()
    if not done:
        conn.execute("UPDATE auto_task SET desired_state='running', message=NULL"
                     " WHERE desired_state='paused'")
        conn.execute("INSERT INTO auto_task (kind, desired_state, message) VALUES"
                     " ('_jobs_v2', 'paused', 'marker: jobs run by themselves')"
                     " ON CONFLICT (kind) DO NOTHING")
    conn.commit()


def _fill(sql: str, tag_version: int) -> str:
    sql = sql.replace("{tag_version}", str(tag_version)).replace("{now}", now_iso())
    if "{recrawl_cutoff}" in sql:
        from . import settings
        days = settings.get_int("crawl.recrawl_days", 90) or 90
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        sql = sql.replace("{recrawl_cutoff}", cutoff)
    return sql


def tag_version() -> int:
    import yaml
    from . import config
    try:
        return int(yaml.safe_load((config.CONFIG_DIR / "brochure_tags.yml").read_text(
            encoding="utf-8"))["config_version"])
    except Exception:
        return 1


def count(conn, sql: str | None, tv: int) -> int | None:
    if not sql:
        return None
    try:
        r = conn.execute(_fill(sql, tv)).fetchone()
        return int(list(r.values())[0] or 0)
    except Exception:
        conn.rollback()
        return None


def requirement(job: Job) -> tuple[bool, str]:
    """Whether a job can run at all, and if not, what it waits for."""
    if job.needs.startswith("ai:"):
        from . import ai
        feat = job.needs[3:]
        if not ai.configured():
            return False, "Waiting for an AI provider in Settings, AI"
        if not ai.enabled(feat):
            if feat not in ai.settings.get_list("ai.features"):
                return False, "Switched off in Settings, AI"
            return False, "Today's AI allowance is used up"
    return True, ""


def states(conn) -> dict[str, dict]:
    try:
        return {r["kind"]: dict(r) for r in conn.execute("SELECT * FROM auto_task")}
    except Exception:
        conn.rollback()
        return {}


def due(job: Job, st: dict, backlog: int | None) -> bool:
    if st.get("force"):
        return True
    if st.get("desired_state") == "paused":
        return False
    nxt = st.get("next_run_at")
    # A failed slice keeps its retry delay even if work remains in the queue.
    # Otherwise a permanently failing source immediately runs again forever.
    if backlog and st.get("last_status") not in ("failed", "timeout"):
        return True
    if not nxt:
        return True
    try:
        due_at = datetime.fromisoformat(nxt)
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)
        return due_at <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return True


def schedule_next(job: Job, backlog_left: int | None) -> str:
    """Straight back in the queue while there is a backlog, else the cadence."""
    if backlog_left:
        return now_iso()
    return (datetime.now(timezone.utc) + timedelta(hours=job.every_hours)).isoformat(
        timespec="seconds")


def request_run(conn, kind: str) -> None:
    conn.execute("UPDATE auto_task SET force=1, next_run_at=? WHERE kind=?",
                 (now_iso(), kind))
    conn.commit()


def request_full_refresh(conn) -> int:
    """Refresh all firms without falsifying their previous observation dates."""
    conn.execute("INSERT INTO firm_refresh_request (crd, requested_at)"
                 " SELECT crd, ? FROM firm_current WHERE true"
                 " ON CONFLICT(crd) DO UPDATE SET requested_at=excluded.requested_at",
                 (now_iso(),))
    conn.commit()
    for kind in ('web_enrich', 'mail_platform', 'email_hunt', 'email_verify',
                 'brochure_retag', 'rescore'):
        request_run(conn, kind)
    return conn.execute('SELECT COUNT(*) n FROM firm_current').fetchone()['n']


def set_paused(conn, kind: str, paused: bool) -> None:
    conn.execute("UPDATE auto_task SET desired_state=?, updated_at=? WHERE kind=?",
                 ("paused" if paused else "running", now_iso(), kind))
    conn.commit()


def overview(conn) -> list[dict]:
    """Every job with its live state, for Settings and the dashboard."""
    tv = tag_version()
    st = states(conn)
    out = []
    for j in JOBS:
        s = st.get(j.kind, {})
        ok, why = requirement(j)
        done = count(conn, j.done_sql, tv)
        total = count(conn, j.total_sql, tv)
        backlog = count(conn, j.backlog_sql, tv)
        if s.get("running_since"):
            state = "running"
        elif s.get("desired_state") == "paused":
            state = "paused"
        elif not ok:
            state = "waiting"
        elif backlog:
            state = "queued"
        else:
            state = "scheduled"
        out.append({"job": j, "state": state, "why": why, "done": done, "total": total,
                    "backlog": backlog, "last_run_at": s.get("last_run_at"),
                    "next_run_at": s.get("next_run_at"),
                    "last_status": s.get("last_status"), "message": s.get("message"),
                    "runs": s.get("runs") or 0, "force": s.get("force")})
    return out
