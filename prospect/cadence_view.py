"""Push to Cadence: the JSON routes the push dialog calls, the bits of People
and the firm page that show where each person stands in Cadence, and
Settings, Cadence.

  POST /api/cadence/preview   what a push would do; writes nothing (prospect/cadence.py preview)
  POST /api/cadence/push      push, first request inline, the rest in the background
  GET  /api/cadence/batch/id  a push's per-contact results so far (its creator, or an admin)
  POST /api/cadence/refresh   Cadence's status by id for the people on a firm page
  POST /api/cadence/suppress  do not contact
  GET  /settings/cadence      admins: connection, switch, caps, queue, audit, suppressions

Everyone signed in can push (decision 16). The middleware already refuses
/api to anyone signed out and /settings to anyone who is not an admin. The
handlers are plain functions, so FastAPI runs them on its thread pool and a
60-second wait on Cadence never holds up another request.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

from fastapi import APIRouter, Body, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import cadence, ui, users
from .webapp import (conn, current_account, current_user, esc, is_admin, nice_name, public_base,
                     qs_join)

router = APIRouter()
BATCH_RE = re.compile(r"^[0-9a-f-]{36}$")


def _j(ok: bool = True, **kw) -> JSONResponse:
    return JSONResponse({"ok": ok, **kw})


def _ids(payload: dict) -> list[str]:
    raw = payload.get("ids") if isinstance(payload, dict) else None
    return [str(x) for x in (raw or []) if isinstance(x, str) and cadence.parse_id(x)][:1000]


def _selection(payload: dict) -> tuple[list[str], str | None, str | None]:
    """(ids, the People filter they came from, a problem). "All matching" on
    People sends the filter instead of ids; it is resolved here with the same
    WHERE the page used, up to the per-push cap."""
    if not isinstance(payload, dict) or payload.get("filter") is None:
        return _ids(payload), None, None
    from . import people_index, people_view
    qs = str(payload.get("filter") or "")[:2000]
    q = parse_qs(qs, keep_blank_values=False)
    f = people_view._filters(**{k: (",".join(v) if k == "des" else v[-1])
                                for k, v in q.items() if k in people_view.FIELDS})
    cap = cadence.max_per_push()
    c = conn()
    try:
        if not people_index.exists(c):
            return [], qs, "The people index is still being built."
        cols = people_index.columns(c)
        where, args = people_view._where(f, cols)
        rows = c.execute(f"SELECT crd, person_key FROM people_index WHERE {where}"
                         f" ORDER BY {people_view._order(f, cols)} LIMIT ?", args + [cap + 1]).fetchall()
    finally:
        c.close()
    if len(rows) > cap:
        return [], qs, (f"More than {cap:,} people match. Narrow the filter; at most {cap:,} go in "
                        "one push.")
    return [cadence.contact_id(r["crd"], r["person_key"]) for r in rows], qs, None


def _base(request: Request) -> str:
    try:
        return public_base(request)
    except Exception:
        return ""


def _lines(c, ids: list[str]) -> dict:
    """The status line and whether the person can be pushed, for the page to
    update in place once the dialog closes."""
    info = cadence.status_map(c, [p for p in (cadence.parse_id(i) for i in ids) if p])
    return {cid: {"html": status_html(v), "can_push": v["can_push"]} for cid, v in info.items()}


# ------------------------------------------------------------------ routes

@router.post("/api/cadence/preview")
def api_preview(request: Request, payload: dict = Body(default={})):
    acct = current_account() or {}
    ids, qs, err = _selection(payload)
    owner = cadence.norm_email(payload.get("owner")) or cadence.account_email(acct)
    extra = {"owners": [{"email": e, "name": n} for e, n in cadence.owner_choices()],
             "owner": owner, "products": [{"key": k, "name": n} for k, n in cadence.product_choices()],
             "note_max": cadence.NOTE_MAX, "max_per_push": cadence.max_per_push()}
    if err:
        return _j(True, problem=err, send=[], skipped=[], disqualified=[], count=0, can_send=False,
                  check={"state": "off", "message": ""}, product=None, preview_id=None, **extra)
    if not ids:
        return _j(True, problem="Nothing selected.", send=[], skipped=[], disqualified=[], count=0,
                  can_send=False, check={"state": "off", "message": ""}, product=None,
                  preview_id=None, **extra)
    c = conn()
    try:
        out = cadence.preview(c, ids, account=acct, product=payload.get("product"),
                              owner_email=owner, note=payload.get("note"), base_url=_base(request))
    finally:
        c.close()
    out.update(extra)
    return JSONResponse(out)


@router.post("/api/cadence/push")
def api_push(request: Request, payload: dict = Body(default={})):
    acct = current_account() or {}
    ids, qs, err = _selection(payload)
    if err:
        return _j(False, message=err)
    c = conn()
    try:
        out = cadence.push(c, ids, account=acct, product=payload.get("product"),
                           owner_email=payload.get("owner"), note=payload.get("note"),
                           confirm_disqualified=bool(payload.get("confirm")),
                           preview_id=str(payload.get("preview_id") or "") or None,
                           origin=str(payload.get("origin") or "people"), filter_qs=qs,
                           base_url=_base(request))
        if out.get("ok"):
            out["lines"] = _lines(c, [r["id"] for r in out["results"] if r.get("id")])
    finally:
        c.close()
    return JSONResponse(out)


@router.get("/api/cadence/batch/{bid}")
def api_batch(bid: str):
    if not BATCH_RE.match(bid):
        return _j(False, message="No such push.")
    c = conn()
    try:
        out = cadence.batch_view(c, bid)
        if out.get("ok") and out["created_by"] != current_user() and not is_admin():
            return _j(False, message="No such push.")
        if out.get("ok"):
            out["lines"] = _lines(c, [r["id"] for r in out["results"] if r.get("id")])
    finally:
        c.close()
    return JSONResponse(out)


@router.post("/api/cadence/refresh")
def api_refresh(payload: dict = Body(default={})):
    ids = _ids(payload)[:cadence.CONTRACT_MAX]
    if not ids:
        return _j(True, lines={}, changed=0)
    c = conn()
    try:
        res = cadence.refresh(c, ids, current_user() or "")
        lines = _lines(c, ids)
    finally:
        c.close()
    msg = (res.get("message") or "") if not res.get("ok") else (
        "Up to date with Cadence." if not res.get("changed") else
        f"{ui.plural(res['changed'], 'status', 'statuses')} updated from Cadence.")
    return _j(bool(res.get("ok")), message=msg, lines=lines, changed=res.get("changed", 0))


SUPP_REASONS = {"opted_out": "Asked not to be contacted", "do_not_call": "Do not call",
                "do_not_email": "Do not email", "other": "Other"}


@router.post("/api/cadence/suppress")
def api_suppress(id: str = Form(""), scope: str = Form("person"), channel: str = Form("all"),
                 reason: str = Form("other"), note: str = Form("")):
    pair = cadence.parse_id(id)
    if not pair:
        return _j(False, message="Not a person Bellwether knows.")
    if reason not in SUPP_REASONS or channel not in ("all", "email", "call") \
            or scope not in ("person", "firm"):
        return _j(False, message="Choose what the do not contact covers.")
    value = id if scope == "person" else pair[0]
    c = conn()
    try:
        added = cadence.suppress(c, scope, value, channel=channel, reason=reason, source="manual",
                                 note=note.strip()[:300], by=current_user())
        c.commit()
        lines = _lines(c, [id])
    finally:
        c.close()
    return _j(True, message=("Marked do not contact. Nobody can push "
                             + ("them" if scope == "person" else "anyone at this firm")
                             + " to Cadence until an admin lifts it.") if added
              else "Already marked do not contact.", lines=lines, reload=added)


# ------------------------------------------------------------------ what People and the firm page show

def status_html(info: dict | None, history: list[str] | None = None) -> str:
    """The quiet line under a person's title: neutral words, readable red only
    for a refusal or a failure, and Open in CRM once Cadence has a link."""
    if not info or not info.get("text"):
        return ""
    tip = ui.tip_attr("Cadence", "", history or [], "History") if history else ""
    row = info.get("row") or {}
    link = ""
    if row.get("crm_url") and not info.get("supp"):
        link = (f' <a class="cad-crm" href="{esc(row["crm_url"])}" target="_blank" rel="noopener"'
                f' data-noprefetch>Open in CRM</a>')
    cls = "cad-st" + (" bad" if info.get("tone") == "bad" else "")
    return f'<span class="{cls}"{tip}>{esc(info["text"])}</span>{link}'


def card_controls(cid: str, name: str, info: dict | None, history: list[str] | None = None,
                  newer: bool = False) -> str:
    """Under a person card's contact lines: Select and Push to Cadence while
    the person can be pushed, else where they stand, with who pushed them,
    and Refresh. Do not contact either way."""
    info = info or {"can_push": True, "pushed": False}
    can = info.get("can_push", True)
    row = info.get("row") or {}
    parts = []
    if can:
        parts.append(f'<label class="cad-pick"><input type="checkbox" data-cad-id="{esc(cid)}"'
                     f' data-cad-name="{esc(name)}"> Select</label>'
                     f'<button type="button" class="sm" data-cad-push="{esc(cid)}"'
                     f' data-cad-name="{esc(name)}">Push to Cadence</button>')
    line = status_html(info, history)
    meta = ""
    if row and info.get("pushed"):
        meta = (f'<span class="meta">by {esc(users.display_name(row.get("pushed_by")))}'
                f'{", " + esc(ui.ago(row.get("first_pushed_at"))) if row.get("first_pushed_at") else ""}'
                f'</span>')
        if newer:
            meta += '<span class="meta warnc">Newer details in Bellwether</span>'
    parts.append(f'<span class="cad-line" data-cad-status="{esc(cid)}">{line}</span>{meta}')
    if info.get("pushed"):
        parts.append(f'<button type="button" class="sm ghost" data-cad-refresh="{esc(cid)}"'
                     f' data-cad-pushed="{esc(cid)}">Refresh</button>')
    if not info.get("supp"):
        parts.append(f'<button type="button" class="sm ghost" data-cad-dnc="{esc(cid)}"'
                     f' data-cad-name="{esc(name)}">Do not contact</button>')
    return f'<div class="cad-ctl">{"".join(parts)}</div>'


def context(origin: str, product: str = "", total: int = 0, qs: str = "") -> str:
    """What the page's push controls need to know, read by app.js."""
    return (f'<div id="cad-ctx" hidden data-origin="{esc(origin)}" data-product="{esc(product)}"'
            f' data-total="{int(total)}" data-qs="{esc(qs)}"></div>')


# ------------------------------------------------------------------ Settings, Cadence

ACTION_WORDS = {"push": "Pushed", "retry": "Retried", "skipped": "Skipped",
                "result": "Cadence answered", "status": "Status from Cadence",
                "suppress": "Do not contact set", "lift": "Do not contact lifted", "hold": "Queue"}
OUTCOME_WORDS = {"created": "Added to the sandbox", "duplicate_in_sandbox": "Already in the sandbox",
                 "already_in_crm": "Already in the CRM", "in_sandbox": "In Cadence",
                 "moved_to_crm": "Moved to the CRM", "opted_out": "Opted out", "removed": "Removed",
                 "not_found": "Cadence has no record of it", "unauthorized": "Held: Cadence refused the key",
                 "integration_disabled": "Held: the integration is switched off in Cadence",
                 "lifted": "Hold lifted", "timeout": "No answer within 60 seconds",
                 "connection": "Could not reach Cadence", "expired": "Key older than 7 days"}
RULE_WORDS = {"not_found": "not in the people list yet", "no_name": "no first and last name",
              "no_contact": "no email and no phone", "opted_out": "opted out in Cadence",
              "do_not_contact": "do not contact", "customer": "a Customer firm",
              "in_cadence": "already pushed", "other_firm": "already pushed from another firm",
              "same_email": "same email as someone pushed", "same_in_push": "same email twice",
              "already_in_crm": "already in the CRM", "cadence_refuses": "Cadence would refuse"}


def outcome_words(outcome: str | None) -> str:
    """An audit outcome code in words, for Settings, Cadence."""
    out = outcome or ""
    kind, _, rest = out.partition(":")
    if kind == "rejected":
        return "Refused: " + cadence.REASON_WORDS.get(rest, rest or "no reason given")
    if kind == "failed":
        return "Failed: " + OUTCOME_WORDS.get(rest, rest.replace("_", " ") or "no answer")
    if kind == "skipped":
        return "Skipped: " + RULE_WORDS.get(rest, rest.replace("_", " "))
    if kind in ("cadence", "manual"):
        return ("Opted out in Cadence" if kind == "cadence" else
                "Set by hand: " + SUPP_REASONS.get(rest, rest.replace("_", " ")).lower())
    if out.startswith("http_"):
        return "Cadence answered " + out[5:].replace("_", " ")
    return OUTCOME_WORDS.get(out, out.replace("_", " "))


SCOPE_WORDS = {"person": "Person", "email": "Email address", "phone": "Phone number",
               "domain": "Email domain", "firm": "Everyone at a firm"}
CHANNEL_WORDS = {"all": "Email and calls", "email": "Email", "call": "Calls"}


def _target(crd: str, key: str, names: dict, scope: str | None = None) -> str:
    """Who an audit row is about, by name, linked to their card or firm."""
    if crd and key:
        name = names.get((crd, key)) or cadence.contact_id(crd, key)
        return f'<a href="/firm/{esc(crd)}#{esc(cadence.anchor(key))}">{esc(name)}</a>'
    if crd:
        return f'<a href="/firm/{esc(crd)}">{esc(names.get((crd, "")) or "CRD " + crd)}</a>'
    return esc({"email": "An email address", "phone": "A phone number",
                "domain": "An email domain"}.get(scope or "", "The whole queue"))


def _event_line(e: dict, names: dict) -> str:
    who = "Bellwether" if e["actor"] == "system" else users.display_name(e["actor"])
    try:
        scope = (json.loads(e["detail"]) if e.get("detail") else {}).get("scope")
    except ValueError:
        scope = None
    return (f'<tr><td class="nowrap">{esc(ui.ago(e["at"]))}</td><td>{esc(who)}</td>'
            f'<td>{esc(ACTION_WORDS.get(e["action"], e["action"]))}</td>'
            f'<td>{esc(outcome_words(e["outcome"]))}</td>'
            f'<td>{_target(e["crd"], e["person_key"], names, scope)}</td></tr>')


def _names(c, pairs: set) -> dict:
    """Names for audit rows: as pushed, else from the People index, else the firm."""
    out: dict = {}
    people = [p for p in pairs if p[1]]
    for i in range(0, len(people), 100):
        chunk = people[i:i + 100]
        cond = " OR ".join("(crd = ? AND person_key = ?)" for _ in chunk)
        args = [x for p in chunk for x in p]
        for table in ("cadence_push", "people_index"):
            try:
                for r in c.execute(f"SELECT crd, person_key, name FROM {table} WHERE {cond}", args):
                    out.setdefault((r["crd"], r["person_key"]), r["name"])
            except Exception:
                c.rollback()
    crds = sorted({p[0] for p in pairs if p[0] and not p[1]})
    if crds:
        for r in c.execute(f"SELECT crd, legal_name FROM firm_current WHERE crd IN ({','.join('?' * len(crds))})",
                           crds):
            out[(r["crd"], "")] = nice_name(r["legal_name"])
    return out


@router.get("/settings/cadence", response_class=HTMLResponse)
def settings_page(msg: str = Query(""), err: str = Query(""), who: str = Query(""),
                  outcome: str = Query("")):
    from .settings_view import _frame, _group_form, how
    url_set, key_set = bool(cadence.api_url()), bool(cadence.api_key())
    c = conn()
    try:
        ok = cadence.ready(c)
        states = {r["state"]: r["n"] for r in c.execute(
            "SELECT state, COUNT(*) n FROM cadence_push GROUP BY state")} if ok else {}
        reqs = {r["state"]: r["n"] for r in c.execute(
            "SELECT state, COUNT(*) n FROM cadence_request GROUP BY state")} if ok else {}
        hold = cadence.held(c) if ok else None
        last = cadence._state_get(c, "last_sync_ok") if ok else None
        sync_err = cadence._state_get(c, "last_sync_error") if ok else None
        where, args = ["1=1"], []
        if who:
            where.append("actor = ?")
            args.append(who)
        if outcome:
            where.append("outcome LIKE ?")
            args.append(outcome[:40] + "%")
        events = [dict(r) for r in c.execute(
            f"SELECT * FROM cadence_push_event WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 100",
            args)] if ok else []
        actors = [r["actor"] for r in c.execute(
            "SELECT DISTINCT actor FROM cadence_push_event ORDER BY actor")] if ok else []
        supp = [dict(r) for r in c.execute(
            "SELECT * FROM contact_suppression WHERE lifted_at IS NULL ORDER BY id DESC LIMIT 200")] if ok else []
        pairs = {(e["crd"], e["person_key"]) for e in events if e["crd"]}
        pairs |= {cadence.parse_id(s["value"]) for s in supp if s["scope"] == "person"
                  and cadence.parse_id(s["value"])}
        pairs |= {(s["value"], "") for s in supp if s["scope"] == "firm"}
        names = _names(c, pairs) if ok else {}
        c.commit()
    finally:
        c.close()

    def flag(name: str, is_set: bool) -> str:
        return (f'<span class="chip {"good" if is_set else "line"}">{esc(name)}'
                f' {"is set" if is_set else "is not set"}</span>')
    problem = cadence.problem()
    state = ('<div class="note good set-status">Connected. Everyone can push to Cadence.'
             if not problem and not hold else
             f'<div class="note bad set-status">{esc(cadence._hold_words(hold))}' if hold else
             f'<div class="note plain set-status">{esc(problem)}')
    state += (f' <span class="pills">{flag("CADENCE_API_URL", url_set)}{flag("CADENCE_API_KEY", key_set)}'
              f'</span><button class="sm" data-post="/settings/cadence/test" data-busy="Testing">'
              f'Test the connection</button></div>')
    sync_line = (f'Last status read from Cadence {esc(ui.ago(last))}.' if last else
                 'No status read from Cadence yet.')
    if sync_err:
        sync_line += f' <span class="warnc">Last error: {esc(sync_err.split(" ", 1)[-1])}</span>'
    labels = {"sending": "Sending", "retry_wait": "Waiting to retry", "in_sandbox": "In Cadence",
              "in_crm": "Already in the CRM", "moved_to_crm": "Moved to CRM",
              "opted_out": "Opted out (Cadence)", "rejected": "Refused", "removed": "Removed",
              "failed": "Failed"}
    queue = "".join(f'<tr><td>{esc(labels.get(k, k))}</td><td class="num">{int(v):,}</td></tr>'
                    for k, v in sorted(states.items(), key=lambda kv: -kv[1]))
    retry = ""
    if states.get("failed"):
        retry = (f'<form method="post" action="/settings/cadence/retry" class="set-actions">'
                 f'<button class="sm" type="submit">Retry {int(states["failed"]):,} failed</button>'
                 f'<span class="meta">Each goes again as a new request with a new key, in the '
                 f'name of whoever pushed it.</span></form>')
    req_line = ", ".join(f"{int(v):,} {k.replace('_', ' ')}" for k, v in sorted(reqs.items()))
    who_opts = ui.opt("", who, "Anyone") + "".join(ui.opt(a, who, users.display_name(a) if a != "system"
                                                                else "Bellwether") for a in actors)
    out_opts = "".join(ui.opt(k, outcome, lab) for k, lab in (
        ("", "Any outcome"), ("created", "Added"), ("duplicate_in_sandbox", "Already in sandbox"),
        ("already_in_crm", "Already in the CRM"), ("rejected", "Refused"), ("failed", "Failed"),
        ("skipped", "Skipped"), ("http_", "Errors")))
    ev_rows = "".join(_event_line(e, names) for e in events)

    def covers(s):
        pair = cadence.parse_id(s["value"]) if s["scope"] == "person" else None
        if pair:
            return _target(pair[0], pair[1], names)
        if s["scope"] == "firm":
            return _target(s["value"], "", names)
        return esc(s["value"])
    supp_rows = "".join(
        f'<tr><td>{esc(SCOPE_WORDS.get(s["scope"], s["scope"]))}</td><td>{covers(s)}</td>'
        f'<td>{esc(CHANNEL_WORDS.get(s["channel"], s["channel"]))}</td>'
        f'<td>{esc(SUPP_REASONS.get(s["reason"], s["reason"]))}</td>'
        f'<td>{"Cadence" if s["source"] == "cadence" else esc(users.display_name(s["set_by"]))}</td>'
        f'<td class="nowrap">{esc(ui.ago(s["set_at"]))}</td>'
        f'<td class="num"><form method="post" action="/settings/cadence/lift">'
        f'<input type="hidden" name="sid" value="{int(s["id"])}">'
        f'<button class="sm ghost" type="submit">Lift</button></form></td></tr>' for s in supp)
    tip = how("Cadence", "Bellwether pushes contacts, never firms, into Cadence's outreach sandbox. "
              "Before a push it asks Cadence who is already in the CRM and leaves them out. Every "
              "10 minutes it reads back who was claimed, moved to the CRM or opted out.",
              ["CADENCE_API_URL: Cadence's address, https", "CADENCE_API_KEY: the same value Cadence "
               "holds as BELLWETHER_API_KEY"], "Set in the server environment, never here")
    inner = f"""{state}
{_group_form("cadence")}
<section class="s"><h2>Queue</h2><p class="lede">{sync_line}{(" Requests: " + esc(req_line) + ".") if req_line else ""}</p>
<table class="tight"><thead><tr><th>Where pushed people stand</th><th class="num">People</th></tr></thead>
<tbody>{queue or '<tr><td colspan="2" class="empty">Nobody pushed yet.</td></tr>'}</tbody></table>{retry}</section>
<section class="s"><h2>Recent activity</h2>
<form class="fbar" method="get" action="/settings/cadence"><label><select name="who" aria-label="Who">{who_opts}</select></label>
<label><select name="outcome" aria-label="Outcome">{out_opts}</select></label><button class="sm" type="submit">Show</button></form>
<table class="tight"><thead><tr><th>When</th><th>Who</th><th>What</th><th>Outcome</th><th>Contact</th></tr></thead>
<tbody>{ev_rows or '<tr><td colspan="5" class="empty">Nothing yet.</td></tr>'}</tbody></table></section>
<section class="s"><h2>Do not contact</h2><p class="lede">Nobody on this list can be pushed. Opt-outs from Cadence arrive by themselves.</p>
<table class="tight"><thead><tr><th>Covers</th><th>Who or what</th><th>Channel</th><th>Reason</th><th>Set by</th><th>When</th><th></th></tr></thead>
<tbody>{supp_rows or '<tr><td colspan="7" class="empty">Nobody is marked do not contact.</td></tr>'}</tbody></table></section>"""
    return _frame("Cadence", "cadence", "Push contacts into Cadence's outreach sandbox, and see "
                  "what happened to them." + tip, inner, msg, err)


@router.post("/settings/cadence/test")
def settings_test():
    c = conn()
    try:
        ok, info, words = cadence.ping(c)
        if ok and info.get("sandboxEnabled") and cadence.held(c):
            cadence.lift_hold(c, current_user() or "system")
            c.commit()
            words += " The held queue goes again now."
    finally:
        c.close()
    return _j(ok, message=words)


@router.post("/settings/cadence/lift")
def settings_lift(sid: int = Form(...)):
    c = conn()
    try:
        done = cadence.lift(c, sid, current_user() or "")
        c.commit()
    finally:
        c.close()
    msg = {"msg": "Lifted. The person can be pushed again."} if done else {"err": "Already lifted."}
    return RedirectResponse(f"/settings/cadence?{qs_join(**msg)}", status_code=303)


@router.post("/settings/cadence/retry")
def settings_retry():
    if cadence.problem():
        return RedirectResponse(f"/settings/cadence?{qs_join(err=cadence.problem())}", status_code=303)
    c = conn()
    try:
        n = cadence.retry_failed(c, current_user() or "", current_account())
    finally:
        c.close()
    return RedirectResponse(f"/settings/cadence?{qs_join(msg=f'{n:,} sent again.')}", status_code=303)
