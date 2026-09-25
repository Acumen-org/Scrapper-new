"""Bellwether web app: the data and intelligence layer for go-to-market.

A bellwether is a leading indicator, which is what every row in this system is:
a filing, a website or a change that says a firm is worth calling before anyone
else notices. Bellwether turns SEC filings and each firm's own public words
into product lists (who fits PHH, AcuBooth and Glynac, and why), signals (what
changed this week) and call-ready firm pages.

Architecture rules learned the hard way:

  - GET handlers never write. All working tables are created once at startup,
    because DDL inside a request is a write transaction that queues behind any
    background ingest.
  - One visual system, defined here once: dark, a single dark red accent, green
    only where something is a lead, amber only where something needs care.
  - A number never renders without the reason for it. Every score on every
    screen can be opened to the evidence that produced it.
  - No page needs a manual. If a screen needs explaining, the explanation goes
    on the screen, next to the thing it explains. UI copy carries no em dashes.

    python -m uvicorn prospect.webapp:app --port 8787
"""

from __future__ import annotations

import contextvars
import html
import os
import re
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from datetime import datetime, timezone

import yaml
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import auth, config, db, procs, products
from .names import nice_name

APP_NAME = "Bellwether"

# Session cookies are marked Secure when served over HTTPS, which is every
# deployment except running it on your own machine over plain http.
SECURE_COOKIES = os.environ.get("BELLWETHER_HTTPS", "").lower() in ("1", "true", "yes")

# Managed mode: a supervisor (Docker, Nomad) owns the process lifecycle. Quit
# disappears from the UI, because killing the process would just make the
# supervisor restart it, and pidfiles from a previous container are always
# stale: PID namespaces start over at 1, so a recorded pid usually names some
# OTHER live process in the new container.
MANAGED = os.environ.get("BELLWETHER_MANAGED", "").lower() in ("1", "true", "yes")

# The signed-in user, request scoped. Context variables propagate into the
# threadpool FastAPI runs sync endpoints on, so this is safe for both kinds.
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
CREATE TABLE IF NOT EXISTS auto_task (
    kind          TEXT PRIMARY KEY,
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
"""


@app.on_event("startup")
def _init_once() -> None:
    """Every table and view the app touches exists before the first request,
    so request handlers never issue DDL and GETs stay pure readers."""
    c = db.connect()
    db.init(c)
    db.init_firm(c)
    c.executescript(APP_TABLES)
    products.init(c)
    # Columns added to app tables after they first shipped.
    if "title" not in {r[1] for r in c.execute("PRAGMA table_info(contact_email)")}:
        c.execute("ALTER TABLE contact_email ADD COLUMN title TEXT")
    c.commit()

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

    # Record the supervisor PID from inside the app rather than trusting the
    # launcher to have done it, so Quit always has a live PID to stop.
    try:
        (config.DATA_DIR / "server.pid").write_text(str(os.getppid()))
    except OSError:
        pass

    # Autopilot is a separate process and does not survive a restart. If a job
    # was left switched on, switch it back on.
    try:
        n = c.execute("SELECT COUNT(*) n FROM auto_task"
                      " WHERE desired_state='running'").fetchone()["n"]
    except sqlite3.Error:
        n = 0
    except Exception:
        c.rollback()
        n = 0
    c.close()
    if n:
        ensure_autopilot()

    # The weekly pull runs itself. One worker wins the claim; the other simply
    # does not schedule.
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
                     " AND started_at::timestamptz > NOW() - INTERVAL '2 hours'").fetchone()["n"]
    if busy:
        return False, "a run is already in flight"
    return True, f"feed is {age_days:.1f} days old"


def _scheduler_loop() -> None:
    """Check due-ness on a slow clock and launch the weekly cycle when a new
    feed file should exist. Failures land in run_log like any manual run."""
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
    """Launch the autopilot worker unless one is already alive. Liveness goes
    through procs.is_alive, never os.kill(pid, 0), which on Windows terminates
    the process being checked."""
    pidfile = config.DATA_DIR / "autopilot.pid"
    if procs.alive_pid(pidfile) is not None:
        return True
    log = open(config.DATA_DIR / "autopilot.log", "ab")
    subprocess.Popen([sys.executable, "-m", "scripts.autopilot"],
                     cwd=str(config.ROOT), stdout=log, stderr=log,
                     creationflags=procs.SPAWN_FLAGS)
    return True


def _kill_tree(pid: int) -> None:
    procs.kill_tree(pid)


def stop_everything() -> None:
    """Stop the whole tool: background jobs first, then the server itself.
    Killing the supervisor tree is the only reliable stop on Windows, because
    workers inherit the listening socket."""
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
        _kill_tree(recorded)
    _kill_tree(target)


def conn():
    """Per-request connection. No DDL here, ever: reads must never become writes."""
    return db.connect()


def esc(v) -> str:
    return html.escape(str(v)) if v is not None else ""


def escn(v) -> str:
    """A firm name, escaped and in readable case."""
    return esc(nice_name(v))


def money(v) -> str:
    if v is None:
        return "-"
    v = float(v)
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.0f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:,.0f}"


SIGNAL_WINDOW_DAYS = 60   # "new" on Home and the sidebar count


def signal_cutoff(days: int = SIGNAL_WINDOW_DAYS) -> str:
    from datetime import date, timedelta
    return (date.today() - timedelta(days=days)).isoformat()


def qs_join(**kw) -> str:
    """A query string from keyword args, skipping empty values."""
    return urllib.parse.urlencode({k: v for k, v in kw.items() if v not in ("", None)})


# ---------------------------------------------------------------------------
# One visual system. Dark neutrals plus exactly three signal colours:
# red (the accent; danger and disqualifiers), green (a lead), amber (care).
PAGE_CSS = """
:root{
 --bg:#1f1e1c; --side:#191816; --raise:#282724; --raise2:#302e2b;
 --ink:#eceae4; --soft:#b6b1a5; --faint:#8a857b;
 --rule:#33312d; --rule2:#46433d;
 --red:#a63232; --red-hi:#c65454; --red-bg:rgba(166,50,50,.16);
 --ok:#63aa7c; --ok-bg:rgba(99,170,124,.13);
 --amber:#cfa95c; --amber-bg:rgba(207,169,92,.12);
}
*{box-sizing:border-box}
html{color-scheme:dark}
body{margin:0 0 0 236px;background:var(--bg);color:var(--ink);
font:14.5px/1.55 "Segoe UI",system-ui,-apple-system,sans-serif;
-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
::selection{background:var(--red-bg)}
a{color:var(--ink);text-decoration:underline;text-decoration-color:var(--rule2);
text-underline-offset:3px;transition:color .12s,text-decoration-color .12s}
a:hover{color:var(--red-hi);text-decoration-color:var(--red-hi)}

/* ---- sidebar */
nav.side{position:fixed;top:0;left:0;bottom:0;width:236px;background:var(--side);
padding:18px 12px 14px;display:flex;flex-direction:column;gap:1px;z-index:10;
overflow-y:auto}
nav.side .brand{display:flex;align-items:center;gap:10px;padding:2px 10px 16px;
text-decoration:none}
nav.side .brand .mark{width:28px;height:28px;border-radius:8px;background:var(--red);
color:#fff;display:flex;align-items:center;justify-content:center;
font:700 16px Georgia,serif;flex:none}
nav.side .brand .t{font:650 17px Georgia,"Times New Roman",serif;
letter-spacing:-.02em;color:var(--ink);line-height:1.15}
nav.side .brand .t small{display:block;font:400 10px "Segoe UI",sans-serif;
color:var(--faint);letter-spacing:.13em;text-transform:uppercase;margin-top:2px}
nav.side .find{display:flex;align-items:center;gap:9px;margin:0 2px 14px;
padding:8px 11px;border-radius:9px;background:var(--raise);color:var(--faint);
font-size:13px;cursor:pointer;border:0;width:auto;text-align:left}
nav.side .find:hover{color:var(--ink);background:var(--raise2)}
nav.side .find kbd{margin-left:auto;font:11px "Segoe UI",sans-serif;color:var(--faint);
background:var(--side);border-radius:5px;padding:1px 6px}
nav.side .find svg{width:14px;height:14px}
nav.side .grp{font-size:10px;letter-spacing:.14em;text-transform:uppercase;
color:var(--faint);padding:14px 11px 5px}
nav.side a.i{display:flex;align-items:center;gap:10px;padding:7px 11px;border-radius:8px;
color:var(--soft);text-decoration:none;font-size:13.5px;position:relative;
transition:background .12s,color .12s}
nav.side a.i svg{width:15px;height:15px;flex:none;opacity:.75}
nav.side a.i .cnt{margin-left:auto;font-size:11px;color:var(--faint);
font-variant-numeric:tabular-nums}
nav.side a.i .cnt.hot{color:var(--ok)}
nav.side a.i:hover{background:rgba(255,255,255,.04);color:var(--ink)}
nav.side a.i.on{background:var(--red-bg);color:var(--ink);font-weight:600}
nav.side a.i.on::before{content:"";position:absolute;left:-12px;top:7px;bottom:7px;
width:3px;border-radius:2px;background:var(--red)}
nav.side a.i.on svg{opacity:1;color:var(--red-hi)}
nav.side a.i .dot{width:7px;height:7px;border-radius:99px;flex:none;margin:0 4px}
nav.side .foot{margin-top:auto;padding:12px 10px 0;font-size:11px;color:var(--faint);
line-height:1.6}
nav.side .foot .me{display:flex;align-items:center;gap:8px;padding:2px 0 6px;
font-size:12.5px;color:var(--ink);font-weight:600}
nav.side .foot .me form{margin-left:auto;display:flex}
nav.side .foot .me button{background:none;border:0;padding:3px;cursor:pointer;
color:var(--faint);display:flex}
nav.side .foot .me button:hover{color:var(--red-hi)}
nav.side .foot svg{width:14px;height:14px}
nav.side .foot a{color:var(--faint);text-decoration:none}
nav.side .foot a:hover{color:var(--red-hi)}

/* ---- page frame: one centred measure, so wide screens get even margins */
.pg{max-width:1360px;margin:0 auto;padding:30px 36px 110px}
.pg.narrow{max-width:1120px}
.crumb{font-size:12.5px;color:var(--faint);margin-bottom:10px}
.crumb a{color:var(--faint);text-decoration:none}
.crumb a:hover{color:var(--ink)}
.head{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;
flex-wrap:wrap;padding-bottom:18px}
h1{margin:0;font:600 30px/1.18 Georgia,"Times New Roman",serif;letter-spacing:-.022em}
h2{font:600 20px/1.3 Georgia,"Times New Roman",serif;letter-spacing:-.015em;margin:0}
h3{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);
font-weight:600;margin:0 0 10px}
.lede{color:var(--soft);font-size:14.5px;margin-top:8px;max-width:760px}
.lede b{color:var(--ink);font-weight:600}
.acts{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
section.s{padding:26px 0 0;margin-top:26px;border-top:1px solid var(--rule)}
section.s:first-of-type{border-top:0;margin-top:0}
.s-head{display:flex;justify-content:space-between;align-items:baseline;gap:16px;
margin-bottom:12px}
.s-head .more{font-size:13px;color:var(--soft)}
.muted{color:var(--faint)}
.soft{color:var(--soft)}
.ok{color:var(--ok)} .bad{color:var(--red-hi)} .warnc{color:var(--amber)}
.small{font-size:12.5px}

/* ---- numbers in a row, separated by hairlines rather than boxes */
.strip{display:flex;flex-wrap:wrap;gap:0;margin:4px 0 6px}
.strip a,.strip div.k{display:block;padding:4px 26px 6px 0;margin-right:26px;
border-right:1px solid var(--rule);text-decoration:none;min-width:90px}
.strip > :last-child{border-right:0}
.strip .n{font:600 25px/1.15 "Segoe UI",system-ui,sans-serif;letter-spacing:-.02em;
color:var(--ink);font-variant-numeric:tabular-nums lining-nums}
.strip .l{font-size:11.5px;color:var(--faint);margin-top:2px}
.strip a:hover .n{color:var(--red-hi)}
.strip a.on .n{color:var(--red-hi)}

/* ---- controls */
form.filters{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;padding:14px 0;
border-top:1px solid var(--rule);border-bottom:1px solid var(--rule);margin:8px 0 4px}
label{display:flex;flex-direction:column;gap:5px;font-size:10.5px;color:var(--faint);
text-transform:uppercase;letter-spacing:.09em}
select,input[type=text],input[type=search],input[type=password],textarea{
font:13.5px "Segoe UI",sans-serif;padding:7px 10px;border:1px solid var(--rule2);
border-radius:8px;background:var(--bg);color:var(--ink);min-width:118px}
input[type=search]{min-width:200px}
select:focus,input:focus,textarea:focus{outline:none;border-color:var(--red);
box-shadow:0 0 0 2px var(--red-bg)}
textarea{width:100%;min-height:96px;resize:vertical}
button,.btn{font:13.5px "Segoe UI",sans-serif;padding:7px 14px;border:1px solid var(--rule2);
border-radius:8px;background:var(--raise2);color:var(--ink);cursor:pointer;
text-decoration:none;display:inline-flex;align-items:center;gap:6px;line-height:1.3;
transition:border-color .12s,background .12s}
button:hover,.btn:hover{border-color:var(--faint);background:#39372f;color:var(--ink)}
button.primary,.btn.primary{background:var(--red);color:#fff;border-color:transparent;
font-weight:600}
button.primary:hover,.btn.primary:hover{background:var(--red-hi)}
button.ghost,.btn.ghost{background:transparent;border-color:transparent;color:var(--soft)}
button.ghost:hover,.btn.ghost:hover{background:var(--raise);color:var(--ink)}
button.sm,.btn.sm{padding:3px 10px;font-size:12px;border-radius:7px}
.seg{display:inline-flex;gap:2px;background:var(--raise);border-radius:9px;padding:3px}
.seg a{padding:5px 12px;border-radius:7px;font-size:13px;color:var(--soft);
text-decoration:none}
.seg a:hover{color:var(--ink)}
.seg a.on{background:var(--raise2);color:var(--ink);font-weight:600}

/* ---- tables: no box, rows separated by a hairline */
table{width:100%;border-collapse:separate;border-spacing:0}
th{font-size:10.5px;text-transform:uppercase;letter-spacing:.1em;color:var(--faint);
text-align:left;padding:12px 12px 8px;border-bottom:1px solid var(--rule2);
font-weight:600;white-space:nowrap}
td{padding:12px;border-bottom:1px solid var(--rule);vertical-align:top;font-size:13.5px}
th:first-child,td:first-child{padding-left:2px}
th:last-child,td:last-child{padding-right:2px}
tbody tr{transition:background .1s}
tbody tr:hover td{background:rgba(255,255,255,.022)}
tbody tr.go{cursor:pointer}
td a{text-decoration:none}
.num{text-align:right;font-variant-numeric:tabular-nums}
.firm{font-weight:600}
.meta{color:var(--faint);font-size:12px;margin-top:3px;font-weight:400}
.why{color:var(--soft);font-size:12.5px;line-height:1.45}
.why b{color:var(--ink);font-weight:600}

/* ---- chips and tiers */
.chip{display:inline-flex;align-items:center;gap:6px;font-size:11px;padding:2px 9px;
border-radius:99px;background:var(--raise);color:var(--soft);white-space:nowrap;
line-height:1.5}
.chip.lead{background:var(--ok-bg);color:var(--ok)}
.chip.dis{background:var(--red-bg);color:var(--red-hi)}
.chip.warn{background:var(--amber-bg);color:var(--amber)}
.chip.line{background:transparent;border:1px solid var(--rule2)}
.tier{display:inline-flex;align-items:center;justify-content:center;min-width:24px;
height:22px;padding:0 7px;border-radius:6px;font:700 12px "Segoe UI",sans-serif;
background:var(--raise);color:var(--faint)}
.tier.A{background:var(--ok-bg);color:var(--ok)}
.tier.B{background:var(--raise2);color:var(--ink)}
.tier.C{background:var(--raise);color:var(--soft)}
.score{display:flex;align-items:center;gap:9px;font-variant-numeric:tabular-nums;
font-size:14px;font-weight:600}
.score .b{flex:none;width:54px;height:4px;border-radius:3px;background:var(--rule2);
overflow:hidden}
.score .b i{display:block;height:100%;background:var(--soft)}
.score.A .b i{background:var(--ok)}
.bar{display:inline-block;height:4px;border-radius:3px;background:var(--ok);
vertical-align:middle;margin-right:7px}
.bar.neg{background:var(--red-hi)}

/* ---- notes, empty states, panels */
.note{background:var(--amber-bg);border-left:2px solid var(--amber);
border-radius:0 8px 8px 0;padding:10px 14px;font-size:13px;color:var(--amber);
margin:10px 0}
.note.plain{background:var(--raise);border-left-color:var(--rule2);color:var(--soft)}
.empty{padding:30px 2px;color:var(--faint);font-size:13.5px}
.panel{background:var(--raise);border-radius:14px;padding:16px 18px}
.pager{display:flex;gap:14px;align-items:center;margin-top:16px;font-size:13px;
color:var(--soft)}
abbr{border-bottom:1px dotted var(--faint);cursor:help;text-decoration:none}
details > summary{cursor:pointer;list-style:none}
details > summary::-webkit-details-marker{display:none}
.kv{display:grid;grid-template-columns:170px 1fr;gap:6px 14px;font-size:13.5px;margin:0}
.kv dt{color:var(--faint)}
.kv dd{margin:0}
@media (max-width:980px){body{margin-left:0} nav.side{position:static;width:auto}
 .pg{padding:22px 18px 80px}}
"""

FAVICON = ('<link rel="icon" href="data:image/svg+xml,'
           '%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E'
           '%3Crect width=%2732%27 height=%2732%27 rx=%277%27 fill=%27%23a63232%27/%3E'
           '%3Ctext x=%2716%27 y=%2722%27 font-family=%27Georgia%27 font-size=%2718%27 '
           'fill=%27white%27 text-anchor=%27middle%27%3EB%3C/text%3E%3C/svg%3E">')


# ---- the command palette: firms by name or CRD, and every page by name.
PALETTE_JS = """
<div id="pal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);
z-index:60" onclick="if(event.target.id=='pal')palHide()">
 <div style="max-width:600px;margin:12vh auto 0;background:var(--raise);
 border-radius:14px;overflow:hidden;box-shadow:0 22px 70px rgba(0,0,0,.55)">
  <input id="palq" placeholder="Find a firm by name or CRD, or go to a page"
   style="width:100%;border:0;background:var(--raise2);padding:15px 18px;
   font-size:15.5px;min-width:0;border-radius:0">
  <div id="palr"></div>
  <div style="padding:8px 16px;font-size:11.5px;color:var(--faint)">
   Arrow keys to move, Enter to open, Esc to close</div>
 </div>
</div>
<script>
var PAGES=__PAGES__;
function palShow(){var p=document.getElementById('pal');p.style.display='block';
 var q=document.getElementById('palq');q.value='';palItems=PAGES.slice(0,8);palSel=0;palRender();
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
  if(e.key=='Enter'&&palItems.length){location.href=palItems[palSel].href;}
  return;
 }
 if(e.target.tagName=='INPUT'||e.target.tagName=='TEXTAREA'||e.target.tagName=='SELECT')return;
 if(e.key=='/'){e.preventDefault();palShow();return;}
 if(typeof rowKey=='function')rowKey(e);
});
document.addEventListener('input',function(e){
 if(e.target.id!='palq')return;
 clearTimeout(palT);
 var v=e.target.value, lv=v.toLowerCase();
 var pages=PAGES.filter(function(p){return p.name.toLowerCase().indexOf(lv)>=0;});
 if(v.length<2){palItems=pages.slice(0,8);palSel=0;palRender();return;}
 palT=setTimeout(function(){
  fetch('/api/search?q='+encodeURIComponent(v)).then(function(r){return r.json();})
  .then(function(d){palItems=pages.slice(0,3).concat(d.map(function(x){
    return {name:x.name,href:'/firm/'+encodeURIComponent(x.crd),
            meta:'CRD '+x.crd+' \\u00b7 '+(x.state||'')+' \\u00b7 '+x.raum
                 +(x.best?' \\u00b7 '+x.best:'')};}));palSel=0;palRender();});
 },120);
});
function palEsc(s){var d=document.createElement('span');
 d.textContent=String(s==null?'':s);return d.innerHTML;}
function palRender(){
 /* Every field is escaped before entering innerHTML: firm names come from SEC
    filings, which is still text somebody else typed. */
 document.getElementById('palr').innerHTML=palItems.map(function(x,i){
  return '<a href="'+palEsc(x.href)+'" style="display:flex;justify-content:space-between;'
   +'gap:12px;padding:10px 18px;text-decoration:none;font-size:14px;'
   +(i==palSel?'background:var(--red-bg)':'')+'">'
   +'<span>'+palEsc(x.name)+'</span><span style="color:var(--faint);font-size:12.5px">'
   +palEsc(x.meta||'Page')+'</span></a>';}).join('')
  || '<div style="padding:12px 18px;color:var(--faint);font-size:13px">Nothing matches</div>';
}
function palMove(d){palSel=Math.max(0,Math.min(palItems.length-1,palSel+d));palRender();}
/* Whole table rows open their firm, except when the click lands on a control. */
document.addEventListener('click',function(e){
 var tr=e.target.closest('tr.go'); if(!tr)return;
 if(e.target.closest('a,button,select,input,form,label,summary'))return;
 location.href=tr.dataset.href;
});
</script>
"""


def _palette() -> str:
    import json as _json
    pages = [{"name": "Home", "href": "/"},
             {"name": "Signals", "href": "/signals"},
             {"name": "Firms", "href": "/firms"},
             {"name": "Saved lists", "href": "/saved"},
             {"name": "System", "href": "/health"},
             {"name": "Review queue", "href": "/review"}]
    for k in products.product_keys():
        pages.insert(1 + products.product_keys().index(k),
                     {"name": products.product(k)["name"] + " list",
                      "href": f"/lists/{k}"})
    return PALETTE_JS.replace("__PAGES__", _json.dumps(pages))


_NAV_CACHE: dict = {"t": 0.0}

I = ('fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"'
     ' stroke-linejoin="round"')
ICONS = {
    "home": f'<svg viewBox="0 0 24 24" {I}><path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/></svg>',
    "signals": f'<svg viewBox="0 0 24 24" {I}><path d="M22 12h-4l-3 8-6-16-3 8H2"/></svg>',
    "firms": f'<svg viewBox="0 0 24 24" {I}><path d="M3 21h18"/><path d="M5 21V7l7-4 7 4v14"/><path d="M9 21v-4h6v4"/></svg>',
    "saved": f'<svg viewBox="0 0 24 24" {I}><path d="M6 3h12v18l-6-4-6 4z"/></svg>',
    "system": f'<svg viewBox="0 0 24 24" {I}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
    "search": f'<svg viewBox="0 0 24 24" {I}><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>',
    "out": f'<svg viewBox="0 0 24 24" {I}><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><path d="M10 17l-5-5 5-5"/><path d="M5 12h11"/></svg>',
}
FAMILY_COLOUR = {"PHH": "#c65454", "AcuBooth": "#cfa95c", "Glynac": "#63aa7c"}


def _nav_counts() -> dict:
    """Sidebar counts, cached for a few seconds: they head every page."""
    import time as _time
    if _time.monotonic() - _NAV_CACHE["t"] < 8.0:
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
    tiers: dict = {}
    try:
        for r in c.execute("SELECT product, COUNT(*) n FROM product_score"
                           " WHERE status='scored' AND tier='A' GROUP BY product"):
            tiers[r["product"]] = r["n"]
    except Exception:
        c.rollback()
    out["tierA"] = tiers
    out["signals"] = one("""SELECT COUNT(*) n FROM trigger_event t
        JOIN firm_scope s ON s.crd=t.crd
        LEFT JOIN trigger_action a ON a.trigger_id=t.id
        WHERE t.suppressed=0 AND a.state IS NULL AND t.detected_date >= ?""",
                         (signal_cutoff(),))
    # Only items on tier A firms earn a badge: those are the calls about to be
    # made. The rest wait in the queue on safe defaults.
    hot = "(SELECT crd FROM product_score WHERE status='scored' AND tier='A')"
    out["review"] = (one(f"SELECT COUNT(*) n FROM adv_13f_match WHERE status='review'"
                         f" AND crd IN {hot}")
                     + one(f"SELECT COUNT(*) n FROM brochure_negation WHERE status='open'"
                           f" AND crd IN {hot}"))
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


def nav(active: str) -> str:
    n = _nav_counts()
    user = current_user()

    def item(key, href, label, cnt=None, hot=False, icon=None, dot=None):
        ic = ICONS.get(icon or key, "")
        if dot:
            ic = f'<span class="dot" style="background:{dot}"></span>'
        c = (f'<span class="cnt{" hot" if hot else ""}">{cnt:,}</span>'
             if cnt else "")
        return (f'<a class="i{" on" if key == active else ""}" href="{href}">'
                f'{ic}{esc(label)}{c}</a>')

    plist = "".join(
        item(f"list:{k}", f"/lists/{k}", products.product(k)["name"],
             n["tierA"].get(k), hot=True,
             dot=FAMILY_COLOUR.get(products.product(k)["family"], "#888"))
        for k in products.product_keys())
    who = ""
    if user:
        who = (f'<div class="me">{esc(auth.display_name(user))}'
               f'<form method="post" action="/logout">'
               f'<button type="submit" title="Sign out">{ICONS["out"]}</button>'
               f'</form></div>')
    quit_link = "" if MANAGED else f'<a href="/quit">Quit {APP_NAME}</a><br>'
    feed = esc(n["feed"]) if n.get("feed") else "none yet"
    return (FAVICON + _palette() +
            '<nav class="side">'
            f'<a class="brand" href="/"><div class="mark">B</div><div class="t">{APP_NAME}'
            '<small>GTM intelligence</small></div></a>'
            f'<button class="find" type="button" onclick="palShow()">{ICONS["search"]}'
            'Find a firm<kbd>Ctrl K</kbd></button>'
            + item("home", "/", "Home")
            + '<div class="grp">Product lists</div>' + plist
            + '<div class="grp">Work</div>'
            + item("signals", "/signals", "Signals", n.get("signals"))
            + item("firms", "/firms", "Firms")
            + item("saved", "/saved", "Saved lists")
            + '<div class="grp">Data</div>'
            + item("system", "/health", "System", n.get("review"))
            + f'<div class="foot">{who}{quit_link}SEC feed of {feed}</div></nav>')


def page(title: str, active: str, body: str, css: str = "", js: str = "",
         status: int = 200) -> HTMLResponse:
    """Every screen goes through here: one head, one sidebar, one stylesheet."""
    return HTMLResponse(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{esc(title)} . {APP_NAME}</title><style>{PAGE_CSS}{css}</style></head>'
        f'<body>{nav(active)}{body}{("<script>" + js + "</script>") if js else ""}'
        f'</body></html>', status_code=status)


def caveat(key: str, text: str) -> str:
    return f'<abbr title="{esc(CAVEATS[key].strip())}">{text}</abbr>'


def tier_chip(tier: str | None, title: str = "") -> str:
    t = tier or "-"
    cls = t if t in ("A", "B", "C") else ""
    return f'<span class="tier {cls}" title="{esc(title)}">{esc(t)}</span>'


def score_cell(score, tier) -> str:
    if score is None:
        return '<span class="muted">-</span>'
    w = max(0, min(100, float(score)))
    cls = tier if tier in ("A", "B", "C") else ""
    return (f'<div class="score {cls}"><span>{score:.0f}</span>'
            f'<span class="b"><i style="width:{w:.0f}%"></i></span></div>')


# --- who is signed in -----------------------------------------------------
# Deny by default: the middleware requires a session for every path that is not
# explicitly public, so a route added later is protected without anyone having
# to remember to protect it.

LOGIN_CSS = """
.signin{max-width:360px;margin:0 auto;padding:90px 24px 40px}
.signin .mk{width:48px;height:48px;border-radius:13px;background:var(--red);
color:#fff;display:flex;align-items:center;justify-content:center;
margin:0 auto 20px;font:700 24px Georgia,serif}
.signin h1{font-size:27px;text-align:center;margin:0 0 6px}
.signin .sub{text-align:center;color:var(--soft);font-size:13.5px;margin:0 0 26px}
.signin label{margin-bottom:14px}
.signin input{width:100%;min-width:0;padding:10px 12px;font-size:14px}
.signin button{width:100%;padding:10px;margin-top:6px;font-size:14px;justify-content:center}
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
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in to {APP_NAME}</title>{FAVICON}
<style>{PAGE_CSS}{LOGIN_CSS}body{{margin:0}}</style>
<div class="signin">
<div class="mk">B</div>
<h1>{APP_NAME}</h1>
<p class="sub">Who to call, why, and what to say</p>
{err}
<form method="post" action="/login">
<input type="hidden" name="next" value="{esc(nxt)}">
<label>Username<input type="text" name="username" autocomplete="username" autofocus
 required></label>
<label>Password<input name="password" type="password"
 autocomplete="current-password" required></label>
<button class="primary" type="submit">Sign in</button>
</form>
<p class="hint">Your name is what marks the firms you own and the reviews you
clear, so a shared queue stays honest about who did what.</p>
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
    # Only ever redirect somewhere inside the app.
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
    """Firm lookup for the palette. Read-only, tiny payload, best list shown."""
    if len(q.strip()) < 2:
        return JSONResponse([])
    c = conn()
    rows = c.execute("""
        SELECT f.crd, f.legal_name, f.state, f.raum, s.best_product, s.best_tier
        FROM firm_current f LEFT JOIN firm_scope s ON s.crd=f.crd
        WHERE (f.legal_name ILIKE ? OR f.business_name ILIKE ? OR f.crd = ?)
        ORDER BY (s.crd IS NULL), s.priority DESC, f.raum DESC NULLS LAST LIMIT 9""",
        (f"%{q.strip()}%", f"%{q.strip()}%", q.strip())).fetchall()
    c.close()
    out = []
    for r in rows:
        best = ""
        if r["best_product"]:
            best = f"{products.product(r['best_product'])['name']} tier {r['best_tier']}"
        out.append({"crd": r["crd"], "name": r["legal_name"], "state": r["state"],
                    "raum": money(r["raum"]), "best": best})
    return JSONResponse(out)


@app.post("/watch/{crd}")
def watch_toggle(crd: str, back: str = Form("/")):
    if not back.startswith("/"):
        back = "/"
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
    ok_page = page in ("signals", "firms") or (
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
    return f"/firms?{v['qs']}"


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.post("/admin/run-weekly")
def run_weekly_now():
    """Kick the weekly cycle from the UI. One at a time; progress lands in
    run_log within seconds and is visible on System."""
    c = conn()
    busy = c.execute("SELECT COUNT(*) n FROM run_log WHERE status='running'"
                     " AND started_at::timestamptz > NOW() - INTERVAL '2 hours'").fetchone()["n"]
    c.close()
    if busy:
        return RedirectResponse("/health?msg=already-running", status_code=303)
    log = open(config.DATA_DIR / "weekly.log", "ab")
    subprocess.Popen(
        [sys.executable, "-m", "scripts.run_weekly", "--brochure-slice", "120"],
        cwd=str(config.ROOT), stdout=log, stderr=log, creationflags=procs.SPAWN_FLAGS)
    return RedirectResponse("/health?msg=started", status_code=303)


@app.post("/admin/rescore")
def rescore_now():
    """Recompute every product list now, from what is already held. Seconds,
    not minutes: useful right after a config change or a burst of reviews."""
    log = open(config.DATA_DIR / "weekly.log", "ab")
    subprocess.Popen([sys.executable, "-m", "scripts.score_products"],
                     cwd=str(config.ROOT), stdout=log, stderr=log,
                     creationflags=procs.SPAWN_FLAGS)
    return RedirectResponse("/health?msg=rescoring", status_code=303)


AUTOPILOT_KINDS = ("brochures", "brochure_retag", "firm_refresh", "contact_extract",
                   "web_enrich", "mail_platform", "infer_emails", "email_verify",
                   "cusip_verify")


@app.post("/admin/task/{kind}/{action}")
def task_control(kind: str, action: str):
    """Start or pause an autopilot job. Start also launches the worker process
    if none is alive; Pause takes effect within one slice."""
    if kind not in AUTOPILOT_KINDS or action not in ("start", "pause"):
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
    return RedirectResponse("/health#jobs", status_code=303)


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
    c = conn()
    try:
        jobs = c.execute("SELECT kind FROM auto_task WHERE desired_state='running'"
                         ).fetchall()
    except Exception:
        jobs = []
    c.close()
    note = ""
    if jobs:
        names = ", ".join(j["kind"].replace("_", " ") for j in jobs)
        note = (f'<p>Background work is running right now ({esc(names)}). It stops '
                f'too, and picks up where it left off next time.</p>')
    return page(f"Quit {APP_NAME}", "quit", f"""<div class="quitbox">
<div class="mk">B</div>
<h1>Quit {APP_NAME}?</h1>
<p>Everything is already saved. Nothing is lost by quitting.</p>
{note}
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
<style>{PAGE_CSS}{QUIT_CSS}
body{{margin:0}}</style>
<div class="quitbox">
<div class="mk">B</div>
<h1>{APP_NAME} has stopped</h1>
<p>You can close this tab. Everything is saved.</p>
<p class="hint">It will be running again the next time you sign in to Windows.
To start it right now, open the {APP_NAME} shortcut on your desktop.</p>
</div>""")


from . import (firm_view, firms_view, home_view, list_view,  # noqa: E402
               lists_view, review_view, signals_view)

app.include_router(home_view.router)      # /
app.include_router(lists_view.router)     # /lists/{product}
app.include_router(signals_view.router)   # /signals
app.include_router(firms_view.router)     # /firms, /saved, exports
app.include_router(firm_view.router)      # /firm/{crd}
app.include_router(list_view.router)      # /health (System)
app.include_router(review_view.router)    # /review


# Old URLs, redirected to their new home so bookmarks keep working.
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
