"""Send to Cold outreach: the JSON routes the send dialog calls, the bits of
People and the firm page that show where each person stands in Cadence, and
Settings, Cadence.

  POST /api/cadence/preview   what a send would do (prospect/cadence.py preview)
  POST /api/cadence/push      send, first request inline, the rest in the background
  GET  /api/cadence/batch/id  a send's per-contact results so far (its sender, or an admin)
  GET  /api/cadence/batch     the same, found by the dialog's own key (?key=) after a
                              gateway error cut the answer off (B078)
  POST /api/cadence/refresh   Cadence's status by id for the people on a firm page
  POST /api/cadence/suppress  do not contact; Cadence is told when it takes it (B003)
  POST /api/cadence/lift      lift a Do not contact set here: admins only, after a confirm
  GET  /settings/cadence      admins: connection, switch, caps, queue, sends, suppressions

The screens say "Send to Cold outreach" and never "sandbox"; the code keeps the
contract's words. My sends is not a page of its own: it is People's Cold
outreach filter "Sent by me", grouped by send (people_view).

Everyone signed in can send (decision 16). The middleware already refuses
/api to anyone signed out and /settings to anyone who is not an admin. The
handlers are plain functions, so FastAPI runs them on its thread pool, and
every call they make to Cadence has a hard deadline and holds no pooled
database connection while it waits (B016).
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

from fastapi import APIRouter, Body, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import cadence, ui, users
from .webapp import (ICONS, conn, current_account, current_user, esc, is_admin, nice_name, public_base,
                     qs_join)

router = APIRouter()
BATCH_RE = re.compile(r"^[0-9a-f-]{36}$")
KEY_RE = re.compile(r"^[0-9A-Za-z-]{8,64}$")


def _j(ok: bool = True, **kw) -> JSONResponse:
    return JSONResponse({"ok": ok, **kw})


def _ids(payload: dict) -> list[str]:
    raw = payload.get("ids") if isinstance(payload, dict) else None
    return [str(x) for x in (raw or []) if isinstance(x, str) and cadence.parse_id(x)][:1000]


def _selection(payload: dict) -> tuple[list[str], str | None, str | None, bool]:
    """(ids, the People filter they came from, a problem, whether the problem
    is only that more people match than one send takes). "All matching" on
    People sends the filter instead of ids; it is resolved here with the same
    WHERE the page used, up to the per-send cap. Over the cap, the dialog
    offers the first ones the page shows ("first": true, B077). A firm page
    has no "all matching": only ids come from there (B019)."""
    if not isinstance(payload, dict) or payload.get("filter") is None:
        return _ids(payload), None, None, False
    if payload.get("origin") == "firm":
        return [], None, "Tick the people to send on this page.", False
    from . import people_index, people_view
    qs = str(payload.get("filter") or "")[:2000]
    q = parse_qs(qs, keep_blank_values=False)
    f = people_view._filters(**{k: (",".join(v) if k == "des" else v[-1])
                                for k, v in q.items() if k in people_view.FIELDS})
    f["me"] = current_user() or ""
    cap = cadence.max_per_push()
    c = conn()
    try:
        if not people_index.exists(c):
            return [], qs, "The people index is still being built.", False
        cols = people_index.columns(c)
        where, args = people_view._where(f, cols)
        rows = c.execute(f"SELECT crd, person_key FROM people_index WHERE {where}"
                         f" ORDER BY {people_view._order(f, cols)} LIMIT ?", args + [cap + 1]).fetchall()
    finally:
        c.close()
    if len(rows) > cap and not payload.get("first"):
        return [], qs, (f"More than {cap:,} people match, and at most {cap:,} go in one send. Send the "
                        f"first {cap:,}, or narrow the filter."), True
    return [cadence.contact_id(r["crd"], r["person_key"]) for r in rows[:cap]], qs, None, False


def _base(request: Request) -> str:
    try:
        return public_base(request)
    except Exception:
        return ""


def _lines(c, ids: list[str], cards: bool = False) -> dict:
    """Each person's status line, with its history hover (B178), and whether
    they can be sent, for the page to update in place. With cards, the whole
    card controls too, so a person who can be sent again gets Select and Send
    back without a reload (N094)."""
    pairs = [p for p in (cadence.parse_id(i) for i in ids) if p]
    info = cadence.status_map(c, pairs)
    hist: dict = {}
    for crd in sorted({p[0] for p in pairs}):
        hist[crd] = cadence.history(c, crd)
    out = {}
    recs = cadence.load_records(c, list(info)) if cards else {}
    for cid, v in info.items():
        crd, key = cadence.parse_id(cid)
        h = hist.get(crd, {}).get(key)
        line = {"html": status_html(v, h), "can_push": v["can_push"]}
        if cards:
            rec = recs.get(cid)
            name = (rec or {}).get("name") or (v.get("row") or {}).get("name") or ""
            newer = None
            if rec:
                em, ph = cadence._email_of(rec), (rec["phones"] or [None])[0]
                newer = cadence.newer_details(v.get("row"), em["value"] if em else None,
                                              ph["value"] if ph else None,
                                              cadence.country_code(rec["firm"].get("country")))
            line["card"] = card_controls(cid, name, v, h, newer)
        out[cid] = line
    return out


# ------------------------------------------------------------------ routes

@router.post("/api/cadence/preview")
def api_preview(request: Request, payload: dict = Body(default={})):
    acct = current_account() or {}
    ids, qs, err, over = _selection(payload)
    owner = cadence.norm_email(payload.get("owner")) or cadence.account_email(acct)
    extra = {"products": [{"key": k, "name": n} for k, n in cadence.product_choices()],
             "note_max": cadence.NOTE_MAX, "max_per_push": cadence.max_per_push()}
    empty = dict(send=[], skipped=[], disqualified=[], again=[], count=0, can_send=False,
                 check={"state": "off", "message": ""}, product=None, preview_id=None, owner=owner)
    if err:
        return _j(True, problem=err, nothing=err, over=over, **empty, owners=[], **extra)
    if not ids:
        return _j(True, problem="Nobody selected.", nothing="Nobody selected.", **empty, owners=[], **extra)
    c = conn()
    try:
        out = cadence.preview(c, ids, account=acct, product=payload.get("product"),
                              owner_email=owner, note=payload.get("note"), base_url=_base(request),
                              reuse=str(payload.get("reuse") or "") or None)
        owners = [{"email": e, "name": n} for e, n in cadence.owner_choices(c)]
    finally:
        c.close()
    if owner and owner not in {o["email"] for o in owners}:
        # The sender always sees themselves, marked if Cadence will not give them the contacts.
        owners.insert(0, {"email": owner, "name": (acct.get("name") or owner)})
    out.update(extra, owners=owners)
    return JSONResponse(out)


@router.post("/api/cadence/push")
def api_push(request: Request, payload: dict = Body(default={})):
    acct = current_account() or {}
    ids, qs, err, _ = _selection(payload)
    if err:
        return _j(False, message=err)
    key = str(payload.get("send_key") or "")
    c = conn()
    try:
        out = cadence.push(c, ids, account=acct, product=payload.get("product"),
                           owner_email=payload.get("owner"), note=payload.get("note"),
                           confirm_disqualified=bool(payload.get("confirm")),
                           preview_id=str(payload.get("preview_id") or "") or None,
                           origin=str(payload.get("origin") or "people"), filter_qs=qs,
                           base_url=_base(request), client_key=key if KEY_RE.match(key) else None)
        if out.get("ok"):
            out["lines"] = _lines(c, [r["id"] for r in out["results"] if r.get("id")])
    finally:
        c.close()
    return JSONResponse(out)


def _batch(c, bid: str) -> dict:
    out = cadence.batch_view(c, bid)
    if out.get("ok") and out["created_by"] != current_user() and not is_admin():
        return {"ok": False, "message": "No such send."}
    if out.get("ok"):
        out["lines"] = _lines(c, [r["id"] for r in out["results"] if r.get("id")])
    return out


@router.get("/api/cadence/batch/{bid}")
def api_batch(bid: str):
    if not BATCH_RE.match(bid):
        return _j(False, message="No such send.")
    c = conn()
    try:
        out = _batch(c, bid)
    finally:
        c.close()
    return JSONResponse(out)


@router.get("/api/cadence/batch")
def api_batch_by_key(key: str = Query("")):
    """The send the dialog started, by its own key: after a gateway error the
    dialog never saw the batch id, and the send may well have gone (B078)."""
    if not KEY_RE.match(key):
        return _j(False, message="No such send.", found=False)
    c = conn()
    try:
        r = c.execute("SELECT id FROM cadence_batch WHERE client_key=? AND created_by=?",
                      (key, current_user() or "")).fetchone()
        out = _batch(c, r["id"]) if r else {"ok": False, "message": "No such send.", "found": False}
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
        lines = _lines(c, ids, cards=bool(payload.get("cards")))
    finally:
        c.close()
    bits = []
    if res.get("changed"):
        bits.append(f"{ui.plural(res['changed'], 'status', 'statuses')} updated")
    if res.get("owners"):
        bits.append(f"{ui.plural(res['owners'], 'owner')} changed")
    msg = (res.get("message") or "") if not res.get("ok") else (
        ("Up to date with Cadence." if not bits else ", ".join(bits).capitalize() + " in Cadence."))
    return _j(bool(res.get("ok")), message=msg, lines=lines, changed=res.get("changed", 0))


SUPP_REASONS = {"opted_out": "Asked not to be contacted", "do_not_call": "Do not call",
                "do_not_email": "Do not email", "other": "Other", "erasure": "Erased"}
TOLD_WORDS = {"sent": "Cadence opted them out too.",
              "pending": "Cadence did not answer; Bellwether tells it within 10 minutes.",
              "not_sent": "Cadence has not been told: opt them out in Cadence too."}


@router.post("/api/cadence/suppress")
def api_suppress(id: str = Form(""), scope: str = Form("person"), reason: str = Form("other"),
                 note: str = Form(""), confirm: str = Form("")):
    """Do not contact, for a person or everyone at their firm. The Channel
    choice is gone until contract v1.2 can carry it (B075): it always means
    email and calls. Everyone at the firm needs the confirm that names how
    many people it covers."""
    pair = cadence.parse_id(id)
    if not pair:
        return _j(False, message="Not a person Bellwether knows.")
    if reason not in SUPP_REASONS or reason == "erasure" or scope not in ("person", "firm"):
        return _j(False, message="Choose who the Do not contact covers, and why.")
    if scope == "firm" and confirm != "1":
        return _j(False, message="Tick the box to confirm it covers everyone at the firm.")
    value = id if scope == "person" else pair[0]
    c = conn()
    try:
        added = cadence.suppress(c, scope, value, reason=reason, source="manual",
                                 note=note.strip()[:300], by=current_user())
        c.commit()
        told = {"state": None}
        if added:
            sid = c.execute("SELECT id FROM contact_suppression WHERE scope=? AND value=? AND channel='all'"
                            " AND lifted_at IS NULL", (scope, value)).fetchone()["id"]
            told = cadence.tell_cadence(c, sid, current_user() or "")
        lines = _lines(c, [id])
    finally:
        c.close()
    if not added:
        return _j(True, message="Already marked Do not contact.", lines=lines)
    who = "them" if scope == "person" else "anyone at this firm"
    return _j(True, message=(f"Marked Do not contact. Nobody can send {who} to Cold outreach until an "
                             f"admin lifts it. {TOLD_WORDS.get(told['state'] or '', '')}").strip(),
              lines=lines, reload=True)


@router.post("/api/cadence/lift")
def api_lift(sid: int = Form(...)):
    if not is_admin():
        return _j(False, message="Only an admin can lift a Do not contact.")
    c = conn()
    try:
        done, words = cadence.lift(c, sid, current_user() or "")
        c.commit()
    finally:
        c.close()
    return _j(done, message=words, reload=done)


# ------------------------------------------------------------------ what People and the firm page show

def status_html(info: dict | None, history: list[str] | None = None) -> str:
    """Where a person stands, as one chip in the plan's words (7.1): red with
    a stop sign for Do not contact (B079), amber for something to act on,
    then the last call's outcome, what is still live in Cadence, and the
    links: Open in Cadence (B176), Open in CRM only for a person in the CRM
    (N037). The hover holds the history, or who set the Do not contact."""
    if not info or not info.get("text"):
        return ""
    row = info.get("row") or {}
    tip = (ui.tip_attr("Cold outreach", info.get("tip") or "", history or [], "History")
           if history or info.get("tip") else "")
    tone = info.get("tone") or "muted"
    icon = ICONS["stop"] if tone == "stop" else ""
    out = [f'<span class="cad-chip {esc(tone)}"{tip}>{icon}{esc(info["text"])}</span>']
    if info.get("outcome"):
        out.append(f'<span class="cad-sub">Last call: {esc(info["outcome"])}</span>')
    if row.get("email_bounced_at") and row.get("state") != "opted_out":
        out.append('<span class="cad-sub warnc">Bounced in Cadence: pick another address</span>')
    if info.get("live"):
        out.append(f'<span class="cad-sub warnc">{esc(info["live"])}</span>')
    if row.get("cadence_url") and row.get("state") in ("in_sandbox", "opted_out", "removed"):
        out.append(f'<a class="cad-link" href="{esc(row["cadence_url"])}" target="_blank" rel="noopener"'
                   f' data-noprefetch>Open in Cadence</a>')
    if row.get("crm_url") and row.get("state") in ("moved_to_crm", "in_crm"):
        out.append(f'<a class="cad-link" href="{esc(row["crm_url"])}" target="_blank" rel="noopener"'
                   f' data-noprefetch>Open in CRM</a>')
    return "".join(out)


SEND_WORDS = {"failed": "Retry", "removed": "Send again"}


def card_controls(cid: str, name: str, info: dict | None, history: list[str] | None = None,
                  newer: str | None = None) -> str:
    """Under a person card's contact lines: Select and Send to Cold outreach
    while the person can be sent (Retry after a failure, Send again after a
    removal, both through the same checks), else where they stand, with who
    sent them, and Refresh. Do not contact, or for an admin the Lift of one
    set here; never a Lift of Cadence's own opt-out (B074)."""
    info = info or {"can_push": True, "pushed": False}
    can = info.get("can_push", True)
    row = info.get("row") or {}
    parts = []
    if can:
        parts.append(f'<label class="cad-pick"><input type="checkbox" data-cad-id="{esc(cid)}"'
                     f' data-cad-name="{esc(name)}"> Select</label>'
                     f'<button type="button" class="sm" data-cad-push="{esc(cid)}"'
                     f' data-cad-name="{esc(name)}">{esc(SEND_WORDS.get(row.get("state"), "Send to Cold outreach"))}'
                     f'</button>')
    line = status_html(info, history)
    meta = ""
    if row and row.get("batch_id"):
        when = row.get("last_pushed_at") or row.get("first_pushed_at")
        meta = (f'<span class="meta">Sent by {esc(users.display_name(row.get("pushed_by")))}'
                f'{", " + esc(cadence._day(when)) if when else ""}</span>')
    if newer:
        meta += f'<span class="meta warnc"{ui.tip_attr("Newer details in Bellwether", newer)}>Newer details in Bellwether</span>'
    parts.append(f'<span class="cad-line" data-cad-status="{esc(cid)}">{line}</span>{meta}')
    if info.get("pushed"):
        parts.append(f'<button type="button" class="sm ghost" data-cad-refresh="{esc(cid)}"'
                     f' data-cad-pushed="{esc(cid)}">Refresh</button>')
    kind = info.get("kind")
    if not info.get("supp"):
        parts.append(f'<button type="button" class="sm ghost" data-cad-dnc="{esc(cid)}"'
                     f' data-cad-name="{esc(name)}">Do not contact</button>')
    elif kind and kind["source"] == "manual" and is_admin():
        what = "everyone at the firm" if kind["row"]["scope"] == "firm" else esc(name)
        parts.append(f'<button type="button" class="sm ghost" data-post="/api/cadence/lift"'
                     f' data-body="sid={int(kind["row"]["id"])}" data-busy="Lifting"'
                     f' data-confirm="Lift the Do not contact set here for {what}? They can then be sent'
                     f' to Cold outreach again. An opt-out Cadence holds stays.">Lift</button>')
    return f'<div class="cad-ctl">{"".join(parts)}</div>'


def unkeyed_controls() -> str:
    """For a person whose name gives no key: why there is no Send (B064)."""
    return ('<div class="cad-ctl"><span class="cad-line"><span class="cad-sub">Cannot be sent to Cold '
            'outreach: Bellwether could not read a first and last name.</span></span></div>')


def context(origin: str, product: str = "", total: int = 0, qs: str = "", firm: str = "",
            people: int = 0) -> str:
    """What the page's send controls need to know, read by app.js: the cap
    on one send (B077) and, on a firm page, the firm and how many people
    Bellwether knows there (the Do not contact dialog, B075)."""
    return (f'<div id="cad-ctx" hidden data-origin="{esc(origin)}" data-product="{esc(product)}"'
            f' data-total="{int(total)}" data-qs="{esc(qs)}" data-cap="{cadence.max_per_push()}"'
            f' data-firm="{esc(firm)}" data-people="{int(people)}"></div>')


# ------------------------------------------------------------------ Settings, Cadence

ACTION_WORDS = {"push": "Sent", "resend": "Sent again", "retry": "Retried", "skipped": "Skipped",
                "result": "Cadence answered", "status": "Status from Cadence",
                "suppress": "Do not contact set", "lift": "Do not contact lifted", "hold": "Queue",
                "told": "Told Cadence"}
OUTCOME_WORDS = {"created": "Added to Cold outreach", "duplicate_in_sandbox": "Already in Cold outreach",
                 "already_in_crm": "Already in CRM", "in_sandbox": "In Cold outreach",
                 "moved_to_crm": "Moved to CRM", "opted_out": "Opted out", "removed": "Removed",
                 "not_found": "Cadence has no record of it", "unauthorized": "Held: Cadence refused the key",
                 "integration_disabled": "Held: switched off in Cadence", "refused": "Refused",
                 "lifted": "Hold lifted", "timeout": "No answer in time", "move_undone": "Move undone in Cadence",
                 "owner": "Owner changed", "connection": "Could not reach Cadence",
                 "expired": "Key older than 7 days"}
RULE_WORDS = {"not_found": "not in the people list yet", "no_name": "no first and last name",
              "no_contact": "no email and no phone", "opted_out": "Do not contact (Cadence)",
              "do_not_contact": "Do not contact", "customer": "a Customer firm",
              "in_cadence": "already sent", "other_firm": "already sent from another firm",
              "same_email": "same email as someone sent", "same_in_push": "same email twice",
              "same_person": "same person twice", "erased": "erased in Cadence",
              "already_in_crm": "already in CRM", "cadence_refuses": "Cadence would refuse"}


def outcome_words(outcome: str | None) -> str:
    """An audit outcome code in words, for Settings, Cadence."""
    out = outcome or ""
    kind, _, rest = out.partition(":")
    if kind == "rejected":
        return "Not sent: " + cadence.REASON_WORDS.get(rest, rest or "no reason given")
    if kind == "failed":
        return "Failed: " + OUTCOME_WORDS.get(rest, rest.replace("_", " ") or "no answer")
    if kind == "skipped":
        return "Skipped: " + RULE_WORDS.get(rest, rest.replace("_", " "))
    if kind in ("cadence", "manual"):
        return ("Opted out in Cadence" if kind == "cadence" else
                "Set here: " + SUPP_REASONS.get(rest, rest.replace("_", " ")).lower())
    if out.startswith("http_"):
        return "Cadence answered " + out[5:].replace("_", " ")
    return OUTCOME_WORDS.get(out, out.replace("_", " "))


SCOPE_WORDS = {"person": "Person", "individual": "The person, at every firm", "email": "Email address",
               "phone": "Phone number", "domain": "Email domain", "firm": "Everyone at a firm"}
TOLD_STATE = {"sent": "Yes", "pending": "Not yet: trying again", "not_sent": "No: Cadence does not take it yet"}


def _target(crd: str, key: str, names: dict, scope: str | None = None) -> str:
    """Who an audit row is about, by name, linked to their card or firm."""
    if crd and key:
        name = names.get((crd, key)) or cadence.contact_id(crd, key)
        return f'<a href="/firm/{esc(crd)}#{esc(cadence.anchor(key))}">{esc(name)}</a>'
    if crd:
        return f'<a href="/firm/{esc(crd)}">{esc(names.get((crd, "")) or "CRD " + crd)}</a>'
    return esc({"email": "An email address", "phone": "A phone number", "individual": "A person",
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
    """Names for audit rows: as sent, else from the People index, else the firm."""
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


def _banner(c, ok: bool, hold: str | None) -> str:
    """Connection status from evidence, never from the settings alone (B020):
    the last real answer to ping, with when, and what it means now."""
    problem = cadence.problem()
    last = cadence.last_ping(c) if ok else None
    when = ui.ago(last["at"]) if last else ""
    contract = ""
    if last and last.get("ok"):
        contract = (" Cadence speaks contract v1.1: a Do not contact set here reaches it." if cadence.speaks_11(c)
                    else " Cadence speaks contract v1 only: a Do not contact set here does not reach it yet.")
    if not cadence.configured():
        return f'<div class="note plain set-status">{esc(problem)}'
    if hold:
        return f'<div class="note bad set-status">{esc(cadence._hold_words(hold))}'
    if not last:
        return ('<div class="note plain set-status">Never tested. Test the connection; sending can be '
                'switched on once a test passes.')
    if not last.get("ok"):
        return f'<div class="note bad set-status">Failed {esc(when)}: {esc(last.get("words") or "")}'
    if not cadence.switched_on():
        return (f'<div class="note plain set-status">Cadence answered {esc(when)}. Sending to Cold outreach is '
                f'switched off: switch it on below.{esc(contract)}')
    return (f'<div class="note good set-status">Connected: Cadence answered {esc(when)}. Everyone can send to '
            f'Cold outreach.{esc(contract)}')


def _sends(c, ok: bool) -> str:
    """Recent sends, one row each with what became of it, and every contact
    on expand (B216)."""
    if not ok:
        return ""
    rows = []
    for b in c.execute("SELECT id FROM cadence_batch ORDER BY created_at DESC, id LIMIT 30").fetchall():
        v = cadence.batch_view(c, b["id"])
        if not v.get("ok"):
            continue
        items = "".join(f'<li><b>{esc(r["name"])}</b> <span class="meta">{esc(r["firm"])}</span>'
                        f'<span class="ln {esc(r["tone"] or "")}">{esc(r["line"])}</span></li>'
                        for r in v["results"])
        rows.append(f'<details class="cad-send"><summary><span class="nowrap">{esc(ui.ago(v["created_at"]))}</span>'
                    f'<span>{esc(users.display_name(v["created_by"]))}</span><b>{esc(v["title"])}</b>'
                    f'<span class="muted">{esc(v["summary"])}</span></summary>'
                    f'<ul class="cad-list results">{items}</ul></details>')
    c.commit()
    return "".join(rows) or '<p class="empty">Nobody has sent anyone yet.</p>'


@router.get("/settings/cadence", response_class=HTMLResponse)
def settings_page(msg: str = Query(""), err: str = Query(""), who: str = Query(""),
                  outcome: str = Query(""), ask: str = Query("")):
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
        banner = _banner(c, ok, hold)
        last = cadence._state_get(c, "last_sync_ok") if ok else None
        sync_err = cadence._state_get(c, "last_sync_error") if ok else None
        # Sends are listed whole below; this is everything else that happened.
        where, args = ["action NOT IN ('push', 'resend', 'result', 'skipped')"], []
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
        sends = _sends(c, ok)
        c.commit()
    finally:
        c.close()

    def flag(name: str, is_set: bool) -> str:
        return (f'<span class="chip {"good" if is_set else "line"}">{esc(name)}'
                f' {"is set" if is_set else "is not set"}</span>')
    state = banner + (f' <span class="pills">{flag("CADENCE_API_URL", url_set)}{flag("CADENCE_API_KEY", key_set)}'
                      f'</span><button class="sm" data-post="/settings/cadence/test" data-busy="Testing"'
                      f' data-timeout="20000">Test the connection</button></div>')
    sync_line = (f'Last status read from Cadence {esc(ui.ago(last))}.' if last else
                 'No status read from Cadence yet.')
    if sync_err:
        sync_line += f' <span class="warnc">Last error: {esc(sync_err.split(" ", 1)[-1])}</span>'
    queue = "".join(f'<tr><td>{esc(cadence.STATE_WORDS.get(k, "Other"))}</td><td class="num">{int(v):,}</td></tr>'
                    for k, v in sorted(states.items(), key=lambda kv: -kv[1]))
    retry = ""
    if states.get("failed"):
        tick = (f'<label class="inline"><input type="checkbox" name="confirm" value="1"> {esc(ask)}</label>'
                if ask else "")
        retry = (f'<form method="post" action="/settings/cadence/retry" class="set-actions">{tick}'
                 f'<button class="sm" type="submit">Retry {int(states["failed"]):,} failed</button>'
                 f'<span class="meta">Each goes again through the same checks, as a new request, in the '
                 f'name of whoever sent it. It counts against nobody\'s daily cap.</span></form>')
    req_line = ", ".join(f"{int(v):,} {k.replace('_', ' ')}" for k, v in sorted(reqs.items()))
    who_opts = ui.opt("", who, "Anyone") + "".join(ui.opt(a, who, users.display_name(a) if a != "system"
                                                                else "Bellwether") for a in actors)
    out_opts = "".join(ui.opt(k, outcome, lab) for k, lab in (
        ("", "Any outcome"), ("cadence", "Opted out in Cadence"), ("manual", "Set here"),
        ("in_sandbox", "In Cold outreach"), ("moved_to_crm", "Moved to CRM"), ("removed", "Removed"),
        ("http_", "Errors")))
    ev_rows = "".join(_event_line(e, names) for e in events)

    def covers(s):
        pair = cadence.parse_id(s["value"]) if s["scope"] == "person" else None
        if pair:
            return _target(pair[0], pair[1], names)
        if s["scope"] == "firm":
            return _target(s["value"], "", names)
        if s["scope"] == "individual":
            return f'SEC individual {esc(s["value"])}'
        return esc(s["value"])

    def lift(s):
        if s["source"] == "cadence":
            return '<span class="meta">Only Cadence can lift it</span>'
        return (f'<button class="sm ghost" data-post="/api/cadence/lift" data-body="sid={int(s["id"])}"'
                f' data-busy="Lifting" data-confirm="Lift this Do not contact? The people it covers can be'
                f' sent to Cold outreach again. An opt-out Cadence holds stays.">Lift</button>')
    supp_rows = "".join(
        f'<tr><td>{esc(SCOPE_WORDS.get(s["scope"], s["scope"]))}</td><td>{covers(s)}</td>'
        f'<td>{esc(SUPP_REASONS.get(s["reason"], s["reason"]))}</td>'
        f'<td>{"Cadence" if s["source"] == "cadence" else esc(users.display_name(s["set_by"]))}</td>'
        f'<td class="nowrap">{esc(ui.ago(s["set_at"]))}</td>'
        f'<td>{esc(TOLD_STATE.get(s.get("to_cadence") or "", "") if s["source"] == "manual" else "")}</td>'
        f'<td class="num">{lift(s)}</td></tr>' for s in supp)
    tip = how("Cadence", "Bellwether sends contacts, never firms, to Cadence's Cold outreach. Before a send "
              "it asks Cadence who is already in the CRM and leaves them out. Every 10 minutes it reads "
              "back who was claimed, moved to the CRM, opted out or removed.",
              ["CADENCE_API_URL: Cadence's address, https", "CADENCE_API_KEY: the same value Cadence "
               "holds as BELLWETHER_API_KEY"], "Set in the server environment, never here")
    inner = f"""{state}
{_group_form("cadence")}
<section class="s"><h2>Queue</h2><p class="lede">{sync_line}{(" Requests: " + esc(req_line) + ".") if req_line else ""}</p>
<table class="tight"><thead><tr><th>Where sent people stand</th><th class="num">People</th></tr></thead>
<tbody>{queue or '<tr><td colspan="2" class="empty">Nobody sent yet.</td></tr>'}</tbody></table>{retry}</section>
<section class="s"><h2>Recent sends</h2><p class="lede">One row a send; open one for each person's result.</p>{sends}</section>
<section class="s"><h2>Other activity</h2>
<form class="fbar" method="get" action="/settings/cadence"><label><select name="who" aria-label="Who">{who_opts}</select></label>
<label><select name="outcome" aria-label="Outcome">{out_opts}</select></label><button class="sm" type="submit">Show</button></form>
<table class="tight"><thead><tr><th>When</th><th>Who</th><th>What</th><th>Outcome</th><th>Contact</th></tr></thead>
<tbody>{ev_rows or '<tr><td colspan="5" class="empty">Nothing yet.</td></tr>'}</tbody></table></section>
<section class="s"><h2>Do not contact</h2><p class="lede">Nobody on this list can be sent to Cold outreach. Opt-outs from Cadence arrive by themselves and only Cadence can lift them.</p>
<table class="tight"><thead><tr><th>Covers</th><th>Who or what</th><th>Reason</th><th>Set by</th><th>When</th><th>Told Cadence</th><th></th></tr></thead>
<tbody>{supp_rows or '<tr><td colspan="7" class="empty">Nobody is marked Do not contact.</td></tr>'}</tbody></table></section>"""
    return _frame("Cadence", "cadence", "Send contacts to Cadence's Cold outreach, and see what happened "
                  "to them." + tip, inner, msg, err)


@router.post("/settings/cadence/test")
def settings_test():
    """A real call to Cadence with a hard deadline, kept and shown on the page
    (B020). When it lifts a hold, the waiting sends go now (B066)."""
    c = conn()
    try:
        ok, info, words = cadence.ping(c, by=current_user() or "system")
        if ok and info.get("sandboxEnabled", True) and cadence.held(c):
            cadence.lift_hold(c, current_user() or "system")
            c.commit()
            cadence.drain_now()
            words += " The waiting sends are going now."
    finally:
        c.close()
    return _j(ok, message=words, reload=True)


@router.post("/settings/cadence/retry")
def settings_retry(confirm: str = Form("")):
    if cadence.problem():
        return RedirectResponse(f"/settings/cadence?{qs_join(err=cadence.problem())}", status_code=303)
    c = conn()
    try:
        res = cadence.retry_failed(c, current_user() or "", current_account(), confirm=confirm == "1")
    finally:
        c.close()
    if res["ask"]:
        return RedirectResponse(f"/settings/cadence?{qs_join(err=res['ask'], ask=res['ask'])}", status_code=303)
    sent = f"{res['sent']:,} sent again."
    return RedirectResponse(f"/settings/cadence?{qs_join(msg=sent)}", status_code=303)
