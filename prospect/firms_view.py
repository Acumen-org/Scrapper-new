"""Firms: every adviser Bellwether knows, searchable, with contacts and exports.

Product lists answer "who fits this product". This answers everything else:
any firm by name, city, size or registration, whether or not it made a list,
plus the person-level contact view and the mail-merge export for any set.
Saved lists (hand-built firm buckets) and saved views live here too.

Everything is server-side filtered and paged; the universe is over 40,000 firms.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from .names import nice_name

from . import contacts, products, ui, xlsx
from .webapp import (conn, current_owner, esc, escn, money, page, qs_join, safe_back,
                     saved_view_href, score_cell, signal_cutoff)

router = APIRouter()

FIRMS_CSS = """
.listpick{min-width:0;padding:3px 7px;font-size:12px}
"""

def contacts_rows(c, crds: list[str]) -> list[dict]:
    """One row per address (and per direct line without an address) at these
    firms, people before shared inboxes, best evidence first. Dead addresses
    are left out: a mail merge should never send to a known bounce."""
    if not crds:
        return []
    out: list[dict] = []
    for i in range(0, len(crds), 1000):
        chunk = crds[i:i + 1000]
        ph = ",".join("?" * len(chunk))
        rows = c.execute(f"""
            SELECT cp.crd, f.legal_name AS firm, f.state, f.phone AS firm_phone,
                   cp.person_key, cp.person_name AS person, cp.title, cp.kind, cp.value,
                   cp.verify_status, cp.source, cp.confidence, cp.is_role
            FROM usable_contact_point cp JOIN firm_current f ON f.crd = cp.crd
            WHERE cp.crd IN ({ph}) AND cp.verify_status NOT IN ('invalid','no_mail_server')
            ORDER BY f.legal_name, cp.crd, (cp.person_key = ''), cp.is_role,
                     cp.confidence DESC""", tuple(chunk)).fetchall()
        phones: dict = {}
        first: dict = {}
        for r in rows:
            if r["kind"] == "phone" and r["person_key"]:
                phones.setdefault((r["crd"], r["person_key"]), r["value"])
                first.setdefault((r["crd"], r["person_key"]), r)
        emailed = set()
        for r in rows:
            if r["kind"] != "email":
                continue
            emailed.add((r["crd"], r["person_key"]))
            status = r["verify_status"]
            label = contacts.VERIFY_LABEL.get(status, status)
            if r["source"] == "pattern" and status in ("unverified", "queued"):
                label = "Guess, not checked"
            out.append({"crd": r["crd"], "firm": nice_name(r["firm"]), "state": r["state"],
                        "person": r["person"] or ("Shared inbox" if r["is_role"] else ""),
                        "title": r["title"], "email": r["value"], "status": label,
                        "status_key": status, "source": r["source"],
                        "confidence": r["confidence"],
                        "phone": phones.get((r["crd"], r["person_key"])) or r["firm_phone"]})
        for k, num in phones.items():
            if k in emailed:
                continue
            r = first[k]
            out.append({"crd": r["crd"], "firm": nice_name(r["firm"]), "state": r["state"],
                        "person": r["person"], "title": r["title"], "email": "",
                        "status": "", "status_key": "", "source": r["source"],
                        "confidence": r["confidence"], "phone": num})
    return out


SIZES = {
    "": ("Any size", None, None),
    "u25": ("Under $25M", 0, 25e6),
    "25-100": ("$25M to $100M", 25e6, 100e6),
    "100-250": ("$100M to $250M", 100e6, 250e6),
    "250-500": ("$250M to $500M", 250e6, 500e6),
    "500-1b": ("$500M to $1B", 500e6, 1e9),
    "1-5b": ("$1B to $5B", 1e9, 5e9),
    "5b": ("$5B and up", 5e9, None),
}
REGS = {"": "Registered advisers", "SEC": "SEC-registered", "STATE": "State-registered",
        "ERA": "Exempt reporting advisers", "all": "Everyone"}

_STATES: dict = {"t": 0.0, "v": []}


def _states(c):
    import time as _t
    if _t.monotonic() - _STATES["t"] > 120:
        _STATES["v"] = [r["state"] for r in c.execute(
            "SELECT DISTINCT state FROM firm_current WHERE state IS NOT NULL"
            " ORDER BY state") if r["state"]]
        _STATES["t"] = _t.monotonic()
    return _STATES["v"]


def _where(q, st, size, reg, lst, stat, owner, trig, list_id, hires="", reach=""):
    where: list[str] = []
    args: list = []
    if reg == "ERA":
        where.append("f.is_era=1")
    elif reg == "all":
        pass
    else:
        where.append("f.is_era=0")
        if reg in ("SEC", "STATE"):
            where.append("f.regulator=?")
            args.append(reg)
    if list_id:
        where.append("f.crd IN (SELECT crd FROM user_list_item WHERE list_id=?)")
        args.append(int(list_id))
    if q:
        where.append("(f.legal_name ILIKE ? OR f.business_name ILIKE ? OR f.crd=?"
                     " OR f.city ILIKE ?)")
        args += [f"%{q}%", f"%{q}%", q, f"%{q}%"]
    if st:
        where.append("f.state=?")
        args.append(st)
    if size in SIZES and SIZES[size][1] is not None:
        lo, hi = SIZES[size][1], SIZES[size][2]
        where.append("f.raum>=?")
        args.append(lo)
        if hi is not None:
            where.append("f.raum<?")
            args.append(hi)
    if lst in products.product_keys():
        where.append("f.crd IN (SELECT crd FROM product_score WHERE product=?"
                     " AND status='scored')")
        args.append(lst)
    elif lst == "any":
        where.append("sc.crd IS NOT NULL")
    elif lst == "none":
        where.append("sc.crd IS NULL")
    if stat:
        where.append("s.status=?")
        args.append(stat)
    if owner == "none":
        where.append("(s.owner IS NULL OR s.owner='')")
    elif owner == "me":
        where.append("s.owner=?")
        args.append(current_owner())
    elif owner:
        where.append("s.owner ILIKE ?")
        args.append(f"%{owner}%")
    if trig == "open":
        where.append("EXISTS (SELECT 1 FROM trigger_event t LEFT JOIN trigger_action a"
                     " ON a.trigger_id=t.id WHERE t.crd=f.crd AND t.suppressed=0"
                     " AND a.state IS NULL AND t.detected_date >= ?)")
        args.append(signal_cutoff())
    if hires:
        where.append("ps.hires_12m >= ?")
        args.append(int(hires))
    if reach == "email":
        where.append("EXISTS (SELECT 1 FROM usable_contact_point x WHERE x.crd=f.crd AND x.kind='email'"
                     " AND x.person_key != '' AND x.is_role=0 AND x.source != 'pattern'"
                     " AND x.verify_status NOT IN ('invalid','no_mail_server'))")
    elif reach == "verified":
        where.append("EXISTS (SELECT 1 FROM usable_contact_point x WHERE x.crd=f.crd AND x.kind='email'"
                     " AND x.verify_status='valid')")
    elif reach == "none":
        where.append("NOT EXISTS (SELECT 1 FROM usable_contact_point x WHERE x.crd=f.crd"
                     " AND x.kind='email' AND x.verify_status NOT IN ('invalid','no_mail_server'))")
    return " AND ".join(where) or "1=1", args


FROM = """FROM firm_current f
    LEFT JOIN firm_scope sc ON sc.crd=f.crd
    LEFT JOIN firm_status s ON s.crd=f.crd
    LEFT JOIN firm_people_stats ps ON ps.crd=f.crd
    WHERE {where}"""

SORTS = {"": ("Best fit", "sc.priority DESC NULLS LAST, f.raum DESC NULLS LAST"),
         "aum": ("Largest", "f.raum DESC NULLS LAST"),
         "hires": ("Most hiring", "ps.hires_12m DESC NULLS LAST, f.raum DESC NULLS LAST"),
         "growth": ("Fastest growing team", "ps.net_12m DESC NULLS LAST, f.raum DESC NULLS LAST"),
         "advisors": ("Most advisors", "f.iar_count DESC NULLS LAST"),
         "name": ("Name", "f.legal_name")}


@router.get("/firms", response_class=HTMLResponse)
def firms(view: str = Query("firms"), q: str = Query(""), st: str = Query(""),
          size: str = Query(""), reg: str = Query(""), lst: str = Query("", alias="on"),
          stat: str = Query(""), owner: str = Query(""),
          trig: str = Query(""), list_id: str = Query("", alias="list"),
          hires: str = Query(""), reach: str = Query(""), sort: str = Query(""),
          preset: str = Query(""), page_n: int = Query(1, ge=1, alias="page"),
          per: int = Query(50, ge=10, le=200)):
    # Links from before the product lists passed a preset; send them to the
    # list that replaced it.
    old = {"phh_a": "/lists/phh_fund", "phh_x": "/lists/phh_fund",
           "acu": "/lists/acubooth", "comp": "/lists/phh_fund?view=disqualified"}
    if preset in old:
        return RedirectResponse(old[preset], status_code=307)
    c = conn()
    hires = hires if hires.isdigit() else ""
    sort = sort if sort in SORTS else ""
    where, args = _where(q, st, size, reg, lst, stat, owner, trig, list_id, hires, reach)
    list_name = ""
    if list_id:
        r = c.execute("SELECT name FROM user_list WHERE id=?", (int(list_id),)).fetchone()
        list_name = r["name"] if r else ""
    qs = qs_join(view=view if view != "firms" else "", q=q, st=st, size=size, reg=reg,
                 on=lst, stat=stat, owner=owner, trig=trig, list=list_id, hires=hires,
                 reach=reach, sort=sort)
    if view == "contacts":
        table, total = _contacts(c, where, args, page_n, per)
    else:
        table, total = _firms(c, where, args, page_n, per, qs, sort)
    states = _states(c)
    lists = c.execute("SELECT id, name FROM user_list ORDER BY name").fetchall()
    c.close()

    def vtab(v, label):
        href = "/firms?" + qs_join(view=v if v != "firms" else "", q=q, st=st, size=size,
                                   reg=reg, on=lst, stat=stat, owner=owner, trig=trig,
                                   list=list_id, hires=hires, reach=reach, sort=sort)
        return f'<a class="{"on" if view == v else ""}" href="{href}">{label}</a>'

    lst_opts = (ui.opt("", lst, "Any") + ui.opt("any", lst, "On any list")
                + ui.opt("none", lst, "On no list")
                + "".join(ui.opt(k, lst, products.product(k)["name"])
                          for k in products.product_keys()))
    pages = max(1, -(-total // per))
    prev = f'<a href="/firms?{qs}&page={page_n-1}">Previous</a>' if page_n > 1 else ""
    nxt = f'<a href="/firms?{qs}&page={page_n+1}">Next</a>' if page_n < pages else ""
    exp = (f'<a class="btn" href="/firms/export.xlsx?{qs}" data-noprefetch>Export contacts</a>'
           if view == "contacts" else
           f'<a class="btn" href="/firms/export.csv?{qs}" data-noprefetch>Export firms</a>')
    title = f"Saved list: {esc(list_name)}" if list_name else "Firms"
    user_list_opts = "".join(ui.opt(str(l["id"]), list_id, l["name"]) for l in lists)
    body = f"""<div class="pg wide">
<div class="head"><div><h1>{title}</h1></div>
<div class="acts">{exp}</div></div>
<form class="filters" method="get" action="/firms">
<input type="hidden" name="view" value="{esc(view if view != 'firms' else '')}">
<label>Search<input type="search" name="q" value="{esc(q)}" placeholder="Firm, city or CRD"></label>
<label>State<select name="st">{ui.opt("", st, "All states")}{"".join(ui.opt(s, st, s) for s in states)}</select></label>
<label>Size<select name="size">{"".join(ui.opt(k, size, v[0]) for k, v in SIZES.items())}</select></label>
<label>Registration<select name="reg">{"".join(ui.opt(k, reg, v) for k, v in REGS.items())}</select></label>
<label>Product list<select name="on">{lst_opts}</select></label>
<label>Hiring<select name="hires">{ui.opt("", hires, "Any")}{ui.opt("1", hires, "Hired in 12 months")}{ui.opt("3", hires, "Hired 3+")}{ui.opt("5", hires, "Hired 5+")}</select></label>
<label>Reach<select name="reach">{ui.opt("", reach, "Any")}{ui.opt("email", reach, "A named person's email")}{ui.opt("verified", reach, "A verified email")}{ui.opt("none", reach, "No email yet")}</select></label>
<label>Owner<select name="owner">{ui.opt("", owner, "Anyone")}{ui.opt("me", owner, "Mine")}{ui.opt("none", owner, "Unclaimed")}</select></label>
<label>Status<select name="stat">{ui.opt("", stat, "Any")}{"".join(ui.opt(s, stat, s.capitalize()) for s in ui.STATUS_OPTIONS)}</select></label>
<label>Signal<select name="trig">{ui.opt("", trig, "Any")}{ui.opt("open", trig, "New in 60 days")}</select></label>
<label>Saved list<select name="list">{ui.opt("", list_id, "Any")}{user_list_opts}</select></label>
<label>Sort<select name="sort">{"".join(ui.opt(k, sort, v[0]) for k, v in SORTS.items())}</select></label>
<button class="primary" type="submit">Apply</button>
<a class="btn ghost" href="/firms">Clear</a>
</form>
<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin:12px 0 0;flex-wrap:wrap">
<div class="seg">{vtab("firms", "Firms")}{vtab("contacts", "Contacts")}</div>
<div class="acts"><span class="small soft"><b style="color:var(--ink)">{total:,}</b>
{"people" if view == "contacts" else "firms"}</span>
<form method="post" action="/views/save" class="acts">
<input type="hidden" name="page" value="firms"><input type="hidden" name="qs" value="{esc(qs)}">
<input type="text" name="name" placeholder="Name this view to save it" style="min-width:200px">
<button type="submit" class="sm">Save view</button></form></div></div>
{table}
<div class="pager">{prev} Page {page_n} of {pages} {nxt}</div>
</div>"""
    return page(title if not list_name else list_name, "firms" if not list_id else "saved",
                body, FIRMS_CSS, js=ADD_JS)


ADD_JS = """
function addToList(sel){
  if(!sel.value) return;
  var f = sel.form;
  if(sel.value==='__new'){
    var name = prompt('Name for the new list:');
    if(!name){ sel.value=''; return; }
    f.new_name.value = name;
  }
  f.submit();
}
"""


def _firms(c, where, args, page_n, per, qs, sort=""):
    base = FROM.format(where=where)
    total = c.execute(f"SELECT COUNT(*) n {base}", args).fetchone()["n"]
    rows = c.execute(f"""
        SELECT f.crd, f.legal_name, f.city, f.state, f.raum, f.hnw_aum, f.iar_count,
               f.regulator, f.is_era, sc.best_product, sc.best_score, sc.best_coverage,
               sc.products, s.status, s.owner, ps.headcount, ps.hires_12m, ps.departures_12m
        {base} ORDER BY {SORTS.get(sort, SORTS[""])[1]}
        LIMIT ? OFFSET ?""", args + [per, (page_n - 1) * per]).fetchall()
    flags = ui.contact_flags(c, [r["crd"] for r in rows])
    lists = c.execute("SELECT id, name FROM user_list ORDER BY name").fetchall()
    listopts = "".join(f'<option value="{l["id"]}">{esc(l["name"])}</option>' for l in lists)
    body = []
    for r in rows:
        hs = (100 * (r["hnw_aum"] or 0) / r["raum"]) if r["raum"] else 0
        best = ""
        if r["best_product"]:
            n_on = len((r["products"] or "").split(","))
            best = (f'{score_cell(r["best_score"], r["best_coverage"], show_cov=False)}'
                    f'<div class="meta">{esc(ui.product_name(r["best_product"]))}'
                    + (f', on {n_on} lists' if n_on > 1 else "") + '</div>')
        reg = "ERA" if r["is_era"] else (r["regulator"] or "")
        who = (f'<span class="chip">{esc(r["status"] or "claimed")}</span>'
               f'<div class="meta">{esc(r["owner"] or "")}</div>'
               if (r["status"] or r["owner"]) else "")
        team = r["headcount"] if r["headcount"] is not None else (r["iar_count"] or 0)
        moves = ""
        if r["hires_12m"] or r["departures_12m"]:
            moves = (f'<div class="meta"><span class="ok">+{r["hires_12m"] or 0}</span> '
                     f'<span class="bad">-{r["departures_12m"] or 0}</span> in 12m</div>')
        add = (f'<form method="post" action="/firms/addtolist">'
               f'<input type="hidden" name="crd" value="{esc(r["crd"])}">'
               f'<input type="hidden" name="back" value="/firms?{esc(qs)}">'
               f'<input type="hidden" name="new_name" value="">'
               f'<select name="list_id" onchange="addToList(this)" class="listpick">'
               f'<option value="">+ list</option>{listopts}'
               f'<option value="__new">New list...</option></select></form>')
        body.append(
            f'<tr class="go" data-href="/firm/{esc(r["crd"])}"><td><div class="firm">'
            f'<a href="/firm/{esc(r["crd"])}">{escn(r["legal_name"] or "(unnamed)")}</a></div>'
            f'<div class="meta">{ui.firm_meta(r)} &middot; {esc(reg)}</div></td>'
            f'<td class="num">{money(r["raum"])}<div class="meta">HNW {hs:.0f}%</div></td>'
            f'<td class="num">{team}{moves}</td>'
            f'<td>{best or "<span class=muted>-</span>"}</td>'
            f'<td>{ui.contact_cell(flags[r["crd"]])}</td><td><details class="row-actions"><summary>Manage</summary>{who}{add}</details></td></tr>')
    empty = '<tr><td colspan="6" class="empty">No firms match these filters.</td></tr>'
    return (f'<table><thead><tr><th>Firm</th><th class="num">AUM</th>'
            f'<th class="num" title="People registered at the firm now, and job moves in 12 months">Team</th>'
            f'<th>Product fit</th><th>Contacts</th><th></th></tr></thead>'
            f'<tbody>{"".join(body) or empty}</tbody></table>'), total


def _contacts(c, where, args, page_n, per):
    crds = [r["crd"] for r in c.execute(
        f"SELECT f.crd {FROM.format(where=where)} ORDER BY sc.priority DESC NULLS LAST,"
        f" f.raum DESC NULLS LAST LIMIT 3000", args)]
    rows = contacts_rows(c, crds)
    total = len(rows)
    body = []
    for r in rows[(page_n - 1) * per: page_n * per]:
        chip = f'v-{r["status_key"]}' if r["status_key"] else ""
        who = (f'<b>{esc(r["person"])}</b>' if r["person"] and r["person"] != "Shared inbox"
               else '<span class="muted">Shared inbox</span>')
        em = ((f'<a href="mailto:{esc(r["email"])}">{esc(r["email"])}</a>'
               f'<div class="meta"><span class="chip {chip}">{esc(r["status"])}</span> '
               f'<span class="muted">{esc(r["source"])}</span></div>')
              if r["email"] else '<span class="muted">-</span>')
        body.append(
            f'<tr><td><a class="firm" href="/firm/{esc(r["crd"])}">{esc(r["firm"])}</a>'
            f'<div class="meta">CRD {esc(r["crd"])} &middot; {esc(r["state"] or "-")}</div></td>'
            f'<td>{who}<div class="meta">{esc(r["title"] or "")}</div></td>'
            f'<td>{em}</td><td class="nowrap">{esc(r["phone"] or "-")}</td></tr>')
    empty = ('<tr><td colspan="4" class="empty">No contacts for this set yet. The website, '
             'brochure and directory jobs fill them in by themselves.</td></tr>')
    return (f'<table><thead><tr><th>Firm</th><th>Person</th><th>Email</th>'
            f'<th>Phone</th></tr></thead>'
            f'<tbody>{"".join(body) or empty}</tbody></table>'), total


@router.get("/firms/export.csv")
def export_csv(q: str = "", st: str = "", size: str = "", reg: str = "",
               on: str = "", stat: str = "", owner: str = "", trig: str = "",
               list_id: str = Query("", alias="list"), hires: str = "", reach: str = "",
               sort: str = ""):
    import csv
    import io
    c = conn()
    hires = hires if hires.isdigit() else ""
    sort = sort if sort in SORTS else ""
    where, args = _where(q, st, size, reg, on, stat, owner, trig, list_id, hires, reach)
    rows = c.execute(f"""
        SELECT f.crd, f.legal_name, f.business_name, f.website, f.phone AS main_phone,
               f.city, f.state, f.regulator, f.raum, f.hnw_clients, f.hnw_aum,
               f.iar_count, ps.headcount, ps.hires_12m, ps.departures_12m,
               sc.best_product, sc.best_score, sc.best_coverage,
               sc.products AS on_lists, s.status AS work_status, s.owner AS work_owner
        {FROM.format(where=where)} ORDER BY {SORTS[sort][1]} LIMIT 60000""",
                     args).fetchall()
    c.close()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    cols = list(rows[0].keys()) if rows else ["crd"]
    w.writerow(cols)
    for r in rows:
        w.writerow([r[k] for k in cols])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="firms.csv"'})


@router.get("/firms/export.xlsx")
def export_xlsx(q: str = "", st: str = "", size: str = "", reg: str = "",
                on: str = "", stat: str = "", owner: str = "", trig: str = "",
                list_id: str = Query("", alias="list"), hires: str = "", reach: str = "",
                sort: str = ""):
    c = conn()
    hires = hires if hires.isdigit() else ""
    sort = sort if sort in SORTS else ""
    where, args = _where(q, st, size, reg, on, stat, owner, trig, list_id, hires, reach)
    crds = [r["crd"] for r in c.execute(
        f"SELECT f.crd {FROM.format(where=where)} ORDER BY {SORTS[sort][1]} LIMIT 5000", args)]
    rows = contacts_rows(c, crds)
    c.close()
    headers = ["Firm", "CRD", "State", "Person", "Title", "Email", "Email status",
               "Source", "Confidence", "Phone"]
    out = [[r["firm"], r["crd"], r["state"] or "", r["person"] or "", r["title"] or "",
            r["email"] or "", r["status"] or "", r["source"] or "", r["confidence"] or "",
            r["phone"] or ""] for r in rows]
    data = xlsx.write_sheet(headers, out, sheet_name="Contacts")
    return Response(data, media_type=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        headers={"Content-Disposition": 'attachment; filename="contacts.xlsx"'})


# ---------------------------------------------------------------- saved

@router.get("/saved", response_class=HTMLResponse)
def saved():
    c = conn()
    lists = c.execute("""
        SELECT u.id, u.name, u.created_at, u.created_by,
               (SELECT COUNT(*) FROM user_list_item i WHERE i.list_id=u.id) AS n
        FROM user_list u ORDER BY u.name""").fetchall()
    views = c.execute("SELECT id, name, page, qs FROM saved_view ORDER BY name").fetchall()
    c.close()
    lrows = "".join(
        f'<tr class="go" data-href="/firms?list={l["id"]}"><td><a class="firm" '
        f'href="/firms?list={l["id"]}">{esc(l["name"])}</a>'
        f'<div class="meta">made {esc((l["created_at"] or "")[:10])}'
        f'{" by " + esc(l["created_by"]) if l["created_by"] else ""}</div></td>'
        f'<td class="num">{l["n"]:,}</td>'
        f'<td><a href="/firms?view=contacts&list={l["id"]}">Contacts</a> &middot; '
        f'<a href="/firms/export.xlsx?list={l["id"]}">Excel</a> &middot; '
        f'<a href="/firms/export.csv?list={l["id"]}">CSV</a></td>'
        f'<td class="num"><form method="post" action="/lists/delete" '
        f'onsubmit="return confirm(\'Delete this list? The firms themselves are not affected.\')">'
        f'<input type="hidden" name="list_id" value="{l["id"]}">'
        f'<button type="submit" class="sm ghost">Delete</button></form></td></tr>'
        for l in lists)
    where_lbl = {"signals": "Signals", "inbox": "Signals", "firms": "Firms", "people": "People"}
    vrows = "".join(
        f'<tr class="go" data-href="{esc(saved_view_href(v))}"><td>'
        f'<a class="firm" href="{esc(saved_view_href(v))}">{esc(v["name"])}</a>'
        f'<div class="meta">{esc(where_lbl.get(v["page"]) or ui.product_name(v["page"][5:]) + " list")}</div></td>'
        f'<td class="num"><form method="post" action="/views/delete">'
        f'<input type="hidden" name="vid" value="{v["id"]}">'
        f'<button type="submit" class="sm ghost">Delete</button></form></td></tr>'
        for v in views)
    body = f"""<div class="pg narrow">
<div class="head"><div><h1>Saved lists</h1>
<div class="lede">Firm lists and saved filters from
any list or search. Add a firm to a list from any row with <b>+ list</b>.</div></div></div>
<section class="s"><div class="s-head"><h2>Your lists</h2></div>
<form class="filters" method="post" action="/lists/create" style="border-top:0;padding-top:0">
<label>New list<input type="text" name="name" placeholder="For example: Q4 push, Boston dinners" required></label>
<button class="primary" type="submit">Create list</button></form>
<table><thead><tr><th>List</th><th class="num">Firms</th><th>Open and export</th><th></th></tr></thead>
<tbody>{lrows or '<tr><td colspan="4" class="empty">No lists yet.</td></tr>'}</tbody></table></section>
<section class="s"><div class="s-head"><h2>Saved views</h2></div>
<table><tbody>{vrows or '<tr><td class="empty">No saved views yet. Name any filtered list or search to save it here.</td></tr>'}</tbody></table>
</section></div>"""
    return page("Saved lists", "saved", body, FIRMS_CSS)


@router.post("/lists/create")
def list_create(name: str = Form(...)):
    c = conn()
    if name.strip():
        c.execute("INSERT INTO user_list (name, created_at, created_by) VALUES (?,?,?)",
                  (name.strip()[:60],
                   datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   current_owner() or None))
        c.commit()
    c.close()
    return RedirectResponse("/saved", status_code=303)


@router.post("/lists/delete")
def list_delete(list_id: int = Form(...)):
    c = conn()
    c.execute("DELETE FROM user_list_item WHERE list_id=?", (list_id,))
    c.execute("DELETE FROM user_list WHERE id=?", (list_id,))
    c.commit()
    c.close()
    return RedirectResponse("/saved", status_code=303)


@router.post("/firms/removefromlist")
def remove_from_list(crd: str = Form(...), list_id: int = Form(...),
                     back: str = Form("/saved")):
    back = safe_back(back, "/saved")
    c = conn()
    c.execute("DELETE FROM user_list_item WHERE list_id=? AND crd=?", (list_id, crd))
    c.commit()
    c.close()
    return RedirectResponse(back, status_code=303)


@router.post("/firms/addtolist")
def add_to_list(crd: str = Form(...), list_id: str = Form(...),
                back: str = Form("/firms"), new_name: str = Form("")):
    back = safe_back(back, "/firms")
    c = conn()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if list_id == "__new":
        name = (new_name.strip() or "My list")[:60]
        cur = c.execute("INSERT INTO user_list (name, created_at, created_by)"
                        " VALUES (?,?,?) RETURNING id",
                        (name, now, current_owner() or None))
        lid = cur.fetchone()["id"]
    elif list_id:
        lid = int(list_id)
    else:
        c.close()
        return RedirectResponse(back, status_code=303)
    c.execute("INSERT OR IGNORE INTO user_list_item (list_id, crd, added_at, added_by)"
              " VALUES (?,?,?,?)", (lid, crd, now, current_owner() or None))
    c.commit()
    c.close()
    return RedirectResponse(back, status_code=303)
