"""People: every person at every adviser firm, and how to reach them.

The SEC's individual registrations give the roster (who works where, since
when, where they were before, their exams and designations and whether they
carry a disclosure); Schedule A gives officers' titles; the firm's website,
vCards, brochures, directories, web search and verification give emails,
phones and LinkedIn profiles. prospect/people_index.py folds all of it into
one table built for this screen, so every filter is a single indexed read and
the page answers in a blink however the list is cut.
"""

from __future__ import annotations

import csv
import io
import time
from datetime import date, timedelta
from functools import lru_cache

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from . import people_index, products, roles, ui
from .webapp import FAMILY_COLOUR, conn, esc, escn, money, nice_name, page, qs_join

router = APIRouter()

VIEWS = {
    "": ("Everyone", ""),
    "email": ("Verified email", "has_email = 1"),
    "direct": ("Direct line", "has_direct = 1"),
    "linkedin": ("LinkedIn", "has_linkedin = 1"),
    "hunting": ("Still hunting", "has_email = 0"),
}
JOINED = [("", "Joined any time"), ("90", "Joined in 90 days"), ("365", "Joined in 12 months"),
          ("1095", "Joined in 3 years")]
ROLE = [("", "Any role"), ("leader", "Leadership"), ("officer", "Officers on Form ADV")]
DESIG = [("", "Any designation"), ("cfp", "CFP"), ("cfa", "CFA")]
SIZE = [("", "Any firm size"), ("100", "$100M+"), ("500", "$500M+"), ("1000", "$1B+"), ("5000", "$5B+")]
SORTS = [("", "Best firms first"), ("joined", "Newest joiners"), ("name", "Name A to Z"),
         ("firm", "Firm A to Z"), ("aum", "Largest firms")]
ORDER = {"": "priority DESC NULLS LAST, raum DESC NULLS LAST, name",
         "joined": "start_date DESC NULLS LAST, name",
         "name": "last_name, first_name",
         "firm": "firm_lc, last_name",
         "aum": "raum DESC NULLS LAST, name"}


def _where(q, st, on, view, role, joined, desig, disc, size, cat):
    where, args = ["1=1"], []
    if q:
        ql = q.lower().strip()
        where.append("(name_lc LIKE ? OR firm_lc LIKE ? OR crd = ?)")
        args += [f"%{ql}%", f"%{ql}%", ql]
    if st:
        where.append("state = ?")
        args.append(st)
    if on in products.product_keys():
        where.append("EXISTS (SELECT 1 FROM product_score ps WHERE ps.crd = people_index.crd"
                     " AND ps.product = ? AND ps.status = 'scored')")
        args.append(on)
    elif on == "any":
        where.append("priority IS NOT NULL")
    if view in VIEWS and VIEWS[view][1]:
        where.append(VIEWS[view][1])
    if role == "leader":
        where.append("is_leader = 1")
    elif role == "officer":
        where.append("is_officer = 1")
    if joined in dict(JOINED) and joined:
        where.append("start_date >= ?")
        args.append((date.today() - timedelta(days=int(joined))).isoformat())
    if desig == "cfp":
        where.append("cfp = 1")
    elif desig == "cfa":
        where.append("cfa = 1")
    if disc:
        where.append("has_disclosure = 1")
    if size in dict(SIZE) and size:
        where.append("raum >= ?")
        args.append(int(size) * 1_000_000)
    if cat:
        where.append("category = ?")
        args.append(cat)
    return " AND ".join(where), args


@lru_cache(maxsize=256)
def _count(where: str, args: tuple, minute: int) -> int:
    c = conn()
    try:
        return c.execute(f"SELECT COUNT(*) n FROM people_index WHERE {where}", args).fetchone()["n"]
    finally:
        c.close()


@lru_cache(maxsize=4)
def _states(minute: int) -> list[str]:
    c = conn()
    try:
        return [r["state"] for r in c.execute(
            "SELECT DISTINCT state FROM firm_current WHERE state IS NOT NULL AND LENGTH(state) = 2"
            " ORDER BY state")]
    finally:
        c.close()


def _categories() -> list[tuple[str, str]]:
    try:
        from . import firmtype
        return [(c["key"], c["label"]) for c in firmtype.categories()]
    except Exception:
        return []


def _person_row(r) -> str:
    title = roles.clean_title(r["title"]) or ("Officer" if r["is_officer"] else "Registered representative")
    badges = []
    if r["cfp"]:
        badges.append('<span class="chip">CFP</span>')
    if r["cfa"]:
        badges.append('<span class="chip">CFA</span>')
    if r["has_disclosure"]:
        badges.append('<span class="chip warn" title="Has a disclosure on record">Disclosure</span>')
    place = " ".join(x for x in (nice_name(r["bcity"] or r["firm_city"]), r["bstate"] or r["state"]) if x)
    reach = ui.contact_pills(email=r["email"], phone=r["phone"], phone_label=r["phone_label"],
                             linkedin=r["linkedin"], email_ok=r["email_status"] == "valid")
    if not r["email"]:
        reach = (reach or "") + '<div class="hunt" title="Bellwether keeps looking: website, search, ' \
                                'patterns checked against the mail server, and AI research"><i></i>Finding email</div>'
    lists = ""
    if r["products"]:
        dots = []
        for k in (r["products"] or "").split(","):
            if not k:
                continue
            try:
                p = products.product(k)
            except KeyError:
                continue
            dots.append(f'<span class="dotc" style="background:{FAMILY_COLOUR.get(p["family"], "#888")}"'
                        f' title="{esc(p["name"])}"></span>')
        lists = "".join(dots)
    since = (r["start_date"] or "")[:7]
    return (f'<tr class="go" data-href="/firm/{esc(r["crd"])}#people">'
            f'<td><div class="ent">{ui.mono(r["name"], "p")}<div><div class="t">{esc(r["name"])}</div>'
            f'<div class="meta">{esc(title)}</div>'
            f'{("<div class=pills style=margin-top:6px>" + "".join(badges) + "</div>") if badges else ""}</div></div></td>'
            f'<td><a class="firm" href="/firm/{esc(r["crd"])}">{escn(r["firm_name"])}</a>'
            f'<div class="meta">{esc(place)}{" . " + money(r["raum"]) if r["raum"] else ""} {lists}</div></td>'
            f'<td>{reach}</td>'
            f'<td><div class="small">{esc(since) or "<span class=muted>-</span>"}</div>'
            f'<div class="meta">{("From " + escn(r["prior_firm"])) if r["prior_firm"] else ""}</div></td></tr>')


@router.get("/people", response_class=HTMLResponse)
def people_page(q: str = Query(""), st: str = Query(""), on: str = Query(""),
                view: str = Query(""), role: str = Query(""), joined: str = Query(""),
                desig: str = Query(""), disc: str = Query(""), size: str = Query(""),
                cat: str = Query(""), sort: str = Query(""),
                page_n: int = Query(1, ge=1, alias="page"),
                per: int = Query(50, ge=10, le=200),
                # Old links used these; they still work.
                reach: str = Query(""), officers: str = Query(""), cfp: str = Query("")):
    if reach and not view:
        view = {"email": "email", "verified": "email", "phone": "direct", "none": "hunting",
                "ready": "email"}.get(reach, "")
    if officers and not role:
        role = "officer"
    if cfp and not desig:
        desig = "cfp"
    c = conn()
    try:
        ready = people_index.exists(c)
        if not ready:
            have = c.execute("SELECT to_regclass('person') t").fetchone()["t"]
    finally:
        c.close()
    if not ready:
        msg = ("Bellwether is building the people index now; this page fills in within a minute."
               if have else "The roster of every registered adviser rep loads with the weekly SEC "
               "individual feed. It runs by itself; this page fills in as soon as it has.")
        return page("People", "people", f'<div class="pg narrow"><h1>People</h1>'
                                        f'<p class="lede">{msg}</p></div>')
    view = view if view in VIEWS else ""
    sort = sort if sort in ORDER else ""
    where, args = _where(q, st, on, view, role, joined, desig, disc, size, cat)
    minute = int(time.monotonic() // 60)
    total = _count(where, tuple(args), minute)
    c = conn()
    try:
        rows = c.execute(f"SELECT * FROM people_index WHERE {where} ORDER BY {ORDER[sort]}"
                         f" LIMIT ? OFFSET ?", args + [per, (page_n - 1) * per]).fetchall()
    finally:
        c.close()
    # The view tabs show how many people each one holds under the other filters.
    tab_counts = {}
    for k in VIEWS:
        w2, a2 = _where(q, st, on, k, role, joined, desig, disc, size, cat)
        tab_counts[k] = _count(w2, tuple(a2), minute)
    vals = dict(q=q, st=st, on=on, view=view, role=role, joined=joined, desig=desig, disc=disc,
                size=size, cat=cat, sort=sort)
    qs = qs_join(**vals)
    tabs = "".join(
        f'<a href="/people?{qs_join(**dict(vals, view=k))}" class="{"on" if view == k else ""}">'
        f'{esc(label)}<span class="cnt">{tab_counts[k]:,}</span></a>'
        for k, (label, _) in VIEWS.items())
    lists = ([("", "Any firm"), ("any", "Firms on any list")]
             + [(k, products.product(k)["name"]) for k in products.product_keys()])
    states = [("", "All states")] + [(s, s) for s in _states(minute // 30)]
    more = [("role", "Role", ROLE), ("joined", "Joined", JOINED), ("desig", "Designation", DESIG),
            ("size", "Firm size", SIZE)]
    cats = _categories()
    if cats:
        more.append(("cat", "Firm type", [("", "Any firm type")] + cats))
    more += [("disc", "With a disclosure", "check"), ("sort", "Sort", SORTS)]
    bar = ui.filter_bar("/people", vals, search=("q", "Search people, firms or CRD"),
                        quick=[("on", "List", lists), ("st", "State", states)],
                        more=more, hidden={"view": view})
    pages = max(1, -(-total // per))
    prev = f'<a href="/people?{qs}&page={page_n - 1}">Previous</a>' if page_n > 1 else ""
    nxt = f'<a href="/people?{qs}&page={page_n + 1}">Next</a>' if page_n < pages else ""
    empty = ('<tr><td colspan="4"><div class="empty"><b>Nobody matches these filters</b>'
             'Remove a filter above, or search a different name.</div></td></tr>')
    body = f"""<div class="pg wide">
<div class="head"><div><h1>People</h1>
<p class="lede">Everyone registered at an advisory firm, with the best way to reach them that
Bellwether has found and checked.</p></div>
<div class="acts"><details class="save-view"><summary>Save view</summary>
<form method="post" action="/views/save"><input type="hidden" name="page" value="people">
<input type="hidden" name="qs" value="{esc(qs)}"><input type="text" name="name" placeholder="Name this view" required>
<button type="submit" class="primary sm">Save</button></form></details>
<a class="btn" href="/people/export.csv?{qs}" data-noprefetch>Export</a></div></div>
<nav class="tabs">{tabs}</nav>
{bar}
<div class="resbar"><span><b>{total:,}</b> {"person" if total == 1 else "people"}</span>
<span class="muted small">Page {page_n} of {pages:,}</span></div>
<div class="table-scroll"><table><thead><tr><th style="width:30%">Person</th><th style="width:28%">Firm</th>
<th>Reach</th><th style="width:16%">At the firm since</th></tr></thead>
<tbody>{"".join(_person_row(r) for r in rows) or empty}</tbody></table></div>
<div class="pager"><span>{total:,} people</span><span class="acts">{prev}{nxt}</span></div></div>"""
    return page("People", "people", body)


@router.get("/people/export.csv")
def people_export(q: str = "", st: str = "", on: str = "", view: str = "", role: str = "",
                  joined: str = "", desig: str = "", disc: str = "", size: str = "", cat: str = "",
                  sort: str = ""):
    c = conn()
    try:
        if not people_index.exists(c):
            return PlainTextResponse("name\n", media_type="text/csv")
        view = view if view in VIEWS else ""
        sort = sort if sort in ORDER else ""
        where, args = _where(q, st, on, view, role, joined, desig, disc, size, cat)
        rows = c.execute(f"""SELECT name, title, firm_name AS firm, crd, state, bcity AS branch_city,
                start_date AS at_firm_since, prior_firm, designations, has_disclosure,
                email, email_status, phone, phone_label, linkedin, iapd_link, category AS firm_type
                FROM people_index WHERE {where} ORDER BY {ORDER[sort]} LIMIT 50000""", args).fetchall()
    finally:
        c.close()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    cols = list(rows[0].keys()) if rows else ["name"]
    w.writerow(cols)
    for r in rows:
        w.writerow([r[k] for k in cols])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="people.csv"'})
