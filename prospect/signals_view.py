"""Signals: everything that changed at a firm on a product list.

The old trigger inbox, moved off the home page and widened from one AUM band to
every firm on any product list. Each row says what happened, how fresh it is,
and where the firm stands on its best list, so a signal is never read without
knowing whether the firm is worth calling. Done, Snooze and Dismiss work as
before, with the same keyboard: j and k to move, d s x to act, Enter to open.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from . import products, ui
from .webapp import (escn, PRODUCTS, TYPE_LABEL, caveat, conn, current_owner, esc, money,
                     page, qs_join, tier_chip)

router = APIRouter()

SIG_CSS = """
tr.done{opacity:.4}
tr.krow.sel td{background:var(--red-bg)}
.act{display:flex;gap:5px;flex-wrap:wrap}
"""

WINDOWS = {"60": "Last 60 days", "365": "Last 12 months", "": "Any time"}


@router.get("/signals", response_class=HTMLResponse)
def signals(ttype: str = Query("", alias="type"), product: str = Query(""),
            state: str = Query("open"), q: str = Query(""), window: str = Query(""),
            page_n: int = Query(1, ge=1, alias="page"),
            per: int = Query(50, ge=10, le=200)):
    c = conn()
    # Old inbox links passed PHH / ACUBOOTH as the product.
    legacy = {"PHH": "phh_fund", "ACUBOOTH": "acubooth"}
    product = legacy.get(product, product)
    where = ["t.suppressed=0"]
    args: list = []
    scope = "JOIN firm_scope sc ON sc.crd=t.crd"
    if product in products.product_keys():
        scope = ("JOIN firm_scope sc ON sc.crd=t.crd "
                 "JOIN product_score ps ON ps.crd=t.crd AND ps.product=? "
                 "AND ps.status='scored'")
        args.append(product)
    if ttype:
        where.append("t.trigger_type=?")
        args.append(ttype)
    if q:
        where.append("(f.legal_name ILIKE ? OR f.crd=?)")
        args += [f"%{q}%", q]
    if state == "open":
        where.append("a.state IS NULL")
    elif state:
        where.append("a.state=?")
        args.append(state)
    if window in ("60", "365"):
        from .webapp import signal_cutoff
        where.append("t.detected_date >= ?")
        args.append(signal_cutoff(int(window)))

    base = f"""FROM trigger_event t
        {scope}
        JOIN firm_current f ON f.crd=t.crd
        LEFT JOIN trigger_action a ON a.trigger_id=t.id
        WHERE {' AND '.join(where)}"""
    counts = {r["k"]: r["n"] for r in c.execute(
        f"SELECT t.trigger_type k, COUNT(*) n {base} GROUP BY 1", args)}
    total = sum(counts.values())
    rows = c.execute(f"""
        SELECT t.id, t.crd, t.trigger_type, t.detected_date, t.description, t.priority,
               a.state, f.legal_name, f.city, f.state AS st, f.raum,
               sc.best_product, sc.best_tier, sc.best_score
        {base}
        ORDER BY t.detected_date DESC, ABS(t.priority) DESC
        LIMIT ? OFFSET ?""", args + [per, (page_n - 1) * per]).fetchall()
    alerts = c.execute("""
        SELECT t.id, t.crd, t.description, t.detected_date, f.legal_name
        FROM trigger_event t
        JOIN firm_watch w ON w.crd = t.crd
        JOIN firm_current f ON f.crd = t.crd
        LEFT JOIN trigger_action a ON a.trigger_id = t.id
        WHERE t.suppressed = 0 AND a.state IS NULL
        ORDER BY t.detected_date DESC LIMIT 6""").fetchall()
    c.close()

    qs = qs_join(type=ttype, product=product, state=state if state != "open" else "",
                 q=q, window=window)
    back = f"/signals?{qs}&page={page_n}"
    body = []
    for r in rows:
        meta = PRODUCTS.get(r["trigger_type"], {"product": "BOTH", "kind": "lead"})
        kchip = "dis" if meta["kind"] == "disqualifier" else "lead"
        done = " done" if r["state"] else ""
        age = (datetime.now(timezone.utc).date()
               - datetime.fromisoformat(r["detected_date"][:10]).date()).days
        old = (" &middot; " + caveat("archive_as_of", "from the archive")
               if age > 200 else "")
        best = ""
        if r["best_product"]:
            best = (f'{tier_chip(r["best_tier"])} <span class="small soft">'
                    f'{esc(ui.product_name(r["best_product"]))}</span>')
        acts = (f'<span class="chip">{esc(r["state"])}</span>' if r["state"] else
                f'<form method="post" action="/signals/action" class="act">'
                f'<input type="hidden" name="tid" value="{r["id"]}">'
                f'<input type="hidden" name="back" value="{esc(back)}">'
                f'<button class="sm" name="state" value="actioned">Done</button>'
                f'<button class="sm ghost" name="state" value="snoozed">Snooze</button>'
                f'<button class="sm ghost" name="state" value="dismissed">Dismiss</button>'
                f'</form>')
        body.append(
            f'<tr class="krow go{done}" data-tid="{r["id"]}" data-href="/firm/{esc(r["crd"])}">'
            f'<td style="white-space:nowrap">{esc(r["detected_date"])}'
            f'<div class="meta">{age} days ago{old}</div></td>'
            f'<td><span class="chip {kchip}">'
            f'{esc(TYPE_LABEL.get(r["trigger_type"], r["trigger_type"]))}</span></td>'
            f'<td><div class="firm"><a href="/firm/{esc(r["crd"])}">'
            f'{escn(r["legal_name"] or "(unnamed)")}</a></div>'
            f'<div class="meta">{ui.firm_meta(dict(r, state=r["st"]))}</div></td>'
            f'<td class="why">{esc(r["description"])}</td>'
            f'<td>{best}</td><td>{acts}</td></tr>')

    alerts_html = ""
    if alerts:
        alerts_html = ('<div class="panel" style="margin:6px 0 14px"><h3>Firms you watch</h3>'
                       + "".join(
                           f'<div class="small" style="padding:4px 0"><a class="firm" '
                           f'href="/firm/{esc(a["crd"])}">{escn(a["legal_name"])}</a> '
                           f'<span class="soft">{esc(a["description"])}</span> '
                           f'<span class="muted">{esc(a["detected_date"])}</span></div>'
                           for a in alerts) + "</div>")

    type_opts = "".join(ui.opt(k, ttype, f"{v} ({counts.get(k, 0):,})")
                        for k, v in TYPE_LABEL.items())
    prod_opts = "".join(ui.opt(k, product, products.product(k)["name"])
                        for k in products.product_keys())
    state_opts = "".join(ui.opt(v, state, l) for v, l in
                         [("open", "Open"), ("actioned", "Done"), ("snoozed", "Snoozed"),
                          ("dismissed", "Dismissed"), ("", "All")])
    win_opts = "".join(ui.opt(k, window, v) for k, v in WINDOWS.items())
    pages = max(1, -(-total // per))
    prev = f'<a href="/signals?{qs}&page={page_n-1}">Previous</a>' if page_n > 1 else ""
    nxt = f'<a href="/signals?{qs}&page={page_n+1}">Next</a>' if page_n < pages else ""
    empty = ('<tr><td colspan="6" class="empty">Nothing matches. Signals arrive with '
             'each weekly SEC feed: a new registration, an assets jump, advisors added, '
             'a custodian move.</td></tr>')
    body_html = f"""<div class="pg">
<div class="head"><div><h1>Signals</h1>
<div class="lede">What changed at firms on your product lists, newest first.
A red chip is a reason <b>not</b> to call (a firm that left Schwab, assets that fell).
Keys: <b>j</b>/<b>k</b> move, <b>d</b> done, <b>s</b> snooze, <b>x</b> dismiss, <b>Enter</b> opens.</div></div></div>
{alerts_html}
<form class="filters" method="get" action="/signals">
<label>Product list<select name="product">{ui.opt("", product, "All lists")}{prod_opts}</select></label>
<label>Signal<select name="type">{ui.opt("", ttype, f"All ({total:,})")}{type_opts}</select></label>
<label>When<select name="window">{win_opts}</select></label>
<label>State<select name="state">{state_opts}</select></label>
<label>Firm<input type="search" name="q" value="{esc(q)}" placeholder="Name or CRD"></label>
<button class="primary" type="submit">Apply</button>
<a class="btn ghost" href="/signals">Clear</a>
</form>
<div style="display:flex;justify-content:space-between;align-items:center;margin:10px 0 0;gap:12px;flex-wrap:wrap">
<div class="small soft"><b style="color:var(--ink)">{total:,}</b> signals</div>
<form method="post" action="/views/save" class="acts">
<input type="hidden" name="page" value="signals"><input type="hidden" name="qs" value="{esc(qs)}">
<input type="text" name="name" placeholder="Name this view to save it" style="min-width:210px">
<button type="submit" class="sm">Save view</button></form></div>
<table><thead><tr><th style="width:110px">When</th><th style="width:150px">Signal</th>
<th style="width:22%">Firm</th><th>What happened</th><th style="width:110px">Best list</th><th style="width:190px"></th></tr></thead>
<tbody>{"".join(body) or empty}</tbody></table>
<div class="pager">{prev} Page {page_n} of {pages} {nxt}</div>
</div>"""
    return page("Signals", "signals", body_html, SIG_CSS, js=KEYS_JS)


KEYS_JS = """
var kSel=-1, kRows=document.querySelectorAll('tr.krow');
function kMark(){kRows.forEach(function(r,i){r.classList.toggle('sel', i==kSel);});
 if(kSel>=0)kRows[kSel].scrollIntoView({block:'nearest'});}
function kAct(state){var r=kRows[kSel];if(!r)return;
 var f=document.createElement('form');f.method='post';f.action='/signals/action';
 f.innerHTML='<input name="tid" value="'+r.dataset.tid+'">'
  +'<input name="state" value="'+state+'"><input name="back" value="'+location.pathname+location.search+'">';
 document.body.appendChild(f);f.submit();}
function rowKey(e){
 if(e.key=='j'){kSel=Math.min(kRows.length-1,kSel+1);kMark();}
 if(e.key=='k'){kSel=Math.max(0,kSel-1);kMark();}
 if(e.key=='d')kAct('actioned');
 if(e.key=='s')kAct('snoozed');
 if(e.key=='x')kAct('dismissed');
 if(e.key=='Enter'&&kSel>=0)location.href=kRows[kSel].dataset.href;
}
"""


@router.post("/signals/action")
@router.post("/action")
def act(tid: int = Form(...), state: str = Form(...), back: str = Form("/signals"),
        reason: str = Form("")):
    if state not in ("actioned", "snoozed", "dismissed"):
        return RedirectResponse("/signals", status_code=303)
    if not back.startswith("/"):
        back = "/signals"
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
