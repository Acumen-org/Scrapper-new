"""People: every person at every adviser firm, and how to reach them.

The SEC's individual registrations give the roster (who works where, since
when, where they were before, their exams and designations and whether they
carry a disclosure); Schedule A gives officers' titles; the firm's website,
vCards, brochures and directories give emails and direct lines; verification
says which addresses a mail server confirmed. This page joins all of it, so a
list of "CFPs who joined a PHH firm this year, with a verified email" is one
filter away.
"""

from __future__ import annotations

import json
import time
from functools import lru_cache
from datetime import date, timedelta

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from . import contacts, products, roles, ui
from .webapp import conn, esc, escn, money, page, qs_join

router = APIRouter()

REACH = {"ready": "Ready to contact", "": "Full directory", "email": "Verified email", "verified": "Verified email",
         "phone": "Has a direct line", "none": "No email yet"}
JOINED = {"": "Any time", "90": "Joined in 90 days", "365": "Joined in 12 months",
          "1095": "Joined in 3 years"}
SORTS = {"": "Best firms first", "joined": "Newest joiners", "name": "Name",
         "firm": "Firm name"}


def _have_people(c) -> bool:
    try:
        return bool(c.execute("SELECT EXISTS (SELECT 1 FROM person LIMIT 1) e").fetchone()["e"])
    except Exception:
        c.rollback()
        return False


def _where(q, st, on, officers, reach, joined, cfp, disc):
    where = ["e.kind='current'"]
    args: list = []
    if q:
        where.append("(p.name ILIKE ? OR f.legal_name ILIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if st:
        where.append("f.state=?")
        args.append(st)
    if on in products.product_keys():
        where.append("EXISTS (SELECT 1 FROM product_score ps WHERE ps.crd=e.org_pk"
                     " AND ps.product=? AND ps.status='scored')")
        args.append(on)
    elif on == "any":
        where.append("sc.crd IS NOT NULL")
    if officers:
        where.append("sa.title IS NOT NULL")
    if reach == 'ready':
        where.append("EXISTS (SELECT 1 FROM usable_contact_point c WHERE c.crd=e.org_pk AND c.person_key='i:'||p.indvl_pk)")
    if reach in ("email", "verified", "none"):
        cond = ("EXISTS (SELECT 1 FROM usable_contact_point c WHERE c.crd=e.org_pk"
                " AND c.person_key='i:'||p.indvl_pk AND c.kind='email'"
                " AND c.verify_status NOT IN ('invalid','no_mail_server')"
                + (" AND c.verify_status='valid'" if reach == "verified" else "") + ")")
        where.append(f"NOT {cond}" if reach == "none" else cond)
    elif reach == "phone":
        where.append("EXISTS (SELECT 1 FROM usable_contact_point c WHERE c.crd=e.org_pk"
                     " AND c.person_key='i:'||p.indvl_pk AND c.kind='phone')")
    if joined in JOINED and joined:
        where.append("e.start_date >= ?")
        args.append((date.today() - timedelta(days=int(joined))).isoformat())
    if cfp:
        where.append("p.designations ILIKE ?")
        args.append("%CFP%")
    if disc:
        where.append("p.has_disclosure=1")
    return " AND ".join(where), args


# The officer title is a lateral lookup, so it is joined only where a filter
# needs it, and otherwise only for the page of rows actually shown: joining it
# for every person before counting cost ten seconds.
SA_JOIN = """LEFT JOIN LATERAL (SELECT a.title FROM schedule_a a WHERE a.crd = e.org_pk
        AND a.is_individual = 1 AND p.last_name IS NOT NULL
        AND a.name ILIKE p.last_name || ', ' || COALESCE(p.first_name, '') || '%'
        LIMIT 1) sa ON true"""

BASE = """FROM person_employment e
    JOIN person p ON p.indvl_pk = e.indvl_pk
    JOIN firm_current f ON f.crd = e.org_pk
    LEFT JOIN firm_scope sc ON sc.crd = e.org_pk
    {sa}
    WHERE {where}"""

ORDER = {"": "sc.priority DESC NULLS LAST, f.raum DESC NULLS LAST, p.name",
         "joined": "e.start_date DESC NULLS LAST",
         "name": "p.last_name, p.first_name", "firm": "f.legal_name, p.last_name"}


@lru_cache(maxsize=128)
def _count_people(where, args, officers, minute):
    c = conn()
    try:
        return c.execute(f"SELECT COUNT(*) n {BASE.format(where=where, sa=SA_JOIN if officers else '')}", args).fetchone()['n']
    finally:
        c.close()


@router.get("/people", response_class=HTMLResponse)
def people_page(q: str = Query(""), st: str = Query(""), on: str = Query(""),
                officers: str = Query(""), reach: str = Query("ready"), joined: str = Query(""),
                cfp: str = Query(""), disc: str = Query(""), sort: str = Query("name"),
                page_n: int = Query(1, ge=1, alias="page"),
                per: int = Query(25, ge=10, le=200)):
    c = conn()
    if not _have_people(c):
        c.close()
        return page("People", "people", """<div class="pg narrow"><h1>People</h1>
<p class="lede">The roster of every registered adviser rep loads with the weekly SEC
individual feed. It runs by itself; this page fills in as soon as it has.</p></div>""")
    sort = sort if sort in ORDER else ""
    where, args = _where(q, st, on, officers, reach, joined, cfp, disc)
    filt = BASE.format(where=where, sa=SA_JOIN if officers else "")
    total = _count_people(where, tuple(args), bool(officers), int(time.monotonic() // 60))
    rows = c.execute(f"""
        WITH pg AS (SELECT p.indvl_pk, e.org_pk, e.start_date, e.city, e.state,
                           sc.priority, f.raum, f.legal_name, p.last_name, p.first_name, p.name
                    {filt} ORDER BY {ORDER[sort]} LIMIT ? OFFSET ?)
        SELECT p.indvl_pk, p.name, p.designations, p.exams, p.has_disclosure, p.iapd_link,
               pg.org_pk AS crd, pg.start_date, pg.city, pg.state, pg.legal_name, pg.raum,
               (SELECT a.title FROM schedule_a a WHERE a.crd = pg.org_pk AND a.is_individual = 1
                  AND p.last_name IS NOT NULL
                  AND a.name ILIKE p.last_name || ', ' || COALESCE(p.first_name, '') || '%'
                  LIMIT 1) AS title,
               (SELECT x.org_name FROM person_employment x WHERE x.indvl_pk = p.indvl_pk
                  AND x.kind = 'previous' ORDER BY x.end_date DESC NULLS LAST LIMIT 1) AS prior
        FROM pg JOIN person p ON p.indvl_pk = pg.indvl_pk
        ORDER BY {ORDER[sort].replace("sc.priority", "pg.priority").replace("f.raum", "pg.raum").replace("f.legal_name", "pg.legal_name").replace("e.start_date", "pg.start_date")}""",
                     args + [per, (page_n - 1) * per]).fetchall()
    keys = [(r["crd"], f"i:{r['indvl_pk']}") for r in rows]
    reach_map: dict = {}
    if keys:
        crds = sorted({k[0] for k in keys})
        pks = sorted({k[1] for k in keys})
        for r in c.execute(
                f"""SELECT crd, person_key, kind, value, verify_status, source, title
                    FROM usable_contact_point WHERE crd IN ({','.join('?' * len(crds))})
                    AND person_key IN ({','.join('?' * len(pks))})
                    AND verify_status NOT IN ('invalid','no_mail_server')
                    ORDER BY (verify_status='valid') DESC, confidence DESC""",
                tuple(crds) + tuple(pks)):
            d = reach_map.setdefault((r["crd"], r["person_key"]), {})
            d.setdefault(r["kind"], r)
            if r["title"]:
                d.setdefault("title", r["title"])
    titles = roles.lookup(c, [r["title"] for r in rows if r["title"]])
    states = [x["state"] for x in c.execute(
        "SELECT DISTINCT state FROM firm_current WHERE state IS NOT NULL ORDER BY state")]
    c.close()

    body = []
    for r in rows:
        rm = reach_map.get((r["crd"], f"i:{r['indvl_pk']}"), {})
        em = rm.get("email")
        ph = rm.get("phone")
        title = r["title"] or rm.get("title") or ""
        clean, role = titles.get(r["title"], (roles.clean_title(title), roles.classify(title))) \
            if title else ("", "")
        try:
            des = json.loads(r["designations"] or "[]")
        except ValueError:
            des = []
        des_s = ", ".join(d if isinstance(d, str) else str(d.get("name", "")) for d in des[:3])
        email_html = ""
        if em:
            label = contacts.VERIFY_LABEL.get(em["verify_status"], em["verify_status"])
            if em["source"] == "pattern" and em["verify_status"] in ("unverified", "queued"):
                label = "Guess"
            email_html = (f'<a href="mailto:{esc(em["value"])}">{esc(em["value"])}</a> '
                          f'<span class="chip v-{esc(em["verify_status"])}">{esc(label)}</span>')
        since = r["start_date"] or ""
        flag = (' <span class="chip" title="Has a disclosure on record">disclosure</span>'
                if r["has_disclosure"] else "")
        body.append(
            f'<tr class="go person" data-href="/firm/{esc(r["crd"])}#people">'
            f'<td><div class="nm">{esc(r["name"])}{flag}</div>'
            f'<div class="meta">{esc(clean or "Registered rep")}'
            f'{" . " + esc(roles.ROLE_LABEL.get(role, "")) if role and role != "other" else ""}</div></td>'
            f'<td><a href="/firm/{esc(r["crd"])}">{escn(r["legal_name"])}</a>'
            f'<div class="meta">{esc(" ".join(x for x in ((r["city"] or "").title(), r["state"] or "") if x))}'
            f' . {money(r["raum"])}</div></td>'
            f'<td class="small">{email_html or "<span class=muted>-</span>"}'
            f'{"<div class=meta>" + esc(ph["value"]) + "</div>" if ph else ""}</td>'
            f'<td><details class="person-extra"><summary>Background</summary><p>At the firm since {esc(since[:7]) or "Not recorded"}</p>'
            f'<p>Previously: {escn(r["prior"]) if r["prior"] else "Not recorded"}</p><p>{esc(des_s) or "No designations recorded"}</p></details></td></tr>')
    qs = qs_join(q=q, st=st, on=on, officers=officers, reach=reach, joined=joined, cfp=cfp,
                 disc=disc, sort=sort)
    pages = max(1, -(-total // per))
    prev = f'<a href="/people?{qs}&page={page_n-1}">Previous</a>' if page_n > 1 else ""
    nxt = f'<a href="/people?{qs}&page={page_n+1}">Next</a>' if page_n < pages else ""
    lst = (ui.opt("", on, "Any firm") + ui.opt("any", on, "Firms on any list")
           + "".join(ui.opt(k, on, products.product(k)["name"]) for k in products.product_keys()))
    body_html = f"""<div class="pg wide">
<div class="head"><div><h1>People</h1>
</div>
<div class="acts"><a class="btn" href="/people/export.csv?{qs}" data-noprefetch>Export</a></div></div>
<nav class="seg"><a href="/people" class="{'on' if reach == 'ready' else ''}">Ready to contact</a><a href="/people?reach=&amp;sort=name" class="{'on' if reach == '' else ''}">Full directory</a><a href="/people?reach=none&amp;sort=name" class="{'on' if reach == 'none' else ''}">Contact discovery</a></nav>
<form class="filters" method="get" action="/people">
<label>Search<input type="search" name="q" value="{esc(q)}" placeholder="Person or firm"></label>
<label>State<select name="st">{ui.opt("", st, "All states")}{"".join(ui.opt(s, st, s) for s in states)}</select></label>
<label>Firm<select name="on">{lst}</select></label>
<label>Joined<select name="joined">{"".join(ui.opt(k, joined, v) for k, v in JOINED.items())}</select></label>
<label>Reach<select name="reach">{"".join(ui.opt(k, reach, v) for k, v in REACH.items())}</select></label>
<label class="inline"><input type="checkbox" name="officers" value="1"{" checked" if officers else ""}> Officers only</label>
<label class="inline"><input type="checkbox" name="cfp" value="1"{" checked" if cfp else ""}> CFP</label>
<label class="inline"><input type="checkbox" name="disc" value="1"{" checked" if disc else ""}> With a disclosure</label>
<label>Sort<select name="sort">{"".join(ui.opt(k, sort, v) for k, v in SORTS.items())}</select></label>
<button class="primary" type="submit">Apply</button><a class="btn ghost" href="/people">Clear</a>
</form>
<div class="row" style="margin:10px 0"><span class="small soft"><b style="color:var(--ink)">{total:,}</b> people</span>
<span class="spacer"></span>
<form method="post" action="/views/save" class="acts"><input type="hidden" name="page" value="people">
<input type="hidden" name="qs" value="{esc(qs)}"><input type="text" name="name" placeholder="Name this view to save it" style="min-width:200px">
<button type="submit" class="sm">Save view</button></form></div>
<table class="people-table"><thead><tr><th>Person</th><th>Firm</th><th>Verified contact</th><th></th></tr></thead>
<tbody>{"".join(body) or '<tr><td colspan="4" class="empty">No contacts ready yet. Follow progress in Contact discovery.</td></tr>'}</tbody></table>
<div class="pager">{prev} Page {page_n} of {pages} {nxt}</div></div>"""
    return page("People", "people", body_html)


@router.get("/people/export.csv")
def people_export(q: str = "", st: str = "", on: str = "", officers: str = "",
                  reach: str = "", joined: str = "", cfp: str = "", disc: str = "",
                  sort: str = ""):
    import csv
    import io
    c = conn()
    if not _have_people(c):
        c.close()
        return PlainTextResponse("no people loaded yet\n", media_type="text/csv")
    sort = sort if sort in ORDER else ""
    where, args = _where(q, st, on, officers, reach, joined, cfp, disc)
    rows = c.execute(f"""
        SELECT p.name, sa.title, f.legal_name AS firm, e.org_pk AS crd, f.state,
               e.start_date AS at_firm_since, p.designations, p.has_disclosure,
               (SELECT value FROM usable_contact_point x WHERE x.crd=e.org_pk
                  AND x.person_key='i:'||p.indvl_pk AND x.kind='email'
                  AND x.verify_status NOT IN ('invalid','no_mail_server')
                  ORDER BY (x.verify_status='valid') DESC, x.confidence DESC LIMIT 1) AS email,
               (SELECT verify_status FROM usable_contact_point x WHERE x.crd=e.org_pk
                  AND x.person_key='i:'||p.indvl_pk AND x.kind='email'
                  AND x.verify_status NOT IN ('invalid','no_mail_server')
                  ORDER BY (x.verify_status='valid') DESC, x.confidence DESC LIMIT 1) AS email_status,
               (SELECT value FROM usable_contact_point x WHERE x.crd=e.org_pk
                  AND x.person_key='i:'||p.indvl_pk AND x.kind='phone'
                  ORDER BY x.confidence DESC LIMIT 1) AS direct_phone,
               p.iapd_link
        {BASE.format(where=where, sa=SA_JOIN)} ORDER BY {ORDER[sort]} LIMIT 25000""", args).fetchall()
    c.close()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    cols = list(rows[0].keys()) if rows else ["name"]
    w.writerow(cols)
    for r in rows:
        w.writerow([r[k] for k in cols])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="people.csv"'})
