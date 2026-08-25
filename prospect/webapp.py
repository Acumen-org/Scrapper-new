"""Bellwether web app.

A bellwether is a leading indicator, which is what every row in this system is:
a filing change that says a firm is worth calling before anyone else notices.

Architecture rules learned the hard way:

  - GET handlers never write. All working tables are created once at startup,
    because a CREATE TABLE inside a request is a write transaction, and while
    the brochure ingester holds the SQLite write lock every page load queued
    behind it. That was the lag.
  - One visual system: Claude-style dark, a single dark red accent, green for
    positive, amber for pending. Nothing else carries color.
  - Numbers never render without their caveats. UI copy carries no em dashes.

    python -m uvicorn prospect.webapp:app --port 8787
"""

from __future__ import annotations

import contextvars
import html
import os
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from datetime import datetime, timezone

import yaml
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import auth, config, db, procs

APP_NAME = "Bellwether"

# Session cookies are marked Secure when served over HTTPS, which is every
# deployment except running it on your own machine over plain http.
SECURE_COOKIES = os.environ.get("BELLWETHER_HTTPS", "").lower() in ("1", "true", "yes")

# Managed mode: a supervisor (Docker with restart:unless-stopped) owns the
# process lifecycle. Quit disappears from the UI, because killing the process
# would just make Docker restart it, and pidfiles from a previous container are
# always stale: PID namespaces start over at 1, so a recorded pid usually names
# some OTHER live process in the new container. Trusting one silently disables
# the scheduler and autopilot forever, which is the worst kind of failure.
MANAGED = os.environ.get("BELLWETHER_MANAGED", "").lower() in ("1", "true", "yes")

# The signed-in user, request scoped. A context variable rather than an argument
# threaded through every view: nav() is called from fifteen places and none of
# them care who is signed in beyond rendering the footer. Context variables
# propagate into the threadpool FastAPI runs sync endpoints on, so this is safe
# for both async and sync handlers.
CURRENT_USER: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_user", default=None)


def current_user() -> str | None:
    return CURRENT_USER.get()


def current_owner() -> str:
    """Display name of whoever is signed in, for stamping ownership fields."""
    u = CURRENT_USER.get()
    return auth.display_name(u) if u else ""

app = FastAPI(title=APP_NAME)

_cfg = config.load()
_scoring = yaml.safe_load((config.CONFIG_DIR / "scoring.yml").read_text(encoding="utf-8"))
PRODUCTS = _scoring["trigger_products"]
CAVEATS = _scoring["caveats"]

TYPE_LABEL = {
    "new_registration": "New RIA registration",
    "reregistration_or_gap": "Re-registration or gap",
    "aum_jump": "AUM jump",
    "aum_drop": "AUM drop",
    "iar_growth": "IAR headcount growth",
    "custodian_change_to_platform": "Moved to Schwab",
    "custodian_change_from_platform": "Left Schwab",
    "custodian_change_other": "Custodian change",
    "first_private_fund": "First private fund",
    "first_real_estate_fund": "First real estate fund",
}

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
    page      TEXT NOT NULL,          -- inbox | firms
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
CREATE TABLE IF NOT EXISTS auto_task (
    kind          TEXT PRIMARY KEY,   -- brochures | firm_refresh | contact_extract
                                      -- | email_verify | cusip_verify
    desired_state TEXT NOT NULL DEFAULT 'paused',  -- running | paused
    progress      INTEGER DEFAULT 0,
    total         INTEGER DEFAULT 0,
    message       TEXT,
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS scheduler_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    last_check   TEXT,                -- heartbeat: scheduler looked at due-ness
    last_started TEXT,                -- last automatic weekly launch
    message      TEXT
);
-- User-built firm buckets ("playlists"): a named set of firms you assemble by
-- hand, separate from the scored presets. This is what the Lists section shows.
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
"""


@app.on_event("startup")
def _init_once() -> None:
    """Every table and view the app touches exists before the first request,
    so request handlers never issue DDL and GETs stay pure readers."""
    c = db.connect()
    db.init(c)
    db.init_firm(c)
    c.executescript(APP_TABLES)
    # Columns added to app tables after they first shipped.
    if "title" not in {r[1] for r in c.execute("PRAGMA table_info(contact_email)")}:
        c.execute("ALTER TABLE contact_email ADD COLUMN title TEXT")
    c.commit()

    # In a container, pidfiles surviving on the mounted volume are lies: the
    # new PID namespace reuses low numbers, so a stale scheduler.pid routinely
    # names a live but unrelated process, procs.is_alive says True, nobody
    # claims the scheduler, and the weekly pull silently never runs again.
    #
    # Purged exactly once per container boot: the marker lives in /tmp, which
    # is container-local and empty on every start, and O_EXCL makes the first
    # worker the only purger. Without this, the second worker's purge would
    # delete the first worker's fresh scheduler claim and both would schedule.
    if MANAGED:
        try:
            os.close(os.open("/tmp/bellwether_boot_purge",
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            for name in ("server.pid", "scheduler.pid", "autopilot.pid"):
                (config.DATA_DIR / name).unlink(missing_ok=True)
        except FileExistsError:
            pass

    # Record the supervisor PID from inside the app rather than trusting the
    # launcher to have done it. Quit needs a PID it can kill, and a stale file
    # from a previous run would make Quit silently do nothing. Both workers
    # write the same parent, so this is idempotent.
    try:
        (config.DATA_DIR / "server.pid").write_text(str(os.getppid()))
    except OSError:
        pass

    # Autopilot is a separate process and does not survive a restart. If a job
    # was left switched on, switch it back on rather than showing a running
    # job with nothing behind it.
    try:
        n = c.execute("SELECT COUNT(*) n FROM auto_task"
                      " WHERE desired_state='running'").fetchone()["n"]
    except sqlite3.Error:
        n = 0
    c.close()
    if n:
        ensure_autopilot()

    # The weekly pull runs itself. Bellwether is up from logon to shutdown, so
    # a scheduler thread here is the reliable place: it also catches a missed
    # week the moment the PC comes back on, which Task Scheduler's fixed slot
    # would not. One worker wins the claim; the other simply does not schedule.
    if procs.claim_pidfile(config.DATA_DIR / "scheduler.pid"):
        t = threading.Thread(target=_scheduler_loop, daemon=True,
                             name="weekly-scheduler")
        t.start()


FEED_DUE_DAYS = 6.5   # the SEC publishes weekly; pull as soon as a new file is due
CHECK_EVERY_S = 1800  # look every 30 minutes; cheap, one indexed query


def _weekly_due(c) -> tuple[bool, str]:
    """Whether an automatic pull should start now, and why or why not."""
    last = c.execute("SELECT MAX(captured_at) t FROM snapshot"
                     " WHERE source_key='adv_feed'").fetchone()["t"]
    if not last:
        return True, "no feed capture held at all"
    age_days = (datetime.now(timezone.utc)
                - datetime.fromisoformat(last)).total_seconds() / 86400
    if age_days < FEED_DUE_DAYS:
        return False, f"feed captured {age_days:.1f} days ago; due at {FEED_DUE_DAYS}"
    busy = c.execute("SELECT COUNT(*) n FROM run_log WHERE status='running'"
                     " AND started_at > datetime('now','-2 hours')").fetchone()["n"]
    if busy:
        return False, "a run is already in flight"
    return True, f"feed is {age_days:.1f} days old"


def _scheduler_loop() -> None:
    """Check due-ness on a slow clock and launch the weekly cycle when a new
    feed file should exist. Failures land in run_log like any manual run, so
    a broken pull is loud on Pipeline health, never silent."""
    import time as _time
    _time.sleep(20)  # let startup settle before the first check
    while True:
        try:
            c = conn()
            due, why = _weekly_due(c)
            now_s = datetime.now(timezone.utc).isoformat(timespec="seconds")
            c.execute("INSERT INTO scheduler_state (id, last_check, message)"
                      " VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET"
                      " last_check=excluded.last_check, message=excluded.message",
                      (now_s, why))
            if due:
                c.execute("UPDATE scheduler_state SET last_started=? WHERE id=1",
                          (now_s,))
                log = open(config.DATA_DIR / "weekly.log", "ab")
                subprocess.Popen(
                    [sys.executable, "-m", "scripts.run_weekly",
                     "--brochure-slice", "120"],
                    cwd=str(config.ROOT), stdout=log, stderr=log,
                    creationflags=procs.SPAWN_FLAGS)
            c.commit()
            c.close()
        except Exception as exc:
            # The loop must survive, but the failure must not be invisible:
            # put it where Pipeline health reads.
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
    """Launch the autopilot worker unless one is already alive.

    Liveness goes through procs.is_alive, never os.kill(pid, 0), which on
    Windows terminates the process being checked. Both web workers run this at
    startup, and the worker claims its pidfile atomically, so a tie is resolved
    by one of them exiting immediately rather than by two doing the same work."""
    pidfile = config.DATA_DIR / "autopilot.pid"
    if procs.alive_pid(pidfile) is not None:
        return True
    log = open(config.DATA_DIR / "autopilot.log", "ab")
    subprocess.Popen([sys.executable, "-m", "scripts.autopilot"],
                     cwd=str(config.ROOT), stdout=log, stderr=log,
                     creationflags=procs.SPAWN_FLAGS)
    return True


def _kill_tree(pid: int) -> None:
    """Kill a process and its descendants, but only if that pid is still the
    process we think it is. Windows recycles pids, and killing a recycled one
    would take down something entirely unrelated. Platform handling lives in
    prospect.procs so this works the same on a Linux host."""
    procs.kill_tree(pid)


def stop_everything() -> None:
    """Stop the whole tool: background jobs first, then the server itself.

    Killing the supervisor tree is the only reliable stop on Windows. Workers
    inherit the listening socket, so the port outlives any single process, and a
    worker that exits on its own is simply respawned by the supervisor.

    The target is our own parent, which is the supervisor by construction and
    therefore cannot be stale. The recorded pidfile is only a cross-check."""
    apf = config.DATA_DIR / "autopilot.pid"
    ap = procs.alive_pid(apf)
    if ap is not None:
        _kill_tree(ap)
    apf.unlink(missing_ok=True)

    srv = config.DATA_DIR / "server.pid"
    recorded = procs.alive_pid(srv)
    srv.unlink(missing_ok=True)
    parent = os.getppid()
    target = parent if parent > 4 else recorded
    if target is None:
        return
    if recorded is not None and recorded != target:
        # Disagreement means something started the server outside the launcher.
        # Stop both trees rather than leaving half of one serving the port.
        _kill_tree(recorded)
    _kill_tree(target)


def conn() -> sqlite3.Connection:
    """Per-request connection. No DDL here, ever: reads must never become
    writes. Generous busy timeout so a writer's brief commit never bounces a
    click while background ingest is running."""
    c = sqlite3.connect(config.DB_PATH, timeout=20.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=20000")
    return c


def esc(v) -> str:
    return html.escape(str(v)) if v is not None else ""


def money(v) -> str:
    if v is None:
        return "-"
    v = float(v)
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.0f}M"
    return f"${v:,.0f}"


# ---------------------------------------------------------------------------
# One visual system. Dark neutrals plus exactly three signal colors:
# red (accent, danger, disqualify), green (positive), amber (pending, caution).
PAGE_CSS = """
:root{
 --bg:#262624; --side:#1e1d1a; --card:#2e2d2a; --card2:#363430;
 --ink:#edebe4; --soft:#b8b3a6; --faint:#8d887a;
 --rule:#3a3833; --rule2:#4b4841;
 --red:#a63232; --red-hi:#c65454; --red-bg:rgba(166,50,50,.15);
 --ok:#63aa7c; --ok-bg:rgba(99,170,124,.12);
 --amber:#cfa95c; --amber-bg:rgba(207,169,92,.11);
 --shadow:none;
}
*{box-sizing:border-box}
html{color-scheme:dark}
body{margin:0 0 0 224px;background:var(--bg);color:var(--ink);
font:14.5px/1.6 "Segoe UI",system-ui,sans-serif;-webkit-font-smoothing:antialiased;
text-rendering:optimizeLegibility}
::selection{background:var(--red-bg)}
nav.side{position:fixed;top:0;left:0;bottom:0;width:224px;background:var(--side);
border-right:1px solid var(--rule);padding:20px 12px 14px;display:flex;
flex-direction:column;gap:2px;z-index:10}
nav.side .brand{display:flex;align-items:center;gap:9px;padding:0 10px 18px}
nav.side .brand .mark{width:26px;height:26px;border-radius:7px;background:var(--red);
color:#fff;display:flex;align-items:center;justify-content:center;
font:700 15px Georgia,serif;flex:none}
nav.side .brand .t{font:650 16.5px Georgia,"Times New Roman",serif;
letter-spacing:-.02em;color:var(--ink);line-height:1.15}
nav.side .brand .t small{display:block;font:400 10px "Segoe UI",sans-serif;
color:var(--faint);letter-spacing:.14em;text-transform:uppercase}
nav.side a{display:flex;align-items:center;gap:10px;
padding:8px 11px;border-radius:8px;color:var(--soft);text-decoration:none;
font-size:13.5px;transition:background .12s,color .12s;position:relative}
nav.side a svg{width:15px;height:15px;flex:none;opacity:.75}
nav.side a .cnt{margin-left:auto;font-size:10.5px;border:1px solid var(--rule2);
border-radius:99px;padding:1px 7px;color:var(--faint);font-variant-numeric:tabular-nums}
nav.side a:hover{background:rgba(255,255,255,.04);color:var(--ink)}
nav.side a.on{background:var(--red-bg);color:var(--ink);font-weight:600}
nav.side a.on::before{content:"";position:absolute;left:-12px;top:7px;bottom:7px;
width:3px;border-radius:2px;background:var(--red)}
nav.side a.on svg{opacity:1;color:var(--red-hi)}
nav.side a.on .cnt{border-color:rgba(198,84,84,.5);color:var(--red-hi)}
nav.side .foot{margin-top:auto;padding:10px 10px 0;font-size:10.5px;
color:var(--faint);line-height:1.6;border-top:1px solid var(--rule)}
nav.side .foot a.quit{display:flex;align-items:center;gap:8px;margin:0 0 9px;
padding:5px 0;color:var(--faint);text-decoration:none;font-size:11.5px}
nav.side .foot a.quit svg{width:13px;height:13px;flex:none}
nav.side .foot a.quit:hover{color:var(--red-hi)}
nav.side .foot .me{display:flex;align-items:center;gap:8px;padding:2px 0 8px;
font-size:12px;color:var(--ink);font-weight:600}
nav.side .foot .me form{margin-left:auto;display:flex}
nav.side .foot .me button{background:none;border:0;padding:3px;cursor:pointer;
color:var(--faint);display:flex}
nav.side .foot .me button:hover{color:var(--red-hi);background:none}
nav.side .foot .me svg{width:14px;height:14px}
header{padding:34px 30px 14px}
h1{margin:0;font:600 27px/1.2 Georgia,"Times New Roman",serif;letter-spacing:-.02em}
.sub{color:var(--soft);font-size:13.5px;line-height:1.6;margin-top:7px;max-width:720px}
.wrap{padding:4px 30px 90px;max-width:1380px}
form.filters{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end;
border-bottom:1px solid var(--rule);padding:2px 0 15px;margin-bottom:6px}
label{display:flex;flex-direction:column;gap:5px;font-size:10px;color:var(--faint);
text-transform:uppercase;letter-spacing:.09em}
select,input[type=text],textarea{font:13.5px "Segoe UI",sans-serif;padding:7px 10px;
border:1px solid var(--rule2);border-radius:8px;background:var(--bg);
color:var(--ink);min-width:150px}
select:focus,input:focus,textarea:focus{outline:none;border-color:var(--red);
box-shadow:0 0 0 2px var(--red-bg)}
button{font:13.5px "Segoe UI",sans-serif;padding:7px 15px;border:1px solid var(--rule2);
border-radius:8px;background:var(--card2);color:var(--ink);cursor:pointer;
transition:border-color .12s,background .12s}
button:hover{border-color:var(--faint);background:#3b3934}
button.primary{background:var(--red);color:#fff;border-color:transparent;font-weight:600}
button.primary:hover{background:var(--red-hi)}
/* Borderless tables. Content aligns to the same left edge as the page heading
   instead of sitting inset inside a bordered box, and rows are separated by a
   hairline rather than by a container. */
table{width:100%;border-collapse:separate;border-spacing:0;background:transparent}
th{font-size:10px;text-transform:uppercase;letter-spacing:.11em;color:var(--faint);
text-align:left;padding:14px 14px 9px;border-bottom:1px solid var(--rule2);
font-weight:600}
td{padding:13px 14px;border-bottom:1px solid var(--rule);vertical-align:top;
font-size:13.5px}
th:first-child,td:first-child{padding-left:2px}
th:last-child,td:last-child{padding-right:2px}
tbody tr{transition:background .1s}
tbody tr:hover td{background:rgba(255,255,255,.022)}
.card table th:first-child,.card table td:first-child{padding-left:0}
tr.done{opacity:.38}
td a,.firm a{text-decoration:none;color:var(--ink)}
td a:hover,.firm a:hover{color:var(--red-hi)}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:10px;
text-transform:uppercase;letter-spacing:.07em;padding:2px 9px;border-radius:99px;
border:1px solid var(--rule2);color:var(--soft);white-space:nowrap;background:transparent}
.chip::before{content:"";width:6px;height:6px;border-radius:99px;background:var(--faint)}
.chip.lead{border-color:rgba(99,170,124,.35);color:var(--ok)}
.chip.lead::before{background:var(--ok)}
.chip.dis{border-color:rgba(198,84,84,.4);color:var(--red-hi)}
.chip.dis::before{background:var(--red-hi)}
.chip.partial,.chip.warn{border-color:rgba(207,169,92,.4);color:var(--amber)}
.chip.partial::before,.chip.warn::before{background:var(--amber)}
.chip.info,.chip.acu,.chip.phh{color:var(--soft)}
.pri{font-variant-numeric:tabular-nums;font-size:13px}
.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{display:inline-block;height:5px;border-radius:3px;background:var(--ok);
vertical-align:middle;margin-right:7px}
.bar.neg{background:var(--red-hi)}
.firm{font-weight:600}
.meta{color:var(--faint);font-size:11.5px;margin-top:3px;font-weight:400}
.act{display:flex;gap:5px;flex-wrap:wrap}
.act button{padding:3px 10px;font-size:11.5px;border-radius:7px}
abbr{border-bottom:1px dotted var(--faint);cursor:help;text-decoration:none}
a{color:var(--ink);text-decoration:underline;text-decoration-color:var(--rule2);
text-underline-offset:3px;transition:text-decoration-color .12s,color .12s}
a:hover{text-decoration-color:var(--red-hi)}
.pager{display:flex;gap:12px;align-items:center;margin-top:14px;font-size:13px;
color:var(--soft)}
.warn{background:var(--amber-bg);border:0;border-left:2px solid var(--amber);
border-radius:0 8px 8px 0;padding:11px 15px;margin-bottom:18px;font-size:13px;
color:var(--amber);line-height:1.6}
/* Surfaces separate by tone, not by outline. One border less per element is the
   single biggest thing that stops a dense screen looking cluttered. */
.card{background:var(--card);border:0;border-radius:14px;padding:17px 19px}
.ok{color:var(--ok)} .bad{color:var(--red-hi)} .warnc{color:var(--amber)}
.seg{display:inline-block;font-size:10px;text-transform:uppercase;letter-spacing:.07em;
padding:2px 9px;border-radius:99px}
.seg.prospect{background:var(--ok-bg);color:var(--ok)}
.seg.competitor,.seg.sponsor{background:var(--red-bg);color:var(--red-hi)}
.seg.ambiguous,.seg.unraised{background:var(--amber-bg);color:var(--amber)}
.hrow td{border-bottom:1px solid #34332e}
"""

FAVICON = ('<link rel="icon" href="data:image/svg+xml,'
           '%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E'
           '%3Crect width=%2732%27 height=%2732%27 rx=%277%27 fill=%27%23a63232%27/%3E'
           '%3Ctext x=%2716%27 y=%2722%27 font-family=%27Georgia%27 font-size=%2718%27 '
           'fill=%27white%27 text-anchor=%27middle%27%3EB%3C/text%3E%3C/svg%3E">')


_NAV_CACHE = {"t": 0.0, "inbox": 0, "review": 0, "feed": None}



PALETTE_JS = """
<div id="pal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);
z-index:60" onclick="if(event.target.id=='pal')palHide()">
 <div style="max-width:560px;margin:12vh auto 0;background:var(--card);
 border:1px solid var(--rule2);border-radius:12px;overflow:hidden;
 box-shadow:0 18px 60px rgba(0,0,0,.5)">
  <input id="palq" placeholder="Jump to a firm by name or CRD"
   style="width:100%;border:0;background:var(--card2);padding:13px 16px;
   font-size:15px;min-width:0;border-radius:0">
  <div id="palr"></div>
 </div>
</div>
<script>
function palShow(){var p=document.getElementById('pal');p.style.display='block';
 var q=document.getElementById('palq');q.value='';document.getElementById('palr').innerHTML='';
 setTimeout(function(){q.focus();},0);}
function palHide(){document.getElementById('pal').style.display='none';}
var palT=null, palSel=0, palItems=[];
document.addEventListener('keydown',function(e){
 if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()=='k'){e.preventDefault();palShow();return;}
 var pal=document.getElementById('pal');
 if(pal.style.display=='block'){
  if(e.key=='Escape'){palHide();}
  if(e.key=='ArrowDown'){e.preventDefault();palMove(1);}
  if(e.key=='ArrowUp'){e.preventDefault();palMove(-1);}
  if(e.key=='Enter'&&palItems.length){location.href='/firm/'+palItems[palSel].crd;}
  return;
 }
 if(e.target.tagName=='INPUT'||e.target.tagName=='TEXTAREA'||e.target.tagName=='SELECT')return;
 if(typeof rowKey=='function')rowKey(e);
});
document.addEventListener('input',function(e){
 if(e.target.id!='palq')return;
 clearTimeout(palT);
 var v=e.target.value;
 palT=setTimeout(function(){
  if(v.length<2){document.getElementById('palr').innerHTML='';palItems=[];return;}
  fetch('/api/search?q='+encodeURIComponent(v)).then(function(r){return r.json();})
  .then(function(d){palItems=d;palSel=0;palRender();});
 },140);
});
function palEsc(s){var d=document.createElement('span');
 d.textContent=String(s==null?'':s);return d.innerHTML;}
function palRender(){
 /* Every field is escaped before entering innerHTML. Firm names come from SEC
    filings, which is still text somebody else typed, and a name containing
    markup must render as text, never execute. */
 document.getElementById('palr').innerHTML=palItems.map(function(x,i){
  return '<a href="/firm/'+encodeURIComponent(x.crd)
   +'" style="display:flex;justify-content:space-between;'
   +'gap:10px;padding:10px 16px;text-decoration:none;font-size:13.5px;'
   +(i==palSel?'background:var(--red-bg)':'')+'">'
   +'<span>'+palEsc(x.name)+'</span><span style="color:var(--faint)">CRD '+palEsc(x.crd)
   +' &middot; '+palEsc(x.state||'')+' &middot; '+palEsc(x.raum)+'</span></a>';}).join('');
}
function palMove(d){palSel=Math.max(0,Math.min(palItems.length-1,palSel+d));palRender();}
</script>
"""


def nav(active: str) -> str:
    """App sidebar with live counts. Counts are cached for a few seconds:
    they head every page, and recomputing them per request was measurable."""
    import time as _time
    if _time.monotonic() - _NAV_CACHE["t"] < 5.0:
        return _nav_html(active, _NAV_CACHE["inbox"], _NAV_CACHE["review"],
                         _NAV_CACHE["feed"])
    c = conn()

    def one(sql):
        try:
            return c.execute(sql).fetchone()["n"]
        except Exception:
            return 0

    inbox_n = one("""SELECT COUNT(*) n FROM trigger_event t
        JOIN firm_current f ON f.crd=t.crd
        LEFT JOIN trigger_action a ON a.trigger_id=t.id
        WHERE t.suppressed=0 AND a.state IS NULL AND f.is_era=0
          AND (f.raum>=25e6 AND f.raum<500e6 OR t.trigger_type IN
               ('new_registration','reregistration_or_gap'))""")
    # The badge counts only items on firms someone will call; the long tail is
    # covered by safe defaults and a four-digit badge just teaches people to
    # ignore the queue entirely.
    called = """(SELECT crd FROM tier_a_rank WHERE in_working_list=1
                 UNION SELECT o.crd FROM firm_overlay o
                       JOIN tier_a_rank t ON t.crd=o.crd WHERE o.phh_13f=1
                 UNION SELECT crd FROM tier_c_score WHERE rank<=100)"""
    review_n = (one(f"SELECT COUNT(*) n FROM adv_13f_match WHERE status='review'"
                    f" AND crd IN {called}")
                + one(f"SELECT COUNT(*) n FROM brochure_negation WHERE status='open'"
                      f" AND crd IN {called}"))
    feed = c.execute("SELECT published_at FROM snapshot WHERE source_key='adv_feed'"
                     " ORDER BY id DESC LIMIT 1").fetchone()
    c.close()
    feed_s = feed["published_at"] if feed else None
    _NAV_CACHE.update(t=_time.monotonic(), inbox=inbox_n, review=review_n, feed=feed_s)
    return _nav_html(active, inbox_n, review_n, feed_s)


def _nav_html(active: str, inbox_n: int, review_n: int, feed_s) -> str:
    user = current_user()
    I = 'fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"'
    icons = {
        "inbox": f'<svg viewBox="0 0 24 24" {I}><path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5 5h14l3 7v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-6z"/></svg>',
        "firms": f'<svg viewBox="0 0 24 24" {I}><path d="M3 21h18"/><path d="M5 21V7l7-4 7 4v14"/><path d="M9 21v-4h6v4"/><path d="M9 10h.01M15 10h.01M9 14h.01M15 14h.01"/></svg>',
        "lists": f'<svg viewBox="0 0 24 24" {I}><path d="M8 6h13M8 12h13M8 18h13"/><path d="M3.5 6h.01M3.5 12h.01M3.5 18h.01"/></svg>',
        "system": f'<svg viewBox="0 0 24 24" {I}><path d="M22 12h-4l-3 8-6-16-3 8H2"/></svg>',
        "guide": f'<svg viewBox="0 0 24 24" {I}><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>',
    }
    items = [("inbox", "/", "Inbox", inbox_n),
             ("firms", "/firms", "Firms", None),
             ("lists", "/lists", "Lists", None),
             ("system", "/health", "System", review_n),
             ("guide", "/guide", "How to use", None)]
    links = "".join(
        f'<a href="{u}" class="{"on" if k == active else ""}">{icons[k]}{t}'
        + (f'<span class="cnt">{n:,}</span>' if n else "") + "</a>"
        for k, u, t, n in items)
    c2 = conn()
    views = c2.execute("SELECT id,name,page,qs FROM saved_view ORDER BY name").fetchall()
    c2.close()
    if views:
        vlinks = "".join(
            f'<a href="{"/firms" if v["page"] == "firms" else "/"}?{esc(v["qs"])}" '
            f'style="font-size:12.5px;padding:5px 11px">'
            f'<span style="color:var(--faint)">&#9656;</span> {esc(v["name"])}</a>'
            for v in views)
        links += ('<div style="margin:12px 10px 4px;font-size:9.5px;letter-spacing:.12em;'
                  'text-transform:uppercase;color:var(--faint)">Saved views</div>' + vlinks)
    power = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
             'stroke-width="1.8" stroke-linecap="round"><path d="M12 3v9"/>'
             '<path d="M6.4 6.4a8 8 0 1 0 11.2 0"/></svg>')
    out = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
           'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
           '<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/>'
           '<path d="M10 17l-5-5 5-5"/><path d="M5 12h11"/></svg>')
    who = ""
    if user:
        who = (f'<div class="me">{esc(auth.display_name(user))}'
               f'<form method="post" action="/logout">'
               f'<button type="submit" title="Sign out">{out}</button>'
               f'</form></div>')
    quit_link = ("" if MANAGED else
                 f'<a class="quit" href="/quit">{power}Quit {APP_NAME}</a>')
    return (FAVICON + PALETTE_JS + '<nav class="side"><div class="brand">'
            f'<div class="mark">B</div><div class="t">{APP_NAME}'
            '<small>SEC filings, ranked</small></div></div>'
            f'{links}<div class="foot">{who}{quit_link}'
            f'Feed snapshot {esc(feed_s) if feed_s else "none"}<br>'
            f'Numbers carry their caveats.</div></nav>')


def caveat(key: str, text: str) -> str:
    return f'<abbr title="{esc(CAVEATS[key].strip())}">{text}</abbr>'


# --- who is signed in -----------------------------------------------------
# Deny by default: the middleware requires a session for every path that is not
# explicitly public, so a route added later is protected without anyone having
# to remember to protect it.

LOGIN_CSS = """
.signin{max-width:360px;margin:0 auto;padding:80px 24px 40px}
.signin .mk{width:46px;height:46px;border-radius:13px;background:var(--red);
color:#fff;display:flex;align-items:center;justify-content:center;
margin:0 auto 20px;font:700 24px Georgia,serif}
.signin h1{font-size:25px;text-align:center;margin:0 0 6px}
.signin .sub{text-align:center;color:var(--soft);font-size:13.5px;margin:0 0 26px}
.signin label{margin-bottom:14px}
.signin input{width:100%;min-width:0;padding:10px 12px;font-size:14px}
.signin button{width:100%;padding:10px;margin-top:6px;font-size:14px}
.signin .err{background:var(--red-bg);border-left:2px solid var(--red);
padding:9px 13px;font-size:13px;color:var(--red-hi);margin-bottom:18px;
border-radius:0 8px 8px 0}
.signin .hint{color:var(--faint);font-size:12px;line-height:1.65;margin-top:22px;
text-align:center}
"""


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in auth.PUBLIC_PATHS:
        return await call_next(request)
    user = auth.read_session(request.cookies.get(auth.COOKIE))
    if not user or user not in auth.load_users():
        if request.method != "GET":
            return RedirectResponse("/login", status_code=303)
        nxt = request.url.path
        if request.url.query:
            nxt += "?" + request.url.query
        return RedirectResponse(
            "/login?next=" + urllib.parse.quote(nxt, safe=""), status_code=303)
    # Handlers and nav() read this instead of re-parsing the cookie.
    request.state.user = user
    token = CURRENT_USER.set(user)
    try:
        return await call_next(request)
    finally:
        CURRENT_USER.reset(token)


def login_page(error: str = "", nxt: str = "/") -> HTMLResponse:
    no_accounts = not auth.load_users()
    err = f'<div class="err">{esc(error)}</div>' if error else ""
    if no_accounts:
        err = ('<div class="err">No accounts exist yet. On the server, run: '
               'python -m scripts.manage_users add &lt;username&gt; '
               '--name "Full Name"</div>')
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>Sign in to {APP_NAME}</title>{FAVICON}
<style>{PAGE_CSS}{LOGIN_CSS}body{{margin:0}}</style>
<div class="signin">
<div class="mk">B</div>
<h1>{APP_NAME}</h1>
<p class="sub">SEC filings, ranked</p>
{err}
<form method="post" action="/login">
<input type="hidden" name="next" value="{esc(nxt)}">
<label>Username<input name="username" autocomplete="username" autofocus
 required></label>
<label>Password<input name="password" type="password"
 autocomplete="current-password" required></label>
<button class="primary" type="submit">Sign in</button>
</form>
<p class="hint">Signing in as yourself is what fills in who owns a firm and who
cleared a review, so the queue stays honest about who did what.</p>
</div>""", status_code=200 if not error else 401)


@app.get("/login", response_class=HTMLResponse)
def login_form(next: str = Query("/")):
    return login_page(nxt=next or "/")


@app.post("/login", response_class=HTMLResponse)
def login_submit(username: str = Form(""), password: str = Form(""),
                 next: str = Form("/")):
    rec = auth.check_login(username, password)
    if not rec:
        return login_page("That username and password combination is not "
                          "recognised.", nxt=next or "/")
    # Only ever redirect somewhere inside the app: an open redirect here would
    # let a crafted login link bounce a signed-in user to another site.
    dest = next if (next or "").startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(dest, status_code=303)
    resp.set_cookie(auth.COOKIE, auth.make_session(username.strip().lower()),
                    max_age=auth.SESSION_DAYS * 86400, httponly=True,
                    samesite="strict", secure=SECURE_COOKIES, path="/")
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/healthz")
def healthz():
    """Unauthenticated liveness only, for the launcher and any load balancer.
    Deliberately carries no data."""
    return {"ok": True, "app": APP_NAME}


# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def inbox(
    ttype: str = Query("", alias="type"),
    product: str = Query(""),
    state: str = Query("open"),
    q: str = Query(""),
    page: int = Query(1, ge=1),
    per: int = Query(50, ge=10, le=200),
):
    c = conn()
    where = ["t.suppressed=0", "f.is_era=0",
             "(f.raum>=25e6 AND f.raum<500e6 OR t.trigger_type IN "
             "('new_registration','reregistration_or_gap'))"]
    args: list = []
    if ttype:
        where.append("t.trigger_type=?"); args.append(ttype)
    if product:
        keys = [k for k, v in PRODUCTS.items() if v["product"] in (product, "BOTH")]
        where.append(f"t.trigger_type IN ({','.join('?'*len(keys))})"); args += keys
    if q:
        where.append("(f.legal_name LIKE ? OR f.crd=?)"); args += [f"%{q}%", q]
    if state == "open":
        where.append("a.state IS NULL")
    elif state:
        where.append("a.state=?"); args.append(state)

    base = f"""FROM trigger_event t
        JOIN firm_current f ON f.crd=t.crd
        LEFT JOIN trigger_action a ON a.trigger_id=t.id
        LEFT JOIN firm_custodian_profile p ON p.crd=t.crd
        LEFT JOIN re_segment r ON r.crd=t.crd
        WHERE {' AND '.join(where)}"""
    counts = {r["k"]: r["n"] for r in c.execute(
        f"SELECT t.trigger_type k, COUNT(*) n {base} GROUP BY 1", args)}
    total = sum(counts.values())
    rows = c.execute(f"""
        SELECT t.id,t.crd,t.trigger_type,t.detected_date,t.description,t.age_days,
               t.priority,a.state,
               f.legal_name,f.state AS st,f.raum,f.iar_count,
               p.schwab_share_reported AS sshare, p.as_of_filing_date AS sasof,
               r.segment AS reseg
        {base}
        ORDER BY ABS(t.priority) DESC, t.detected_date DESC
        LIMIT ? OFFSET ?""", args + [per, (page - 1) * per]).fetchall()
    alerts = c.execute("""
        SELECT t.id, t.crd, t.trigger_type, t.detected_date, t.description,
               f.legal_name
        FROM trigger_event t
        JOIN firm_watch w ON w.crd = t.crd
        JOIN firm_current f ON f.crd = t.crd
        LEFT JOIN trigger_action a ON a.trigger_id = t.id
        WHERE t.suppressed = 0 AND a.state IS NULL
        ORDER BY t.detected_date DESC LIMIT 8""").fetchall()
    c.close()
    maxp = max((abs(r["priority"] or 0) for r in rows), default=1) or 1

    def opt(val, cur, label):
        return f'<option value="{esc(val)}"{" selected" if val==cur else ""}>{esc(label)}</option>'

    tsel = "".join([opt("", ttype, f"All types ({total})")] +
                   [opt(k, ttype, f"{TYPE_LABEL.get(k, k)} ({counts.get(k, 0)})")
                    for k in TYPE_LABEL])
    psel = "".join(opt(v, product, l) for v, l in
                   [("", "All products"), ("ACUBOOTH", "AcuBooth"), ("PHH", "PHH")])
    ssel = "".join(opt(v, state, l) for v, l in
                   [("open", "Open"), ("actioned", "Actioned"), ("snoozed", "Snoozed"),
                    ("dismissed", "Dismissed"), ("", "All")])

    body = []
    for r in rows:
        meta = PRODUCTS.get(r["trigger_type"], {"product": "BOTH", "kind": "lead"})
        kchip = "dis" if meta["kind"] == "disqualifier" else "lead"
        pri = r["priority"] or 0
        w = max(2, int(abs(pri) / maxp * 54))
        sshare = ""
        if r["sshare"] is not None:
            sshare = (" &middot; " + caveat("schwab_share_reported",
                      f"Schwab {r['sshare']*100:.0f}% of reported")
                      + f' <span class="meta">as of {esc(r["sasof"])}</span>')
        reseg = f' &middot; RE: <b>{esc(r["reseg"])}</b>' if r["reseg"] else ""
        done = " class='done'" if r["state"] else ""
        back = esc(f"/?type={ttype}&product={product}&state={state}&q={q}&page={page}")
        age_note = (caveat("archive_as_of", "archive-derived")
                    if (r["age_days"] or 0) > 200 else "live")
        body.append(f"""<tr{done} class="krow" data-tid="{r['id']}" data-crd="{esc(r['crd'])}">
<td class="pri"><span class="bar{' neg' if pri < 0 else ''}" style="width:{w}px"></span>{pri:+.2f}</td>
<td><span class="chip {kchip}">{esc(TYPE_LABEL.get(r['trigger_type'], r['trigger_type']))}</span>
    <span class="chip">{esc(meta['product'])}</span></td>
<td><div class="firm"><a href="/firm/{esc(r['crd'])}">{esc(r['legal_name'] or '(unnamed)')}</a></div>
    <div class="meta">CRD {esc(r['crd'])} &middot; {esc(r['st'] or '--')} &middot;
    {money(r['raum'])} RAUM &middot; {r['iar_count'] or 0} IARs{reseg}{sshare}</div></td>
<td>{esc(r['description'])}
    <div class="meta">{esc(r['detected_date'])} &middot; {r['age_days'] or 0} days old
    &middot; {age_note}</div></td>
<td>{f'<span class="chip">{esc(r["state"])}</span>' if r["state"] else f'''
  <form method="post" action="/action" class="act">
   <input type="hidden" name="tid" value="{r['id']}">
   <input type="hidden" name="back" value="{back}">
   <button name="state" value="actioned">Done</button>
   <button name="state" value="snoozed">Snooze</button>
   <button name="state" value="dismissed">Dismiss</button>
  </form>'''}</td></tr>""")

    if alerts:
        arows = "".join(
            f'<div style="display:flex;gap:10px;align-items:baseline;padding:6px 0;'
            f'border-bottom:1px solid rgba(198,84,84,.18)">'
            f'<span class="chip dis">watched</span>'
            f'<a href="/firm/{esc(a["crd"])}" style="font-weight:600">'
            f'{esc(a["legal_name"] or a["crd"])}</a>'
            f'<span style="font-size:12.5px;color:var(--soft)">{esc(a["description"])}</span>'
            f'<span class="meta" style="margin-left:auto;white-space:nowrap">'
            f'{esc(a["detected_date"])}</span></div>'
            for a in alerts)
        alerts_html = (f'<div class="card" style="border-color:rgba(198,84,84,.45);'
                       f'margin-bottom:14px"><div style="font-size:10px;letter-spacing:.1em;'
                       f'text-transform:uppercase;color:var(--red-hi);font-weight:700;'
                       f'margin-bottom:4px">Watchlist alerts</div>{arows}</div>')
    else:
        alerts_html = ""
    pages = max(1, -(-total // per))
    qs = f"type={ttype}&product={product}&state={state}&q={q}&per={per}"
    prev = f'<a href="/?{qs}&page={page-1}">Previous</a>' if page > 1 else ""
    nxt = f'<a href="/?{qs}&page={page+1}">Next</a>' if page < pages else ""

    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>Trigger inbox</title><style>{PAGE_CSS}</style>
{nav("inbox")}
<header><h1>Trigger inbox</h1>
<div class="sub">{total:,} events &middot; in-band registered advisers, exempt reporting
advisers excluded &middot; sorted by recency-weighted priority &middot;
<a href="/guide#inbox">how to work this screen</a></div></header>
<div class="wrap">
{alerts_html}<div class="warn">Archive-derived events are at least 18 months old and weight low by
construction. Live triggers accumulate weekly from the next feed capture. A red bar is
a disqualifying event, not a lead.</div>
<form class="filters" method="get">
<label>Trigger type<select name="type">{tsel}</select></label>
<label>Product<select name="product">{psel}</select></label>
<label>State<select name="state">{ssel}</select></label>
<label>Firm or CRD<input type="text" name="q" value="{esc(q)}" placeholder="name or CRD"></label>
<button class="primary" type="submit">Apply</button>
<a href="/" style="align-self:center;font-size:13px">Reset</a>
</form>
<form method="post" action="/views/save" style="display:flex;gap:8px;margin:-6px 0 14px">
<input type="hidden" name="page" value="inbox"><input type="hidden" name="qs" value="{esc(qs)}">
<input type="text" name="name" placeholder="Save this view as..." style="min-width:220px">
<button type="submit">Save view</button>
</form>
<table><thead><tr><th style="width:96px">Priority</th><th style="width:230px">Trigger</th>
<th style="width:330px">Firm</th><th>What happened</th><th style="width:200px">Action</th>
</tr></thead>
<tbody>{''.join(body) or
'<tr><td colspan="5" style="padding:26px;color:var(--faint)">Nothing matches these filters.</td></tr>'}
</tbody></table>
<div class="pager">{prev} Page {page} of {pages} &middot; {total:,} events {nxt}</div>
</div>
<script>
var kSel=-1, kRows=document.querySelectorAll('tr.krow');
function kMark(){{kRows.forEach(function(r,i){{
 r.style.outline=(i==kSel)?'2px solid var(--red)':'';r.style.outlineOffset='-2px';}});
 if(kSel>=0)kRows[kSel].scrollIntoView({{block:'nearest'}});}}
function kAct(state){{var r=kRows[kSel];if(!r)return;
 var f=document.createElement('form');f.method='post';f.action='/action';
 f.innerHTML='<input name="tid" value="'+r.dataset.tid+'">'
  +'<input name="state" value="'+state+'"><input name="back" value="'+location.pathname+location.search+'">';
 document.body.appendChild(f);f.submit();}}
function rowKey(e){{
 if(e.key=='j'){{kSel=Math.min(kRows.length-1,kSel+1);kMark();}}
 if(e.key=='k'){{kSel=Math.max(0,kSel-1);kMark();}}
 if(e.key=='d')kAct('actioned');
 if(e.key=='s')kAct('snoozed');
 if(e.key=='x')kAct('dismissed');
 if(e.key=='Enter'&&kSel>=0)location.href='/firm/'+kRows[kSel].dataset.crd;
}}
</script>""")


@app.post("/action")
def act(tid: int = Form(...), state: str = Form(...), back: str = Form("/"),
        reason: str = Form("")):
    if state not in ("actioned", "snoozed", "dismissed"):
        return RedirectResponse("/", status_code=303)
    if not back.startswith("/"):
        back = "/"
    c = conn()
    c.execute(
        "INSERT INTO trigger_action (trigger_id,state,reason,actioned_by,actioned_at)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(trigger_id) DO UPDATE SET state=excluded.state,"
        " reason=excluded.reason, actioned_at=excluded.actioned_at",
        (tid, state, reason or None, current_owner() or "bd",
         datetime.now(timezone.utc).isoformat(timespec="seconds")))
    c.commit()
    c.close()
    return RedirectResponse(back, status_code=303)


@app.get("/health.json")
def health_json():
    c = conn()
    out = {
        "snapshots": [dict(r) for r in c.execute(
            "SELECT source_key,published_at,bytes,captured_at FROM snapshot ORDER BY id")],
        "runs": [dict(r) for r in c.execute(
            "SELECT source_key,stage,status,rows_out,flagged,message,finished_at"
            " FROM run_log ORDER BY id DESC LIMIT 15")],
    }
    c.close()
    return JSONResponse(out)


@app.get("/api/search")
def api_search(q: str = Query("", min_length=0)):
    """Firm lookup for the Ctrl+K palette. Read-only, tiny payload."""
    if len(q.strip()) < 2:
        return JSONResponse([])
    c = conn()
    rows = c.execute("""
        SELECT crd, legal_name, state, raum FROM firm_current
        WHERE is_era = 0 AND (legal_name LIKE ? OR business_name LIKE ? OR crd = ?)
        ORDER BY raum DESC LIMIT 9""",
        (f"%{q}%", f"%{q}%", q.strip())).fetchall()
    c.close()
    return JSONResponse([{"crd": r["crd"], "name": r["legal_name"],
                          "state": r["state"], "raum": money(r["raum"])}
                         for r in rows])


@app.post("/watch/{crd}")
def watch_toggle(crd: str, back: str = Form("/")):
    if not back.startswith("/"):
        back = "/"
    c = conn()
    if c.execute("SELECT 1 FROM firm_watch WHERE crd=?", (crd,)).fetchone():
        c.execute("DELETE FROM firm_watch WHERE crd=?", (crd,))
    else:
        c.execute("INSERT INTO firm_watch VALUES (?,?,?)",
                  (crd, current_owner() or "bd", datetime.now(timezone.utc).isoformat(timespec="seconds")))
    c.commit()
    c.close()
    return RedirectResponse(back, status_code=303)


@app.post("/views/save")
def view_save(page: str = Form(...), qs: str = Form(""), name: str = Form(...)):
    if page not in ("inbox", "firms") or not name.strip():
        return RedirectResponse("/", status_code=303)
    c = conn()
    c.execute("INSERT INTO saved_view (name,page,qs,created_at) VALUES (?,?,?,?)",
              (name.strip()[:60], page, qs,
               datetime.now(timezone.utc).isoformat(timespec="seconds")))
    c.commit()
    c.close()
    return RedirectResponse("/firms" if page == "firms" else "/", status_code=303)


@app.post("/views/delete")
def view_delete(vid: int = Form(...)):
    c = conn()
    c.execute("DELETE FROM saved_view WHERE id=?", (vid,))
    c.commit()
    c.close()
    return RedirectResponse("/", status_code=303)


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.post("/admin/run-weekly")
def run_weekly_now():
    """Kick the weekly cycle from the UI. One at a time; progress lands in
    run_log within seconds and is visible on Pipeline health."""
    import subprocess
    import sys as _sys
    c = conn()
    busy = c.execute("SELECT COUNT(*) n FROM run_log WHERE status='running'"
                     " AND started_at > datetime('now','-2 hours')").fetchone()["n"]
    c.close()
    if busy:
        return RedirectResponse("/health?msg=already-running", status_code=303)
    log = open(config.DATA_DIR / "weekly.log", "ab")
    flags = procs.SPAWN_FLAGS
    subprocess.Popen(
        [_sys.executable, "-m", "scripts.run_weekly", "--brochure-slice", "120"],
        cwd=str(config.ROOT), stdout=log, stderr=log, creationflags=flags)
    return RedirectResponse("/health?msg=started", status_code=303)


@app.post("/admin/task/{kind}/{action}")
def task_control(kind: str, action: str):
    """Start or pause an autopilot job. Start also launches the worker process
    if none is alive; Pause takes effect within one slice."""
    if kind not in ("brochures", "firm_refresh", "contact_extract",
                    "web_enrich", "infer_emails", "email_verify", "cusip_verify") \
            or action not in ("start", "pause"):
        return RedirectResponse("/health", status_code=303)
    c = conn()
    c.execute("INSERT OR IGNORE INTO auto_task (kind) VALUES (?)", (kind,))
    c.execute("UPDATE auto_task SET desired_state=?, updated_at=? WHERE kind=?",
              ("running" if action == "start" else "paused",
               datetime.now(timezone.utc).isoformat(timespec="seconds"), kind))
    c.commit()
    c.close()
    if action == "start":
        ensure_autopilot()
    return RedirectResponse("/health", status_code=303)


# --- quitting -------------------------------------------------------------
# There is no stop script. The tool starts itself at logon and is stopped from
# inside itself, so the only way to shut it down is a deliberate click.

QUIT_CSS = """
.quitbox{max-width:520px;margin:0 auto;padding:90px 30px 40px;text-align:center}
.quitbox .mk{width:46px;height:46px;border-radius:13px;background:var(--red);
color:#fff;display:flex;align-items:center;justify-content:center;margin:0 auto 22px;
font:700 24px Georgia,serif}
.quitbox h1{font-size:29px;margin:0 0 12px}
.quitbox p{color:var(--soft);font-size:15px;line-height:1.7;margin:0 0 10px}
.quitbox .acts{display:flex;gap:10px;justify-content:center;margin-top:26px}
.quitbox .acts a{color:var(--soft);text-decoration:none;padding:8px 16px;
border-radius:8px;font-size:13.5px}
.quitbox .acts a:hover{background:var(--card);color:var(--ink)}
.quitbox .hint{color:var(--faint);font-size:12.5px;margin-top:30px;line-height:1.7}
"""


@app.get("/quit", response_class=HTMLResponse)
def quit_confirm():
    """Confirmation, because a misclick in the sidebar should not take the
    server down under two other people who are using it."""
    if MANAGED:
        return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>Managed by the server</title><style>{PAGE_CSS}{QUIT_CSS}</style>
{nav("quit")}
<div class="quitbox"><div class="mk">B</div>
<h1>Nothing to quit here</h1>
<p>This {APP_NAME} runs on a server that restarts it automatically, so
quitting from inside would only bounce it. To actually stop it, someone with
server access runs <b>docker compose down</b>.</p>
<div class="acts"><a href="/">Back</a></div></div>""")
    c = conn()
    try:
        jobs = c.execute("SELECT kind FROM auto_task WHERE desired_state='running'"
                         ).fetchall()
    except sqlite3.Error:
        jobs = []
    c.close()
    note = ""
    if jobs:
        names = ", ".join(j["kind"].replace("_", " ") for j in jobs)
        note = (f'<p>Background work is running right now ({esc(names)}). It stops '
                f'too, and picks up where it left off next time.</p>')
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>Quit {APP_NAME}</title><style>{PAGE_CSS}{QUIT_CSS}</style>
{nav("quit")}
<div class="quitbox">
<div class="mk">B</div>
<h1>Quit {APP_NAME}?</h1>
<p>Everything is already saved. Nothing is lost by quitting, and no work in
progress is thrown away.</p>
{note}
<div class="acts">
<form method="post" action="/admin/quit">
<button type="submit" class="primary">Quit {APP_NAME}</button></form>
<a href="/">Cancel</a>
</div>
<p class="hint">It starts again by itself the next time you sign in to Windows,
or immediately from the {APP_NAME} shortcut.</p>
</div>""")


@app.post("/admin/quit", response_class=HTMLResponse)
def quit_now():
    """Answer first, then die. The response has to reach the browser before the
    process serving it goes away, so the kill runs on a short timer."""
    if MANAGED:
        return RedirectResponse("/quit", status_code=303)
    threading.Timer(0.8, stop_everything).start()
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>{APP_NAME} has stopped</title>{FAVICON}
<style>{PAGE_CSS}{QUIT_CSS}
body{{margin:0}} nav.side{{display:none}}</style>
<div class="quitbox">
<div class="mk">B</div>
<h1>{APP_NAME} has stopped</h1>
<p>You can close this tab. Everything is saved.</p>
<p class="hint">It will be running again the next time you sign in to Windows.
To start it right now, open the {APP_NAME} shortcut on your desktop.</p>
</div>""")


from . import (firm_view, firms_view, guide_view, list_view,  # noqa: E402
               review_view)

app.include_router(firms_view.router)     # /firms, /lists, exports, add-to-list
app.include_router(firm_view.router)      # /firm/{crd}
app.include_router(list_view.router)      # /health (System)
app.include_router(review_view.router)    # /review (System)
app.include_router(guide_view.router)     # /guide


# Old section URLs, redirected to their new home so bookmarks and any lingering
# links keep working after the sidebar was consolidated.
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
