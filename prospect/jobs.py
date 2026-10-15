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

Jobs run in lanes, side by side: mail servers, websites, search and AI, and
the data jobs. Within a lane they take turns, so a long website slice never
holds up the email hunt, which has a lane of its own. prospect/health.py
watches all of it and repairs what it can.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .custodian_refresh import BACKLOG_SQL as _CUSTODIAN_BACKLOG  # noqa: E402

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
                 ("force", "INTEGER DEFAULT 0"), ("last_output", "TEXT"),
                 ("fails", "INTEGER DEFAULT 0"), ("ok_at", "TEXT"))

# One row per slice: how long it took, how it ended, the most memory it used.
# Settings and the diagnostics read lane use and job speed from here.
RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_run (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    lane        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    seconds     REAL,
    status      TEXT NOT NULL,
    peak_mb     REAL,
    output      TEXT
);
CREATE INDEX IF NOT EXISTS ix_job_run_started ON job_run (started_at);
"""

# Lanes run side by side, each with its own turn order. Mail and search wait on
# other people's servers and use little memory; websites may start a headless
# browser; the data jobs read and write the big tables, one at a time.
LANES = ("mail", "web", "search", "data")
LANE_LABEL = {"mail": "Mail servers", "web": "Websites", "search": "Search and AI",
              "data": "Filings and data"}

# A failing job waits longer each time, so a broken source is not hammered,
# and is retried for ever: a fix on the other side is picked up by itself.
RETRY_MINUTES = (15, 60, 180, 480)


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
    needs: str = ""                  # "", "ai:<feature>", "ocr"
    group: str = "enrich"
    extra: dict = field(default_factory=dict)
    lane: str = "data"               # jobs in one lane take turns; lanes run side by side
    min_gap_minutes: float = 0       # a forced run waits this long after the last one
    recent_sql: str | None = None    # what the job got done in the last day, for the screens
    recent_label: str = ""


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
        "hour and the screen stays instant. Most runs only check what changed and refresh "
        "just those firms; the whole table is rebuilt daily.",
        "scripts.build_people_index", (), every_hours=0.5, timeout_s=900,
        backlog_sql="SELECT CASE WHEN to_regclass('people_index') IS NULL THEN 1 ELSE 0 END n",
        group="contacts", min_gap_minutes=15),
    Job("growth", "Growth and hiring",
        "Builds every firm's Growth tab and the ATS list: registered people now and a year "
        "ago, who joined and who left, assets, clients and staff against the filing a year "
        "earlier, support staff per advisor against similar firms, senior departures, "
        "advisors with 30 or more years, new offices and new states, and open roles. The "
        "staff and client history is read from the filings once; state filings again "
        "whenever a weekly feed arrives.",
        "scripts.build_growth", (), every_hours=24, timeout_s=3600,
        # Also due once when the archive has not been read for staff and
        # clients in its current form (scripts/build_growth.py ARCHIVE_KEY).
        backlog_sql="SELECT CASE WHEN to_regclass('firm_growth') IS NULL OR NOT EXISTS (SELECT 1 FROM firm_growth) OR (to_regclass('growth_meta') IS NOT NULL AND NOT EXISTS (SELECT 1 FROM growth_meta WHERE k='archive_staff_v2') AND EXISTS (SELECT 1 FROM snapshot WHERE source_key='adv_filing_crosswalk')) THEN 1 ELSE 0 END n",
        done_sql="SELECT COUNT(*) n FROM firm_growth",
        total_sql="SELECT COUNT(*) n FROM firm_current", group="filings", min_gap_minutes=60,
        recent_sql="SELECT COUNT(*) n FROM firm_growth WHERE computed_at >= LEFT('{day_ago}', 10)",
        recent_label="firms measured"),
    Job("open_roles", "Open roles",
        "Reads each firm's website for its careers page and the openings on it or on the "
        "tracking system it uses (Greenhouse, Lever, BambooHR and others), and sorts each "
        "opening by function. Firms never checked go first, then growing firms; every firm "
        "is checked again after 21 days.",
        "scripts.find_open_roles", ("--limit", "60", "--seconds", "540", "--workers", "4"),
        every_hours=1, timeout_s=1200,
        backlog_sql="SELECT COUNT(*) n FROM firm_current f LEFT JOIN firm_careers c ON c.crd=f.crd WHERE c.crd IS NULL OR c.checked_at < '{ago:21}' OR (c.status='unreachable' AND c.checked_at < '{ago:4}')",
        done_sql="SELECT COUNT(*) n FROM firm_careers c JOIN firm_current f ON f.crd=c.crd",
        total_sql="SELECT COUNT(*) n FROM firm_current", group="contacts", lane="web",
        recent_sql="SELECT COUNT(*) n FROM firm_careers WHERE checked_at >= '{day_ago}'",
        recent_label="careers pages checked"),
    Job("job_boards", "Job boards",
        "Searches the job boards (LinkedIn, Indeed, Glassdoor and others) for openings at "
        "firms whose own site lists none, growing firms first, and keeps only postings that "
        "name the firm. Each firm again after 30 days. Shares the search engines' pacing "
        "with the contact search.",
        "scripts.find_open_roles", ("--boards", "--limit", "12", "--seconds", "300"),
        # On a cadence, not a backlog: while the engines rest after a block the
        # run ends at once, and a backlog would start it again straight away.
        every_hours=0.5, timeout_s=900,
        backlog_sql=None,
        done_sql="SELECT COUNT(*) n FROM firm_careers WHERE searched_at IS NOT NULL",
        group="contacts", lane="search", min_gap_minutes=45,
        recent_sql="SELECT COUNT(*) n FROM firm_careers WHERE searched_at >= '{day_ago}'",
        recent_label="firms searched"),
    Job("brochures", "Brochures",
        "Downloads each firm's Part 2A brochure and reads it for what the firm does, in "
        "its own words. Best-scored firms first; failed downloads retry after a day. Every "
        "30 days each firm is checked for a newer brochure, which replaces the old one.",
        "scripts.brochures", ("--scope", "scored", "--limit", "25"), every_hours=24,
        backlog_sql="SELECT COUNT(*) n FROM firm_scope s LEFT JOIN brochure b ON b.crd=s.crd WHERE b.crd IS NULL OR (b.status IN ('ok','no_brochure') AND b.fetched_at < to_char(NOW() - INTERVAL '30 days', 'YYYY-MM-DD\"T\"HH24:MI:SS'))",
        recent_sql="SELECT COUNT(*) n FROM brochure WHERE fetched_at >= '{day_ago}'",
        recent_label="brochures fetched or checked",
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
        # Each of the three shards claims firms from one shared queue until its
        # seconds are up, so no shard waits for the slowest (scripts/web_enrich.py).
        "scripts.web_enrich", ("--limit", "40", "--parallel", "3", "--seconds", "420"), every_hours=6, timeout_s=2400,
        backlog_sql="SELECT COUNT(*) n FROM firm_current f LEFT JOIN web_enrich_state w ON w.crd=f.crd LEFT JOIN firm_refresh_request r ON r.crd=f.crd WHERE f.website IS NOT NULL AND f.website != '' AND (w.crd IS NULL OR w.scanned_at < '{recrawl_cutoff}' OR r.requested_at > w.scanned_at)",
        done_sql="SELECT COUNT(*) n FROM web_enrich_state",
        total_sql="SELECT COUNT(*) n FROM firm_current WHERE website IS NOT NULL AND website != ''",
        group="contacts", lane="web",
        recent_sql="SELECT COUNT(*) n FROM web_enrich_state WHERE scanned_at >= '{day_ago}'",
        recent_label="websites read"),
    Job("directories", "Directories and sources",
        "Crawls the directories and websites added on the Enrichment screen, each on its "
        "own schedule, and matches what it finds to firms and people.",
        "scripts.crawl_directories", (), every_hours=1, timeout_s=3600,
        backlog_sql="SELECT COUNT(*) n FROM directory_source WHERE status != 'paused' AND (next_run_at IS NULL OR next_run_at <= '{now}')",
        group="contacts", lane="web"),
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
        "Finds published personal emails, phones and LinkedIn profiles, decision-makers at the "
        "best-scored firms first. Keeps searching while any channel is missing; people who "
        "already have a LinkedIn profile come after everyone without one. Repeat interval is "
        "set in Crawling. Search engines are rate limited and source pages checked.",
        "scripts.search_contacts", ("--limit", "40", "--seconds", "420"), every_hours=0.25,
        timeout_s=900,
        backlog_sql=None,
        done_sql="SELECT COUNT(*) n FROM contact_search_state x JOIN person_employment e ON e.org_pk=x.crd AND x.person_key='i:'||e.indvl_pk WHERE e.kind='current'",
        total_sql="SELECT COUNT(*) n FROM person_employment WHERE kind='current'",
        group="contacts", lane="search",
        recent_sql="SELECT COUNT(*) n FROM contact_search_state WHERE searched_at >= '{day_ago}'",
        recent_label="people searched"),
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
        total_sql="SELECT COUNT(*) n FROM firm_current", group="contacts", lane="mail",
        recent_sql="SELECT COUNT(*) n FROM email_attempt WHERE status='valid' AND checked_at >= '{day_ago}'",
        recent_label="emails confirmed"),
    Job("email_verify", "Email verification",
        "Checks every published address with the mail server that would receive it, "
        "without sending anything, and re-checks after 90 days. Personal addresses first. "
        "Guessed addresses are the email hunt's.",
        "scripts.verify_emails", ("--limit", "60"), every_hours=1, timeout_s=1800,
        # Addresses waiting on a domain that is backed off (no clear answer, or it
        # refused our check) are not work to do yet.
        backlog_sql="SELECT COUNT(*) n FROM contact_point cp LEFT JOIN mail_domain md ON md.domain = split_part(cp.value, '@', 2) WHERE cp.kind='email' AND cp.source NOT IN ('pattern','ai_web') AND (cp.verify_status = 'queued' OR (cp.verify_status = 'unverified' AND (md.retry_at IS NULL OR md.retry_at <= '{now}')))",
        done_sql="SELECT COUNT(*) n FROM contact_point WHERE kind='email' AND source NOT IN ('pattern','ai_web') AND verify_status NOT IN ('unverified','queued')",
        total_sql="SELECT COUNT(*) n FROM contact_point WHERE kind='email' AND source NOT IN ('pattern','ai_web')", group="contacts",
        lane="mail", min_gap_minutes=10,
        recent_sql="SELECT COUNT(*) n FROM contact_point WHERE kind='email' AND verified_at >= '{day_ago}'",
        recent_label="addresses checked"),
    Job("mail_platform", "Email platform",
        "Tells Microsoft 365 from Google from public mail records, re-checked every 90 days.",
        "scripts.mail_platform", ("--limit", "400"), every_hours=24,
        backlog_sql="SELECT COUNT(*) n FROM firm_current f LEFT JOIN firm_mail_platform m ON m.crd=f.crd LEFT JOIN firm_refresh_request r ON r.crd=f.crd WHERE m.crd IS NULL OR r.requested_at > m.checked_at",
        # Every firm, priority firms first (scripts/mail_platform.py), so done
        # and total count the same firms the backlog does.
        done_sql="SELECT COUNT(*) n FROM firm_mail_platform m JOIN firm_current f ON f.crd=m.crd",
        total_sql="SELECT COUNT(*) n FROM firm_current", group="filings",
        recent_sql="SELECT COUNT(*) n FROM firm_mail_platform WHERE checked_at >= '{day_ago}'",
        recent_label="firms checked"),
    Job("firm_refresh", "Custodian refresh",
        "Reads today's custodian names from the current ADV for firms that report custody, "
        "since the bulk custodian archive ends December 2024. A firm is read again only when "
        "the weekly SEC feed shows a newer filing, or after 180 days; a filing that fails is "
        "retried after a day, a week, then a month.",
        "scripts.autopilot_slice", ("firm_refresh",), every_hours=24, timeout_s=600,
        # The same rule the slice picks its filings by (prospect/custodian_refresh.py),
        # so the count and the queue never disagree.
        backlog_sql=_CUSTODIAN_BACKLOG,
        done_sql="SELECT COUNT(*) n FROM firm_refresh WHERE status='ok'",
        group="filings",
        recent_sql="SELECT COUNT(*) n FROM firm_refresh WHERE fetched_at >= '{day_ago}'",
        recent_label="filings read"),
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
        "Recomputes every product list from the latest data and writes only the scores that "
        "changed.",
        "scripts.score_products", (), every_hours=3, timeout_s=1200, group="system",
        min_gap_minutes=30),
    Job("cusip_verify", "Security map check",
        "Re-checks the 13F security identifiers against real filings every 90 days.",
        "scripts.autopilot_slice", ("cusip_verify",), every_hours=24, group="filings"),
    Job("ai_briefs", "AI firm briefs",
        "Writes a short brief for the best firms on each list and refreshes it when "
        "their data changes. Uses the AI provider in Settings, within its daily limit.",
        "scripts.ai_jobs", ("briefs", "--limit", "15"), every_hours=12, needs="ai:brief",
        group="ai", lane="search"),
    Job("ai_clean", "AI clean-up",
        "Tidies people's titles and sorts them into roles where the rules could not.",
        "scripts.ai_jobs", ("clean", "--limit", "80"), every_hours=12, needs="ai:clean",
        group="ai", lane="search"),
    Job("ai_research", "AI contact research",
        "For the best-placed people still missing a confirmed email, a direct phone or a "
        "LinkedIn profile after every free source, asks the AI provider to find what they "
        "or their firm published, then re-reads each cited page itself and keeps only what "
        "is really there. Emails it finds still need the mail server's confirmation. "
        "Repeat interval is set in Crawling; the AI daily limit still applies.",
        "scripts.ai_research", ("--limit", "8"), every_hours=1, timeout_s=1500,
        needs="ai:research", group="ai", lane="search"),
    Job("ai_extract", "AI page reading",
        "Reads the team pages the website reader could not make sense of (names with no "
        "title or email) with the AI provider, best firms first, and keeps only what the "
        "page prints. Emails it finds still need the mail server's confirmation. A page is "
        "sent again only when its text changes, and the job uses at most half of the AI "
        "daily limit, so AI research keeps the rest.",
        "scripts.ai_jobs", ("extract", "--limit", "30", "--seconds", "600"), every_hours=1,
        timeout_s=1500, needs="ai:extract", group="ai", lane="search",
        # Queued pages, but 0 once today's extraction share (half of ai.daily_limit) or the
        # reserve AI research keeps is reached, so a capped job is not restarted every turn.
        # Must match scripts/web_enrich.py extract_room.
        backlog_sql="WITH lim AS (SELECT COALESCE((SELECT NULLIF(value, '')::numeric FROM app_setting WHERE key = 'ai.daily_limit'), 400) AS n), used AS (SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE feature = 'extract') AS x FROM ai_call WHERE at >= LEFT('{now}', 10)) SELECT CASE WHEN used.x + 2 > FLOOR(lim.n * 0.5) OR used.n + 2 > lim.n - GREATEST(20, FLOOR(lim.n * 0.25)) THEN 0 ELSE (SELECT COUNT(*) FROM web_ai_page WHERE status = 'queued') END AS n FROM lim, used",
        done_sql="SELECT COUNT(*) n FROM web_ai_page WHERE status='done'",
        total_sql="SELECT COUNT(*) n FROM web_ai_page",
        recent_sql="SELECT COUNT(*) n FROM web_ai_page WHERE status='done' AND done_at >= '{day_ago}'",
        recent_label="pages read"),
    Job("brochure_ocr", "Brochures without a text layer",
        "Reads the few brochures that come back empty from the PDF reader: most were typed "
        "into the SEC's fillable form, so their text is read exactly from the form fields; "
        "scanned pages are read with OCR (Tesseract on this server, or an Unlimited-OCR "
        "server if one is set in Settings, Crawling); a blank template is marked as such.",
        "scripts.ocr_brochures", ("--limit", "6"), every_hours=24, timeout_s=1800,
        group="filings", lane="search",
        backlog_sql="SELECT COUNT(*) n FROM brochure WHERE status='ok' AND COALESCE(ocr_status,'') = '' AND COALESCE(text_chars,0) < 50 * GREATEST(COALESCE(pages,1),1)",
        done_sql="SELECT COUNT(*) n FROM brochure WHERE ocr_status IN ('ok','empty')",
        total_sql="SELECT COUNT(*) n FROM brochure WHERE status='ok' AND (ocr_status IS NOT NULL OR COALESCE(text_chars,0) < 50 * GREATEST(COALESCE(pages,1),1))"),
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
    add_column(conn, "run_log", "slice_id", "TEXT")
    conn.executescript(RUN_SCHEMA)
    # Jobs retired from the registry leave a row that would read as overdue.
    conn.execute("DELETE FROM auto_task WHERE kind IN ('infer_emails')")
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
    if "{day_ago}" in sql:
        sql = sql.replace("{day_ago}", (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(
            timespec="seconds"))
    if "{recrawl_cutoff}" in sql:
        from . import settings
        days = settings.get_int("crawl.recrawl_days", 90) or 90
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        sql = sql.replace("{recrawl_cutoff}", cutoff)
    if "{ago:" in sql:
        sql = re.sub(r"\{ago:(\d+)\}", lambda m: (datetime.now(timezone.utc) - timedelta(
            days=int(m.group(1)))).isoformat(timespec="seconds"), sql)
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
    if job.needs == "ocr":
        from . import ocr
        if not ocr.configured():
            return False, "Waiting for an OCR server in Settings, Crawling"
    return True, ""


def lane_jobs(lane: str) -> list[Job]:
    return [j for j in JOBS if j.lane == lane]


def retry_at(fails: int) -> str:
    """When a job that has failed this many times in a row runs again."""
    mins = RETRY_MINUTES[min(max(fails, 1), len(RETRY_MINUTES)) - 1]
    return (datetime.now(timezone.utc) + timedelta(minutes=mins)).isoformat(timespec="seconds")


def _age_minutes(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        t = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 60


def states(conn) -> dict[str, dict]:
    try:
        return {r["kind"]: dict(r) for r in conn.execute("SELECT * FROM auto_task")}
    except Exception:
        conn.rollback()
        return {}


def due(job: Job, st: dict, backlog: int | None) -> bool:
    if st.get("force"):
        # Other jobs ask for a rescore or a verification pass after every slice;
        # those requests are folded together rather than run back to back.
        age = _age_minutes(st.get("last_run_at"))
        if not job.min_gap_minutes or age is None or age >= job.min_gap_minutes:
            return True
        # Inside the gap the request waits, folded into the run at its end.
        # request_run also moved next_run_at to now, so falling through to the
        # schedule below would let every request straight in.
        return False
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


# Each job's done, total, backlog and last-day figures are about a hundred
# counts over the largest tables, and took Settings many seconds to open. They
# move slowly, so they are kept for two minutes and, once older, served as
# they are while a background thread works out fresh ones. Job states (running,
# paused, last run) are always read live.
COUNTS_TTL_S = 120
_COUNTS: dict = {"t": 0.0, "v": None, "busy": False}
_COUNTS_LOCK = threading.Lock()


def _all_counts(conn) -> dict[str, tuple]:
    tv = tag_version()
    return {j.kind: (count(conn, j.done_sql, tv), count(conn, j.total_sql, tv),
                     count(conn, j.backlog_sql, tv), count(conn, j.recent_sql, tv))
            for j in JOBS}


def _refresh_counts() -> None:
    from . import db
    try:
        c = db.connect()
        try:
            v = _all_counts(c)
        finally:
            c.close()
        with _COUNTS_LOCK:
            _COUNTS.update(t=time.monotonic(), v=v)
    except Exception:
        pass
    finally:
        with _COUNTS_LOCK:
            _COUNTS["busy"] = False


def job_counts(conn) -> dict[str, tuple]:
    with _COUNTS_LOCK:
        have, age = _COUNTS["v"], time.monotonic() - _COUNTS["t"]
        if have is not None and age >= COUNTS_TTL_S and not _COUNTS["busy"]:
            _COUNTS["busy"] = True
            threading.Thread(target=_refresh_counts, daemon=True, name="job-counts").start()
    if have is not None:
        return have
    v = _all_counts(conn)
    with _COUNTS_LOCK:
        _COUNTS.update(t=time.monotonic(), v=v)
    return v


def summary(conn) -> dict:
    """How many jobs are working and paused, from their states alone: what
    the Settings overview says, without counting anyone's backlog."""
    st = states(conn)
    run = sum(1 for j in JOBS if st.get(j.kind, {}).get("running_since"))
    paused = sum(1 for j in JOBS if st.get(j.kind, {}).get("desired_state") == "paused"
                 and not st.get(j.kind, {}).get("running_since"))
    return {"jobs": len(JOBS), "running": run, "paused": paused}


def overview(conn) -> list[dict]:
    """Every job with its live state, for Settings and the dashboard."""
    st = states(conn)
    counts = job_counts(conn)
    out = []
    for j in JOBS:
        s = st.get(j.kind, {})
        ok, why = requirement(j)
        done, total, backlog, recent = counts.get(j.kind, (None, None, None, None))
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
                    "backlog": backlog, "recent": recent, "last_run_at": s.get("last_run_at"),
                    "fails": s.get("fails") or 0, "ok_at": s.get("ok_at"),
                    "running_since": s.get("running_since"),
                    "next_run_at": s.get("next_run_at"),
                    "last_status": s.get("last_status"), "message": s.get("message"),
                    "runs": s.get("runs") or 0, "force": s.get("force")})
    return out
