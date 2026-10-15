"""Settings: admins only. Everything that configures Bellwether, in one place.

  Overview      the state of every integration at a glance, with what to do next
  Users         who has access and in which seat: admin, product owner, user
  Sign-in       Microsoft sign-in (the main way in) and password fallback
  AI            the model provider behind Bellwether AI, its key and daily limit
  Verification  how email checking talks to mail servers, and whether it can
  Crawling      how firm websites and directories are read
  Firm types    what kind of firm each adviser is (independent RIA, custodian,
                wirehouse, asset manager ...): counts, spot checks, corrections
                by hand, Reclassify all and Rescore all
  Industry knowledge  the known names, firm-type wording, glossary and Acumen's
                own purpose, which the classifier, scores and Bellwether AI read
  Jobs          every background job: running by itself, with Run now and Pause
  System        data freshness, run history, snapshots, record counts
  Review queue  the few decisions that need a person

The middleware refuses every /settings path to anyone who is not an admin, so
nothing here re-checks the role; the forms still validate every value.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import date, datetime, timezone

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import ai, auth, jobs, msauth, products, settings, ui, users
from .webapp import (MANAGED, conn, current_owner, esc, page, public_base, qs_join,
                     start_weekly)

router = APIRouter()

TABS = [("overview", "Overview", "/settings"), ("users", "Users", "/settings/users"),
        ("signin", "Sign-in", "/settings/signin"), ("ai", "AI", "/settings/ai"),
        ("verify", "Verification", "/settings/verify"), ("crawl", "Crawling", "/settings/crawl"),
        ("firmtypes", "Firm types", "/settings/firmtypes"),
        ("knowledge", "Industry knowledge", "/settings/knowledge"),
        ("jobs", "Jobs", "/settings/jobs"), ("system", "System", "/settings/system"),
        ("review", "Review queue", "/settings/review")]


def settings_tabs(active: str) -> str:
    return ('<div class="seg set-tabs">' + "".join(
        f'<a class="{"on" if k == active else ""}" href="{href}">{esc(label)}</a>'
        for k, label, href in TABS) + '</div>')


def how(title: str, body: str = "", points: list | None = None, point_head: str = "",
        foot: str = "", label: str = "How it works") -> str:
    """A quiet "How it works" at the end of a lede or beside a heading. The
    explanation lives in its hover, so the page itself stays short."""
    return f' <span class="set-how"{ui.tip_attr(title, body, points, point_head, foot)}>{esc(label)}</span>'


def _frame(title: str, tab: str, lede: str, inner: str, msg: str = "", err: str = "") -> HTMLResponse:
    flash = ""
    if msg:
        flash = f'<div class="note good">{esc(msg)}</div>'
    if err:
        flash = f'<div class="note bad">{esc(err)}</div>'
    body = (f'<div class="pg"><div class="head"><div><h1>{esc(title)}</h1>'
            f'<div class="lede">{lede}</div></div></div>{settings_tabs(tab)}{flash}'
            f'<div class="set-body">{inner}</div></div>')
    return page(title, "settings", body)


CHOICE_LABEL = {"none": "Not connected", "anthropic": "Anthropic (Claude)", "edenai": "Eden AI",
                "openai": "OpenAI-compatible", "auto": "Automatic", "reacher": "Reacher server",
                "native": "Built-in SMTP check", "dns": "Domain check only", "on": "On",
                "off": "Off", "never": "Never"}


# The few field explanations too long to read at a glance: a short line under
# the field, with the full explanation behind "More".
FIELD_SHORT = {
    "app.public_url": "Leave blank to use the address the browser used.",
    "ai.provider": "Eden AI reaches many model vendors through one key.",
    "ai.model_smart": "Leave blank for the provider default (Claude Opus 5.5).",
    "verify.per_minute": "A cap across all mail servers; it slows down by itself when servers push back.",
    "crawl.contact_retry_days": "How long web and AI research wait before looking again for a missing contact.",
    "ocr.base_url": "Leave blank to read scans with Tesseract on this server.",
}
# Help that names a stored value ("auto") rather than what the menu shows.
FIELD_HELP = {
    "verify.engine": "Automatic uses a Reacher server when one answers, else the built-in SMTP check.",
    "crawl.use_browser": "Automatic renders a page in a headless browser only when plain fetching "
                         "returns an empty shell.",
}


def _field(key: str) -> str:
    """One setting as a form field, from its spec. Secrets are write-only."""
    s = settings.BY_KEY[key]
    pinned = settings.pinned(key)
    dis = " disabled" if pinned else ""
    full = FIELD_HELP.get(key, s.help)
    short = FIELD_SHORT.get(key)
    hint = (f'<div class="meta">{esc(short)}{how(s.label, full, label="More")}</div>' if short else
            f'<div class="meta">{esc(full)}</div>' if full else "")
    pin = ('<div class="meta warnc" style="text-transform:none;letter-spacing:0">Fixed by the '
           f'server environment ({esc(s.env)}); change it there.</div>' if pinned else "")
    if s.secret:
        have = settings.is_set(key)
        ctl = (f'<input type="password" name="{esc(key)}" autocomplete="new-password" '
               f'placeholder="{"Saved; type to replace" if have else "Paste it here"}"{dis}>'
               + (f'<label class="inline small" style="margin-top:4px"><input type="checkbox" '
                  f'name="clear:{esc(key)}" value="1"> Remove the saved value</label>'
                  if have and not pinned else ""))
    elif s.choices:
        cur = settings.get(key)
        ctl = (f'<select name="{esc(key)}"{dis}>' + "".join(
            ui.opt(ch, cur, CHOICE_LABEL.get(ch, ch)) for ch in s.choices) + '</select>')
    else:
        ctl = (f'<input type="text" name="{esc(key)}" value="{esc(settings.get(key, ""))}"'
               f' placeholder="{esc(s.default)}"{dis} style="width:340px;max-width:100%;min-width:0">')
    return (f'<label style="margin-bottom:16px;max-width:640px">{esc(s.label)}{ctl}{hint}{pin}'
            f'</label>')


def _group_form(group: str, keys: list[str] | None = None, extra: str = "") -> str:
    keys = keys or [s.key for s in settings.SPECS if s.group == group]
    fields = "".join(_field(k) for k in keys)
    return (f'<form method="post" action="/settings/save"><input type="hidden" name="_group" '
            f'value="{esc(group)}">{fields}{extra}<button class="primary" type="submit">Save</button>'
            f'</form>')


@router.post("/settings/save")
async def settings_save(request: Request):
    form = await request.form()
    group = form.get("_group") or ""
    back = {"signin": "/settings/signin", "ai": "/settings/ai", "verify": "/settings/verify",
            "crawl": "/settings/crawl"}.get(group, "/settings")
    who = current_owner()
    try:
        for s in settings.SPECS:
            if s.group != group or settings.pinned(s.key):
                continue
            if s.secret:
                if form.get(f"clear:{s.key}"):
                    settings.set(s.key, None, who)
                    continue
                v = (form.get(s.key) or "").strip()
                if v:
                    settings.set(s.key, v, who)
                continue
            if s.key == "ai.features":
                v = ",".join(f for f in ai.FEATURES if form.get(f"feature:{f}"))
                settings.set(s.key, v or "none", who)
                continue
            if s.key in form:
                settings.set(s.key, (form.get(s.key) or "").strip(), who)
    except ValueError as e:
        return RedirectResponse(f"{back}?{qs_join(err=str(e))}", status_code=303)
    return RedirectResponse(f"{back}?{qs_join(msg='Saved.')}", status_code=303)


# ------------------------------------------------------------------ overview

STATUS_CHIP = {"ok": ("good", "Working"), "look": ("warn", "Needs a look"),
               "bad": ("bad", "Not working"), "off": ("line", "Not set up")}


def _status_row(name: str, state: str, text: str, href: str, word: str = "") -> str:
    cls, default = STATUS_CHIP[state]
    return (f'<tr class="go" data-href="{href}"><td class="set-ov-name"><b>{esc(name)}</b></td>'
            f'<td><div class="status-cell"><span class="chip {cls}">{esc(word or default)}</span>'
            f'<span>{text}</span></div></td>'
            f'<td class="num"><a href="{href}">Open</a></td></tr>')


ENGINE_LABEL = {"reacher": "Reacher server", "native": "built-in mail server check",
                "dns": "domain check only"}


def _cap(s: str | None) -> str:
    """First letter up, the rest as written: for status lines stored lower case."""
    s = (s or "").strip().rstrip(".")
    return s[:1].upper() + s[1:]


@router.get("/settings", response_class=HTMLResponse)
def overview(msg: str = Query("")):
    rows = []
    ms = msauth.configured()
    pw = settings.get_bool("auth.password_login")
    rows.append(_status_row(
        "Microsoft sign-in", "ok" if ms else "off",
        ("People sign in with their Microsoft account." if ms else
         "People sign in with Bellwether passwords until it is.")
        + ("" if pw or not ms else " Password sign-in is off."), "/settings/signin"))
    us = users.all_users()
    n_admin = sum(1 for u in us if u["role"] == "admin")
    rows.append(_status_row("Users", "ok", f"{ui.plural(len(us), 'account')} &middot; "
                            f"{ui.plural(n_admin, 'admin')}", "/settings/users", word="Active"))
    st = ai.status()
    rows.append(_status_row(
        "AI provider", "ok" if st["configured"] else "off",
        (f"{esc(st['provider'])}, {esc(st['model'])} &middot; {st['used_today']:,} of "
         f"{st['limit']:,} calls used today" if st["configured"] else
         "Bellwether AI works as a firm finder until one is connected."), "/settings/ai"))
    try:
        from . import verify
        es = verify.engine_status_cached()
        if es is None:
            rows.append(_status_row("Email verification", "look",
                                    "Testing the mail connection now; refresh in a few seconds.",
                                    "/settings/verify", word="Testing"))
        else:
            good = es["resolved"] in ("reacher", "native")
            txt = (f"Checking mailboxes with the {esc(ENGINE_LABEL.get(es['resolved'], es['resolved']))}."
                   if good else "Domain checks only: outbound port 25 is closed and no Reacher server is set.")
            rows.append(_status_row("Email verification", "ok" if good else "look", txt,
                                    "/settings/verify"))
    except Exception:
        rows.append(_status_row("Email verification", "bad", "The verification module did not load.",
                                "/settings/verify"))
    try:
        from . import crawl
        b = crawl.browser_available() if hasattr(crawl, "browser_available") else None
        rows.append(_status_row("Website reading", "ok",
                                "JavaScript pages are rendered in a headless browser." if b else
                                "JavaScript-only pages are skipped: no headless browser is installed.",
                                "/settings/crawl"))
    except Exception:
        rows.append(_status_row("Website reading", "look", "The website reader did not load.",
                                "/settings/crawl"))
    c = conn()
    try:
        sm = jobs.summary(c)
    except Exception:
        c.rollback()
        sm = {"jobs": 0, "running": 0, "paused": 0}
    from . import health
    open_alerts = health.alerts(c)
    run, paused = sm["running"], sm["paused"]
    bad = [a for a in open_alerts if a["level"] == "bad"]
    if open_alerts:
        txt = (f"{len(open_alerts)} need{'s' if len(open_alerts) == 1 else ''} a person: "
               + esc("; ".join(a["title"] for a in open_alerts[:3])) + ".")
    else:
        txt = (f"{sm['jobs']} jobs &middot; {run} working now"
               f"{' &middot; ' + str(paused) + ' paused' if paused else ''} &middot; nothing needs you")
    rows.append(_status_row("Background jobs", "bad" if bad else ("look" if open_alerts else "ok"),
                            txt, "/settings/jobs"))
    feed = c.execute("SELECT published_at FROM snapshot WHERE source_key='adv_feed'"
                     " ORDER BY id DESC LIMIT 1").fetchone()
    sched = None
    try:
        sched = c.execute("SELECT * FROM scheduler_state WHERE id=1").fetchone()
    except Exception:
        c.rollback()
    c.close()
    fresh_state, fresh_word, fresh_txt = "look", "No feed yet", "No SEC feed captured yet."
    if feed:
        try:
            m, d_, y = feed["published_at"].split("/")
            age = (date.today() - date(int(y), int(m), int(d_))).days
            fresh_state, fresh_word = ("ok", "Current") if age <= 10 else ("bad", "Out of date")
            fresh_txt = (f"SEC adviser feed published {esc(feed['published_at'])}, "
                         f"{'today' if age <= 0 else ui.plural(age, 'day') + ' ago'}.")
        except Exception:
            pass
    if sched and sched["message"]:
        fresh_txt += f' <span class="muted">{esc(_cap(sched["message"]))}.</span>'
    rows.append(_status_row("Data freshness", fresh_state, fresh_txt, "/settings/system",
                            word=fresh_word))
    inner = f'<table class="set-ov"><tbody>{"".join(rows)}</tbody></table>'
    return _frame("Settings", "overview",
                  "What each part of Bellwether is doing right now. Admins only.", inner, msg=msg)


# ------------------------------------------------------------------ users

@router.get("/settings/users", response_class=HTMLResponse)
def users_page(msg: str = Query(""), err: str = Query("")):
    us = users.all_users()
    rows = []
    for u in us:
        fam = "".join(
            f'<label class="inline small"><input type="checkbox" name="fam" value="{f}"'
            f'{" checked" if f in u["families"] else ""}> {f}</label>' for f in users.FAMILIES)
        listed = settings.get_list("auth.admins")
        boot = (u["login"] in listed) or ((u.get("email") or "") in listed)
        always_tip = ui.tip_attr("Admin, always", "On the always-admin list in Sign-in, so this "
                                 "seat cannot be changed here.")
        role_sel = (f'<span class="chip"{always_tip}>Admin, always</span>'
                    if boot else
                    f'<select name="role">{"".join(ui.opt(r, u["role"], users.ROLE_LABEL[r]) for r in users.ROLES)}</select>')
        via = "Microsoft" if u.get("ms_oid") else ("Password" if u.get("password_hash") else "Not signed in yet")
        rows.append(
            f'<tr><td><b>{esc(u["name"])}</b><div class="meta">{esc(u.get("email") or u["login"])}</div></td>'
            f'<td><form method="post" action="/settings/users/save" class="row">'
            f'<input type="hidden" name="login" value="{esc(u["login"])}">{role_sel}'
            f'<span class="row" title="For product owners: which products they own">{fam}</span>'
            f'<label class="inline small"><input type="checkbox" name="active" value="1"'
            f'{" checked" if u.get("active") else ""}> Active</label>'
            f'<button class="sm" type="submit">Save</button></form></td>'
            f'<td class="small">{esc(via)}<div class="meta">{("Signed in " + esc(ui.ago(u.get("last_login_at")))) if u.get("last_login_at") else ("" if via.startswith("Not") else "Never signed in")}</div></td>'
            f'<td class="num"><form method="post" action="/settings/users/delete" '
            f'onsubmit="return confirm(\'Remove this account? Their notes and ownership stay.\')">'
            f'<input type="hidden" name="login" value="{esc(u["login"])}">'
            f'<button class="sm ghost" type="submit">Remove</button></form></td></tr>')
    seat_tip = ui.tip_attr(
        "Seats", "What each person may change.",
        ["Admin: everything, including Settings",
         "Product owner: the scoring of the products ticked, and enrichment sources",
         "User: works the lists"],
        foot="Active off keeps the account but stops it signing in.")
    add = f"""<section class="s"><h2>Add someone</h2>
<p class="lede">Microsoft users get an account when they first sign in; add them here to choose
their seat first.</p>
<form method="post" action="/settings/users/add" class="filters">
<label>Email or username<input type="text" name="login" required placeholder="jane.doe@acumen-strategy.com"></label>
<label>Name<input type="text" name="name" required></label>
<label>Seat<select name="role">{"".join(ui.opt(r, "user", users.ROLE_LABEL[r]) for r in users.ROLES)}</select></label>
<label>Owns<span class="row">{"".join(f'<label class="inline small"><input type="checkbox" name="fam" value="{f}"> {f}</label>' for f in users.FAMILIES)}</span></label>
<label>Password, only without Microsoft<input type="password" name="password" autocomplete="new-password"></label>
<button class="primary" type="submit">Add</button></form></section>"""
    inner = (f'<table class="set-stack"><thead><tr><th>Person</th><th><span class="set-tipped"{seat_tip}>Seat and '
             f'access</span></th><th>Sign-in</th><th></th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table>{add}')
    return _frame("Users and roles", "users", "Who can use Bellwether, and what each person may change.",
                  inner, msg, err)


@router.post("/settings/users/save")
async def users_save(request: Request):
    form = await request.form()
    login = (form.get("login") or "").strip().lower()
    role = form.get("role")
    fams = form.getlist("fam")
    me = users.get(current_owner_login())
    if login == (me or {}).get("login") and role and role != "admin":
        return RedirectResponse("/settings/users?err=You+cannot+remove+your+own+admin+seat.",
                                status_code=303)
    try:
        users.save(login, role=role if role in users.ROLES else None, products=list(fams),
                   active=bool(form.get("active")))
    except ValueError as e:
        return RedirectResponse(f"/settings/users?{qs_join(err=str(e))}", status_code=303)
    return RedirectResponse("/settings/users?msg=Saved.", status_code=303)


def current_owner_login() -> str:
    from .webapp import current_user
    return current_user() or ""


@router.post("/settings/users/add")
async def users_add(request: Request):
    form = await request.form()
    login = (form.get("login") or "").strip().lower()
    name = (form.get("name") or "").strip()
    role = form.get("role") or "user"
    pw = form.get("password") or ""
    if not login or not name:
        return RedirectResponse("/settings/users?err=Give+an+email+or+username+and+a+name.",
                                status_code=303)
    if pw and len(pw) < 12:
        return RedirectResponse("/settings/users?err=Passwords+need+at+least+12+characters.",
                                status_code=303)
    if users.get(login):
        return RedirectResponse("/settings/users?err=That+account+already+exists.", status_code=303)
    users.create(login, name, role=role, password_hash=auth.hash_password(pw) if pw else None,
                 email=login if "@" in login else None, products=form.getlist("fam"))
    return RedirectResponse("/settings/users?msg=Added.", status_code=303)


@router.post("/settings/users/delete")
def users_delete(login: str = Form(...)):
    if login.strip().lower() == current_owner_login():
        return RedirectResponse("/settings/users?err=You+cannot+remove+yourself.", status_code=303)
    users.delete(login)
    return RedirectResponse("/settings/users?msg=Removed.", status_code=303)


# ------------------------------------------------------------------ sign-in

@router.get("/settings/signin", response_class=HTMLResponse)
def signin_page(request: Request, msg: str = Query(""), err: str = Query("")):
    base = public_base(request)
    uri = msauth.redirect_uri(base)
    ok = msauth.configured()
    steps = f"""<details class="panel set-steps"{"" if ok else " open"}><summary>Setting it up, once</summary>
<ol class="small soft">
<li>In the Microsoft Entra admin centre, open App registrations, then New registration. Name it
Bellwether; supported account types: this organisation only.</li>
<li>Redirect URI: platform <b>Web</b>, address <b style="color:var(--ink)" class="mono">{esc(uri)}</b></li>
<li>From the app's Overview, copy the Directory (tenant) ID and the Application (client) ID into
the fields below.</li>
<li>Certificates &amp; secrets, New client secret. Copy its <b>Value</b> into the secret field.</li>
<li>Save, then <a href="/auth/microsoft" data-noprefetch>try signing in with Microsoft</a>. Once it works, you can
turn password sign-in off.</li></ol></details>"""
    state = ('<div class="note good">Microsoft sign-in is set up.</div>' if ok else
             '<div class="note plain">Microsoft sign-in is not set up yet, so password sign-in stays on.'
             '</div>')
    inner = state + steps + _group_form("signin")
    return _frame("Sign-in", "signin", "How people get into Bellwether. Microsoft is the main way in.",
                  inner, msg, err)


# ------------------------------------------------------------------ AI

@router.get("/settings/ai", response_class=HTMLResponse)
def ai_page(msg: str = Query(""), err: str = Query("")):
    feats = settings.get_list("ai.features")
    checks = "".join(
        f'<label class="inline set-feat"><input type="checkbox" name="feature:{k}" value="1"'
        f'{" checked" if k in feats else ""}><span><b>{esc(k.capitalize())}</b> <span class="muted small">{esc(v)}</span></span></label>'
        for k, v in ai.FEATURES.items())
    keys = [k for k in ("ai.provider", "ai.api_key", "ai.base_url", "ai.model_smart",
                        "ai.model_fast", "ai.daily_limit")]
    form = _group_form("ai", keys, extra=f'<fieldset class="set-feats"><legend>What it may do</legend>'
                                         f'{checks}</fieldset>')
    st = ai.status()
    c = conn()
    usage = c.execute("""SELECT feature, COUNT(*) n, SUM(ok) ok, SUM(COALESCE(in_tokens,0)) tin,
        SUM(COALESCE(out_tokens,0)) tout FROM ai_call WHERE at >= ? GROUP BY feature
        ORDER BY n DESC""", ((date.today().replace(day=1)).isoformat(),)).fetchall()
    errs = c.execute("SELECT at, feature, error FROM ai_call WHERE ok=0 ORDER BY id DESC LIMIT 5").fetchall()
    c.close()
    use_html = "".join(f'<tr><td>{esc((r["feature"] or "").capitalize())}</td><td class="num">{r["n"]:,}</td>'
                       f'<td class="num">{(r["ok"] or 0):,}</td><td class="num">{(r["tin"] or 0):,}</td>'
                       f'<td class="num">{(r["tout"] or 0):,}</td></tr>' for r in usage)
    err_html = "".join(f'<div class="gline"><span class="muted">{esc(ui.ago(e["at"]))}</span> '
                       f'{esc(e["feature"])}: {esc(e["error"])}</div>' for e in errs)
    status = (f'<div class="note good set-status">Connected: {esc(st["provider"])}, {esc(st["model"])} '
              f'&middot; {st["used_today"]:,} of {st["limit"]:,} calls used today'
              if st["configured"] else
              '<div class="note plain set-status">No AI provider is connected')
    status += ('<button class="sm" data-post="/settings/ai/test" data-busy="Testing">'
               'Test the connection</button></div>')
    tip = how("Bellwether AI",
              "Answers questions about firms and the market, writes a brief on each firm page, reads "
              "team pages the rules could not parse and tidies titles. Everything it writes is "
              "labelled as AI and grounded in Bellwether's own data.",
              ["Anthropic: Claude, default model claude-opus-5-5",
               "Eden AI: one key for many vendors, models named like anthropic/claude-opus-5-5",
               "Any OpenAI-compatible endpoint"], "Providers")
    inner = f"""{status}
{form}
<section class="s"><h2>Use this month</h2>
<table class="tight"><thead><tr><th>Feature</th><th class="num">Calls</th><th class="num">Succeeded</th>
<th class="num">Tokens in</th><th class="num">Tokens out</th></tr></thead><tbody>{use_html or '<tr><td colspan=5 class=empty>No calls yet.</td></tr>'}</tbody></table>
{('<h3 class="set-h3">Recent errors</h3>' + err_html) if err_html else ""}</section>"""
    return _frame("AI", "ai", "The model behind Bellwether AI, with a hard daily limit." + tip,
                  inner, msg, err)


@router.post("/settings/ai/test")
def ai_test():
    ok, text = ai.test_connection()
    return JSONResponse({"ok": ok, "message": text})


# ------------------------------------------------------------------ verification

@router.get("/settings/verify", response_class=HTMLResponse)
def verify_page(msg: str = Query(""), err: str = Query(""), refresh: str = Query("")):
    try:
        from . import verify
        es = verify.engine_status(refresh=bool(refresh))
    except Exception as e:
        es = {"configured": settings.get("verify.engine"), "resolved": "dns",
              "port25_ok": None, "port25_detail": f"verification module failed to load: {e}",
              "reacher_url": None, "reacher_ok": None, "reacher_detail": ""}
    p25 = es.get("port25_ok")
    p25_tip = ui.tip_attr(
        "Outbound port 25",
        "Mail servers judge the machine asking. Checks need outbound port 25 and work best from a "
        "server with a fixed address, a reverse DNS name matching the HELO name and an SPF record "
        "on the MAIL FROM domain that allows it.",
        foot='From a home or office connection, Microsoft 365 and others often refuse to answer. '
             'That shows as "Could not tell", never as a bad address.')
    resolved = es.get("resolved") or ""
    # The engine's own detail repeats the state ("outbound port 25 open (x
    # answered)"); keep only what it adds.
    p25_detail = re.sub(r"(?i)^outbound port 25 (is )?(open|closed)[\s:;,]*", "",
                        es.get("port25_detail") or "").strip()
    if p25_detail.startswith("(") and p25_detail.endswith(")"):
        p25_detail = p25_detail[1:-1]
    es = dict(es, port25_detail=p25_detail,
              reacher_detail=es.get("reacher_detail") if es.get("reacher_url") else "")
    status = (f'<table class="tight set-kv"><tbody>'
              f'<tr><td class="set-kv-k">Engine in use</td><td><b>{esc(CHOICE_LABEL.get(resolved, resolved))}</b> '
              f'<span class="muted">(setting: {esc(CHOICE_LABEL.get(es.get("configured") or "", es.get("configured") or ""))})</span></td></tr>'
              f'<tr><td><span class="set-tipped"{p25_tip}>Outbound port 25</span></td>'
              f'<td>{"<span class=ok>Open</span>" if p25 else "<span class=bad>Closed</span>" if p25 is False else "Not tested"}'
              f' <span class="muted small">{esc(es.get("port25_detail") or "")}</span></td></tr>'
              f'<tr><td>Reacher server</td><td>{esc(es.get("reacher_url") or "Not set")} '
              f'{"<span class=ok>answering</span>" if es.get("reacher_ok") else ("<span class=bad>not answering</span>" if es.get("reacher_ok") is False else "")}'
              f' <span class="muted small">{esc(es.get("reacher_detail") or "")}</span></td></tr>'
              f'</tbody></table><div class="set-actions"><a class="btn sm" href="/settings/verify?refresh=1">Test again</a></div>')
    c = conn()
    counts = c.execute("""SELECT verify_status, COUNT(*) n FROM contact_point WHERE kind='email'
                          GROUP BY verify_status ORDER BY n DESC""").fetchall()
    c.close()
    from . import contacts
    cnt = "".join(f'<tr><td><span class="chip v-{esc(r["verify_status"])}">'
                  f'{esc(contacts.VERIFY_LABEL.get(r["verify_status"], r["verify_status"]))}</span></td>'
                  f'<td class="num">{r["n"]:,}</td></tr>' for r in counts)
    inner = f"""<div class="cols-2 set-cols"><div><h3>Right now</h3>{status}</div>
<div><h3>Addresses by result</h3><table class="tight"><tbody>{cnt or '<tr><td class="empty">No addresses yet.</td></tr>'}</tbody></table></div></div>
<section class="s"><h2>Settings</h2>{_group_form("verify")}</section>"""
    tip = how("How an address is checked",
              "Bellwether asks the mail server that would receive the address whether it would accept "
              "that mailbox, then asks the same about a made-up address at the same domain. Nothing "
              "is sent. Only a server that accepts the real one and turns away the made-up one "
              "counts as verified.",
              foot="The method of the open-source check-if-email-exists (Reacher), built in; a "
                   "Reacher server can be used instead.")
    return _frame("Email verification", "verify",
                  "How Bellwether confirms an email address exists, without sending anything." + tip,
                  inner, msg, err)


# ------------------------------------------------------------------ crawling

@router.get("/settings/crawl", response_class=HTMLResponse)
def crawl_page(msg: str = Query(""), err: str = Query("")):
    b = None
    try:
        from . import crawl
        if hasattr(crawl, "browser_available"):
            b = crawl.browser_available()
    except Exception:
        pass
    browser = ('<div class="note good set-status">A headless browser is installed: JavaScript-only '
               'pages are rendered.</div>' if b else
               '<div class="note plain set-status">No headless browser is installed here, so '
               'JavaScript-only pages are skipped.</div>')
    tip = how("Reading websites",
              "Firm websites and the directories on the Enrichment screen are read with Scrapling, "
              "using a real browser's network fingerprint so ordinary sites answer as they would a "
              "person. Pages that only draw themselves with JavaScript need a headless browser.")
    ocr_tip = how("Brochures without a text layer",
                  "A few brochures come back empty from the PDF reader. Most were typed into the "
                  "SEC's fillable form, and their text is read exactly from the form fields. Scanned "
                  "pages are read with Tesseract on this server's CPU, or with the Unlimited-OCR "
                  "server above when one is set.")
    inner = f"""{browser}
{_group_form("crawl")}
<section class="s"><h2>Brochures without a text layer</h2>
<p class="lede">Scanned brochures are read with OCR.{ocr_tip}</p>
<form method="post" action="/settings/ocr/test" class="set-actions"><button class="sm" type="submit">Test OCR</button></form></section>"""
    return _frame("Crawling", "crawl", "How firm websites, directories and scanned brochures are read."
                  + tip, inner, msg, err)


@router.post("/settings/ocr/test")
def ocr_test():
    from . import ocr
    ok, text = ocr.test_connection()
    key = "msg" if ok else "err"
    return RedirectResponse(f"/settings/crawl?{qs_join(**{key: text})}", status_code=303)


# ------------------------------------------------------------------ jobs

def _cadence(hours: float) -> str:
    """How often a job runs once caught up, in words."""
    if hours < 1:
        return f"every {round(hours * 60)} minutes"
    if hours == 1:
        return "every hour"
    if hours == 24:
        return "daily"
    if hours == 168:
        return "weekly"
    return f"every {hours:g} hours"


def _is_flag(job) -> bool:
    """A backlog query that only says due (1) or not (0), not how many are left."""
    return (job.backlog_sql or "").lstrip().upper().startswith("SELECT CASE")


JOB_CHIP = {"running": ("lead", "Working now"), "queued": ("line", "Catching up"),
            "scheduled": ("line", "Up to date"), "paused": ("", "Paused"), "waiting": ("", "Waiting")}
NEEDS_HREF = {"ai": "/settings/ai", "ocr": "/settings/crawl"}


def _job_row(j: dict, alert: dict | None) -> str:
    """One job: what it is (the name, explained on hover), whether it is working,
    caught up or stuck, how far through its backlog, its last run, and actions."""
    job = j["job"]
    lane = jobs.LANE_LABEL.get(job.lane, job.lane)
    tip = ui.tip_attr(job.label, job.blurb,
                      foot=f"{lane} lane, where jobs take turns. Runs {_cadence(job.every_hours)} "
                           f"once caught up.")
    name = f'<b class="set-tipped"{tip}>{esc(job.label)}</b>'
    if alert:
        state = (f'<span class="chip {"bad" if alert["level"] == "bad" else "warn"}">'
                 f'{"Needs you" if alert["level"] == "bad" else "Check"}</span>')
    else:
        cls, word = JOB_CHIP.get(j["state"], ("", j["state"]))
        state = f'<span class="chip {cls}">{esc(word)}</span>'
    if j["state"] == "running" and j.get("running_since"):
        state += f'<div class="meta">Started {esc(ui.ago(j["running_since"]))}</div>'
    elif j["next_run_at"] and j["state"] == "scheduled":
        # A next run already in the past means the worker will pick it up now.
        nxt = ui.ago(j["next_run_at"])
        nxt = "Due now" if "ago" in nxt or nxt in ("just now", "today") else "Next run " + nxt
        state += f'<div class="meta">{esc(nxt)}</div>'

    prog = ""
    total, done, backlog = j.get("total"), j.get("done"), j.get("backlog")
    left = "" if _is_flag(job) or not backlog else f" &middot; {backlog:,} to go"
    if total and done is not None:
        pct = min(done / total * 100, 100)
        prog = (f'<div class="meter"><i style="width:{pct:.1f}%"></i></div>'
                f'<div class="meta">{done:,} of {total:,} done{left}</div>')
    elif left:
        prog = f'<div class="meta">{backlog:,} to go</div>'
    if job.recent_sql and j.get("recent") is not None:
        prog += f'<div class="meta">Last day: {j["recent"]:,} {esc(job.recent_label)}</div>'

    if j["last_run_at"]:
        bad = j["last_status"] in ("failed", "timeout")
        flag = ""
        if bad:
            word = "Failed" if j["last_status"] == "failed" else "Timed out"
            times = f' {j["fails"]} times in a row' if (j.get("fails") or 0) > 1 else ""
            flag = f' <span class="chip bad">{word}{times}</span>'
        message = (j["message"] or "").strip()
        last = (f'<div class="small">Ran {esc(ui.ago(j["last_run_at"]))}{flag}</div>'
                + (f'<div class="meta set-clamp" title="{esc(message)}">{esc(message[:240])}</div>'
                   if message else ""))
    elif j["why"]:
        href = NEEDS_HREF.get(job.needs.split(":")[0], "")
        last = (f'<div class="meta"><a href="{href}">{esc(j["why"])}</a></div>' if href else
                f'<div class="meta">{esc(j["why"])}</div>')
    else:
        last = '<div class="meta">Not run yet</div>'

    pause = ("resume", "Resume") if j["state"] == "paused" else ("pause", "Pause")
    return (f'<tr><td class="set-job-name">{name}</td><td class="jstate">{state}</td>'
            f'<td class="set-job-prog">{prog}</td><td class="set-job-last">{last}</td>'
            f'<td class="num nowrap"><form method="post" action="/settings/jobs/{esc(job.kind)}/run" style="display:inline">'
            f'<button class="sm" type="submit">Run now</button></form> '
            f'<form method="post" action="/settings/jobs/{esc(job.kind)}/{pause[0]}" style="display:inline">'
            f'<button class="sm ghost" type="submit">{pause[1]}</button></form></td></tr>')


@router.get("/settings/jobs", response_class=HTMLResponse)
def jobs_page(msg: str = Query("")):
    from . import health
    c = conn()
    ov = jobs.overview(c)
    weekly = c.execute("SELECT * FROM scheduler_state WHERE id=1").fetchone()
    open_alerts = health.alerts(c)
    fixes = health.recent_fixes(c)
    try:
        lanes = {r["lane"]: dict(r) for r in c.execute("SELECT * FROM worker_lane")}
    except Exception:
        c.rollback()
        lanes = {}
    c.close()
    alert_of = {a["kind"]: a for a in open_alerts}
    groups: dict = {}
    for j in ov:
        groups.setdefault(j["job"].group, []).append(j)
    sections = []
    for g, items in groups.items():
        rows = "".join(_job_row(j, alert_of.get(j["job"].kind)) for j in items)
        sections.append(f'<section class="s"><h2>{esc(jobs.GROUP_LABEL.get(g, g))}</h2>'
                        f'<table class="jobs-table"><tbody>{rows}</tbody></table></section>')

    wmsg = _cap((weekly["message"] if weekly else "") or "Not checked in yet")
    wlast = ui.ago((weekly or {}).get("last_started")) if weekly else ""
    wtip = ui.tip_attr("Weekly SEC cycle",
                       "Captures the SEC's weekly adviser and individual feeds as soon as they "
                       "publish, then rebuilds firms, people, hiring, signals and scores.",
                       foot="Runs by itself when a new feed is due.")
    weekly_html = f"""<section class="s"><h2>Weekly SEC cycle</h2><table class="jobs-table"><tbody><tr>
<td class="set-job-name"><b class="set-tipped"{wtip}>SEC feeds and rebuild</b></td>
<td class="jstate"><span class="chip line">Automatic</span></td>
<td class="set-job-prog"><div class="meta">{esc(wmsg)}</div></td>
<td class="set-job-last"><div class="{"small" if wlast else "meta"}">{"Started " + esc(wlast) if wlast else "Not started yet"}</div></td>
<td class="num nowrap"><form method="post" action="/settings/jobs/weekly_cycle/run" style="display:inline"><button class="sm" type="submit">Run now</button></form></td>
</tr></tbody></table></section>"""

    watch = ("Bellwether checks every job every five minutes, restarts anything stuck and moves "
             "overdue work to the front of its lane. Anything it cannot fix appears here and as a "
             "count on Settings.")
    if open_alerts:
        items = "".join(
            f'<div class="alert-row {esc(a["level"])}"><div><b>{esc(a["title"])}</b>'
            f'<p>{esc(a["detail"] or "")}</p><div class="meta">Raised {esc(ui.ago(a["since"]))}</div></div>'
            f'{"<a class=btn href=" + esc(a["href"]) + ">Fix</a>" if a.get("href") and a["href"] != "/settings/jobs" else ""}</div>'
            for a in open_alerts)
        attention = (f'<section class="s attention"><h2>Needs your attention'
                     f'{how("What Bellwether fixes by itself", watch, label="Why")}</h2>{items}</section>')
    else:
        attention = (f'<div class="all-clear"><b>Everything is running.</b> Nothing needs you.'
                     f'{how("What Bellwether fixes by itself", watch)}</div>')

    lane_line = ""
    if lanes:
        bits = []
        for ln in jobs.LANES:
            row = lanes.get(ln)
            if not row:
                continue
            now_job = jobs.BY_KIND.get(row.get("job") or "")
            bits.append(f'<span class="pill{"" if now_job else " off"}">{esc(jobs.LANE_LABEL.get(ln, ln))}: '
                        f'{esc(now_job.label) if now_job else "idle"}</span>')
        lanes_tip = ui.tip_attr("Lanes", "Jobs run in four lanes side by side. Jobs in one lane "
                                "take turns; Run now puts a job first in its lane.")
        lane_line = (f'<div class="set-lanes"><span class="set-lanes-l set-tipped"{lanes_tip}>'
                     f'Running now</span><div class="pills">{"".join(bits)}</div></div>')

    fixed = ""
    if fixes:
        fixed = ('<section class="s"><h2>Fixed by itself in the last day</h2><table class="jobs-table"><tbody>'
                 + "".join(f'<tr><td class="nowrap small" style="width:140px">{esc(ui.ago(e["at"]))}</td>'
                           f'<td>{esc(e["detail"] or "")}</td></tr>' for e in fixes)
                 + '</tbody></table></section>')
    inner = f'<div class="set-jobs-top">{attention}{lane_line}</div>{fixed}{weekly_html}{"".join(sections)}'
    return _frame("Jobs", "jobs", "Background work that keeps the data current. Run now puts a job "
                  "first in its lane.", inner, msg)


@router.post("/settings/jobs/{kind}/{action}")
def job_action(kind: str, action: str):
    c = conn()
    try:
        if kind == "weekly_cycle" and action == "run":
            start_weekly()
            c.execute("INSERT INTO auto_task (kind, desired_state, last_run_at) VALUES"
                      " ('weekly_cycle','running',?) ON CONFLICT (kind) DO UPDATE SET"
                      " last_run_at=excluded.last_run_at",
                      (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
            c.commit()
            return RedirectResponse("/settings/jobs?msg=Weekly+cycle+started.", status_code=303)
        if kind not in jobs.BY_KIND or action not in ("run", "pause", "resume"):
            return RedirectResponse("/settings/jobs", status_code=303)
        if action == "run":
            jobs.request_run(c, kind)
        else:
            jobs.set_paused(c, kind, action == "pause")
    finally:
        c.close()
    from .webapp import ensure_autopilot
    ensure_autopilot()
    word = {"run": "is queued", "pause": "is paused",
            "resume": "will run automatically when due"}[action]
    return RedirectResponse(f"/settings/jobs?{qs_join(msg=jobs.BY_KIND[kind].label + ' ' + word + '.')}",
                            status_code=303)


# ------------------------------------------------------------------ firm types

def _ft_badge(t: dict | None) -> str:
    """A firm's type as a small badge with its confidence and source."""
    if not t:
        return '<span class="chip line">Not classified yet</span>'
    cls = "lead" if t.get("core") else ("line" if t["category"] == "unknown" else "")
    src = {"manual": "set by hand", "ai": "AI", "rules": "rules"}.get(t.get("source"), "")
    return (f'<span class="chip {cls}">{esc(t["label"])}</span> '
            f'<span class="meta" style="text-transform:none;letter-spacing:0">'
            f'{t["confidence"]}% &middot; {esc(src)}</span>')


def _ft_form(crd: str, current: str | None, back: str, compact: bool = False) -> str:
    """Set a firm's type by hand, or hand it back to the rules."""
    from . import firmtype
    opts = ui.opt("", "" if current is None else "-",
                  "Use the rules' answer" if current is None else "Back to the rules") + "".join(
        ui.opt(c["key"], current or "", c["label"]) for c in firmtype.categories()
        if c["key"] in firmtype.ASSIGNABLE)
    note = ('' if compact else
            '<input type="text" name="note" placeholder="Why (optional)" style="min-width:180px">')
    return (f'<form method="post" action="/settings/firmtypes/override" class="row">'
            f'<input type="hidden" name="crd" value="{esc(crd)}">'
            f'<input type="hidden" name="back" value="{esc(back)}">'
            f'<select name="category" style="max-width:240px">{opts}</select>{note}'
            f'<button class="sm" type="submit">Set</button></form>')


def _job_line(c, kind: str) -> str:
    try:
        r = c.execute("SELECT last_run_at, last_status, message, force FROM auto_task"
                      " WHERE kind=?", (kind,)).fetchone()
    except Exception:
        c.rollback()
        r = None
    if not r:
        return '<span class="meta">Not run yet</span>'
    bits = []
    if r["force"]:
        bits.append("Queued to run now")
    if r["last_run_at"]:
        failed = r["last_status"] in ("failed", "timeout")
        bits.append(f'Last ran {esc(ui.ago(r["last_run_at"]))}'
                    + (' <span class="chip bad">Failed</span>' if failed else ""))
    msg = (f'<div class="meta set-clamp" title="{esc(r["message"])}">{esc((r["message"] or "")[:160])}</div>'
           if r["message"] else "")
    every = (f"Runs by itself, {_cadence(jobs.BY_KIND[kind].every_hours)}" if kind in jobs.BY_KIND
             else "Runs by itself")
    return f'<span class="small soft">{" &middot; ".join(bits) or every}</span>{msg}'


@router.get("/settings/firmtypes", response_class=HTMLResponse)
def firmtypes_page(q: str = Query(""), cat: str = Query(""), msg: str = Query(""),
                   err: str = Query("")):
    from . import firmtype
    from .names import nice_name
    c = conn()
    try:
        counts = firmtype.counts(c)
        found = firmtype.search(c, q, 25) if q.strip() else []
        listed = firmtype.firms_in(c, cat, 50) if cat in firmtype.KEYS else []
        manual = firmtype.overrides(c)
        classify_line, rescore_line = _job_line(c, "classify"), _job_line(c, "rescore")
    finally:
        c.close()
    reach: dict[str, list[str]] = {}
    for k in products.product_keys():
        rule = products.firm_type_rule(k)
        if rule and not rule["off"]:
            for a in rule["allow"]:
                reach.setdefault(a, []).append(products.product(k)["short"])
    total = sum(v["n"] for v in counts.values())
    rows = []
    for cdef in firmtype.categories():
        k = cdef["key"]
        n = counts.get(k, {})
        lists = ("All lists" if k == "unknown" else ", ".join(reach.get(k, [])) or "No list")
        rows.append(
            f'<tr><td class="set-ft-name"><b class="set-tipped"{ui.tip_attr(cdef["label"], cdef["description"])}>'
            f'{esc(cdef["label"])}</b></td>'
            f'<td class="num"><a href="/settings/firmtypes?{qs_join(cat=k)}#firms">{n.get("n", 0):,}</a></td>'
            f'<td class="num">{n.get("sec", 0):,}</td>'
            f'<td class="num">{n.get("manual", 0):,}</td>'
            f'<td class="num">{n.get("conf", 0):.0f}%</td>'
            f'<td class="small set-ft-lists">{esc(lists)}<div class="meta">{esc(cdef["treatment"])}</div></td></tr>')
    lists_tip = ui.tip_attr("Product lists", "The lists firms of this type may appear on. Each "
                            "product sets its own on its Scoring screen.",
                            foot="A firm classified below that product's confidence threshold, or "
                                 "not classified, is never removed.")
    summary = (f'<table><thead><tr><th>Type</th><th class="num">Firms</th>'
               f'<th class="num">SEC-registered</th><th class="num">Set by hand</th>'
               f'<th class="num">Avg confidence</th><th><span class="set-tipped"{lists_tip}>'
               f'Product lists</span></th></tr></thead>'
               f'<tbody>{"".join(rows)}</tbody></table>'
               f'<p class="meta set-after">{total:,} firms classified. Hover a type for what it means; '
               f'click its count for the largest firms.</p>')
    actions = f"""<div class="set-actions-grid">
<form method="post" action="/settings/firmtypes/reclassify"><button class="primary" type="submit">Reclassify all firms</button></form>
<span>{classify_line}</span>
<form method="post" action="/settings/firmtypes/rescore"><button type="submit">Rescore all firms</button></form>
<span>{rescore_line}</span></div>"""

    def firm_rows(items: list[dict], back: str) -> str:
        out = []
        for r in items:
            t = r.get("firm_class")
            ev = "; ".join((t or {}).get("evidence") or [])
            out.append(
                f'<tr><td style="width:30%"><a href="/firm/{esc(r["crd"])}"><b>{esc(nice_name(r["legal_name"]))}</b></a>'
                f'<div class="meta">CRD {esc(r["crd"])} &middot; {esc(nice_name(r.get("city") or ""))} {esc(r.get("state") or "")}'
                f' &middot; {esc(firmtype._money(r.get("raum")))}</div></td>'
                f'<td style="width:22%">{_ft_badge(t)}</td>'
                f'<td class="small soft">{esc(ev[:260])}</td>'
                f'<td style="width:30%">{_ft_form(r["crd"], (t or {}).get("category") if (t or {}).get("source") == "manual" else None, back)}</td></tr>')
        return "".join(out)

    search_html = f"""<section class="s"><h2>Find a firm and set its type</h2>
<form method="get" action="/settings/firmtypes" class="row" style="margin-bottom:12px">
<input type="text" name="q" value="{esc(q)}" placeholder="Firm name or CRD" style="min-width:300px">
<button type="submit">Search</button></form>"""
    if q.strip():
        search_html += (f'<table><tbody>{firm_rows(found, qs_join(q=q))}</tbody></table>' if found
                        else '<p class="muted">No firm matches.</p>')
    search_html += "</section>"

    cat_html = ""
    if listed:
        types = {}
        c = conn()
        try:
            types = firmtype.get_many(c, [r["crd"] for r in listed])
        finally:
            c.close()
        items = [dict(r, firm_class=types.get(r["crd"])) for r in listed]
        cat_html = (f'<section class="s" id="firms"><h2>Largest firms typed {esc(firmtype.label(cat))}</h2>'
                    f'<p class="lede">The 50 largest by assets. Where the rules are wrong, set the type '
                    f'by hand; it survives every reclassification.</p>'
                    f'<table><tbody>{firm_rows(items, qs_join(cat=cat))}</tbody></table></section>')

    man_rows = "".join(
        f'<tr><td><a href="/firm/{esc(m["crd"])}"><b>{esc(nice_name(m.get("legal_name") or m["crd"]))}</b></a>'
        f'<div class="meta">CRD {esc(m["crd"])}</div></td>'
        f'<td>{_ft_badge(m)}</td>'
        f'<td class="small">{esc(m.get("overridden_by") or "")}<div class="meta">{esc(ui.ago(m.get("overridden_at")))}</div></td>'
        f'<td class="small soft">{esc(m.get("note") or "")}'
        + (f'<div class="meta" style="text-transform:none;letter-spacing:0">The rules say '
           f'{esc(firmtype.label(m.get("rules_category")))} ({m.get("rules_confidence") or 0}%)</div>'
           if m.get("rules_category") and m.get("rules_category") != m["category"] else "")
        + f'</td><td class="num"><form method="post" action="/settings/firmtypes/override">'
          f'<input type="hidden" name="crd" value="{esc(m["crd"])}"><input type="hidden" name="category" value="">'
          f'<button class="sm ghost" type="submit">Back to the rules</button></form></td></tr>'
        for m in manual)
    manual_html = (f'<section class="s"><h2>Set by hand ({len(manual)})</h2>'
                   + (f'<table><thead><tr><th>Firm</th><th>Type</th><th>By</th><th>Note</th><th></th></tr></thead>'
                      f'<tbody>{man_rows}</tbody></table>' if manual else
                      '<p class="muted">No firm has a type set by hand yet.</p>')
                   + '</section>')
    rules_html = ('<section class="s"><details class="set-more"><summary>How the rules decide</summary>'
                  '<p class="lede">Tried in this order; the first that applies decides. Known names '
                  'live in <a href="/settings/knowledge?kind=entity">Industry knowledge</a>.</p>'
                  '<ol class="set-rules">'
                  + "".join(f'<li><b>{esc(t)}</b> <span class="small soft">{esc(b)}</span></li>'
                            for t, b in firmtype.RULES_SUMMARY)
                  + '</ol></details></section>')
    inner = actions + summary + search_html + cat_html + manual_html + rules_html
    return _frame("Firm types", "firmtypes",
                  "What kind of firm each adviser is. Each product sells only to some types.",
                  inner, msg, err)


@router.post("/settings/firmtypes/override")
async def firmtypes_override(request: Request):
    from . import firmtype
    form = await request.form()
    crd = (form.get("crd") or "").strip()
    category = (form.get("category") or "").strip() or None
    note = (form.get("note") or "").strip()
    # Only the search or the type being viewed comes back, rebuilt rather
    # than echoed, so nothing but those two values reaches the redirect.
    prev = urllib.parse.parse_qs(form.get("back") or "")
    back = qs_join(q=(prev.get("q") or [""])[0][:120], cat=(prev.get("cat") or [""])[0][:40])
    c = conn()
    try:
        t = firmtype.set_override(c, crd, category, current_owner(), note)
    except ValueError as e:
        return RedirectResponse("/settings/firmtypes?" + "&".join(
            x for x in (back, qs_join(err=str(e))) if x), status_code=303)
    finally:
        c.close()
    word = (f"set to {t['label']} by hand" if category else
            f"back to the rules: {t['label']}") if t else "updated"
    done = qs_join(msg=f"CRD {crd} {word}. Its scores are updated.")
    return RedirectResponse("/settings/firmtypes?" + "&".join(x for x in (back, done) if x),
                            status_code=303)


@router.post("/settings/firmtypes/reclassify")
def firmtypes_reclassify():
    c = conn()
    try:
        # The job row exists once the app has started with the job registered;
        # make sure of it, then ask for a run now.
        c.execute("INSERT INTO auto_task (kind, desired_state) VALUES ('classify', 'running')"
                  " ON CONFLICT (kind) DO NOTHING")
        c.commit()
        jobs.request_run(c, "classify")
    finally:
        c.close()
    from .webapp import ensure_autopilot
    ensure_autopilot()
    return RedirectResponse("/settings/firmtypes?" + qs_join(
        msg="Reclassification queued for all firms. Scores will update if firm types change."),
        status_code=303)


@router.post("/settings/firmtypes/rescore")
def firmtypes_rescore():
    c = conn()
    try:
        ok = products.request_rescore(conn=c)
    finally:
        c.close()
    from .webapp import ensure_autopilot
    ensure_autopilot()
    if not ok:
        return RedirectResponse("/settings/firmtypes?" + qs_join(
            err="The Scores job is not set up yet; restart Bellwether."), status_code=303)
    return RedirectResponse("/settings/firmtypes?" + qs_join(
        msg="Rescoring queued for all firms and all products."), status_code=303)


# ------------------------------------------------------------- industry knowledge

def _kn_fields(it: dict | None, kind: str) -> str:
    """The form fields for one item (or a new one) of a kind."""
    from . import firmtype
    it = it or {"title": "", "body": "", "data": {}}
    d = it.get("data") or {}
    title_label = {"category": "Name", "entity": "Name", "glossary": "Term",
                   "product": "Product", "fact": "Title"}.get(kind, "Title")
    body_label = {"category": "What it is", "entity": "Note", "glossary": "Meaning",
                  "product": "What it is and who buys it", "fact": "Text"}.get(kind, "Text")
    f = [f'<label style="max-width:640px">{title_label}<input type="text" name="title" '
         f'value="{esc(it["title"])}" required></label>']
    if kind == "category":
        f.append(f'<label style="max-width:640px">Short label (badges)<input type="text" name="short" '
                 f'value="{esc(d.get("short") or "")}"></label>')
    if kind == "entity":
        opts = "".join(ui.opt(c["key"], d.get("category") or "", c["label"])
                       for c in firmtype.categories() if c["key"] in firmtype.ASSIGNABLE)
        f.append(f'<label style="max-width:640px">Firm type<select name="category">{opts}</select></label>')
        f.append(f'<label style="max-width:640px">Name patterns, one per line (matched against the '
                 f'firm\'s own names)<textarea name="patterns" rows="4">{esc(chr(10).join(d.get("patterns") or []))}</textarea></label>')
        f.append(f'<label style="max-width:640px">Owner patterns, one per line (matched against '
                 f'companies owning 50%+ of a firm)<textarea name="parents" rows="3">{esc(chr(10).join(d.get("parents") or []))}</textarea></label>')
        f.append(f'<label style="max-width:240px">Confidence (30 to 99, blank for 95)<input type="number" '
                 f'name="confidence" min="30" max="99" value="{esc(d.get("confidence") or "")}"></label>')
    f.append(f'<label style="max-width:640px">{body_label}<textarea name="body" rows="3">{esc(it.get("body") or "")}</textarea></label>')
    if kind == "category":
        f.append(f'<label style="max-width:640px">How Bellwether treats it<textarea name="treatment" rows="2">'
                 f'{esc(d.get("treatment") or "")}</textarea></label>')
    return "".join(f)


@router.get("/settings/knowledge", response_class=HTMLResponse)
def knowledge_page(kind: str = Query("entity"), msg: str = Query(""), err: str = Query(""), q: str = Query("")):
    from . import firmtype, knowledge
    kind = kind if kind in knowledge.KINDS else "entity"
    c = conn()
    try:
        all_items = knowledge.items(c)
    finally:
        c.close()
    per = {k: sum(1 for i in all_items if i["kind"] == k) for k in knowledge.KINDS}
    seg = ('<div class="seg set-tabs set-subtabs">' + "".join(
        f'<a class="{"on" if k == kind else ""}" href="/settings/knowledge?{qs_join(kind=k)}">'
        f'{esc(label)} <span class="muted">{per[k]}</span></a>'
        for k, label in knowledge.KINDS.items()) + '</div>')
    items = [i for i in all_items if i["kind"] == kind]
    if q.strip():
        needle = q.strip().casefold()
        items = [i for i in items if needle in (i['title'] + ' ' + (i.get('body') or '')
                 + ' ' + str(i.get('data') or {})).casefold()]
    if kind == "category":
        order = {k: n for n, k in enumerate(firmtype.KEYS)}
        items.sort(key=lambda i: order.get(i["key"], 99))
    rows = []
    for it in items:
        d = it.get("data") or {}
        state = ('<span class="chip line">Off</span>' if not it.get("active") else
                 ('<span class="chip">Edited</span>' if it.get("edited") else
                  '<span class="chip line">Built in</span>'))
        detail = ""
        if kind == "entity":
            detail = (f'<div class="small set-kn-type"><span class="chip">{esc(firmtype.label(d.get("category")))}</span></div>'
                      f'<div class="meta" style="text-transform:none;letter-spacing:0">Names: '
                      f'{esc(", ".join(d.get("patterns") or []) or "none")}</div>'
                      + (f'<div class="meta" style="text-transform:none;letter-spacing:0">Owners: '
                         f'{esc(", ".join(d.get("parents") or []))}</div>' if d.get("parents") else ""))
        elif kind == "category":
            detail = (f'<div class="meta" style="text-transform:none;letter-spacing:0">Badge: '
                      f'{esc(d.get("short") or "")}. {esc(d.get("treatment") or "")}</div>')
        buttons = []
        if kind != "category":
            buttons.append(
                f'<form method="post" action="/settings/knowledge/toggle" style="display:inline">'
                f'<input type="hidden" name="id" value="{it["id"]}">'
                f'<input type="hidden" name="active" value="{0 if it.get("active") else 1}">'
                f'<button class="sm ghost" type="submit">{"Switch off" if it.get("active") else "Switch on"}</button></form>')
        shipped = knowledge.is_shipped(it)
        if shipped and (it.get("edited") or not it.get("active")):
            buttons.append(
                f'<form method="post" action="/settings/knowledge/reset" style="display:inline" '
                f'onsubmit="return confirm(\'Put this item back to the built-in version?\')">'
                f'<input type="hidden" name="id" value="{it["id"]}">'
                f'<button class="sm ghost" type="submit">Reset</button></form>')
        if not shipped and kind != "category":
            buttons.append(
                f'<form method="post" action="/settings/knowledge/delete" style="display:inline" '
                f'onsubmit="return confirm(\'Delete this item?\')">'
                f'<input type="hidden" name="id" value="{it["id"]}">'
                f'<button class="sm ghost" type="submit">Delete</button></form>')
        edit = (f'<details class="source-help"><summary>Edit</summary>'
                f'<form method="post" action="/settings/knowledge/save" class="set-form set-edit">'
                f'<input type="hidden" name="id" value="{it["id"]}"><input type="hidden" name="kind" value="{esc(kind)}">'
                f'{_kn_fields(it, kind)}<button class="primary sm" type="submit">Save</button></form></details>')
        who = ("" if not it.get("edited") else
               f'<div class="meta">{esc(it.get("updated_by") or "")} &middot; {esc(ui.ago(it.get("updated_at")))}</div>')
        rows.append(
            f'<tr{" style=opacity:.55" if not it.get("active") else ""}><td style="width:30%"><b>{esc(it["title"])}</b>{who}</td>'
            f'<td><div class="small soft">{esc(it.get("body") or "")}</div>{detail}{edit}</td>'
            f'<td class="nowrap">{state}</td><td class="num nowrap">{" ".join(buttons)}</td></tr>')
    add = ""
    if kind != "category":
        noun = {"entity": "a known entity", "product": "a product note", "glossary": "a term",
                "fact": "a note"}.get(kind, "an item")
        add = (f'<section class="s"><details class="set-add"{" open" if not items and not q.strip() else ""}>'
               f'<summary class="btn">Add {esc(noun)}</summary>'
               f'<form method="post" action="/settings/knowledge/save" class="set-form">'
               f'<input type="hidden" name="kind" value="{esc(kind)}">{_kn_fields(None, kind)}'
               f'<button class="primary" type="submit">Add</button></form></details></section>')
    try:
        digest = knowledge.ai_context()
    except Exception:
        digest = ""
    ai_html = (f'<section class="s"><h2>What Bellwether AI reads</h2><p class="lede">A digest of '
               f'this knowledge, about {len(digest.split()):,} words, goes with every question and '
               f'brief.</p><details class="source-help">'
               f'<summary>Show the digest</summary><pre class="small soft" style="white-space:pre-wrap">'
               f'{esc(digest)}</pre></details></section>') if digest else ""
    search = (f'<form class="knowledge-search" method="get" action="/settings/knowledge">'
              f'<input type="hidden" name="kind" value="{esc(kind)}">'
              f'<input type="search" name="q" value="{esc(q)}" aria-label="Search industry knowledge" placeholder="Search names or rules">'
              f'<button type="submit">Search</button></form>')
    help_txt = knowledge.KIND_HELP.get(kind, "")
    first, _, rest = help_txt.partition(". ")
    help_html = (f'<p class="lede set-kind-help">{esc(first.rstrip(".") + ".")}'
                 + (how(knowledge.KINDS.get(kind, ""), rest) if rest else "") + '</p>') if help_txt else ""
    inner = (seg + search + help_html
             + f'<table class="set-stack"><tbody>{"".join(rows) or "<tr><td class=empty>Nothing here yet.</td></tr>"}</tbody></table>'
             + add + ai_html)
    return _frame("Industry knowledge", "knowledge",
                  "The firm names, definitions and product knowledge behind classification and AI.",
                  inner, msg, err)


def _knowledge_changed(c, item):
    if (item or {}).get('kind') == 'entity':
        jobs.request_run(c, 'classify')
        from .webapp import ensure_autopilot
        ensure_autopilot()


@router.post("/settings/knowledge/save")
async def knowledge_save(request: Request):
    from . import knowledge
    form = await request.form()
    kind = form.get("kind") or ""
    raw_id = (form.get("id") or "").strip()
    back = f"/settings/knowledge?{qs_join(kind=kind)}"
    data = {"category": form.get("category"), "patterns": form.get("patterns") or "",
            "parents": form.get("parents") or "", "confidence": form.get("confidence"),
            "short": form.get("short"), "treatment": form.get("treatment")}
    c = conn()
    try:
        knowledge.save_item(c, int(raw_id) if raw_id.isdigit() else None, kind,
                            form.get("title") or "", form.get("body") or "", data, current_owner())
        if kind == "entity":
            c.execute("INSERT INTO auto_task (kind, desired_state) VALUES ('classify', 'running')"
                      " ON CONFLICT (kind) DO NOTHING")
            c.commit()
            jobs.request_run(c, "classify")
    except ValueError as e:
        return RedirectResponse(f"{back}&{qs_join(err=str(e))}", status_code=303)
    finally:
        c.close()
    msg = "Saved."
    if kind == "entity":
        from .webapp import ensure_autopilot
        ensure_autopilot()
        msg = "Saved. Reclassification queued; scores will update if firm types change."
    return RedirectResponse(f"{back}&{qs_join(msg=msg)}", status_code=303)


@router.post("/settings/knowledge/toggle")
def knowledge_toggle(id: int = Form(...), active: int = Form(...)):
    from . import knowledge
    c = conn()
    try:
        it = knowledge.get(c, id)
        knowledge.set_active(c, id, bool(active), current_owner())
        _knowledge_changed(c, it)
    except ValueError as e:
        return RedirectResponse(f"/settings/knowledge?{qs_join(err=str(e))}", status_code=303)
    finally:
        c.close()
    kind = (it or {}).get("kind", "entity")
    return RedirectResponse(f"/settings/knowledge?{qs_join(kind=kind, msg='Switched on.' if active else 'Switched off.')}",
                            status_code=303)


@router.post("/settings/knowledge/reset")
def knowledge_reset(id: int = Form(...)):
    from . import knowledge
    c = conn()
    try:
        it = knowledge.get(c, id)
        ok = knowledge.reset_item(c, id)
        if ok:
            _knowledge_changed(c, it)
    finally:
        c.close()
    kind = (it or {}).get("kind", "entity")
    return RedirectResponse(f"/settings/knowledge?{qs_join(kind=kind, msg='Back to the shipped version.' if ok else '', err='' if ok else 'Nothing to reset.')}",
                            status_code=303)


@router.post("/settings/knowledge/delete")
def knowledge_delete(id: int = Form(...)):
    from . import knowledge
    c = conn()
    try:
        it = knowledge.get(c, id)
        knowledge.delete_item(c, id)
        _knowledge_changed(c, it)
    except ValueError as e:
        return RedirectResponse(f"/settings/knowledge?{qs_join(err=str(e))}", status_code=303)
    finally:
        c.close()
    kind = (it or {}).get("kind", "entity")
    return RedirectResponse(f"/settings/knowledge?{qs_join(kind=kind, msg='Deleted.')}", status_code=303)


# ------------------------------------------------------------------ system

# What each source and table is, in words. The stored key stays on hover
# (title), for anyone matching a row to the logs.
SOURCE_LABEL = {
    "adv_feed": "SEC adviser feed", "adv_state_feed": "State adviser feed",
    "ia_indvl_feed": "SEC individual feed", "people": "People and hiring",
    "people_index": "People screen", "brochures": "Brochures", "brochure": "Brochures",
    "brochure_contacts": "Brochure contacts", "firm_class": "Firm types",
    "holding_13f": "13F holdings", "edgar_13f_index": "13F filing index",
    "adv_13f_match": "13F matches", "13f": "13F filings", "filing_13f": "13F filings",
    "form_d": "Form D", "mail_platform": "Email platform", "cusip_map": "Security map",
    "growth": "Growth", "directories": "Directories", "open_roles": "Open roles",
    "email_hunt": "Email hunt", "contacts": "Contacts", "diff_snapshots": "Change detection",
    "firm_overlay": "Firm overlay", "custodian_share": "Custodian share",
    "firm_history": "Firm history", "ai_research": "AI contact research",
    "firm_adv_extra": "Form ADV extra answers", "firm_adv_profile": "Form ADV profile",
    "firm_refresh": "Custodian refresh", "adv_filing_crosswalk": "ADV filing archive",
    "schedule_a_b": "Schedule A and B", "schedule_d_archive": "Schedule D archive",
    "offices": "Office phones", "web_enrich": "Firm websites", "contact_search": "Web search",
}
TABLE_LABEL = {
    "firm": "Firms", "firm_scope": "Firms in scope", "product_score": "Product scores",
    "person": "People", "person_employment": "Employment records", "people_event": "People moves",
    "contact_point": "Contact points", "trigger_event": "Trigger events", "brochure": "Brochures",
    "brochure_tag": "Brochure tags", "firm_mail_platform": "Email platforms",
    "web_page": "Web pages read", "web_signal": "Web signals",
    "schedule_a": "Owners and officers (Schedule A)", "sched_d_7b1": "Private funds (Schedule D 7.B.1)",
    "holding_13f": "13F holdings", "directory_record": "Directory records", "ai_call": "AI calls",
    "score_override": "Score overrides",
}


def _source(key: str | None) -> str:
    key = key or ""
    base, _, rest = key.partition(":")
    label = SOURCE_LABEL.get(key) or SOURCE_LABEL.get(base) or base.replace("_", " ").capitalize()
    if rest:
        label += ": " + rest.replace("_", " ")
    return f'<span title="{esc(key)}">{esc(label)}</span>'


def _size(n: int | None) -> str:
    n = int(n or 0)
    for unit, step in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= step:
            return f"{n / step:,.1f} {unit}"
    return f"{n:,} byte{'' if n == 1 else 's'}"


@router.get("/settings/system", response_class=HTMLResponse)
def system_page():
    c = conn()

    def safe(sql, args=()):
        try:
            r = c.execute(sql, args).fetchone()
            return r["n"] if r else None
        except Exception:
            c.rollback()
            return None

    snaps = c.execute("SELECT * FROM snapshot ORDER BY id").fetchall()
    runs = c.execute("SELECT * FROM run_log ORDER BY id DESC LIMIT 30").fetchall()
    sched = None
    try:
        sched = c.execute("SELECT * FROM scheduler_state WHERE id=1").fetchone()
    except Exception:
        c.rollback()
    counts = []
    for t in ("firm", "firm_scope", "product_score", "person", "person_employment",
              "people_event", "contact_point", "trigger_event", "brochure", "brochure_tag",
              "firm_mail_platform", "web_page", "web_signal", "schedule_a", "sched_d_7b1",
              "holding_13f", "directory_record", "ai_call", "score_override"):
        n = safe(f"SELECT COUNT(*) n FROM {t}")
        if n is not None:
            counts.append(f'<tr><td><span title="{t}">{esc(TABLE_LABEL.get(t, t))}</span></td>'
                          f'<td class="num">{n:,}</td></tr>')
    c.close()
    beat = ""
    if sched and sched["last_check"]:
        mins = (datetime.now(timezone.utc) - datetime.fromisoformat(sched["last_check"])).total_seconds() / 60
        on = mins < 15
        beat = (f'<div class="note {"plain" if on else "bad"} set-status"><span><b>Scheduler '
                f'{"on" if on else "stalled"}</b> &middot; last checked {esc(ui.ago(sched["last_check"]))}'
                f'{" &middot; " + esc(_cap(sched["message"])) if sched["message"] else ""}</span></div>')
    words = {"ok": "OK", "failed": "Failed", "skipped": "Skipped", "running": "Running"}
    rrow = []
    for r in runs:
        cls = {"ok": "ok", "failed": "bad", "skipped": "warnc", "running": "warnc"}.get(r["status"], "")
        flag = ' <span class="chip warn">Flagged</span>' if r["flagged"] else ""
        message = r["message"] or ""
        rrow.append(f'<tr><td>{_source(r["source_key"])}</td><td>{esc(_cap((r["stage"] or "").replace("_", " ")))}</td>'
                    f'<td class="{cls} nowrap"><b>{esc(words.get(r["status"], _cap(r["status"])))}</b>{flag}</td>'
                    f'<td class="num">{format(r["rows_out"], ",") if r["rows_out"] is not None else "-"}</td>'
                    f'<td class="small nowrap">{esc(ui.ago(r["finished_at"] or r["started_at"]))}</td>'
                    f'<td class="small soft" title="{esc(message)}">{esc(message[:140])}</td></tr>')
    srow = "".join(f'<tr><td>{_source(s["source_key"])}</td><td>{esc(s["published_at"])}</td>'
                   f'<td class="num" title="{s["bytes"]:,} bytes">{_size(s["bytes"])}</td>'
                   f'<td class="small">{esc(s["captured_at"][:10])}</td></tr>'
                   for s in snaps)
    snap_tip = ui.tip_attr("Snapshots", "Every SEC file Bellwether reads is kept exactly as "
                           "downloaded, so any figure can be traced back to its source.")
    inner = f"""{beat}
<section class="s"><h2>Recent runs</h2><table class="tight"><thead><tr><th>Source</th><th>Stage</th>
<th>Status</th><th class="num">Rows</th><th>When</th><th>Message</th></tr></thead>
<tbody>{"".join(rrow) or '<tr><td colspan="6" class="empty">No runs yet.</td></tr>'}</tbody></table></section>
<section class="s"><div class="cols-2 set-cols"><div><h3><span class="set-tipped"{snap_tip}>Snapshots</span></h3><table class="tight">
<thead><tr><th>Source</th><th>Published</th><th class="num">Size</th><th>Captured</th></tr></thead>
<tbody>{srow or '<tr><td colspan="4" class="empty">None yet.</td></tr>'}</tbody></table></div><div><h3>Record counts</h3><table class="tight"><tbody>{"".join(counts)}</tbody></table>
<p class="meta set-after"><a href="/settings/system.json">Raw JSON</a></p></div></div></section>"""
    return _frame("System", "system", "Whether the data is current, and what has run.", inner)


@router.get("/settings/system.json")
def system_json():
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


from . import review_view  # noqa: E402  (imports settings_tabs from this module)

router.include_router(review_view.router)
