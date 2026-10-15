"""People: every person at every adviser firm, and how to reach them.

The SEC's individual registrations give the roster (who works where, since
when, where they were before, their exams and credentials and whether they
carry a disclosure); Schedule A adds officers' titles and the executives who
hold no registration; the firm's website, vCards, brochures, directories, web
search and verification add the staff the filings never name (HR, talent,
operations) and their emails, phones and LinkedIn profiles.
prospect/people_index.py folds all of it into one table built for this
screen, so every filter is a single indexed read and the page answers in a
blink however the list is cut.

Designations are the job titles each product's sellers look for: pick a
product list and its designations appear as one-click filters (Head of Talent
Acquisition for ATS, Chief Compliance Officer for Glynac), or type any title.
Admins and a product's owner edit the list from here.
"""

from __future__ import annotations

import csv
import io
import time
from datetime import date, timedelta
from functools import lru_cache

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from . import designations, people_index, products, roles, ui, users
from .names import first_name
from .webapp import (conn, current_account, current_owner, esc, escn, is_admin, money, nice_name,
                     page, qs_join)

router = APIRouter()

VIEWS = {
    "": ("Everyone", ""),
    "email": ("With email", "has_email = 1"),
    "verified": ("Verified email", "email_status = 'valid'"),
    "direct": ("Direct line", "has_direct = 1"),
    "linkedin": ("LinkedIn", "has_linkedin = 1"),
    "hunting": ("Still hunting", "has_email = 0"),
}
JOINED = [("", "Joined any time"), ("90", "Joined in 90 days"), ("365", "Joined in 12 months"),
          ("1095", "Joined in 3 years")]
ROLE = [("", "Any role"), ("leader", "Leadership"), ("officer", "Officers on Form ADV")]
CRED = [("", "Any credential"), ("cfp", "CFP"), ("cfa", "CFA")]
SRC = [("", "Anywhere"), ("sec", "SEC registration"), ("adv", "Form ADV officers only"),
       ("web", "Firm website or web search")]
SIZE = [("", "Any firm size"), ("100", "$100M+"), ("500", "$500M+"), ("1000", "$1B+"), ("5000", "$5B+")]
SORTS = [("", "Best firms first"), ("joined", "Newest joiners"), ("name", "Name A to Z"),
         ("firm", "Firm A to Z"), ("aum", "Largest firms")]
ORDER = {"": "priority DESC NULLS LAST, raum DESC NULLS LAST, name",
         "joined": "start_date DESC NULLS LAST, name",
         "name": "last_name, first_name",
         "firm": "firm_lc, last_name",
         "aum": "raum DESC NULLS LAST, name"}
FIELDS = ("q", "st", "on", "view", "role", "joined", "cred", "disc", "size", "cat", "src",
          "des", "title", "sort")


def _product(on: str) -> str | None:
    return on if on in products.product_keys() else None


def _order(f: dict, cols: set) -> str:
    """Best firms first means best on the chosen list, when one is chosen."""
    p = _product(f["on"])
    if not f["sort"] and p and f"sc_{p}" in cols:
        return f"sc_{p} DESC NULLS LAST, raum DESC NULLS LAST, name"
    return ORDER[f["sort"]]


def _where(f: dict, cols: set, skip: str = "") -> tuple[str, list]:
    where, args = ["1=1"], []
    if f["q"]:
        ql = f["q"].lower().strip()
        where.append("(name_lc LIKE ? OR firm_lc LIKE ? OR crd = ?)")
        args += [f"%{ql}%", f"%{ql}%", ql]
    if f["st"]:
        where.append("state = ?")
        args.append(f["st"])
    p = _product(f["on"])
    if p and f"sc_{p}" in cols:
        where.append(f"sc_{p} IS NOT NULL")
    elif p:
        where.append("EXISTS (SELECT 1 FROM product_score ps WHERE ps.crd = people_index.crd"
                     " AND ps.product = ? AND ps.status = 'scored')")
        args.append(p)
    elif f["on"] == "any":
        where.append("priority IS NOT NULL")
    if skip != "view" and f["view"] in VIEWS and VIEWS[f["view"]][1]:
        where.append(VIEWS[f["view"]][1])
    if f["role"] == "leader":
        where.append("is_leader = 1")
    elif f["role"] == "officer":
        where.append("is_officer = 1")
    if f["joined"] in dict(JOINED) and f["joined"]:
        where.append("start_date >= ?")
        args.append((date.today() - timedelta(days=int(f["joined"]))).isoformat())
    if f["cred"] == "cfp":
        where.append("cfp = 1")
    elif f["cred"] == "cfa":
        where.append("cfa = 1")
    if f["disc"]:
        where.append("has_disclosure = 1")
    if f["size"] in dict(SIZE) and f["size"]:
        where.append("raum >= ?")
        args.append(int(f["size"]) * 1_000_000)
    if f["cat"]:
        where.append("category = ?")
        args.append(f["cat"])
    if f["src"] in dict(SRC) and f["src"] and "src" in cols:
        where.append("src = ?")
        args.append(f["src"])
    if skip != "des" and f["des"] and "desig" in cols:
        where.append(f"desig && ARRAY[{','.join('?' * len(f['des']))}]::text[]")
        args += list(f["des"])
    if f["title"]:
        if "title_norm" in cols:
            sql, targs = designations.typed_sql(f["title"])
            if sql:
                where.append(sql)
                args += targs
        else:
            where.append("title ILIKE ?")
            args.append(f"%{f['title'].strip()}%")
    return " AND ".join(where), args


@lru_cache(maxsize=256)
def _tab_counts(where: str, args: tuple, minute: int) -> dict:
    """How many people each view tab holds under the other filters, in one
    pass over the index rather than one count per tab."""
    cols = ", ".join(f"COUNT(*) FILTER (WHERE {cond}) AS v_{k}" for k, (_, cond) in VIEWS.items() if k)
    c = conn()
    try:
        r = c.execute(f"SELECT COUNT(*) AS v_, {cols} FROM people_index WHERE {where}", args).fetchone()
        return {k: int(r[f"v_{k}"] or 0) for k in VIEWS}
    finally:
        c.close()


@lru_cache(maxsize=128)
def _desig_counts(where: str, args: tuple, tags: tuple, minute: int) -> dict:
    """How many people under the other filters hold each designation: one
    read, through the designation index."""
    if not tags:
        return {}
    c = conn()
    try:
        return {r["d"]: r["n"] for r in c.execute(
            f"SELECT d, COUNT(*) n FROM people_index, unnest(desig) d WHERE {where}"
            f" AND desig && ARRAY[{','.join('?' * len(tags))}]::text[] GROUP BY d",
            args + tags)}
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


def _filters(**kw) -> dict:
    # A filter left out of a direct call (the release check renders this page
    # in Python) arrives as FastAPI's Query default, not a string.
    f = {k: (kw.get(k) if isinstance(kw.get(k), str) else "") for k in FIELDS}
    f["view"] = f["view"] if f["view"] in VIEWS else ""
    f["sort"] = f["sort"] if f["sort"] in ORDER else ""
    f["title"] = " ".join(f["title"].split())[:120]
    known = {designations.tag(p, d["key"])
             for p in products.product_keys() + [designations.GENERAL]
             for d in designations.for_product(p)}
    f["des"] = tuple(dict.fromkeys(t for t in f["des"].split(",") if t in known))[:12]
    return f


def _vals(f: dict, **change) -> dict:
    v = dict(f, des=",".join(f["des"]))
    v.update(change)
    return v


def can_edit_designations(product: str) -> bool:
    if product == designations.GENERAL:
        return is_admin()
    try:
        return users.can_edit_product(current_account(), products.product(product)["family"])
    except KeyError:
        return False


def _person_row(r, prod_tags: list, active: tuple = ()) -> str:
    title = roles.clean_title(r["title"]) or ("Officer" if r["is_officer"] else "Registered representative")
    src = r.get("src") or "sec"
    badges = []
    mine = set(r.get("desig") or [])
    held = [t for t in prod_tags if t in mine]
    picked = [t for t in held if t in active] or held[:1]
    for t in picked:
        badges.append(f'<span class="chip line">{esc(designations.label_of(t))}</span>')
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
                                'patterns checked against the mail server, and AI research">Finding email</div>'
    lists = ""
    if r["products"]:
        names = []
        for k in (r["products"] or "").split(","):
            if not k:
                continue
            try:
                names.append(products.product(k)["name"])
            except KeyError:
                continue
        lists = f'&middot; {esc(", ".join(names))}' if names else ""
    if src == "sec":
        since = (r["start_date"] or "")[:7]
        joined = (f'<div class="small">{esc(since) or "<span class=muted>Unknown</span>"}</div>'
                  f'<div class="meta">{("From " + escn(r["prior_firm"])) if r["prior_firm"] else ""}</div>')
    else:
        joined = ('<div class="small muted">Not registered</div><div class="meta">'
                  + ("Officer on Form ADV" if src == "adv" else "Firm website or web search")
                  + '</div>')
    return (f'<tr class="go" data-href="/firm/{esc(r["crd"])}#people">'
            f'<td><div class="ent">{ui.mono(r["name"], "p")}<div><div class="t">{esc(r["name"])}</div>'
            f'<div class="meta">{esc(title)}</div>'
            f'{("<div class=pills style=margin-top:6px>" + "".join(badges) + "</div>") if badges else ""}</div></div></td>'
            f'<td>{esc(first_name(r["name"], r.get("first_name"))) or "<span class=muted>Unknown</span>"}</td>'
            f'<td><a class="firm" href="/firm/{esc(r["crd"])}">{escn(r["firm_name"])}</a>'
            f'<div class="meta">{esc(place)}{" &middot; " + money(r["raum"]) if r["raum"] else ""} {lists}</div></td>'
            f'<td>{reach}</td><td>{joined}</td></tr>')


def _designation_strip(f: dict, cols: set, minute: int) -> tuple[str, set]:
    """The chosen product's designations as one-click filters with counts, and
    a box to type any other title."""
    prod = _product(f["on"]) or designations.GENERAL
    items = designations.for_product(prod)
    tags = tuple(designations.tag(prod, d["key"]) for d in items)
    if "desig" not in cols:
        return ('<div class="desig"><div class="desig-head"><b>Designation</b><span class="meta">'
                'Designation filters arrive with the next rebuild of the people index, within '
                'half an hour.</span></div></div>'), set(tags)
    where, args = _where(f, cols, skip="des")
    counts = _desig_counts(where, tuple(args), tags, minute)
    chips = []
    for d, t in zip(items, tags):
        on = t in f["des"]
        nxt = tuple(x for x in f["des"] if x != t) if on else f["des"] + (t,)
        href = "/people?" + qs_join(**_vals(f, des=",".join(nxt)))
        n = counts.get(t, 0)
        chips.append(f'<a class="dchip{" on" if on else ""}{" zero" if not n and not on else ""}" '
                     f'href="{esc(href)}" aria-pressed="{"true" if on else "false"}">{esc(d["label"])}'
                     f'<span class="cnt">{n:,}</span></a>')
    keep = "".join(f'<input type="hidden" name="{k}" value="{esc(v)}">'
                   for k, v in _vals(f).items() if v and k != "title")
    typed = (f'<form class="dtyped" method="get" action="/people">{keep}'
             f'<input type="text" name="title" value="{esc(f["title"])}" placeholder="Type any title"'
             f' aria-label="Type a title to find" autocomplete="off">'
             f'<button type="submit" class="sm">Find</button></form>')
    if prod == designations.GENERAL:
        scope = "Common titles. Pick a product list above for its own."
    else:
        scope = f"For {esc(products.product(prod)['name'])}"
    edit = (f'<a class="desig-edit" href="/people/designations/{esc(prod)}">Edit designations</a>'
            if can_edit_designations(prod) else "")
    html = (f'<div class="desig"><div class="desig-head"><b>Designation</b>'
            f'<span class="meta">{scope}. Choose several to see anyone who holds one of them.</span>{edit}</div>'
            f'<div class="desig-row"><div class="dchips">{"".join(chips)}</div>{typed}</div></div>')
    # In the product's own order, most specific first, for the row badges.
    return html, (list(tags) if prod != designations.GENERAL else [])


@router.get("/people", response_class=HTMLResponse)
def people_page(q: str = Query(""), st: str = Query(""), on: str = Query(""),
                view: str = Query(""), role: str = Query(""), joined: str = Query(""),
                cred: str = Query(""), disc: str = Query(""), size: str = Query(""),
                cat: str = Query(""), src: str = Query(""), des: str = Query(""),
                title: str = Query(""), sort: str = Query(""),
                page_n: int = Query(1, ge=1, alias="page"),
                per: int = Query(50, ge=10, le=200),
                # Old links used these; they still work.
                reach: str = Query(""), officers: str = Query(""), cfp: str = Query(""),
                desig: str = Query("")):
    # Called directly (the release check renders this page in Python), any
    # filter left out arrives as FastAPI's Query default rather than a string.
    s = {k: (v if isinstance(v, str) else "") for k, v in dict(
        q=q, st=st, on=on, view=view, role=role, joined=joined, cred=cred, disc=disc, size=size,
        cat=cat, src=src, des=des, title=title, sort=sort, reach=reach, officers=officers,
        cfp=cfp, desig=desig).items()}
    q, st, on, view, role, joined, cred, disc, size, cat, src, des, title, sort = (
        s[k] for k in ("q", "st", "on", "view", "role", "joined", "cred", "disc", "size", "cat",
                       "src", "des", "title", "sort"))
    reach, officers, cfp, desig = s["reach"], s["officers"], s["cfp"], s["desig"]
    page_n = page_n if isinstance(page_n, int) else 1
    per = per if isinstance(per, int) else 50
    if reach and not view:
        view = {"email": "email", "verified": "verified", "phone": "direct", "none": "hunting",
                "ready": "email"}.get(reach, "")
    if officers and not role:
        role = "officer"
    if not cred:
        cred = "cfp" if cfp else (desig if desig in ("cfp", "cfa") else "")
    c = conn()
    try:
        ready = people_index.exists(c)
        if not ready:
            have = c.execute("SELECT to_regclass('person') t").fetchone()["t"]
        cols = people_index.columns(c) if ready else set()
    finally:
        c.close()
    if not ready:
        msg = ("The people index is being prepared. Refresh this page after the job finishes."
               if have else "The roster of every registered adviser rep loads with the weekly SEC "
               "individual feed. It runs by itself; this page fills in as soon as it has.")
        return page("People", "people", f'<div class="pg narrow"><h1>People</h1>'
                                        f'<p class="lede">{msg}</p></div>')
    f = _filters(q=q, st=st, on=on, view=view, role=role, joined=joined, cred=cred, disc=disc,
                 size=size, cat=cat, src=src, des=des, title=title, sort=sort)
    where, args = _where(f, cols)
    minute = int(time.monotonic() // 60)
    # The view tabs show how many people each one holds under the other filters.
    w_all, a_all = _where(f, cols, skip="view")
    tab_counts = _tab_counts(w_all, tuple(a_all), minute)
    total = tab_counts[f["view"]]
    c = conn()
    try:
        rows = c.execute(f"SELECT * FROM people_index WHERE {where} ORDER BY {_order(f, cols)}"
                         f" LIMIT ? OFFSET ?", args + [per, (page_n - 1) * per]).fetchall()
    finally:
        c.close()
    vals = _vals(f)
    qs = qs_join(**vals)
    tabs = "".join(
        f'<a href="/people?{qs_join(**_vals(f, view=k))}" class="{"on" if f["view"] == k else ""}">'
        f'{esc(label)}<span class="cnt">{tab_counts[k]:,}</span></a>'
        for k, (label, _) in VIEWS.items())
    lists = ([("", "Any firm"), ("any", "Priority firms")]
             + [(k, products.product(k)["name"]) for k in products.product_keys()])
    states = [("", "All states")] + [(s, s) for s in _states(minute // 30)]
    more = [("role", "Role", ROLE), ("joined", "Joined", JOINED), ("cred", "Credential", CRED),
            ("size", "Firm size", SIZE)]
    cats = _categories()
    if cats:
        more.append(("cat", "Firm type", [("", "Any firm type")] + cats))
    if "src" in cols:
        more.append(("src", "Found in", SRC))
    more += [("disc", "With a disclosure", "check"), ("sort", "Sort", SORTS)]
    extra = [(designations.label_of(t),
              "/people?" + qs_join(**_vals(f, des=",".join(x for x in f["des"] if x != t))))
             for t in f["des"]]
    if f["title"]:
        extra.append((f'Title: {f["title"]}', "/people?" + qs_join(**_vals(f, title=""))))
    bar = ui.filter_bar("/people", {k: v for k, v in vals.items() if k not in ("des", "title")},
                        search=("q", "Search people, firms or CRD"),
                        quick=[("on", "List", lists), ("st", "State", states)],
                        more=more, hidden={"view": f["view"]},
                        carry={"des": vals["des"], "title": f["title"]}, extra_chips=extra)
    strip, prod_tags = _designation_strip(f, cols, minute)
    pages = max(1, -(-total // per))
    prev = f'<a href="/people?{qs}&page={page_n - 1}">Previous</a>' if page_n > 1 else ""
    nxt = f'<a href="/people?{qs}&page={page_n + 1}">Next</a>' if page_n < pages else ""
    empty = ('<tr><td colspan="5"><div class="empty"><b>Nobody matches these filters</b>'
             'Remove a filter above, or try a broader title.</div></td></tr>')
    body = f"""<div class="pg wide">
<div class="head"><div><h1>People</h1>
<p class="lede">Everyone at every adviser firm: registered reps, the officers on Form ADV, and the
staff firm websites name. Filter by the titles your product sells to.</p></div>
<div class="acts"><details class="save-view"><summary>Save view</summary>
<form method="post" action="/views/save"><input type="hidden" name="page" value="people">
<input type="hidden" name="qs" value="{esc(qs)}"><input type="text" name="name" placeholder="Name this view" required>
<button type="submit" class="primary sm">Save</button></form></details>
<a class="btn" href="/people/export.csv?{qs}" data-noprefetch>Export</a></div></div>
<nav class="tabs">{tabs}</nav>
{bar}
{strip}
<div class="resbar"><span><b>{total:,}</b> {"person" if total == 1 else "people"}</span>
<span class="muted small">Page {page_n} of {pages:,}</span></div>
<div class="table-scroll"><table><thead><tr><th style="width:28%">Person</th><th>First name</th><th style="width:26%">Firm</th>
<th>Contact</th><th style="width:14%">Joined firm</th></tr></thead>
<tbody>{"".join(_person_row(r, prod_tags, f["des"]) for r in rows) or empty}</tbody></table></div>
<div class="pager"><span>{total:,} people</span><span class="acts">{prev}{nxt}</span></div></div>"""
    return page("People", "people", body)


@router.get("/people/export.csv")
def people_export(q: str = "", st: str = "", on: str = "", view: str = "", role: str = "",
                  joined: str = "", cred: str = "", disc: str = "", size: str = "", cat: str = "",
                  src: str = "", des: str = "", title: str = "", sort: str = "", desig: str = ""):
    if not cred and desig in ("cfp", "cfa"):
        cred = desig
    c = conn()
    try:
        if not people_index.exists(c):
            return PlainTextResponse("name,first_name\n", media_type="text/csv")
        cols = people_index.columns(c)
        f = _filters(q=q, st=st, on=on, view=view, role=role, joined=joined, cred=cred, disc=disc,
                     size=size, cat=cat, src=src, des=des, title=title, sort=sort)
        where, args = _where(f, cols)
        extra = ", src AS found_in, desig" if "desig" in cols else ""
        rows = c.execute(f"""SELECT name, first_name, title, firm_name AS firm, crd, state, bcity AS branch_city,
                start_date AS at_firm_since, prior_firm, designations AS credentials, has_disclosure,
                email, email_status, phone, phone_label, linkedin, iapd_link, category AS firm_type{extra}
                FROM people_index WHERE {where} ORDER BY {_order(f, cols)} LIMIT 50000""", args).fetchall()
    finally:
        c.close()
    prod = _product(f["on"]) or designations.GENERAL
    mine = {designations.tag(prod, d["key"]) for d in designations.for_product(prod)}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    cols_out = [k for k in (rows[0].keys() if rows else ["name", "first_name"]) if k != "desig"]
    if rows and "desig" in rows[0].keys():
        cols_out.append("designation")
    w.writerow(cols_out)
    for r in rows:
        out = []
        for k in cols_out:
            if k == "first_name":
                out.append(first_name(r["name"], r["first_name"]))
            elif k == "designation":
                out.append("; ".join(designations.label_of(t) for t in (r["desig"] or []) if t in mine))
            elif k == "found_in":
                out.append({"sec": "SEC registration", "adv": "Form ADV officer",
                            "web": "Website or web search"}.get(r[k], r[k] or ""))
            else:
                out.append(r[k])
        w.writerow(out)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="people.csv"'})


# ------------------------------------------------------------------ editing

@lru_cache(maxsize=2)
def _title_counts(ten_minutes: int) -> list[tuple[str, int]]:
    """The commonest titles in the index, for the editor's preview."""
    c = conn()
    try:
        return [(r["title"], r["n"]) for r in c.execute(
            "SELECT title, COUNT(*) n FROM people_index WHERE title IS NOT NULL AND title != ''"
            " GROUP BY title ORDER BY n DESC LIMIT 30000")]
    except Exception:
        c.rollback()
        return []
    finally:
        c.close()


def _preview(d: dict, titles: list[tuple[str, int]]) -> str:
    anyp = [tuple(designations.words(p)) for p in d["any"]]
    notp = [tuple(designations.words(p)) for p in d["not"]]
    hits, n = [], 0
    for t, k in titles:
        tw = designations.words(t)
        if any(designations._hit(tw, p) for p in anyp) and not any(designations._hit(tw, p) for p in notp):
            n += k
            if len(hits) < 4:
                ct = roles.clean_title(t)
                hits.append(f"{ct if len(ct) <= 48 else ct[:46].rstrip(' ,') + '…'} ({k:,})")
    if not n:
        return '<div class="meta warnc">No titles in the index match yet.</div>'
    return (f'<div class="small"><b>{n:,}</b> people</div>'
            f'<div class="meta">{esc(", ".join(hits))}</div>')


@router.get("/people/designations/{prod}", response_class=HTMLResponse)
def designations_page(prod: str, msg: str = Query(""), err: str = Query("")):
    if prod != designations.GENERAL and prod not in products.product_keys():
        return RedirectResponse("/people", status_code=303)
    edit = can_edit_designations(prod)
    name = "Common titles" if prod == designations.GENERAL else products.product(prod)["name"]
    items = designations.for_product(prod)
    titles = _title_counts(int(time.monotonic() // 600))
    dis = "" if edit else " disabled"
    rows = []
    for i, d in enumerate(items + [{"key": "", "label": "", "any": [], "not": []}] * (2 if edit else 0)):
        new = not d["label"]
        phrases = ", ".join(d["any"])
        # Tall enough to show every phrase without scrolling the box.
        lines = max(2, min(7, -(-len(phrases) // 34)))
        rows.append(
            f'<tr><td><input type="hidden" name="k-{i}" value="{esc(d["key"])}">'
            f'<textarea name="l-{i}" rows="2" class="dname" aria-label="Designation name"{dis}'
            f' placeholder="{"Add a designation" if new else ""}">{esc(d["label"])}</textarea></td>'
            f'<td><textarea name="a-{i}" rows="{lines}" aria-label="Titles that count"{dis}'
            f' placeholder="{"e.g. head of talent, recruiting director" if new else ""}">{esc(phrases)}</textarea></td>'
            f'<td><input type="text" name="n-{i}" value="{esc(", ".join(d["not"]))}" aria-label="Except titles with"{dis}></td>'
            f'<td>{"" if new else _preview(d, titles)}</td>'
            f'<td class="num">{"" if new or not edit else f"<label class=inline><input type=checkbox name=x-{i} value=1> Remove</label>"}</td></tr>')
    ed = designations.edited(prod)
    who = (f'Last changed by {esc(ed["by"] or "someone")} on {esc((ed["at"] or "")[:16].replace("T", " "))} UTC.'
           if ed else "These are the shipped defaults from config/designations.yml.")
    flash = (f'<div class="note good">{esc(msg)}</div>' if msg else "") + (
        f'<div class="note bad">{esc(err)}</div>' if err else "")
    actions = ""
    if edit:
        actions = (f'<div class="savebar"><span class="muted small">{len(items)} designations</span>'
                   f'<span class="spacer"></span><button type="submit" class="primary">Save designations</button></div>')
    reset = (f'<form method="post" action="/people/designations/{esc(prod)}/reset" style="margin-top:12px" '
             f'onsubmit="return confirm(\'Put these designations back to the shipped defaults?\')">'
             f'<button type="submit" class="ghost sm">Reset to the shipped defaults</button></form>'
             if edit and ed else "")
    back = "/people?" + qs_join(on="" if prod == designations.GENERAL else prod)
    body = f"""<div class="pg">
<div class="crumb"><a href="/people">People</a><span class="sep">/</span>Designations</div>
<div class="head"><div><h1>{esc(name)} designations</h1>
<p class="lede">The job titles sellers filter People by when {esc(name if prod != designations.GENERAL else "no list")} is
chosen. A designation counts anyone whose title matches one of its phrases.</p></div>
<div class="acts"><a class="btn" href="{esc(back)}">Back to People</a></div></div>
{flash}
<details class="source-help"><summary>How titles are matched</summary>
<p>Case, punctuation and small words (of, the, and) are ignored, and short forms are spelled out:
VP, HR, COO, CEO, CFO, CCO, CIO, Ops, Dir, Sr and others. A phrase matches when each of its words
starts a word of the title, in any order, so <b>director human resources</b> finds HR Director and
Director of Human Resources, and <b>recruit</b> finds Recruiter and Recruiting Manager. A title with
one of the Except phrases is left out. Vice President never counts as President.</p></details>
<p class="meta">{who} Changes reach the People list within a few minutes, once its index rebuilds.
{"" if edit else "Only admins and this product&rsquo;s owner can change these."}</p>
<form method="post" action="/people/designations/{esc(prod)}">
<div class="table-scroll"><table class="desig-edit-table"><thead><tr><th style="width:21%">Designation</th>
<th>Titles that count, separated by commas</th><th style="width:14%">Except</th><th style="width:24%">Matches now</th><th style="width:96px"></th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
{actions}</form>{reset}</div>"""
    return page(f"{name} designations", "people", body)


@router.post("/people/designations/{prod}")
async def designations_save(prod: str, request: Request):
    if (prod != designations.GENERAL and prod not in products.product_keys()) or not can_edit_designations(prod):
        return RedirectResponse("/people", status_code=303)
    form = await request.form()
    items = []
    i = 0
    while f"l-{i}" in form:
        if not form.get(f"x-{i}"):
            label = " ".join((form.get(f"l-{i}") or "").split())
            anyp = designations.parse_phrases(form.get(f"a-{i}") or "")
            if label and anyp:
                items.append({"key": form.get(f"k-{i}") or label.lower(), "label": label, "any": anyp,
                              "not": designations.parse_phrases(form.get(f"n-{i}") or "")})
            elif label or anyp:
                return RedirectResponse(
                    f"/people/designations/{prod}?{qs_join(err='Each designation needs a name and at least one title phrase.')}",
                    status_code=303)
        i += 1
    try:
        designations.save(prod, items, current_owner())
    except ValueError as e:
        return RedirectResponse(f"/people/designations/{prod}?{qs_join(err=str(e))}", status_code=303)
    _rebuild_index()
    return RedirectResponse(
        f"/people/designations/{prod}?{qs_join(msg='Saved. The People list uses these within a few minutes.')}",
        status_code=303)


@router.post("/people/designations/{prod}/reset")
def designations_reset(prod: str):
    if (prod != designations.GENERAL and prod not in products.product_keys()) or not can_edit_designations(prod):
        return RedirectResponse("/people", status_code=303)
    designations.reset(prod)
    _rebuild_index()
    return RedirectResponse(f"/people/designations/{prod}?{qs_join(msg='Back to the shipped defaults.')}",
                            status_code=303)


def _rebuild_index() -> None:
    from . import jobs
    c = conn()
    try:
        jobs.request_run(c, "people_index")
    except Exception:
        c.rollback()
    finally:
        c.close()
