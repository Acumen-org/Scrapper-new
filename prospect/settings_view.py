"""Settings: admins only. Everything that configures Bellwether, in one place.

  Overview      the state of every integration at a glance, with what to do next
  Users         who has access and in which seat: admin, product owner, user
  Sign-in       Microsoft sign-in (the main way in) and password fallback
  AI            the model provider behind Bellwether AI, its key and daily limit
  Verification  how email checking talks to mail servers, and whether it can
  Crawling      how firm websites and directories are read
  Jobs          every background job: running by itself, with Run now and Pause
  System        data freshness, run history, snapshots, record counts
  Review queue  the few decisions that need a person

The middleware refuses every /settings path to anyone who is not an admin, so
nothing here re-checks the role; the forms still validate every value.
"""

from __future__ import annotations

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
        ("jobs", "Jobs", "/settings/jobs"), ("system", "System", "/settings/system"),
        ("review", "Review queue", "/settings/review")]


def settings_tabs(active: str) -> str:
    return ('<div class="seg" style="margin:4px 0 18px">' + "".join(
        f'<a class="{"on" if k == active else ""}" href="{href}">{esc(label)}</a>'
        for k, label, href in TABS) + '</div>')


def _frame(title: str, tab: str, lede: str, inner: str, msg: str = "", err: str = "") -> HTMLResponse:
    flash = ""
    if msg:
        flash = f'<div class="note good">{esc(msg)}</div>'
    if err:
        flash = f'<div class="note bad">{esc(err)}</div>'
    body = (f'<div class="pg"><div class="head"><div><h1>{esc(title)}</h1>'
            f'<div class="lede">{lede}</div></div></div>{settings_tabs(tab)}{flash}{inner}</div>')
    return page(title, "settings", body)


CHOICE_LABEL = {"none": "Not connected", "anthropic": "Anthropic (Claude)", "edenai": "Eden AI",
                "openai": "OpenAI-compatible", "auto": "Automatic", "reacher": "Reacher server",
                "native": "Built-in SMTP check", "dns": "Domain check only", "on": "On",
                "off": "Off", "never": "Never"}


def _field(key: str) -> str:
    """One setting as a form field, from its spec. Secrets are write-only."""
    s = settings.BY_KEY[key]
    pinned = settings.pinned(key)
    dis = " disabled" if pinned else ""
    hint = f'<div class="meta" style="text-transform:none;letter-spacing:0">{esc(s.help)}</div>' if s.help else ""
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
               f' placeholder="{esc(s.default)}"{dis} style="min-width:340px">')
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

def _status_row(name: str, ok: bool | None, text: str, href: str) -> str:
    dot = "var(--ok)" if ok else ("var(--amber)" if ok is None else "var(--red-hi)")
    return (f'<tr class="go" data-href="{href}"><td style="width:200px"><b>{esc(name)}</b></td>'
            f'<td><span class="dotc" style="background:{dot}"></span>{text}</td>'
            f'<td class="num"><a href="{href}">Open</a></td></tr>')


@router.get("/settings", response_class=HTMLResponse)
def overview(msg: str = Query("")):
    rows = []
    ms = msauth.configured()
    pw = settings.get_bool("auth.password_login")
    rows.append(_status_row(
        "Microsoft sign-in", ms,
        ("Set up. People sign in with their Microsoft account." if ms else
         "Not set up yet. Until it is, people sign in with Bellwether passwords.")
        + ("" if pw or not ms else " Password sign-in is off."), "/settings/signin"))
    us = users.all_users()
    n_admin = sum(1 for u in us if u["role"] == "admin")
    rows.append(_status_row("Users", True, f"{len(us)} accounts, {n_admin} admin"
                            f"{'s' if n_admin != 1 else ''}.", "/settings/users"))
    st = ai.status()
    rows.append(_status_row(
        "AI provider", st["configured"] or None,
        (f"{esc(st['provider'])}, {esc(st['model'])}. {st['used_today']} of {st['limit']} "
         f"calls used today." if st["configured"] else
         "Not connected. Bellwether AI works as a firm finder until one is."), "/settings/ai"))
    try:
        from . import verify
        es = verify.engine_status()
        good = es["resolved"] in ("reacher", "native")
        txt = (f"Checking mailboxes with the {esc(es['resolved'])} engine."
               if good else "Only domain checks: outbound port 25 is closed and no Reacher server is set.")
        rows.append(_status_row("Email verification", good or None, txt, "/settings/verify"))
    except Exception:
        rows.append(_status_row("Email verification", False, "The verification module did not load.",
                                "/settings/verify"))
    try:
        from . import crawl
        b = crawl.browser_available() if hasattr(crawl, "browser_available") else None
        rows.append(_status_row("Website reading", True,
                                "Scrapling with browser impersonation"
                                + ("; JavaScript pages rendered in a headless browser." if b else
                                   "; JavaScript-only pages are skipped (no browser installed)."),
                                "/settings/crawl"))
    except Exception:
        rows.append(_status_row("Website reading", None, "Crawler module not loaded.", "/settings/crawl"))
    c = conn()
    try:
        ov = jobs.overview(c)
    except Exception:
        c.rollback()
        ov = []
    run = sum(1 for j in ov if j["state"] == "running")
    paused = sum(1 for j in ov if j["state"] == "paused")
    failed = sum(1 for j in ov if j.get("last_status") in ("failed", "timeout"))
    rows.append(_status_row("Background jobs", (not failed) or None,
                            f"{len(ov)} jobs running by themselves: {run} working now, "
                            f"{paused} paused by an admin, {failed} whose last slice failed.",
                            "/settings/jobs"))
    feed = c.execute("SELECT published_at FROM snapshot WHERE source_key='adv_feed'"
                     " ORDER BY id DESC LIMIT 1").fetchone()
    sched = None
    try:
        sched = c.execute("SELECT * FROM scheduler_state WHERE id=1").fetchone()
    except Exception:
        c.rollback()
    c.close()
    fresh_ok, fresh_txt = None, "No SEC feed captured yet."
    if feed:
        try:
            m, d_, y = feed["published_at"].split("/")
            age = (date.today() - date(int(y), int(m), int(d_))).days
            fresh_ok = age <= 10
            fresh_txt = f"SEC adviser feed is {age} days old (published {esc(feed['published_at'])})."
        except Exception:
            pass
    if sched and sched["message"]:
        fresh_txt += f" Scheduler: {esc(sched['message'])}."
    rows.append(_status_row("Data freshness", fresh_ok, fresh_txt, "/settings/system"))
    inner = f'<table><tbody>{"".join(rows)}</tbody></table>'
    return _frame("Settings", "overview",
                  "Only admins see this. Everything that configures Bellwether, with what each "
                  "integration is doing right now.", inner, msg=msg)


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
        role_sel = ("<span class='chip lead' title='On the always-admin list in Sign-in'>Admin, always</span>"
                    if boot else
                    f'<select name="role">{"".join(ui.opt(r, u["role"], users.ROLE_LABEL[r]) for r in users.ROLES)}</select>')
        how = "Microsoft" if u.get("ms_oid") else ("Password" if u.get("password_hash") else "Not signed in yet")
        rows.append(
            f'<tr><td><b>{esc(u["name"])}</b><div class="meta">{esc(u.get("email") or u["login"])}</div></td>'
            f'<td><form method="post" action="/settings/users/save" class="row">'
            f'<input type="hidden" name="login" value="{esc(u["login"])}">{role_sel}'
            f'<span class="row" title="For product owners: which products they own">{fam}</span>'
            f'<label class="inline small"><input type="checkbox" name="active" value="1"'
            f'{" checked" if u.get("active") else ""}> active</label>'
            f'<button class="sm" type="submit">Save</button></form></td>'
            f'<td class="small">{esc(how)}<div class="meta">{esc(ui.ago(u.get("last_login_at")) or "never")}</div></td>'
            f'<td class="num"><form method="post" action="/settings/users/delete" '
            f'onsubmit="return confirm(\'Remove this account? Their notes and ownership stay.\')">'
            f'<input type="hidden" name="login" value="{esc(u["login"])}">'
            f'<button class="sm ghost" type="submit">Remove</button></form></td></tr>')
    add = f"""<section class="s"><h2>Add someone</h2>
<p class="lede">People with a Microsoft account in your organisation get in by signing in; their
account is created then, as a user. Add them here first to give them a seat before they arrive.
A password is only for someone without Microsoft sign-in.</p>
<form method="post" action="/settings/users/add" class="filters" style="border-top:0">
<label>Email or username<input type="text" name="login" required placeholder="jane.doe@acumen-strategy.com"></label>
<label>Name<input type="text" name="name" required></label>
<label>Seat<select name="role">{"".join(ui.opt(r, "user", users.ROLE_LABEL[r]) for r in users.ROLES)}</select></label>
<label>Owns<span class="row">{"".join(f'<label class="inline small"><input type="checkbox" name="fam" value="{f}"> {f}</label>' for f in users.FAMILIES)}</span></label>
<label>Password (optional)<input type="password" name="password" autocomplete="new-password"></label>
<button class="primary" type="submit">Add</button></form></section>"""
    inner = (f'<p class="lede">Admins see Settings and can change everything. Product owners '
             f'change the scoring of the products they own (PHH, AcuBooth, Glynac) and manage '
             f'enrichment sources. Users work the lists.</p>'
             f'<table><thead><tr><th>Person</th><th>Seat</th><th>Sign-in</th><th></th></tr></thead>'
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
    steps = f"""<div class="panel" style="margin-bottom:22px"><h3>Setting it up, once</h3>
<ol class="small soft" style="margin:6px 0 0;padding-left:18px;line-height:1.8">
<li>In the Microsoft Entra admin centre, open App registrations, then New registration. Name it
Bellwether; supported account types: this organisation only.</li>
<li>Redirect URI: platform <b>Web</b>, address <b style="color:var(--ink)" class="mono">{esc(uri)}</b></li>
<li>From the app's Overview, copy the Directory (tenant) ID and the Application (client) ID into
the fields below.</li>
<li>Certificates &amp; secrets, New client secret. Copy its <b>Value</b> into the secret field.</li>
<li>Save, then <a href="/auth/microsoft" data-noprefetch>try signing in with Microsoft</a>. Once it works, you can
turn password sign-in off.</li></ol></div>"""
    state = ('<div class="note good">Microsoft sign-in is set up.</div>' if ok else
             '<div class="note">Microsoft sign-in is not set up yet. Password sign-in stays on '
             'until it is, so nobody is locked out.</div>')
    inner = state + steps + _group_form("signin")
    return _frame("Sign-in", "signin", "How people get into Bellwether. Microsoft is the main way in; "
                  "anyone on the always-admin list is an admin the first time they sign in.",
                  inner, msg, err)


# ------------------------------------------------------------------ AI

@router.get("/settings/ai", response_class=HTMLResponse)
def ai_page(msg: str = Query(""), err: str = Query("")):
    feats = settings.get_list("ai.features")
    checks = "".join(
        f'<label class="inline" style="margin-bottom:6px"><input type="checkbox" name="feature:{k}" value="1"'
        f'{" checked" if k in feats else ""}> <b>{esc(k)}</b> <span class="muted small">{esc(v)}</span></label>'
        for k, v in ai.FEATURES.items())
    keys = [k for k in ("ai.provider", "ai.api_key", "ai.base_url", "ai.model_smart",
                        "ai.model_fast", "ai.daily_limit")]
    form = _group_form("ai", keys, extra=f'<h3 style="margin-top:6px">What it may do</h3>{checks}'
                                         f'<div style="height:14px"></div>')
    st = ai.status()
    c = conn()
    usage = c.execute("""SELECT feature, COUNT(*) n, SUM(ok) ok, SUM(COALESCE(in_tokens,0)) tin,
        SUM(COALESCE(out_tokens,0)) tout FROM ai_call WHERE at >= ? GROUP BY feature
        ORDER BY n DESC""", ((date.today().replace(day=1)).isoformat(),)).fetchall()
    errs = c.execute("SELECT at, feature, error FROM ai_call WHERE ok=0 ORDER BY id DESC LIMIT 5").fetchall()
    c.close()
    use_html = "".join(f'<tr><td>{esc(r["feature"])}</td><td class="num">{r["n"]:,}</td>'
                       f'<td class="num">{(r["ok"] or 0):,}</td><td class="num">{(r["tin"] or 0):,}</td>'
                       f'<td class="num">{(r["tout"] or 0):,}</td></tr>' for r in usage)
    err_html = "".join(f'<div class="gline"><span class="muted">{esc(ui.ago(e["at"]))}</span> '
                       f'{esc(e["feature"])}: {esc(e["error"])}</div>' for e in errs)
    status = (f'<div class="note good">Connected: {esc(st["provider"])}, {esc(st["model"])}. '
              f'{st["used_today"]} of {st["limit"]} calls used today.</div>' if st["configured"] else
              '<div class="note">No AI provider is connected.</div>')
    inner = f"""{status}
<p class="lede">Bellwether AI answers questions about firms and the market, writes a brief on each
firm page, reads team pages the rules could not parse, and tidies titles. Everything it writes is
labelled as AI and grounded in Bellwether's own data. Choose a provider: Anthropic (Claude, default
model claude-opus-5-5), Eden AI (one key for many vendors; models are named like
anthropic/claude-opus-5-5 or openai/gpt-4o), or any OpenAI-compatible endpoint.</p>
<div class="row" style="margin:10px 0 22px"><button class="sm" data-post="/settings/ai/test" data-busy="Testing">Test the connection</button></div>
{form}
<section class="s"><h2>Use this month</h2>
<table class="tight"><thead><tr><th>Feature</th><th class="num">Calls</th><th class="num">Succeeded</th>
<th class="num">Tokens in</th><th class="num">Tokens out</th></tr></thead><tbody>{use_html or '<tr><td colspan=5 class=empty>No calls yet.</td></tr>'}</tbody></table>
{('<h3 style="margin-top:16px">Recent errors</h3>' + err_html) if err_html else ""}</section>"""
    return _frame("AI", "ai", "The model provider behind Bellwether AI, with a hard daily limit.",
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
    status = (f'<table class="tight"><tbody>'
              f'<tr><td style="width:220px">Engine in use</td><td><b>{esc(es.get("resolved"))}</b> '
              f'<span class="muted">(setting: {esc(es.get("configured"))})</span></td></tr>'
              f'<tr><td>Outbound port 25</td><td>{"<span class=ok>open</span>" if p25 else "<span class=bad>closed</span>" if p25 is False else "not tested"}'
              f' <span class="muted small">{esc(es.get("port25_detail") or "")}</span></td></tr>'
              f'<tr><td>Reacher server</td><td>{esc(es.get("reacher_url") or "not set")} '
              f'{"<span class=ok>answering</span>" if es.get("reacher_ok") else ("<span class=bad>not answering</span>" if es.get("reacher_ok") is False else "")}'
              f' <span class="muted small">{esc(es.get("reacher_detail") or "")}</span></td></tr>'
              f'</tbody></table><p><a class="btn sm" href="/settings/verify?refresh=1">Test again</a></p>')
    c = conn()
    counts = c.execute("""SELECT verify_status, COUNT(*) n FROM contact_point WHERE kind='email'
                          GROUP BY verify_status ORDER BY n DESC""").fetchall()
    c.close()
    from . import contacts
    cnt = "".join(f'<tr><td><span class="chip v-{esc(r["verify_status"])}">'
                  f'{esc(contacts.VERIFY_LABEL.get(r["verify_status"], r["verify_status"]))}</span></td>'
                  f'<td class="num">{r["n"]:,}</td></tr>' for r in counts)
    inner = f"""<p class="lede">Bellwether checks each address with the mail server that would receive
it, without sending anything: it asks whether the server would accept that mailbox, then asks the
same about a made-up address at the same domain. Only a server that accepts the real one and turns
away the made-up one counts as verified. This is the method of the open-source check-if-email-exists
(Reacher), built in; a Reacher server can be used instead.</p>
<div class="note plain">Mail servers judge the machine asking. Checks need outbound port 25 and work
best from a server with a fixed address, a reverse DNS name matching the HELO name below, and an SPF
record on the MAIL FROM domain that allows it. From a home or office connection, Microsoft 365 and
others often refuse to answer, which shows as "Could not tell", never as a bad address.</div>
<h3 style="margin-top:18px">Right now</h3>{status}
<h3 style="margin-top:18px">Addresses by result</h3><table class="tight" style="max-width:420px"><tbody>{cnt}</tbody></table>
<section class="s"><h2>Settings</h2>{_group_form("verify")}</section>"""
    return _frame("Email verification", "verify", "How Bellwether confirms an email address exists.",
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
    inner = f"""<p class="lede">Firm websites and the directories on the Enrichment screen are read with
Scrapling, using a real browser's network fingerprint so ordinary sites answer as they would a
person. Pages that only draw themselves with JavaScript are rendered in a headless browser when one
is installed{"" if b else " (none is installed here, so those pages are skipped)"}.</p>
{_group_form("crawl")}"""
    return _frame("Crawling", "crawl", "How firm websites and directories are read.", inner, msg, err)


# ------------------------------------------------------------------ jobs

@router.get("/settings/jobs", response_class=HTMLResponse)
def jobs_page(msg: str = Query("")):
    c = conn()
    ov = jobs.overview(c)
    weekly = c.execute("SELECT * FROM scheduler_state WHERE id=1").fetchone()
    wk = c.execute("SELECT * FROM auto_task WHERE kind='weekly_cycle'").fetchone()
    c.close()
    groups: dict = {}
    for j in ov:
        groups.setdefault(j["job"].group, []).append(j)
    chips = {"running": "lead", "queued": "warn", "scheduled": "line", "paused": "", "waiting": "dis"}
    words = {"running": "Working now", "queued": "Catching up", "scheduled": "Up to date",
             "paused": "Paused", "waiting": "Waiting"}
    sections = []
    for g, items in groups.items():
        rows = []
        for j in items:
            job = j["job"]
            pct = (j["done"] / j["total"] * 100) if j.get("total") else None
            prog = (f'<div class="meter"><i style="width:{pct:.1f}%"></i></div>'
                    f'<div class="meta">{j["done"]:,} of {j["total"]:,}'
                    f'{" . " + format(j["backlog"], ",") + " to go" if j.get("backlog") else ""}</div>'
                    if pct is not None else
                    (f'<div class="meta">{j["backlog"]:,} to go</div>' if j.get("backlog") else ""))
            last = ""
            if j["last_run_at"]:
                lc = "bad" if j["last_status"] in ("failed", "timeout") else "muted"
                last = (f'<div class="small">{esc(ui.ago(j["last_run_at"]))} '
                        f'<span class="{lc}">{esc(j["last_status"] or "")}</span></div>'
                        f'<div class="meta">{esc((j["message"] or "")[:140])}</div>')
            elif j["why"]:
                last = f'<div class="meta warnc">{esc(j["why"])}</div>'
            nxt = esc(ui.ago(j["next_run_at"])) if j["next_run_at"] and j["state"] == "scheduled" else ""
            pause = ("resume", "Resume") if j["state"] == "paused" else ("pause", "Pause")
            rows.append(
                f'<tr><td style="width:30%"><b>{esc(job.label)}</b><div class="meta">{esc(job.blurb)}</div></td>'
                f'<td><span class="chip {chips.get(j["state"], "")}">{words.get(j["state"], j["state"])}</span>'
                f'{"<div class=meta>next " + nxt + "</div>" if nxt else ""}</td>'
                f'<td style="min-width:200px">{prog}</td><td style="width:22%">{last}</td>'
                f'<td class="num nowrap"><form method="post" action="/settings/jobs/{esc(job.kind)}/run" style="display:inline">'
                f'<button class="sm" type="submit">Run now</button></form> '
                f'<form method="post" action="/settings/jobs/{esc(job.kind)}/{pause[0]}" style="display:inline">'
                f'<button class="sm ghost" type="submit">{pause[1]}</button></form></td></tr>')
        sections.append(f'<section class="s"><h2>{esc(jobs.GROUP_LABEL.get(g, g))}</h2>'
                        f'<table><tbody>{"".join(rows)}</tbody></table></section>')
    wmsg = esc((weekly["message"] if weekly else "") or "not checked in yet")
    wlast = esc(ui.ago((weekly or {}).get("last_started")) or "never") if weekly else "never"
    weekly_html = f"""<section class="s"><h2>Weekly SEC cycle</h2><table><tbody><tr>
<td style="width:30%"><b>SEC feeds, people, triggers, scores</b><div class="meta">Captures the SEC's
weekly adviser and individual feeds as soon as they publish, then rebuilds firms, people, hiring,
signals and scores. Runs by itself when a new feed is due.</div></td>
<td><span class="chip line">Automatic</span></td><td class="small soft">{wmsg}</td>
<td class="small">Last started {wlast}</td>
<td class="num"><form method="post" action="/settings/jobs/weekly_cycle/run"><button class="sm" type="submit">Run now</button></form></td>
</tr></tbody></table></section>"""
    inner = (f'<p class="lede">Every job runs by itself: it works through its backlog in short '
             f'slices, best firms first, then checks back on its own schedule. Run now makes a job '
             f'go immediately; Pause holds it until you resume it.</p>{weekly_html}{"".join(sections)}')
    return _frame("Jobs", "jobs", "What Bellwether is doing in the background.", inner, msg)


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
    word = {"run": "will run within a minute", "pause": "is paused",
            "resume": "is running by itself again"}[action]
    return RedirectResponse(f"/settings/jobs?{qs_join(msg=jobs.BY_KIND[kind].label + ' ' + word + '.')}",
                            status_code=303)


# ------------------------------------------------------------------ system

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
            counts.append(f'<tr><td>{t}</td><td class="num">{n:,}</td></tr>')
    c.close()
    beat = ""
    if sched and sched["last_check"]:
        mins = (datetime.now(timezone.utc) - datetime.fromisoformat(sched["last_check"])).total_seconds() / 60
        beat = (f'<p><b>Scheduler:</b> {"<span class=ok>on</span>" if mins < 15 else "<span class=bad>stalled</span>"}, '
                f'last checked {mins:.0f} minutes ago. {esc(sched["message"] or "")}.</p>')
    rrow = []
    for r in runs:
        cls = {"ok": "ok", "failed": "bad", "skipped": "warnc", "running": "warnc"}.get(r["status"], "")
        flag = ' <b class="warnc">FLAGGED</b>' if r["flagged"] else ""
        rrow.append(f'<tr><td>{esc(r["source_key"])}</td><td>{esc(r["stage"])}</td>'
                    f'<td class="{cls}"><b>{esc(r["status"])}</b>{flag}</td>'
                    f'<td class="num">{r["rows_out"] if r["rows_out"] is not None else "-"}</td>'
                    f'<td class="small">{esc(ui.ago(r["finished_at"] or r["started_at"]))}</td>'
                    f'<td class="small soft">{esc((r["message"] or "")[:140])}</td></tr>')
    srow = "".join(f'<tr><td>{esc(s["source_key"])}</td><td>{esc(s["published_at"])}</td>'
                   f'<td class="num">{s["bytes"]:,}</td><td class="small">{esc(s["captured_at"][:10])}</td></tr>'
                   for s in snaps)
    inner = f"""{beat}
<section class="s"><h2>Recent runs</h2><table class="tight"><thead><tr><th>Source</th><th>Stage</th>
<th>Status</th><th class="num">Rows</th><th>When</th><th>Message</th></tr></thead>
<tbody>{"".join(rrow)}</tbody></table></section>
<section class="s"><div class="cols-2"><div><h3>Immutable snapshots</h3><table class="tight">
<thead><tr><th>Source</th><th>Published</th><th class="num">Bytes</th><th>Captured</th></tr></thead>
<tbody>{srow}</tbody></table></div><div><h3>Record counts</h3><table class="tight"><tbody>{"".join(counts)}</tbody></table>
<p class="meta"><a href="/settings/system.json">Raw JSON</a></p></div></div></section>"""
    return _frame("System", "system", "Whether the data is current and what has run. Red means failed "
                  "or stale; amber means flagged.", inner)


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
