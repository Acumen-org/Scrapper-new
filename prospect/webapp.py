"""Bellwether web app: the intelligence platform on US investment advisers.

A bellwether is a leading indicator, which is what every row in this system is:
a filing, a website, a hire or a change that says a firm is worth calling before
anyone else notices. Bellwether turns SEC filings, each firm's own public
words, its people and its websites into ranked product lists, signals and
firm dossiers, with Bellwether AI on top to ask questions in plain English.

Architecture rules learned the hard way:

  - GET handlers never write. All working tables are created once at startup,
    because DDL inside a request is a write transaction that queues behind any
    background ingest.
  - One visual system, in static/app.css: dark, one dark red accent, green
    only where something is a lead or confirmed, amber where something is
    missing or needs care.
  - A number never renders without the reason for it, and a score never
    renders without how much of it rests on known data.
  - No page needs a manual. If a screen needs explaining, the explanation goes
    on the screen, next to the thing it explains. UI copy carries no em dashes.
  - Jobs run by themselves. Nobody has to remember to start anything.

    python -m uvicorn prospect.webapp:app --port 8787
"""

from __future__ import annotations

import contextvars
import hashlib
import html
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from . import auth, config, db, procs, products, settings, users
from .names import nice_name

APP_NAME = "Bellwether"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Session cookies are marked Secure when served over HTTPS, which is every
# deployment except running it on your own machine over plain http.
SECURE_COOKIES = os.environ.get("BELLWETHER_HTTPS", "").lower() in ("1", "true", "yes")

# Managed mode: a supervisor (Docker, Nomad) owns the process lifecycle. Quit
# disappears from the UI, because killing the process would just make the
# supervisor restart it, and pidfiles from a previous container are always
# stale: PID namespaces start over at 1, so a recorded pid usually names some
# OTHER live process in the new container.
MANAGED = os.environ.get("BELLWETHER_MANAGED", "").lower() in ("1", "true", "yes")

# The signed-in account, request scoped. Context variables propagate into the
# threadpool FastAPI runs sync endpoints on, so this is safe for both kinds.
CURRENT_USER: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_user", default=None)
CURRENT_ACCOUNT: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "current_account", default=None)


def current_user() -> str | None:
    return CURRENT_USER.get()


def current_account() -> dict | None:
    return CURRENT_ACCOUNT.get()


def current_owner() -> str:
    """Display name of whoever is signed in, for stamping ownership fields."""
    a = CURRENT_ACCOUNT.get()
    return (a or {}).get("name") or (CURRENT_USER.get() or "")


def is_admin() -> bool:
    return users.is_admin(current_account())


app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_cfg = config.load()
_scoring = yaml.safe_load((config.CONFIG_DIR / "scoring.yml").read_text(encoding="utf-8"))
PRODUCTS = _scoring["trigger_products"]
CAVEATS = _scoring["caveats"]

TYPE_LABEL = {
    "new_registration": "New registration",
    "reregistration_or_gap": "Re-registration",
    "aum_jump": "Assets jump",
    "aum_drop": "Assets drop",
    "iar_growth": "Advisors added",
    "custodian_change_to_platform": "Moved to Schwab",
    "custodian_change_from_platform": "Left Schwab",
    "custodian_change_other": "Custodian change",
    "first_private_fund": "First private fund",
    "first_real_estate_fund": "First real estate fund",
}

STATUSES = ["new", "working", "meeting set", "qualified", "disqualified", "customer"]

APP_TABLES = """
CREATE TABLE IF NOT EXISTS trigger_action (
    trigger_id   INTEGER PRIMARY KEY REFERENCES trigger_event(id),
    state        TEXT NOT NULL,
    reason       TEXT,
    snooze_until TEXT,
    actioned_by  TEXT,
    actioned_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_action_state ON trigger_action (state);
CREATE TABLE IF NOT EXISTS firm_note (
    crd        TEXT PRIMARY KEY,
    note       TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS firm_status (
    crd        TEXT PRIMARY KEY,
    status     TEXT,
    owner      TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brochure_negation (
    id         INTEGER PRIMARY KEY,
    crd        TEXT NOT NULL,
    tag        TEXT NOT NULL,
    phrase     TEXT,
    sentence   TEXT,
    status     TEXT NOT NULL DEFAULT 'open',
    decided_by TEXT,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_bneg_status ON brochure_negation (status);
CREATE TABLE IF NOT EXISTS firm_watch (
    crd       TEXT PRIMARY KEY,
    added_by  TEXT,
    added_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS saved_view (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    page      TEXT NOT NULL,          -- signals | firms | list:<product>
    qs        TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contact (
    crd            TEXT NOT NULL,
    individual_crd TEXT,
    name           TEXT NOT NULL,
    title          TEXT,
    source         TEXT NOT NULL,     -- ia_indvl
    PRIMARY KEY (crd, individual_crd)
);
CREATE INDEX IF NOT EXISTS ix_contact_crd ON contact (crd);
CREATE TABLE IF NOT EXISTS contact_email (
    id        INTEGER PRIMARY KEY,
    crd       TEXT NOT NULL,
    name      TEXT,
    title     TEXT,                                -- filed role, for the export
    email     TEXT NOT NULL,
    pattern   TEXT,
    status    TEXT NOT NULL DEFAULT 'candidate',  -- candidate | queued | valid | invalid | error
    checked_at TEXT,
    UNIQUE (crd, email)
);
CREATE INDEX IF NOT EXISTS ix_cemail_status ON contact_email (status);
CREATE TABLE IF NOT EXISTS firm_contact_info (
    id      INTEGER PRIMARY KEY,
    crd     TEXT NOT NULL,
    kind    TEXT NOT NULL,             -- email | phone
    value   TEXT NOT NULL,
    source  TEXT NOT NULL,             -- brochure | adv_feed
    context TEXT,                      -- the brochure line it appeared on
    found_at TEXT,
    UNIQUE (crd, kind, value)
);
CREATE INDEX IF NOT EXISTS ix_fci_crd ON firm_contact_info (crd);
CREATE TABLE IF NOT EXISTS scheduler_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    last_check   TEXT,                -- heartbeat: scheduler looked at due-ness
    last_started TEXT,                -- last automatic weekly launch
    message      TEXT
);
-- Hand-built firm lists ("playlists"): a named set of firms you assemble
-- yourself, separate from the scored product lists.
CREATE TABLE IF NOT EXISTS user_list (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT
);
CREATE TABLE IF NOT EXISTS user_list_item (
    list_id  INTEGER NOT NULL REFERENCES user_list(id),
    crd      TEXT NOT NULL,
    added_at TEXT NOT NULL,
    added_by TEXT,
    PRIMARY KEY (list_id, crd)
);
CREATE INDEX IF NOT EXISTS ix_uli_crd ON user_list_item (crd);
-- Indexes the busiest screens lean on.
CREATE INDEX IF NOT EXISTS ix_trig_crd_date ON trigger_event (crd, detected_date);
CREATE INDEX IF NOT EXISTS ix_trig_date ON trigger_event (detected_date);
CREATE INDEX IF NOT EXISTS ix_hist_crd ON firm_history (crd, filing_date);
"""


def _init_optional(c, module: str) -> None:
    """Modules whose tables are optional at boot: a failure here is logged by
    being skipped, never allowed to stop the app from serving."""
    try:
        mod = __import__(f"prospect.{module}", fromlist=["init"])
        mod.init(c)
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass


@app.on_event("startup")
def _init_once() -> None:
    """Every table the app touches exists before the first request, so request
    handlers never issue DDL and GETs stay pure readers."""
    c = db.connect()
    db.init(c)
    db.init_firm(c)
    # A brand new install has none of the tables the weekly cycle creates, and
    # the app's own tables reference some of them (a signal's action points at
    # the signal). Create those first, from the scripts that own them.
    for mod in ("scripts.triggers", "scripts.build_firm_history", "scripts.ingest_13f_index",
                "scripts.match_13f", "scripts.ingest_13f_holdings", "scripts.brochures",
                "scripts.ingest_schedule_a", "scripts.web_enrich", "scripts.mail_platform",
                "scripts.ingest_adv_extra", "scripts.custodian_share", "scripts.segment_real_estate",
                "scripts.ingest_offices"):
        try:
            m = __import__(mod, fromlist=["SCHEMA"])
            c.executescript(m.SCHEMA)
            if mod == "scripts.web_enrich":
                for col, decl in m.STATE_COLUMNS:
                    db.add_column(c, "web_enrich_state", col, decl)
            c.commit()
        except Exception:
            c.rollback()
    try:
        c.executescript(APP_TABLES)
    except Exception:
        c.rollback()
        c.executescript(APP_TABLES.split("-- Indexes the busiest")[0])
    products.init(c)
    if "title" not in {r[1] for r in c.execute("PRAGMA table_info(contact_email)")}:
        c.execute("ALTER TABLE contact_email ADD COLUMN title TEXT")
    c.commit()
    settings.init(c)
    users.init(c)
    for mod in ("contacts", "jobs", "ai", "roles", "people", "verify", "directory",
                "websignals", "firmtype", "knowledge", "websearch"):
        _init_optional(c, mod)
    c.close()

    # First boot after the contact tables were unified: copy the old ones in.
    # In the background, so the health check answers while it runs, and under
    # an advisory lock, so exactly one process of the two workers does it.
    def _backfill():
        b = db.connect()
        try:
            got = b.execute("SELECT pg_try_advisory_lock(424242) ok").fetchone()["ok"]
            if not got:
                return
            empty = b.execute("SELECT NOT EXISTS (SELECT 1 FROM contact_point) e").fetchone()["e"]
            b.commit()
            if empty:
                from . import contacts
                contacts.backfill(b)
        except Exception:
            try:
                b.rollback()
            except Exception:
                pass
        finally:
            # Session locks outlive a transaction, and this connection goes
            # back to the pool: release it on every path.
            try:
                b.execute("SELECT pg_advisory_unlock_all()")
                b.commit()
            except Exception:
                pass
            b.close()
    threading.Thread(target=_backfill, daemon=True, name="contact-backfill").start()

    # In a container, pidfiles surviving on the mounted volume are lies: the
    # new PID namespace reuses low numbers, so a stale scheduler.pid routinely
    # names a live but unrelated process and the weekly pull never runs again.
    # Purged exactly once per container boot: the marker lives in /tmp, which
    # is container-local, and O_EXCL makes the first worker the only purger.
    if MANAGED:
        try:
            os.close(os.open("/tmp/bellwether_boot_purge",
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            for name in ("server.pid", "scheduler.pid", "autopilot.pid"):
                (config.DATA_DIR / name).unlink(missing_ok=True)
        except FileExistsError:
            pass
        except OSError:
            pass

    # Record the supervisor PID from inside the app rather than trusting the
    # launcher to have done it, so Quit always has a live PID to stop.
    try:
        (config.DATA_DIR / "server.pid").write_text(str(os.getppid()))
    except OSError:
        pass

    # Work out Home's numbers in the background now, so the first person to
    # open it after a deploy does not wait for them.
    def _warm():
        try:
            from . import home_view
            home_view.data()
        except Exception:
            pass
    threading.Thread(target=_warm, daemon=True, name="warm-home").start()

    # A new install (or the first boot of this release) has no People index
    # yet: build it now rather than wait for the background worker's turn.
    def _people_index():
        b = db.connect()
        try:
            from . import people_index
            if people_index.exists(b):
                return
            if not b.execute("SELECT pg_try_advisory_lock(424243) ok").fetchone()["ok"]:
                return
            b.commit()
            people_index.build(b)
        except Exception:
            try:
                b.rollback()
            except Exception:
                pass
        finally:
            try:
                b.execute("SELECT pg_advisory_unlock_all()")
                b.commit()
            except Exception:
                pass
            b.close()
    threading.Thread(target=_people_index, daemon=True, name="people-index").start()

    # The scheduler runs itself. One worker wins the claim; the other simply
    # does not schedule. BELLWETHER_SCHEDULER=0 switches it off, for a test copy
    # that must not crawl or check mail on its own.
    if os.environ.get("BELLWETHER_SCHEDULER", "1") == "0":
        return
    if procs.claim_pidfile(config.DATA_DIR / "scheduler.pid"):
        t = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
        t.start()


FEED_DUE_DAYS = 6.5   # the SEC publishes weekly; pull as soon as a new file is due
CHECK_EVERY_S = 300   # look every five minutes; cheap, a few indexed queries


def _weekly_due(c) -> tuple[bool, str]:
    """Whether an automatic pull should start now, and why or why not."""
    last = c.execute("SELECT MAX(captured_at) t FROM snapshot"
                     " WHERE source_key='adv_feed'").fetchone()["t"]
    forced = c.execute("SELECT force FROM auto_task WHERE kind='weekly_cycle'").fetchone()
    if forced and forced["force"]:
        return True, "run requested from Settings"
    if not last:
        return True, "no feed capture held at all"
    age_days = (datetime.now(timezone.utc)
                - datetime.fromisoformat(last)).total_seconds() / 86400
    if age_days < FEED_DUE_DAYS:
        due = max(0.0, FEED_DUE_DAYS - age_days)
        got = "today" if age_days < 1 else ("yesterday" if age_days < 2 else f"{age_days:.0f} days ago")
        nxt = "today" if due < 1 else ("tomorrow" if due < 2 else f"in {due:.0f} days")
        return False, f"SEC feed captured {got}; the next pull is due {nxt}"
    busy = c.execute("SELECT COUNT(*) n FROM run_log WHERE status='running'"
                     " AND started_at::timestamptz > NOW() - INTERVAL '2 hours'").fetchone()["n"]
    if busy:
        return False, "a run is already in flight"
    return True, f"the SEC feed is {age_days:.0f} days old"


def start_weekly() -> None:
    log = open(config.DATA_DIR / "weekly.log", "ab")
    subprocess.Popen([sys.executable, "-m", "scripts.run_weekly", "--brochure-slice", "120"],
                     cwd=str(config.ROOT), stdout=log, stderr=log,
                     creationflags=procs.SPAWN_FLAGS)


def _scheduler_loop() -> None:
    """Keep everything running without anyone thinking about it: launch the
    weekly SEC cycle when a new feed is due, and make sure the background
    worker that runs every other job is alive. Failures land in scheduler_state
    for Settings to show."""
    import time as _time
    _time.sleep(15)  # let startup settle before the first check
    while True:
        try:
            ensure_autopilot()
            c = conn()
            c.execute("INSERT INTO auto_task (kind, desired_state) VALUES ('weekly_cycle',"
                      " 'running') ON CONFLICT (kind) DO NOTHING")
            due, why = _weekly_due(c)
            now_s = datetime.now(timezone.utc).isoformat(timespec="seconds")
            c.execute("INSERT INTO scheduler_state (id, last_check, message)"
                      " VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET"
                      " last_check=excluded.last_check, message=excluded.message",
                      (now_s, why))
            if due:
                c.execute("UPDATE scheduler_state SET last_started=? WHERE id=1", (now_s,))
                c.execute("UPDATE auto_task SET force=0, last_run_at=? WHERE kind='weekly_cycle'",
                          (now_s,))
                start_weekly()
            c.commit()
            c.close()
        except Exception as exc:
            try:
                c2 = conn()
                c2.execute("INSERT INTO scheduler_state (id, last_check, message)"
                           " VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET"
                           " last_check=excluded.last_check,"
                           " message=excluded.message",
                           (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            f"scheduler error: {type(exc).__name__}: {exc}"))
                c2.commit()
                c2.close()
            except Exception:
                pass
        _time.sleep(CHECK_EVERY_S)


def ensure_autopilot() -> bool:
    """Launch the background worker unless one is already alive. Liveness goes
    through procs.is_alive, never os.kill(pid, 0), which on Windows terminates
    the process being checked."""
    if os.environ.get("BELLWETHER_SCHEDULER", "1") == "0":
        return False
    pidfile = config.DATA_DIR / "autopilot.pid"
    if procs.alive_pid(pidfile) is not None:
        return True
    log = open(config.DATA_DIR / "autopilot.log", "ab")
    subprocess.Popen([sys.executable, "-m", "scripts.autopilot"],
                     cwd=str(config.ROOT), stdout=log, stderr=log,
                     creationflags=procs.SPAWN_FLAGS)
    return True


def stop_everything() -> None:
    """Stop the whole tool: background jobs first, then the server itself.
    Killing the supervisor tree is the only reliable stop on Windows, because
    workers inherit the listening socket."""
    apf = config.DATA_DIR / "autopilot.pid"
    ap = procs.alive_pid(apf)
    if ap is not None:
        procs.kill_tree(ap)
    apf.unlink(missing_ok=True)
    srv = config.DATA_DIR / "server.pid"
    recorded = procs.alive_pid(srv)
    srv.unlink(missing_ok=True)
    parent = os.getppid()
    target = parent if parent > 4 else recorded
    if target is None:
        return
    if recorded is not None and recorded != target:
        procs.kill_tree(recorded)
    procs.kill_tree(target)


def conn():
    """Per-request connection, from the pool. No DDL here, ever: reads must
    never become writes."""
    return db.connect()


# ---------------------------------------------------------------- formatting

def esc(v) -> str:
    return html.escape(str(v)) if v is not None else ""


def escn(v) -> str:
    """A firm name, escaped and in readable case."""
    return esc(nice_name(v))


def money(v) -> str:
    if v is None:
        return "-"
    v = float(v)
    if v >= 1e12:
        return f"${v/1e12:.2f}T"
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.0f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:,.0f}"


def num(v) -> str:
    return f"{v:,.0f}" if isinstance(v, (int, float)) else "-"


def initials(name: str) -> str:
    parts = [p for p in (name or "").replace(".", " ").split() if p[:1].isalpha()]
    return ((parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()
            if parts else "?")


SIGNAL_WINDOW_DAYS = 60   # "new" on Home and the sidebar count


def signal_cutoff(days: int = SIGNAL_WINDOW_DAYS) -> str:
    from datetime import date, timedelta
    return (date.today() - timedelta(days=days)).isoformat()


def safe_back(target: str | None, default: str = "/") -> str:
    """A redirect target that can only stay inside this app.

    "/firms" is fine; "//evil.example", "/\\evil.example" and "https://..." are
    not: browsers read the first two as a jump to another host, which made
    every `back` field an open redirect."""
    t = (target or "").strip()
    if (not t.startswith("/") or t.startswith("//") or t.startswith("/\\")
            or "\r" in t or "\n" in t):
        return default
    return t


def qs_join(**kw) -> str:
    """A query string from keyword args, skipping empty values."""
    return urllib.parse.urlencode({k: v for k, v in kw.items() if v not in ("", None)})


def caveat(key: str, text: str) -> str:
    return f'<abbr title="{esc(CAVEATS[key].strip())}">{text}</abbr>'


def score_cell(score, coverage=None, potential=None, show_cov: bool = True) -> str:
    """A score with its foundation: the solid bar is what the firm earned on
    data we hold, the hatched part is what is still unknown, and the label says
    how much of the scoring rests on known data."""
    if score is None:
        return '<span class="muted">-</span>'
    score = float(score)
    cov = float(coverage) if coverage is not None else 100.0
    pot = float(potential) if potential is not None else score
    unk = max(0.0, min(100.0 - score, pot - score))
    hi = " hi" if score >= 60 and cov >= 80 else ""
    tip = (f"{score:.0f} of 100, earned on known data. {cov:.0f}% of the scoring rests on "
           f"data Bellwether holds" + (f"; with the missing {100 - cov:.0f}% it could reach "
                                       f"{pot:.0f}." if cov < 99.5 else "."))
    cov_html = ""
    if coverage is None:
        # A score from before coverage was recorded: say nothing rather than
        # claim it rests on complete data. The next rescore fills it in.
        tip = f"{score:.0f} of 100."
    elif show_cov:
        cov_html = (f'<span class="cov{" part" if cov < 80 else ""}">'
                    f'{cov:.0f}% known</span>')
    return (f'<div class="score{hi}" title="{esc(tip)}"><span class="v">{score:.0f}</span>'
            f'<span class="sbar"><i class="k" style="width:{score:.0f}%"></i>'
            f'<i class="u" style="width:{unk:.0f}%"></i></span>{cov_html}</div>')


def missing_chip(missing: str | None, max_items: int = 3) -> str:
    items = [m for m in (missing or "").split("|") if m]
    if not items:
        return ""
    shown = ", ".join(items[:max_items]) + (f" +{len(items) - max_items}" if len(items) > max_items else "")
    return (f'<span class="missing" title="Missing data: {esc(", ".join(items))}">'
            f'Missing: {esc(shown)}</span>')


# ---------------------------------------------------------------- static assets

_ASSET_V: dict[str, str] = {}


def asset(path: str) -> str:
    """A static URL that changes whenever the file does, so browsers can cache
    it for a year and still never run yesterday's script."""
    v = _ASSET_V.get(path)
    if v is None:
        try:
            v = hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:10]
        except OSError:
            v = "0"
        _ASSET_V[path] = v
    return f"/static/{path}?v={v}"


FAVICON = '<link rel="icon" type="image/svg+xml" href="/static/mark.svg">'

# Pages render before the click lands: hovering a link for a moment starts
# loading the next page in the background (Chrome and Edge), so moving around
# the app feels instant. GET pages never write, which is what makes this safe.
SPECULATION = """<script type="speculationrules">{"prerender":[{"where":{"and":[
{"href_matches":"/*"},{"not":{"href_matches":"/logout"}},{"not":{"href_matches":"/quit"}},
{"not":{"href_matches":"/auth/*"}},{"not":{"href_matches":"/*export*"}},
{"not":{"href_matches":"/static/*"}},{"not":{"selector_matches":"[data-noprefetch]"}}]},
"eagerness":"moderate"}]}</script>"""

PALETTE_HTML = """<div id="pal"><div class="box">
<input id="palq" placeholder="Find a firm, a person, or a page" autocomplete="off">
<div id="palr"></div>
<div class="foot">Arrow keys to move, Enter to open, Esc to close</div></div></div>"""

I = ('fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"'
     ' stroke-linejoin="round"')
ICONS = {
    "home": f'<svg viewBox="0 0 24 24" {I}><path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/></svg>',
    "ask": f'<svg viewBox="0 0 24 24" {I}><circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="3"/><path d="M12 3.5v2M12 18.5v2M3.5 12h2M18.5 12h2"/></svg>',
    "signals": f'<svg viewBox="0 0 24 24" {I}><path d="M22 12h-4l-3 8-6-16-3 8H2"/></svg>',
    "firms": f'<svg viewBox="0 0 24 24" {I}><path d="M3 21h18"/><path d="M5 21V7l7-4 7 4v14"/><path d="M9 21v-4h6v4"/></svg>',
    "people": f'<svg viewBox="0 0 24 24" {I}><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18 14.8c1.9.7 3.1 2.4 3.5 5.2"/></svg>',
    "saved": f'<svg viewBox="0 0 24 24" {I}><path d="M6 3h12v18l-6-4-6 4z"/></svg>',
    "enrich": f'<svg viewBox="0 0 24 24" {I}><path d="M12 3v4M12 17v4M3 12h4M17 12h4"/><circle cx="12" cy="12" r="4"/></svg>',
    "settings": f'<svg viewBox="0 0 24 24" {I}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
    "search": f'<svg viewBox="0 0 24 24" {I}><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>',
    "out": f'<svg viewBox="0 0 24 24" {I}><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><path d="M10 17l-5-5 5-5"/><path d="M5 12h11"/></svg>',
    "spark": f'<svg viewBox="0 0 24 24" {I}><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/></svg>',
    "mail": f'<svg viewBox="0 0 24 24" {I}><rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M3.5 7l8.5 6 8.5-6"/></svg>',
    "phone": f'<svg viewBox="0 0 24 24" {I}><path d="M5 3.5h3.2l1.6 4.3-2.2 1.5a11 11 0 0 0 7.1 7.1l1.5-2.2 4.3 1.6V19a1.8 1.8 0 0 1-2 1.8A16.6 16.6 0 0 1 3.2 5.5 1.8 1.8 0 0 1 5 3.5z"/></svg>',
    "linkedin": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M4.98 3.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5zM3 9.75h4v11H3zm6.5 0h3.8v1.6h.06c.53-1 1.83-2.06 3.77-2.06 4.03 0 4.77 2.65 4.77 6.1v5.36h-4v-4.75c0-1.13-.02-2.6-1.58-2.6-1.59 0-1.83 1.24-1.83 2.52v4.83h-4z"/></svg>',
    "globe": f'<svg viewBox="0 0 24 24" {I}><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.8 3.9 5.8 3.9 9s-1.3 6.2-3.9 9c-2.6-2.8-3.9-5.8-3.9-9S9.4 5.8 12 3z"/></svg>',
    "pin": f'<svg viewBox="0 0 24 24" {I}><path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/></svg>',
    "check": f'<svg viewBox="0 0 24 24" {I}><path d="M4.5 12.5l5 5 10-11"/></svg>',
    "plus": f'<svg viewBox="0 0 24 24" {I}><path d="M12 5v14M5 12h14"/></svg>',
    "arrow": f'<svg viewBox="0 0 24 24" {I}><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
    "send": f'<svg viewBox="0 0 24 24" {I}><path d="M12 19V5M6 11l6-6 6 6"/></svg>',
    "lists": f'<svg viewBox="0 0 24 24" {I}><path d="M8 6h13M8 12h13M8 18h13"/><path d="M3.5 6h.01M3.5 12h.01M3.5 18h.01"/></svg>',
    "filter": f'<svg viewBox="0 0 24 24" {I}><path d="M4 6h16M7 12h10M10 18h4"/></svg>',
    "shield": f'<svg viewBox="0 0 24 24" {I}><path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6z"/><path d="M9 12l2.2 2.2L15.5 10"/></svg>',
    "clock": f'<svg viewBox="0 0 24 24" {I}><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    "star": f'<svg viewBox="0 0 24 24" {I}><path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.3-4.1 5.9-.8z"/></svg>',
    "bolt": f'<svg viewBox="0 0 24 24" {I}><path d="M13 2.5L4.5 13.5H12l-1 8 8.5-11H12z"/></svg>',
    "chart": f'<svg viewBox="0 0 24 24" {I}><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>',
}
FAMILY_COLOUR = {"PHH": "#c65454", "AcuBooth": "#cfa95c", "Glynac": "#63aa7c"}

_NAV_CACHE: dict = {"t": 0.0}


def _nav_counts() -> dict:
    """Sidebar counts, cached for half a minute: they head every page."""
    import time as _time
    if _time.monotonic() - _NAV_CACHE["t"] < 30.0:
        return _NAV_CACHE
    c = conn()

    def one(sql, args=()):
        try:
            return c.execute(sql, args).fetchone()["n"]
        except Exception:
            # Postgres aborts the transaction on a failed statement; without
            # the rollback every later query on this connection fails too.
            try:
                c.rollback()
            except Exception:
                pass
            return 0

    out = {"t": _time.monotonic()}
    lists: dict = {}
    try:
        for r in c.execute("SELECT product, COUNT(*) n FROM product_score"
                           " WHERE status='scored' GROUP BY product"):
            lists[r["product"]] = r["n"]
    except Exception:
        c.rollback()
    out["lists"] = lists
    out["signals"] = one("""SELECT COUNT(*) n FROM trigger_event t
        JOIN firm_scope s ON s.crd=t.crd
        LEFT JOIN trigger_action a ON a.trigger_id=t.id
        WHERE t.suppressed=0 AND a.state IS NULL AND t.detected_date >= ?""",
                         (signal_cutoff(),))
    out["review"] = (one("SELECT COUNT(*) n FROM adv_13f_match WHERE status='review'")
                     + one("SELECT COUNT(*) n FROM brochure_negation WHERE status='open'"))
    feed = None
    try:
        feed = c.execute("SELECT published_at FROM snapshot WHERE source_key='adv_feed'"
                         " ORDER BY id DESC LIMIT 1").fetchone()
    except Exception:
        c.rollback()
    c.close()
    out["feed"] = feed["published_at"] if feed else None
    _NAV_CACHE.clear()
    _NAV_CACHE.update(out)
    return out


def _pages_for(acct: dict | None) -> list[dict]:
    pages = [{"name": "Home", "href": "/"}, {"name": "Bellwether AI", "href": "/ask"},
             {"name": "Signals", "href": "/signals"}, {"name": "Firms", "href": "/firms"},
             {"name": "People", "href": "/people"}, {"name": "Saved lists", "href": "/saved"}]
    for k in products.product_keys():
        pages.append({"name": products.product(k)["name"] + " list", "href": f"/lists/{k}"})
        pages.append({"name": products.product(k)["name"] + " scoring",
                      "href": f"/lists/{k}?view=scoring"})
    if users.can_manage_enrichment(acct):
        pages.append({"name": "Enrichment", "href": "/enrichment"})
    if users.is_admin(acct):
        for name, href in (("Settings", "/settings"), ("Jobs", "/settings/jobs"),
                           ("Users and roles", "/settings/users"),
                           ("AI provider", "/settings/ai"),
                           ("Microsoft sign-in", "/settings/signin"),
                           ("System health", "/settings/system"),
                           ("Review queue", "/settings/review")):
            pages.append({"name": name, "href": href})
    return pages


def nav(active: str) -> str:
    n = _nav_counts()
    acct = current_account()

    def item(key, href, label, cnt=None, hot=False, icon=None, dot=None):
        ic = ICONS.get(icon or key, "")
        if dot:
            ic = f'<span class="dot" style="background:{dot}"></span>'
        c = (f'<span class="cnt{" hot" if hot else ""}">{cnt:,}</span>' if cnt else "")
        return (f'<a class="i{" on" if key == active else ""}" href="{href}">'
                f'{ic}{esc(label)}{c}</a>')

    # Product lists sit right under Home and stay open on every page: they are
    # where the work starts, so they must never be one click away or below the fold.
    plist = "".join(
        item(f"list:{k}", f"/lists/{k}", products.product(k)["name"], n["lists"].get(k),
             dot=FAMILY_COLOUR.get(products.product(k)["family"], "#888"))
        for k in products.product_keys())
    data = ""
    if users.can_manage_enrichment(acct):
        data += item("enrichment", "/enrichment", "Enrichment", icon="enrich")
    if users.is_admin(acct):
        data += item("settings", "/settings", "Settings", n.get("review"))
    if data:
        data = '<div class="grp">Data</div>' + data
    who = ""
    if acct:
        role = users.ROLE_LABEL.get(acct.get("role"), "")
        if acct.get("role") == "owner" and acct.get("families"):
            role = "Owner, " + ", ".join(acct["families"])
        who = (f'<div class="me"><div class="av">{esc(initials(acct.get("name") or ""))}</div>'
               f'<div class="who"><b>{esc(acct.get("name") or "")}</b><span>{esc(role)}</span></div>'
               f'<form method="post" action="/logout"><button type="submit" title="Sign out" aria-label="Sign out">'
               f'{ICONS["out"]}</button></form></div>')
    quit_link = ("" if MANAGED or not users.is_admin(acct)
                 else f' &middot; <a href="/quit">Quit</a>')
    feed = esc(n["feed"]) if n.get("feed") else "none yet"
    pages = json.dumps(_pages_for(acct))
    return (f'<script>window.BW_PAGES={pages};</script>' + PALETTE_HTML +
            '<header class="mobile-nav"><a href="/"><img src="/static/mark.svg" alt="">Bellwether</a>'
            '<button type="button" data-nav-toggle aria-controls="main-nav" aria-expanded="false">Menu</button></header>'
            '<nav class="side" id="main-nav" aria-label="Main navigation">'
            f'<a class="brand" href="/"><img src="/static/mark.svg" width="30" height="30" alt="">'
            f'<div class="t">{APP_NAME}<small>Acumen Strategy</small></div></a>'
            f'<button class="find" type="button" onclick="palShow()">{ICONS["search"]}'
            'Search<kbd>Ctrl K</kbd></button>'
            + item("home", "/", "Home")
            + item("ask", "/ask", "Bellwether AI", icon="spark")
            + '<div class="grp">Product lists</div><div class="lists">' + plist + '</div>'
            + '<div class="grp">Explore</div>'
            + item("firms", "/firms", "Firms")
            + item("people", "/people", "People")
            + item("signals", "/signals", "Signals", n.get("signals"), hot=True)
            + item("saved", "/saved", "Saved lists")
            + data
            + f'<div class="foot">{who}<div class="fresh"><i></i>SEC data of {feed}{quit_link}</div></div>'
            + '</nav>')


def page(title: str, active: str, body: str, css: str = "", js: str = "",
         status: int = 200, orbs: bool = False) -> HTMLResponse:
    """Every screen goes through here: one head, one sidebar, one stylesheet."""
    orb = (f'<script type="module" src="{asset("orb.js")}"></script>' if orbs else "")
    return HTMLResponse(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{esc(title)} &middot; {APP_NAME}</title>{FAVICON}'
        f'<link rel="preload" href="/static/fonts/InstrumentSans.ttf" as="font" type="font/ttf" crossorigin>'
        f'<link rel="stylesheet" href="{asset("app.css")}">'
        f'{("<style>" + css + "</style>") if css else ""}{SPECULATION}</head>'
        f'<body>{nav(active)}<main id="main-content">{body}</main>'
        f'<script src="{asset("app.js")}" defer></script>'
        f'<script src="{asset("charts.js")}" defer></script>{orb}'
        f'{("<script>" + js + "</script>") if js else ""}'
        f'</body></html>', status_code=status)


def forbidden(what: str = "this page") -> HTMLResponse:
    return page("Not available", "", f"""<div class="pg narrow">
<h1>Not available to you</h1><p class="lede">{esc(what.capitalize())} is for admins. If you
need it, ask an admin to change your role in Settings, Users.</p>
<p><a class="btn" href="/">Back to Home</a></p></div>""", status=403)


# --- who is signed in -----------------------------------------------------
# Deny by default: the middleware requires a session for every path that is not
# explicitly public, so a route added later is protected without anyone having
# to remember to protect it. Admin areas are guarded here too, by prefix.

PUBLIC_PREFIXES = ("/static/",)
PUBLIC_PATHS = auth.PUBLIC_PATHS | {"/auth/microsoft", "/auth/microsoft/callback"}
ADMIN_PREFIXES = ("/settings", "/admin/", "/health", "/review", "/quit")
ENRICH_PREFIXES = ("/enrichment",)


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)
    login = auth.read_session(request.cookies.get(auth.COOKIE))
    acct = users.get(login) if login else None
    if not acct or not acct.get("active"):
        if path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": "Signed out. Reload the page."},
                                status_code=401)
        if request.method != "GET":
            return RedirectResponse("/login", status_code=303)
        nxt = request.url.path
        if request.url.query:
            nxt += "?" + request.url.query
        return RedirectResponse(
            "/login?next=" + urllib.parse.quote(nxt, safe=""), status_code=303)
    request.state.user = acct["login"]
    t1 = CURRENT_USER.set(acct["login"])
    t2 = CURRENT_ACCOUNT.set(acct)
    try:
        if path.startswith(ADMIN_PREFIXES) and not users.is_admin(acct):
            if path.startswith("/api/") or request.method != "GET":
                return JSONResponse({"ok": False, "error": "Admins only."}, status_code=403)
            return forbidden("settings")
        if path.startswith(ENRICH_PREFIXES) and not users.can_manage_enrichment(acct):
            if request.method != "GET":
                return JSONResponse({"ok": False, "error": "Admins and product owners only."},
                                    status_code=403)
            return forbidden("enrichment")
        return await call_next(request)
    finally:
        CURRENT_ACCOUNT.reset(t2)
        CURRENT_USER.reset(t1)


# --- sign-in throttling ----------------------------------------------------
# Failed sign-ins are counted per address and per username. Passing the limit
# on either locks sign-in for that key for the window, which stops password
# guessing without a table or a dependency. Kept in memory per worker: a
# restart forgets it, which only ever errs towards letting a person back in.
LOGIN_WINDOW_S = 15 * 60
LOGIN_MAX_FAILS = 8
_FAILS: dict[str, list[float]] = {}
_FAILS_LOCK = threading.Lock()


def client_ip(request: Request) -> str:
    """The caller's address. Behind the gateway the socket peer is the proxy,
    so the first X-Forwarded-For hop is the real client."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _keys(ip: str, who: str) -> list[str]:
    return [f"ip:{ip}"] + ([f"user:{who}"] if who else [])


def _throttled(ip: str, who: str) -> int:
    """Minutes until sign-in is allowed again, or 0 if it is allowed now."""
    import time as _t
    now = _t.time()
    worst = 0.0
    with _FAILS_LOCK:
        for k in _keys(ip, who):
            recent = [t for t in _FAILS.get(k, []) if now - t < LOGIN_WINDOW_S]
            _FAILS[k] = recent
            if len(recent) >= LOGIN_MAX_FAILS:
                worst = max(worst, LOGIN_WINDOW_S - (now - recent[0]))
    return int(worst // 60) + 1 if worst else 0


def _record_failure(ip: str, who: str) -> None:
    import time as _t
    with _FAILS_LOCK:
        if len(_FAILS) > 10000:          # never grow without bound
            _FAILS.clear()
        for k in _keys(ip, who):
            _FAILS.setdefault(k, []).append(_t.time())


def _clear_failures(ip: str, who: str) -> None:
    with _FAILS_LOCK:
        for k in _keys(ip, who):
            _FAILS.pop(k, None)


# --- response hardening -----------------------------------------------------
# Set by the app itself, so they hold behind any gateway: the Caddyfile adds
# them, but the live deployment sits behind APISIX and none reached the
# browser. The CSP allows the inline style and script every page carries and
# nothing from any other origin.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self' https://login.microsoftonline.com; "
       "frame-ancestors 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    # An internal tool: nothing here is for search engines, and a crawler
    # judging a bare sign-in form out of context is how a private login page
    # ends up flagged as deceptive.
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}


@app.middleware("http")
async def harden(request: Request, call_next):
    """Upgrade plain HTTP, then stamp every response."""
    if SECURE_COOKIES and request.headers.get("x-forwarded-proto", "").lower() == "http":
        return RedirectResponse(str(request.url.replace(scheme="https")),
                                status_code=308)
    resp = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    if SECURE_COOKIES:
        resp.headers.setdefault("Strict-Transport-Security",
                                "max-age=31536000; includeSubDomains")
    path = request.url.path
    if path.startswith("/static/"):
        # Versioned URLs (see asset()), so a year is safe: a changed file gets
        # a new URL and the old one is simply never asked for again.
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path not in ("/healthz", "/favicon.ico", "/robots.txt"):
        # Pages carry client names, contacts and notes: never keep them in a
        # shared or browser cache.
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# Outermost: compress what everything inside produced. Pages are mostly
# repeated markup and shrink four to six times.
app.add_middleware(GZipMiddleware, minimum_size=800)


@app.get("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")


@app.get("/healthz")
def healthz():
    """Unauthenticated liveness only, for the launcher and any load balancer.
    Deliberately carries no data."""
    return {"ok": True, "app": APP_NAME}


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


# --- sign in ----------------------------------------------------------------

MS_LOGO = ('<svg viewBox="0 0 21 21"><rect x="1" y="1" width="9" height="9" fill="#f25022"/>'
           '<rect x="11" y="1" width="9" height="9" fill="#7fba00"/>'
           '<rect x="1" y="11" width="9" height="9" fill="#00a4ef"/>'
           '<rect x="11" y="11" width="9" height="9" fill="#ffb900"/></svg>')


def password_login_allowed() -> bool:
    """Passwords stay available until Microsoft sign-in is set up, whatever the
    setting says: switching them off first would lock everyone out."""
    from . import msauth
    return settings.get_bool("auth.password_login") or not msauth.configured()


def login_page(error: str = "", nxt: str = "/", status: int | None = None,
               show_password: bool = False) -> HTMLResponse:
    from . import msauth
    ms = msauth.configured()
    pw = password_login_allowed()
    err = f'<div class="err">{esc(error)}</div>' if error else ""
    if users.count() == 0 and not ms:
        err = ('<div class="err">No accounts exist yet. On the server, run: '
               'python -m scripts.manage_users add &lt;username&gt; --name "Full Name" '
               '--role admin</div>')
    ms_btn = ""
    if ms:
        ms_btn = (f'<a class="btn ms" href="/auth/microsoft?next={esc(urllib.parse.quote(nxt))}"'
                  f' data-noprefetch>{MS_LOGO}Sign in with Microsoft</a>')
    pw_form = ""
    if pw:
        form = (f'<form class="pw" method="post" action="/login">'
                f'<input type="hidden" name="next" value="{esc(nxt)}">'
                f'<label>Username<input type="text" name="username" autocomplete="username"'
                f'{" autofocus" if not ms else ""} required></label>'
                f'<label>Password<input name="password" type="password"'
                f' autocomplete="current-password" required></label>'
                f'<button class="{"primary" if not ms else ""}" type="submit">Sign in</button>'
                f'</form>')
        if ms:
            pw_form = (f'<details class="alt"{" open" if show_password else ""}>'
                       f'<summary>Sign in with a password</summary>{form}</details>')
        else:
            pw_form = form
    stats = dict((k, v) for k, v in _signin_stats())
    caption = ""
    if stats.get("advisory firms"):
        caption = (f'<p class="signin-caption">Watching <b>{stats["advisory firms"]}</b> advisory firms'
                   + (f' and <b>{stats["people"]}</b> people' if stats.get("people") else "")
                   + ' across the United States</p>')
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in to {APP_NAME}</title>{FAVICON}
<link rel="stylesheet" href="{asset('app.css')}"></head>
<body class="signin-page">
<canvas id="signin-map" aria-hidden="true"></canvas>
<div class="signin-wrap">
<main class="signin">
<div class="signin-brand">
<a class="wm" href="/login"><img src="/static/mark.svg" width="40" height="40" alt="">{APP_NAME}</a>
<p>GTM intelligence</p>
<svg class="signin-emblem" viewBox="0 0 160 140" fill="none" aria-hidden="true">
<path d="M8 66 80 10l72 56M8 98l72-56 72 56M8 130l72-56 72 56" stroke="currentColor" stroke-width="1.2"/>
<path d="m34 66 46-36 46 36" stroke="var(--red-hi)" stroke-width="2"/>
</svg>
</div>
<div class="signin-form">
<h1>Sign in</h1>
{err}{ms_btn}{pw_form}
</div>
</main>
{caption}
<div class="signin-owner">Acumen Strategy</div>
</div>
<script src="{asset('signin.js')}" defer></script>
</body></html>""",
                        status_code=status or (200 if not error else 401))


_SIGNIN_CACHE: dict = {"t": 0.0, "v": []}


def _signin_stats() -> list[tuple[str, str]]:
    """The two headline numbers under the sign-in card, refreshed every ten
    minutes; an estimate for people keeps the public page cheap."""
    import time as _time
    if _time.monotonic() - _SIGNIN_CACHE["t"] < 600 and _SIGNIN_CACHE["v"]:
        return _SIGNIN_CACHE["v"]
    out: list[tuple[str, str]] = []
    try:
        c = conn()
        try:
            firms = c.execute("SELECT COUNT(*) n FROM firm_current").fetchone()["n"]
            people = c.execute("SELECT GREATEST(reltuples, 0)::bigint n FROM pg_class"
                               " WHERE relname='person'").fetchone()
            if firms:
                out = [("advisory firms", f"{firms:,}")]
            if people and people["n"]:
                out.append(("people", f"{int(people['n']):,}"))
        finally:
            c.close()
    except Exception:
        out = []
    _SIGNIN_CACHE.update(t=_time.monotonic(), v=out)
    return out


@app.get("/login", response_class=HTMLResponse)
def login_form(next: str = Query("/"), pw: str = Query("")):
    return login_page(nxt=safe_back(next), show_password=bool(pw))


def _session_response(login: str, nxt: str) -> HTMLResponse:
    """Set the session and move on with a page rather than a redirect.

    The session cookie is SameSite=Strict. After a sign-in that began on
    login.microsoftonline.com, browsers treat a redirect chain as cross-site
    and leave a Strict cookie off the next request, which bounces straight back
    to the sign-in page. A page that navigates by itself starts a fresh,
    same-site navigation, so the cookie is sent."""
    target = safe_back(nxt)
    resp = HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<meta http-equiv="refresh" content="0;url={esc(target)}">
<link rel="stylesheet" href="{asset('app.css')}">
<body class="signin-page"><p class="muted">Signing you in</p></body>""")
    resp.set_cookie(auth.COOKIE, auth.make_session(login),
                    max_age=auth.SESSION_DAYS * 86400, httponly=True,
                    samesite="strict", secure=SECURE_COOKIES, path="/")
    return resp


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(""),
                 password: str = Form(""), next: str = Form("/")):
    if not password_login_allowed():
        return login_page("Password sign-in is switched off. Use Microsoft.",
                          nxt=safe_back(next), status=403)
    who = (username or "").strip().lower()
    ip = client_ip(request)
    wait = _throttled(ip, who)
    if wait:
        unit = "minute" if wait == 1 else "minutes"
        return login_page(f"Too many failed sign-ins. Try again in {wait} {unit}.",
                          nxt=safe_back(next), status=429, show_password=True)
    acct = users.check_password(username, password)
    if not acct:
        _record_failure(ip, who)
        return login_page("That username and password combination is not recognised.",
                          nxt=safe_back(next), show_password=True)
    _clear_failures(ip, who)
    resp = RedirectResponse(safe_back(next), status_code=303)
    resp.set_cookie(auth.COOKIE, auth.make_session(acct["login"]),
                    max_age=auth.SESSION_DAYS * 86400, httponly=True,
                    samesite="strict", secure=SECURE_COOKIES, path="/")
    return resp


def public_base(request: Request) -> str:
    """The address people reach Bellwether at, for the Microsoft redirect URI."""
    fixed = settings.get("app.public_url").strip().rstrip("/")
    if fixed:
        return fixed
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    if SECURE_COOKIES:
        proto = "https"
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host")
            or request.url.netloc).split(",")[0].strip()
    return f"{proto}://{host}"


@app.get("/auth/microsoft")
def ms_start(request: Request, next: str = Query("/")):
    from . import msauth
    if not msauth.configured():
        return login_page("Microsoft sign-in is not set up yet. An admin can set it up in "
                          "Settings, Sign-in.", status=503, show_password=True)
    try:
        url, cookie = msauth.start(public_base(request), safe_back(next))
    except msauth.SignInError as e:
        return login_page(str(e), status=503, show_password=True)
    except Exception:
        return login_page("Microsoft sign-in could not start. Check the settings in "
                          "Settings, Sign-in.", status=503, show_password=True)
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(msauth.FLOW_COOKIE, cookie, max_age=msauth.FLOW_TTL_S, httponly=True,
                    samesite="lax", secure=SECURE_COOKIES, path="/auth/microsoft")
    return resp


@app.get("/auth/microsoft/callback", response_class=HTMLResponse)
def ms_callback(request: Request):
    from . import msauth
    params = dict(request.query_params)
    try:
        claims, nxt = msauth.finish(request.cookies.get(msauth.FLOW_COOKIE), params)
    except msauth.SignInError as e:
        return login_page(str(e), status=401)
    except Exception:
        return login_page("Microsoft sign-in failed. Please try again.", status=401)
    email = msauth.email_of(claims)
    acct = users.from_microsoft(email, claims.get("name") or email, claims.get("oid"))
    if not acct.get("active"):
        return login_page("Your Bellwether account is switched off. Ask an admin.", status=403)
    resp = _session_response(acct["login"], nxt)
    resp.delete_cookie(msauth.FLOW_COOKIE, path="/auth/microsoft")
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


# --- small shared actions --------------------------------------------------

@app.post("/watch/{crd}")
def watch_toggle(crd: str, back: str = Form("/")):
    back = safe_back(back)
    c = conn()
    if c.execute("SELECT 1 FROM firm_watch WHERE crd=?", (crd,)).fetchone():
        c.execute("DELETE FROM firm_watch WHERE crd=?", (crd,))
    else:
        c.execute("INSERT INTO firm_watch VALUES (?,?,?)",
                  (crd, current_owner() or "bd",
                   datetime.now(timezone.utc).isoformat(timespec="seconds")))
    c.commit()
    c.close()
    return RedirectResponse(back, status_code=303)


@app.post("/views/save")
def view_save(page: str = Form(...), qs: str = Form(""), name: str = Form(...)):
    ok_page = page in ("signals", "firms", "people") or (
        page.startswith("list:") and page[5:] in products.product_keys())
    if not ok_page or not name.strip():
        return RedirectResponse("/", status_code=303)
    c = conn()
    c.execute("INSERT INTO saved_view (name,page,qs,created_at) VALUES (?,?,?,?)",
              (name.strip()[:60], page, qs,
               datetime.now(timezone.utc).isoformat(timespec="seconds")))
    c.commit()
    c.close()
    return RedirectResponse("/saved", status_code=303)


@app.post("/views/delete")
def view_delete(vid: int = Form(...)):
    c = conn()
    c.execute("DELETE FROM saved_view WHERE id=?", (vid,))
    c.commit()
    c.close()
    return RedirectResponse("/saved", status_code=303)


def saved_view_href(v) -> str:
    p = v["page"]
    if p.startswith("list:"):
        return f"/lists/{p[5:]}?{v['qs']}"
    if p in ("inbox", "signals"):
        return f"/signals?{v['qs']}"
    if p == "people":
        return f"/people?{v['qs']}"
    return f"/firms?{v['qs']}"


# --- quitting -------------------------------------------------------------
# There is no stop script. On a desktop the tool starts itself at logon and is
# stopped from inside itself, so the only way to shut it down is a deliberate
# click; on a server the orchestrator owns its lifetime and Quit is hidden.

QUIT_CSS = """
.quitbox{max-width:520px;margin:0 auto;padding:90px 30px 40px;text-align:center}
.quitbox .mk{width:46px;height:46px;border-radius:13px;background:var(--red);
color:#fff;display:flex;align-items:center;justify-content:center;margin:0 auto 22px;
font:700 24px Georgia,serif}
.quitbox h1{font-size:29px;margin:0 0 12px}
.quitbox p{color:var(--soft);font-size:15px;line-height:1.7;margin:0 0 10px}
.quitbox .acts{display:flex;gap:10px;justify-content:center;margin-top:26px}
.quitbox .hint{color:var(--faint);font-size:12.5px;margin-top:30px;line-height:1.7}
"""


@app.get("/quit", response_class=HTMLResponse)
def quit_confirm():
    """Confirmation, because a misclick should not take the server down."""
    if MANAGED:
        return page("Managed by the server", "quit", f"""<div class="quitbox">
<div class="mk">B</div><h1>Nothing to quit here</h1>
<p>This {APP_NAME} runs on a server that restarts it automatically, so quitting
from inside would only bounce it.</p>
<div class="acts"><a class="btn" href="/">Back</a></div></div>""", QUIT_CSS)
    return page(f"Quit {APP_NAME}", "quit", f"""<div class="quitbox">
<div class="mk">B</div>
<h1>Quit {APP_NAME}?</h1>
<p>Everything is already saved. Background jobs stop too and pick up where they
left off next time.</p>
<div class="acts">
<form method="post" action="/admin/quit">
<button type="submit" class="primary">Quit {APP_NAME}</button></form>
<a class="btn ghost" href="/">Cancel</a>
</div>
<p class="hint">It starts again by itself the next time you sign in to Windows,
or immediately from the {APP_NAME} shortcut.</p>
</div>""", QUIT_CSS)


@app.post("/admin/quit", response_class=HTMLResponse)
def quit_now():
    """Answer first, then die: the response has to reach the browser before
    the process serving it goes away, so the kill runs on a short timer."""
    if MANAGED:
        return RedirectResponse("/quit", status_code=303)
    threading.Timer(0.8, stop_everything).start()
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>{APP_NAME} has stopped</title>{FAVICON}
<link rel="stylesheet" href="{asset('app.css')}"><style>{QUIT_CSS}body{{margin:0}}</style>
<div class="quitbox">
<div class="mk">B</div>
<h1>{APP_NAME} has stopped</h1>
<p>You can close this tab. Everything is saved.</p>
<p class="hint">It will be running again the next time you sign in to Windows.
To start it right now, open the {APP_NAME} shortcut on your desktop.</p>
</div>""")


from . import (api_view, ask_view, enrich_view, firm_view, firms_view,  # noqa: E402
               home_view, lists_view, people_view, settings_view, signals_view)

app.include_router(api_view.router)       # /api/*
app.include_router(home_view.router)      # /
app.include_router(ask_view.router)       # /ask
app.include_router(lists_view.router)     # /lists/{product}
app.include_router(signals_view.router)   # /signals
app.include_router(firms_view.router)     # /firms, /saved, exports
app.include_router(people_view.router)    # /people
app.include_router(firm_view.router)      # /firm/{crd}
app.include_router(enrich_view.router)    # /enrichment
app.include_router(settings_view.router)  # /settings (admins)


# Old URLs, redirected to their new home so bookmarks keep working.
@app.get("/health")
def _moved_health():
    return RedirectResponse("/settings/system", status_code=307)


@app.get("/review")
def _moved_review():
    return RedirectResponse("/settings/review", status_code=307)


@app.get("/guide")
def _moved_guide():
    return RedirectResponse("/", status_code=307)


@app.get("/lists")
def _moved_lists():
    return RedirectResponse("/saved", status_code=307)


@app.get("/lists/working")
@app.get("/outreach")
def _moved_outreach():
    return RedirectResponse("/firms?view=contacts", status_code=307)


@app.get("/outreach.xlsx")
def _moved_outreach_xlsx():
    return RedirectResponse("/firms/export.xlsx", status_code=307)


@app.get("/firms.csv")
def _moved_firms_csv(request: Request):
    q = ("?" + request.url.query) if request.url.query else ""
    return RedirectResponse("/firms/export.csv" + q, status_code=307)
