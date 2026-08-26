"""Firms: the one place to find, filter, and export firms and their contacts.

This is the merge of what used to be three sections (firm list, working lists,
outreach). The model:

  - A PRESET picks the base set of firms: everything, the PHH tier A list, the
    PHH intersection, the AcuBooth list, competitors, or one of your own saved
    lists. A preset is just a filter, so it composes with the others.
  - FILTERS (search, state, AUM band, real estate segment, status, owner, open
    trigger) narrow the set further.
  - Two VIEWS of that same set: "Firms" (one row per firm, with its score when a
    scored preset is active) and "Contacts" (one row per person, the mail-merge
    list). Each exports what is on screen.
  - Any firm can be added to one of your lists from here.

Everything is server-side filtered and paged; the universe is 13,720 firms.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from . import xlsx
from .webapp import PAGE_CSS, conn, current_owner, esc, money, nav

router = APIRouter()

FIRMS_CSS = PAGE_CSS + """
.vtab{font-size:14px;text-decoration:none;color:var(--soft);padding:6px 2px;
margin-right:18px;display:inline-block}
.vtab:hover{color:var(--ink)}
.vtab.on{color:var(--red-hi);font-weight:700;border-bottom:2px solid var(--red)}
.toolbar{display:flex;justify-content:space-between;align-items:flex-end;
gap:16px;flex-wrap:wrap;margin:6px 0 14px}
.toolbar .count{font-size:13px;color:var(--soft)}
.toolbar .count b{color:var(--ink)}
.listpick{min-width:0;padding:3px 7px;font-size:11.5px}
"""

STATUS_LABEL = {"domain_accepts_mail": "domain ok", "no_mail_server": "dead domain",
                "bad_syntax": "malformed", "queued": "unchecked", "candidate": "guess"}

# The person-level contact worklist, shared by the Contacts view and the Excel
# export. {extra} is an optional extra WHERE clause the caller supplies to scope
# it to the firms the filters selected. One row per person; a firm inbox (info@)
# is kept as a firm-level row, never pinned to an officer's name.
WORKLIST = """
WITH people AS (
    SELECT w.crd, w.person, w.title AS role, w.email, w.phone AS person_phone,
           'their website' AS source, 'domain_accepts_mail' AS status, 3 AS trust
    FROM web_contact w WHERE w.person IS NOT NULL AND w.email IS NOT NULL
    UNION ALL
    SELECT ce.crd, ce.name AS person, ce.title AS role, ce.email, NULL AS person_phone,
           'inferred: ' || ce.pattern AS source, ce.status, 1 AS trust
    FROM contact_email ce
    UNION ALL
    SELECT f.crd, NULL AS person, 'firm inbox' AS role, f.value AS email,
           NULL AS person_phone, 'filed at firm' AS source,
           'domain_accepts_mail' AS status, 2 AS trust
    FROM firm_contact_info f WHERE f.kind='email'
)
SELECT p.crd, fc.legal_name, fc.state, fc.phone AS firm_phone,
       p.person, MAX(p.role) AS role, p.email, p.source, p.status,
       MAX(p.trust) AS trust, MAX(p.person_phone) AS person_phone,
       (ta.crd IS NOT NULL) AS is_tier_a,
       (tc.crd IS NOT NULL) AS is_tier_c,
       (ov.crd IS NOT NULL) AS is_intersection
FROM people p
JOIN firm_current fc ON fc.crd=p.crd
LEFT JOIN tier_a_rank ta ON ta.crd=p.crd AND ta.in_working_list=1
LEFT JOIN (SELECT crd FROM tier_c_score WHERE rank<=100) tc ON tc.crd=p.crd
LEFT JOIN firm_overlay ov ON ov.crd=p.crd AND ov.phh_13f=1
WHERE fc.is_era=0 AND fc.raum>=25e6 AND fc.raum<500e6 {extra}
GROUP BY p.crd, p.email, COALESCE(p.person,'')
ORDER BY fc.legal_name, p.crd, MAX(p.trust) DESC, p.person
"""

BANDS = {
    "": ("All AUM", None, None),
    "25-100": ("25 to 100M", 25e6, 100e6),
    "100-250": ("100 to 250M", 100e6, 250e6),
    "250-500": ("250 to 500M", 250e6, 500e6),
}

# preset -> (label, scope SQL producing a crd column, has a score to show)
PRESETS = {
    "all":   ("All firms", None, False),
    "phh_a": ("PHH - Tier A", "SELECT crd FROM tier_a_rank WHERE in_working_list=1", True),
    "phh_x": ("PHH - Intersection",
              "SELECT o.crd FROM firm_overlay o JOIN tier_a_rank t ON t.crd=o.crd"
              " WHERE o.phh_13f=1", False),
    "acu":   ("AcuBooth - Tier C", "SELECT crd FROM tier_c_score", True),
    "comp":  ("Competitors and sponsors",
              "SELECT crd FROM re_segment WHERE segment IN ('sponsor','competitor')", False),
}

_STATES: dict = {"t": 0.0, "v": []}


def _states(c):
    import time as _t
    if _t.monotonic() - _STATES["t"] > 60:
        _STATES["v"] = [r["state"] for r in c.execute(
            "SELECT DISTINCT state FROM firm_current WHERE state IS NOT NULL"
            " AND is_era=0 ORDER BY state") if r["state"]]
        _STATES["t"] = _t.monotonic()
    return _STATES["v"]


def _scope_sql(preset: str, list_id: str) -> str | None:
    """The crd-restricting SQL for the active preset or list, or None for all."""
    if list_id:
        return f"SELECT crd FROM user_list_item WHERE list_id={int(list_id)}"
    p = PRESETS.get(preset)
    return p[1] if p else None


def _where(preset, list_id, q, st, band, seg, stat, owner, trig):
    where = ["f.is_era=0", "f.raum>=25e6", "f.raum<500e6"]
    args: list = []
    scope = _scope_sql(preset, list_id)
    if scope:
        where.append(f"f.crd IN ({scope})")
    if q:
        where.append("(f.legal_name LIKE ? OR f.business_name LIKE ? OR f.crd=?)")
        args += [f"%{q}%", f"%{q}%", q]
    if st:
        where.append("f.state=?"); args.append(st)
    if band in BANDS and BANDS[band][1] is not None:
        where.append("f.raum>=? AND f.raum<?"); args += [BANDS[band][1], BANDS[band][2]]
    if seg:
        where.append("r.segment=?"); args.append(seg)
    if stat:
        where.append("s.status=?"); args.append(stat)
    if owner:
        where.append("s.owner LIKE ?"); args.append(f"%{owner}%")
    if trig == "open":
        where.append("EXISTS (SELECT 1 FROM trigger_event t LEFT JOIN trigger_action a"
                     " ON a.trigger_id=t.id WHERE t.crd=f.crd AND t.suppressed=0"
                     " AND a.state IS NULL)")
    return " AND ".join(where), args


def _qs(preset, view, q, st, band, seg, stat, owner, trig, list_id) -> str:
    return (f"preset={esc(preset)}&view={esc(view)}&q={esc(q)}&st={esc(st)}"
            f"&band={esc(band)}&seg={esc(seg)}&stat={esc(stat)}&owner={esc(owner)}"
            f"&trig={esc(trig)}&list={esc(list_id)}")


@router.get("/firms", response_class=HTMLResponse)
def firms(preset: str = Query("all"), view: str = Query("firms"),
          q: str = Query(""), st: str = Query(""), band: str = Query(""),
          seg: str = Query(""), stat: str = Query(""), owner: str = Query(""),
          trig: str = Query(""), list_id: str = Query("", alias="list"),
          page: int = Query(1, ge=1), per: int = Query(50, ge=10, le=200)):
    c = conn()
    if preset not in PRESETS:
        preset = "all"
    has_score = PRESETS.get(preset, (None, None, False))[2]
    where, args = _where(preset, list_id, q, st, band, seg, stat, owner, trig)

    lists = c.execute("SELECT id, name FROM user_list ORDER BY name").fetchall()
    active_list_name = ""
    if list_id:
        r = c.execute("SELECT name FROM user_list WHERE id=?", (list_id,)).fetchone()
        active_list_name = r["name"] if r else ""

    if view == "contacts":
        body, total = _contacts_body(c, where, args, page, per)
    else:
        body, total = _firms_body(c, where, args, has_score, preset, page, per,
                                  _qs(preset, view, q, st, band, seg, stat, owner,
                                      trig, list_id))
    # Read the state list before closing: calling _states(conn()) from inside
    # the template opened a second connection on every page load and never
    # closed it.
    state_list = _states(c)
    c.close()

    def opt(v, cur, label):
        return f'<option value="{esc(v)}"{" selected" if v==cur else ""}>{esc(label)}</option>'

    qs = _qs(preset, view, q, st, band, seg, stat, owner, trig, list_id)
    pages = max(1, -(-total // per))
    prev = f'<a href="/firms?{qs}&page={page-1}">Previous</a>' if page > 1 else ""
    nxt = f'<a href="/firms?{qs}&page={page+1}">Next</a>' if page < pages else ""

    # preset selector, with any saved lists appended
    preset_opts = "".join(opt(k, "" if list_id else preset, v[0])
                          for k, v in PRESETS.items())
    list_opts = "".join(
        f'<option value="list:{l["id"]}"{" selected" if str(l["id"])==str(list_id) else ""}>'
        f'My list: {esc(l["name"])}</option>' for l in lists)

    def vtab(v, label):
        # One class, styled in the sheet. Two style attributes on one element
        # means the browser keeps the first and silently drops the second, which
        # is how these tabs lost their underline removal and padding.
        href = "/firms?" + _qs(preset, v, q, st, band, seg, stat, owner, trig, list_id)
        cls = "vtab on" if view == v else "vtab"
        return f'<a class="{cls}" href="{href}">{label}</a>'

    title = active_list_name and f"My list: {esc(active_list_name)}" or \
        PRESETS.get(preset, ("Firms",))[0]
    exp = "/firms/export.xlsx?" + qs if view == "contacts" else "/firms/export.csv?" + qs
    exp_label = "Export contacts (Excel)" if view == "contacts" else "Export firms (CSV)"

    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>Firms</title><style>{FIRMS_CSS}</style>
{nav("firms")}
<header><h1>Firms</h1>
<div class="sub">Find, filter, and export firms and their contacts. A preset
picks the base set (all, PHH, AcuBooth, competitors, or one of your lists); the
filters narrow it; the two tabs show firms or the mail-merge contact list.</div>
</header>
<div class="wrap">
<form class="filters" method="get">
<input type="hidden" name="view" value="{esc(view)}">
<label>List / preset<select name="__sel" onchange="applySel(this)">
{preset_opts}{list_opts}</select></label>
<input type="hidden" name="preset" value="{esc('' if list_id else preset)}">
<input type="hidden" name="list" value="{esc(list_id)}">
<label>Search<input type="text" name="q" value="{esc(q)}" placeholder="name or CRD"></label>
<label>State<select name="st">{opt("", st, "All states")}
{"".join(opt(s, st, s) for s in state_list)}</select></label>
<label>AUM band<select name="band">
{"".join(opt(k, band, v[0]) for k, v in BANDS.items())}</select></label>
<label>Real estate<select name="seg">{opt("", seg, "Any")}
{"".join(opt(s, seg, s) for s in ("prospect","competitor","sponsor","ambiguous","unraised"))}
</select></label>
<label>Status<select name="stat">{opt("", stat, "Any")}
{"".join(opt(x, stat, x) for x in ("new","working","meeting set","qualified","disqualified","customer"))}
</select></label>
<label>Triggers<select name="trig">{opt("", trig, "Any")}{opt("open", trig, "Has open trigger")}</select></label>
<button class="primary" type="submit">Apply</button>
<a href="/firms" style="align-self:center;font-size:13px">Reset</a>
</form>
<div class="toolbar">
<div>{vtab("firms","Firms")}{vtab("contacts","Contacts")}</div>
<div class="count"><b>{title}</b>, {total:,} {"people" if view=="contacts" else "firms"}
&nbsp;&middot;&nbsp; <a href="{exp}">{exp_label}</a></div>
</div>
{body}
<div class="pager">{prev} Page {page} of {pages} {nxt}</div>
</div>
<script>
function applySel(sel){{
  var v = sel.value, f = sel.form;
  if(v.indexOf('list:')===0){{ f.list.value=v.slice(5); f.preset.value=''; }}
  else {{ f.preset.value=v; f.list.value=''; }}
  f.submit();
}}
function addToList(sel){{
  if(!sel.value) return;
  var f = sel.form;
  if(sel.value==='__new'){{
    var name = prompt('Name for the new list:');
    if(!name){{ sel.value=''; return; }}
    f.new_name.value = name;
  }}
  f.submit();
}}
</script>""")


def _firms_body(c, where, args, has_score, preset, page, per, qs):
    score_join = ""
    score_sel = "NULL AS score"
    if preset == "phh_a":
        score_join = "LEFT JOIN tier_a_rank sc ON sc.crd=f.crd"
        score_sel = "sc.total_score AS score, sc.rank AS srank"
    elif preset == "acu":
        score_join = "LEFT JOIN tier_c_score sc ON sc.crd=f.crd"
        score_sel = "sc.total_score AS score, sc.rank AS srank"
    else:
        score_sel = "NULL AS score, NULL AS srank"

    base = f"""FROM firm_current f
        LEFT JOIN re_segment r ON r.crd=f.crd
        LEFT JOIN firm_status s ON s.crd=f.crd
        {score_join}
        WHERE {where}"""
    total = c.execute(f"SELECT COUNT(*) n {base}", args).fetchone()["n"]
    order = "sc.rank" if has_score else "f.raum DESC"
    rows = c.execute(f"""
        SELECT f.crd, f.legal_name, f.state, f.raum, f.hnw_aum, f.hnw_clients,
               f.phone, {score_sel},
               r.segment,
               s.status AS wstatus, s.owner AS wowner,
               (SELECT COUNT(*) FROM contact_email ce WHERE ce.crd=f.crd) AS n_email,
               (SELECT COUNT(*) FROM trigger_event t LEFT JOIN trigger_action a
                 ON a.trigger_id=t.id WHERE t.crd=f.crd AND t.suppressed=0
                 AND a.state IS NULL) AS trigs
        {base} ORDER BY {order} LIMIT ? OFFSET ?""",
        args + [per, (page-1)*per]).fetchall()

    lists = c.execute("SELECT id, name FROM user_list ORDER BY name").fetchall()
    listopts = "".join(f'<option value="{l["id"]}">{esc(l["name"])}</option>' for l in lists)

    body = []
    for r in rows:
        seg = (f'<span class="seg {esc(r["segment"])}">{esc(r["segment"])}</span>'
               if r["segment"] else "")
        score = ""
        if r["score"] is not None:
            score = f'<td class="pri">{r["score"]:.1f}<div class="meta">#{r["srank"]}</div></td>'
        elif has_score:
            score = '<td class="pri">-</td>'
        contact = []
        if r["n_email"]:
            contact.append(f'<span class="chip lead">{r["n_email"]} email</span>')
        if r["phone"]:
            contact.append('<span class="chip">phone</span>')
        # Built outside the f-string below: Python 3.11 forbids backslashes in
        # an f-string expression, so nested quotes have to be resolved here.
        contact_cell = " ".join(contact) or '<span class="meta">-</span>'
        owner_div = (f'<div class="meta">{esc(r["wowner"])}</div>'
                     if r["wowner"] else "")
        addform = (
            f'<form method="post" action="/firms/addtolist" style="display:inline">'
            f'<input type="hidden" name="crd" value="{esc(r["crd"])}">'
            f'<input type="hidden" name="back" value="/firms?{esc(qs)}">'
            f'<input type="hidden" name="new_name" value="">'
            f'<select name="list_id" onchange="addToList(this)" '
            f'class="listpick">'
            f'<option value="">+ list</option>{listopts}'
            f'<option value="__new">+ new list...</option></select></form>')
        body.append(
            f'<tr>{score}<td><a href="/firm/{esc(r["crd"])}">{esc(r["legal_name"] or "(unnamed)")}</a>'
            f'<div class="meta">CRD {esc(r["crd"])} &middot; {esc(r["state"] or "-")} &middot; '
            f'{money(r["raum"])} RAUM</div></td>'
            f'<td class="num">{(100*(r["hnw_aum"] or 0)/r["raum"]) if r["raum"] else 0:.0f}%</td>'
            f'<td class="num">{r["hnw_clients"] or 0}</td>'
            f'<td>{seg}</td>'
            f'<td>{contact_cell}</td>'
            f'<td>{esc(r["wstatus"] or "")}{owner_div}</td>'
            f'<td class="num">{r["trigs"] or ""}</td>'
            f'<td>{addform}</td></tr>')
    sc_h = '<th style="width:70px">Score</th>' if has_score else ""
    ncols = 9 if has_score else 8
    empty = (f'<tr><td colspan="{ncols}" style="padding:26px;color:var(--faint)">'
             f'No firms match these filters.</td></tr>')
    table = (f'<table><thead><tr>{sc_h}<th>Firm</th><th class="num">HNW%</th>'
             f'<th class="num">HNW cl</th><th>Real estate</th><th>Contacts</th>'
             f'<th>Status</th><th class="num">Trig</th><th>List</th></tr></thead>'
             f'<tbody>{"".join(body) or empty}</tbody></table>')
    return table, total


def _contacts_body(c, where, args, page, per):
    # Reuse the outreach worklist, scoped to the firms the filters selected.
    scope = f"AND fc.crd IN (SELECT f.crd FROM firm_current f " \
            f"LEFT JOIN re_segment r ON r.crd=f.crd " \
            f"LEFT JOIN firm_status s ON s.crd=f.crd WHERE {where})"
    sql = WORKLIST.format(extra=scope)
    rows = c.execute(sql, args).fetchall()
    total = len(rows)
    window = rows[(page-1)*per: page*per]
    body = []
    for r in window:
        chip = "lead" if r["status"] == "domain_accepts_mail" else (
            "dis" if r["status"] in ("no_mail_server", "bad_syntax") else "partial")
        src_chip = "lead" if r["trust"] == 3 else "partial"
        phone = r["person_phone"] or r["firm_phone"] or "-"
        # A firm inbox has no person. Say so rather than rendering an empty
        # bold cell that reads as missing data.
        who = (f'<b>{esc(r["person"])}</b>' if r["person"]
               else '<span style="color:var(--faint)">Shared inbox</span>')
        body.append(
            f'<tr><td><a href="/firm/{esc(r["crd"])}">{esc(r["legal_name"] or "")}</a>'
            f'<div class="meta">CRD {esc(r["crd"])} &middot; {esc(r["state"] or "-")}</div></td>'
            f'<td>{who}<div class="meta">{esc(r["role"] or "")}</div></td>'
            f'<td><a href="mailto:{esc(r["email"])}">{esc(r["email"])}</a>'
            f'<div class="meta"><span class="chip {src_chip}">{esc(r["source"])}</span> '
            f'<span class="chip {chip}">{esc(STATUS_LABEL.get(r["status"], r["status"]))}</span></div></td>'
            f'<td>{esc(phone)}</td></tr>')
    empty = ('<tr><td colspan="4" style="padding:26px;color:var(--faint)">'
             'No contacts for this set. Widen the filter, or let the website '
             'enrichment and email inference jobs finish.</td></tr>')
    table = (f'<table><thead><tr><th style="width:280px">Firm</th>'
             f'<th style="width:220px">Person</th><th>Email</th>'
             f'<th style="width:150px">Phone</th></tr></thead>'
             f'<tbody>{"".join(body) or empty}</tbody></table>')
    return table, total


@router.get("/firms/export.csv")
def export_csv(preset: str = Query("all"), q: str = Query(""), st: str = Query(""),
               band: str = Query(""), seg: str = Query(""), stat: str = Query(""),
               owner: str = Query(""), trig: str = Query(""),
               list_id: str = Query("", alias="list")):
    import csv
    import io
    c = conn()
    where, args = _where(preset, list_id, q, st, band, seg, stat, owner, trig)
    rows = c.execute(f"""
        SELECT f.crd, f.legal_name, f.business_name, f.website, f.phone,
               (SELECT MIN(value) FROM firm_contact_info fi WHERE fi.crd=f.crd AND fi.kind='email') AS filed_email,
               f.city, f.state, f.raum, f.hnw_clients, f.hnw_aum, f.iar_count,
               r.segment AS re_segment, p.primary_canonical AS custodian,
               s.status AS work_status, s.owner AS work_owner
        FROM firm_current f
        LEFT JOIN re_segment r ON r.crd=f.crd
        LEFT JOIN firm_custodian_profile p ON p.crd=f.crd
        LEFT JOIN firm_status s ON s.crd=f.crd
        WHERE {where} LIMIT 50000""", args).fetchall()
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
def export_xlsx(preset: str = Query("all"), q: str = Query(""), st: str = Query(""),
                band: str = Query(""), seg: str = Query(""), stat: str = Query(""),
                owner: str = Query(""), trig: str = Query(""),
               list_id: str = Query("", alias="list")):
    c = conn()
    where, args = _where(preset, list_id, q, st, band, seg, stat, owner, trig)
    scope = f"AND fc.crd IN (SELECT f.crd FROM firm_current f " \
            f"LEFT JOIN re_segment r ON r.crd=f.crd " \
            f"LEFT JOIN firm_status s ON s.crd=f.crd WHERE {where})"
    rows = c.execute(WORKLIST.format(extra=scope), args).fetchall()
    c.close()
    headers = ["Firm", "CRD", "State", "Person", "Role/title", "Email",
               "Email source", "Email status", "Phone", "Lists"]
    out = []
    for r in rows:
        tags = []
        if r["is_intersection"]:
            tags.append("intersection")
        if r["is_tier_a"]:
            tags.append("tier A")
        if r["is_tier_c"]:
            tags.append("tier C")
        out.append([r["legal_name"] or "", r["crd"], r["state"] or "",
                    r["person"] or "", r["role"] or "", r["email"] or "",
                    r["source"] or "", STATUS_LABEL.get(r["status"], r["status"]),
                    r["person_phone"] or r["firm_phone"] or "", ", ".join(tags)])
    data = xlsx.write_sheet(headers, out, sheet_name="Contacts")
    return Response(data, media_type=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        headers={"Content-Disposition": 'attachment; filename="contacts.xlsx"'})


# ---------------------------------------------------------------- playlists

@router.get("/lists", response_class=HTMLResponse)
def my_lists(response_class=HTMLResponse):
    c = conn()
    lists = c.execute("""
        SELECT u.id, u.name, u.created_at, u.created_by,
               (SELECT COUNT(*) FROM user_list_item i WHERE i.list_id=u.id) AS n
        FROM user_list u ORDER BY u.name""").fetchall()
    c.close()
    rows = []
    for l in lists:
        rows.append(
            f'<tr><td><a href="/firms?list={l["id"]}"><b>{esc(l["name"])}</b></a>'
            f'<div class="meta">created {esc((l["created_at"] or "")[:10])}'
            f'{" by " + esc(l["created_by"]) if l["created_by"] else ""}</div></td>'
            f'<td class="num">{l["n"]}</td>'
            f'<td><a href="/firms?list={l["id"]}">Open</a> &middot; '
            f'<a href="/firms/export.xlsx?list={l["id"]}">Contacts</a> &middot; '
            f'<a href="/firms/export.csv?list={l["id"]}">CSV</a></td>'
            f'<td><form method="post" action="/lists/delete" '
            f'onsubmit="return confirm(\'Delete this list? The firms are not affected.\')">'
            f'<input type="hidden" name="list_id" value="{l["id"]}">'
            f'<button type="submit" style="padding:3px 10px;font-size:11.5px">Delete</button>'
            f'</form></td></tr>')
    empty = ('<tr><td colspan="4" style="padding:26px;color:var(--faint)">'
             'No lists yet. Add firms to one from the Firms section, or create '
             'an empty list below.</td></tr>')
    table = (f'<table><thead><tr><th>List</th><th class="num">Firms</th>'
             f'<th>Open and export</th><th></th></tr></thead>'
             f'<tbody>{"".join(rows) or empty}</tbody></table>')

    # Saved filters: the other kind of saved thing, moved here out of the
    # sidebar so everything you saved lives in one section.
    c2 = conn()
    views = c2.execute("SELECT id,name,page,qs FROM saved_view ORDER BY name").fetchall()
    c2.close()
    vrows = "".join(
        f'<tr><td><a href="{"/firms" if v["page"]=="firms" else "/"}?{esc(v["qs"])}">'
        f'<b>{esc(v["name"])}</b></a>'
        f'<div class="meta">{"Firms filter" if v["page"]=="firms" else "Inbox filter"}</div></td>'
        f'<td><form method="post" action="/views/delete">'
        f'<input type="hidden" name="vid" value="{v["id"]}">'
        f'<button type="submit" style="padding:3px 10px;font-size:11.5px">Delete</button>'
        f'</form></td></tr>' for v in views)
    vtable = (f'<h2 style="font-size:13px;text-transform:uppercase;'
              f'letter-spacing:.08em;color:var(--faint);margin:28px 0 4px">'
              f'Saved filters</h2>'
              f'<p class="meta" style="margin:0 0 10px">Filter combinations you '
              f'saved from the Inbox or Firms. Open one to jump straight back to '
              f'that view.</p>'
              f'<table><tbody>{vrows}</tbody></table>') if views else ""

    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>Lists</title><style>{PAGE_CSS}</style>
{nav("lists")}
<header><h1>Lists</h1>
<div class="sub">Everything you saved. Lists are firm buckets you build by hand,
like playlists: open one as a filtered Firms view, or export its contacts.
Saved filters are shortcuts back to a filter combination.</div></header>
<div class="wrap">
<form class="filters" method="post" action="/lists/create">
<label>New list<input type="text" name="name" placeholder="e.g. Q3 push, Boston metro" required></label>
<button class="primary" type="submit">Create list</button>
</form>
{table}
{vtable}
</div>""")


@router.post("/lists/create")
def list_create(name: str = Form(...)):
    from datetime import datetime, timezone
    c = conn()
    if name.strip():
        c.execute("INSERT INTO user_list (name, created_at, created_by) VALUES (?,?,?)",
                  (name.strip()[:60],
                   datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   current_owner() or None))
        c.commit()
    c.close()
    return RedirectResponse("/lists", status_code=303)


@router.post("/lists/delete")
def list_delete(list_id: int = Form(...)):
    c = conn()
    c.execute("DELETE FROM user_list_item WHERE list_id=?", (list_id,))
    c.execute("DELETE FROM user_list WHERE id=?", (list_id,))
    c.commit()
    c.close()
    return RedirectResponse("/lists", status_code=303)


@router.post("/firms/removefromlist")
def remove_from_list(crd: str = Form(...), list_id: int = Form(...),
                     back: str = Form("/lists")):
    if not back.startswith("/"):
        back = "/lists"
    c = conn()
    c.execute("DELETE FROM user_list_item WHERE list_id=? AND crd=?", (list_id, crd))
    c.commit()
    c.close()
    return RedirectResponse(back, status_code=303)


@router.post("/firms/addtolist")
def add_to_list(crd: str = Form(...), list_id: str = Form(...),
                back: str = Form("/firms"), new_name: str = Form("")):
    if not back.startswith("/"):
        back = "/firms"
    c = conn()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if list_id == "__new":
        name = (new_name.strip() or "My list")[:60]
        cur = c.execute("INSERT INTO user_list (name, created_at, created_by)"
                        " VALUES (?,?,?) RETURNING id",
                        (name, now, current_owner() or None))
        lid = cur.lastrowid
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
