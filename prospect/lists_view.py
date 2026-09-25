"""Product lists: one ranked, explainable list per product.

Each list is the output of config/products.yml applied to every firm. The page
has three views of the same product:

  Ranked       the firms that passed the gates, best first, each with its
               tier, score and the two reasons that earned the most points
  Disqualified firms that passed the gates and were then removed, with why
  How it works the product's scoring table itself, rendered from the config,
               so the rules on screen are always the rules in force

Filters compose, the view can be saved, and both exports carry exactly what
is on screen.
"""

from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from . import products, ui, xlsx
from .webapp import (escn, TYPE_LABEL, conn, esc, money, page, qs_join, score_cell,
                     signal_cutoff, tier_chip)

router = APIRouter()

LIST_CSS = """
.rank{color:var(--faint);font-variant-numeric:tabular-nums;font-size:12.5px;width:34px}
.rules h3{margin-top:22px}
.rules table td{vertical-align:top}
.lv{display:flex;gap:10px;font-size:12.5px;color:var(--soft);padding:1px 0}
.lv b{flex:none;width:30px;text-align:right;color:var(--ink);font-variant-numeric:tabular-nums}
.wt{font:600 17px "Segoe UI",sans-serif;font-variant-numeric:tabular-nums}
.hand{font-size:11px;color:var(--amber);margin-top:4px}
.gate{display:flex;gap:10px;padding:5px 0;font-size:13.5px}
.gate .k{flex:none;width:92px;color:var(--faint);font-size:11px;text-transform:uppercase;
letter-spacing:.08em;padding-top:2px}
.listpick{min-width:0;padding:3px 7px;font-size:12px}
"""

SORTS = {"score": "Best score", "signal": "Newest signal", "aum": "Largest",
         "name": "Name"}


def _key_or_404(key: str):
    if key not in products.product_keys():
        return None
    return products.product(key)


def _where(key, tier, q, st, owner, stat, sig, reach, status="scored"):
    where = ["p.product=?", "p.status=?"]
    args: list = [key, status]
    if tier:
        where.append("p.tier=?")
        args.append(tier)
    if q:
        where.append("(f.legal_name ILIKE ? OR f.business_name ILIKE ? OR f.crd=?"
                     " OR f.city ILIKE ?)")
        args += [f"%{q}%", f"%{q}%", q, f"%{q}%"]
    if st:
        where.append("f.state=?")
        args.append(st)
    if owner == "none":
        where.append("(s.owner IS NULL OR s.owner='')")
    elif owner:
        where.append("s.owner ILIKE ?")
        args.append(f"%{owner}%")
    if stat == "open":
        where.append("COALESCE(s.status,'') NOT IN ('disqualified','customer')")
    elif stat:
        where.append("s.status=?")
        args.append(stat)
    if sig:
        where.append("EXISTS (SELECT 1 FROM trigger_event t LEFT JOIN trigger_action a"
                     " ON a.trigger_id=t.id WHERE t.crd=p.crd AND t.suppressed=0"
                     " AND a.state IS NULL AND t.detected_date >= ?)")
        args.append(signal_cutoff())
    if reach == "email":
        where.append("(EXISTS (SELECT 1 FROM firm_contact_info i WHERE i.crd=p.crd"
                     " AND i.kind='email') OR EXISTS (SELECT 1 FROM web_contact w"
                     " WHERE w.crd=p.crd AND w.email IS NOT NULL))")
    return " AND ".join(where), args


BASE = """FROM product_score p
    JOIN firm_current f ON f.crd=p.crd
    LEFT JOIN firm_status s ON s.crd=p.crd
    WHERE {where}"""


def _states(c, key):
    return [r["state"] for r in c.execute(
        "SELECT DISTINCT f.state FROM product_score p JOIN firm_current f ON f.crd=p.crd"
        " WHERE p.product=? AND p.status='scored' AND f.state IS NOT NULL"
        " ORDER BY f.state", (key,))]


@router.get("/lists/{key}", response_class=HTMLResponse)
def product_list(key: str, view: str = Query("ranked"), tier: str = Query(""),
                 q: str = Query(""), st: str = Query(""), owner: str = Query(""),
                 stat: str = Query(""), sig: str = Query(""), reach: str = Query(""),
                 sort: str = Query("score"), page_n: int = Query(1, ge=1, alias="page"),
                 per: int = Query(50, ge=10, le=200)):
    p = _key_or_404(key)
    if p is None:
        return RedirectResponse("/", status_code=303)
    c = conn()
    counts = {r["tier"]: r["n"] for r in c.execute(
        "SELECT tier, COUNT(*) n FROM product_score WHERE product=? AND status='scored'"
        " GROUP BY tier", (key,))}
    total_scored = sum(counts.values())
    dq_n = c.execute("SELECT COUNT(*) n FROM product_score WHERE product=?"
                     " AND status='disqualified'", (key,)).fetchone()["n"]
    fresh_n = c.execute("""SELECT COUNT(DISTINCT p.crd) n FROM product_score p
        JOIN trigger_event t ON t.crd=p.crd LEFT JOIN trigger_action a ON a.trigger_id=t.id
        WHERE p.product=? AND p.status='scored' AND t.suppressed=0 AND a.state IS NULL
          AND t.detected_date >= ?""", (key, signal_cutoff())).fetchone()["n"]

    qs = qs_join(tier=tier, q=q, st=st, owner=owner, stat=stat, sig=sig, reach=reach,
                 sort=sort if sort != "score" else "")

    def tab(v, label, n=None):
        cls = "on" if view == v else ""
        cnt = f' <span class="muted">{n:,}</span>' if n is not None else ""
        return f'<a class="{cls}" href="/lists/{key}?view={v}">{label}{cnt}</a>'

    strip = ['<div class="strip">']
    for thr, label, action in p["tiers"]:
        label = str(label)
        if label == "-":
            continue
        n = counts.get(label, 0)
        on = " on" if tier == label and view == "ranked" else ""
        strip.append(f'<a class="{on.strip()}" href="/lists/{key}?tier={label}" '
                     f'title="{esc(action)}"><div class="n">{n:,}</div>'
                     f'<div class="l">Tier {label}: {esc(action)}</div></a>')
    strip.append(f'<a href="/lists/{key}"><div class="n">{total_scored:,}</div>'
                 f'<div class="l">Scored in total</div></a>')
    strip.append(f'<a class="{"on" if sig else ""}" href="/lists/{key}?sig=1">'
                 f'<div class="n">{fresh_n:,}</div><div class="l">With a new signal</div></a>')
    strip.append("</div>")

    if view == "rules":
        body_main = rules_html(key)
    elif view == "disqualified":
        body_main = _disq(c, key, q, page_n, per)
    else:
        body_main = _ranked(c, key, p, tier, q, st, owner, stat, sig, reach, sort,
                            page_n, per, qs)
    c.close()

    note = (f'<div class="note plain">{esc(p["note"].strip())}</div>'
            if p.get("note") and view != "rules" else "")
    exp = ""
    if view == "ranked":
        exp = (f'<a class="btn" href="/lists/{key}/export.csv?{qs}">Export list</a>'
               f'<a class="btn" href="/lists/{key}/export.xlsx?{qs}">Export contacts</a>')
    body = f"""<div class="pg">
<div class="crumb"><a href="/">Home</a> / Product lists</div>
<div class="head"><div><h1>{esc(p["name"])}</h1>
<div class="lede">{esc(p["audience"])}. <span class="muted">&ldquo;{esc(p.get("pitch", ""))}&rdquo;</span></div></div>
<div class="acts">{exp}</div></div>
{"".join(strip)}
<div style="margin:18px 0 4px" class="seg">{tab("ranked", "Ranked")}{tab("disqualified", "Disqualified", dq_n)}{tab("rules", "How it is scored")}</div>
{note}
{body_main}
</div>"""
    return page(p["name"], f"list:{key}", body, LIST_CSS, js=ADD_JS)


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


def _ranked(c, key, p, tier, q, st, owner, stat, sig, reach, sort, page_n, per, qs):
    where, args = _where(key, tier, q, st, owner, stat, sig, reach)
    base = BASE.format(where=where)
    total = c.execute(f"SELECT COUNT(*) n {base}", args).fetchone()["n"]
    order = {"signal": "fresh DESC NULLS LAST, p.rank", "aum": "f.raum DESC NULLS LAST",
             "name": "f.legal_name"}.get(sort, "p.rank")
    rows = c.execute(f"""
        SELECT p.crd, p.rank, p.score, p.tier, p.detail_json, f.legal_name, f.city,
               f.state, f.raum, s.owner, s.status,
               (SELECT MAX(t.detected_date) FROM trigger_event t
                 LEFT JOIN trigger_action a ON a.trigger_id=t.id
                 WHERE t.crd=p.crd AND t.suppressed=0 AND a.state IS NULL
                   AND t.detected_date >= ?) AS fresh
        {base} ORDER BY {order} LIMIT ? OFFSET ?""",
                     [signal_cutoff()] + args + [per, (page_n - 1) * per]).fetchall()
    crds = [r["crd"] for r in rows]
    flags = ui.contact_flags(c, crds)
    trig = {}
    if crds:
        ph = ",".join("?" * len(crds))
        for r in c.execute(f"""SELECT DISTINCT ON (crd) crd, trigger_type, detected_date
                FROM trigger_event WHERE crd IN ({ph}) AND suppressed=0
                  AND detected_date >= ? ORDER BY crd, detected_date DESC""",
                           tuple(crds) + (signal_cutoff(),)):
            trig[r["crd"]] = r
    lists = c.execute("SELECT id, name FROM user_list ORDER BY name").fetchall()
    listopts = "".join(f'<option value="{l["id"]}">{esc(l["name"])}</option>' for l in lists)
    back = f"/lists/{key}?{qs}&page={page_n}"

    body = []
    for r in rows:
        t = trig.get(r["crd"])
        tcell = (f'<span class="chip lead">{esc(TYPE_LABEL.get(t["trigger_type"], t["trigger_type"]))}</span>'
                 f'<div class="meta">{esc(t["detected_date"])}</div>' if t else "")
        who = ""
        if r["owner"] or r["status"]:
            who = (f'<span class="chip">{esc(r["status"] or "claimed")}</span>'
                   f'<div class="meta">{esc(r["owner"] or "")}</div>')
        add = (f'<form method="post" action="/firms/addtolist">'
               f'<input type="hidden" name="crd" value="{esc(r["crd"])}">'
               f'<input type="hidden" name="back" value="{esc(back)}">'
               f'<input type="hidden" name="new_name" value="">'
               f'<select name="list_id" class="listpick" onchange="addToList(this)" '
               f'title="Add to one of your saved lists">'
               f'<option value="">+ list</option>{listopts}'
               f'<option value="__new">New list...</option></select></form>')
        body.append(
            f'<tr class="go" data-href="/firm/{esc(r["crd"])}?p={key}">'
            f'<td class="rank">{r["rank"] or ""}</td>'
            f'<td><div class="firm"><a href="/firm/{esc(r["crd"])}?p={key}">'
            f'{escn(r["legal_name"] or "(unnamed)")}</a></div>'
            f'<div class="meta">{ui.firm_meta(r)}</div></td>'
            f'<td>{tier_chip(r["tier"])}</td>'
            f'<td>{score_cell(r["score"], r["tier"])}</td>'
            f'<td class="why">{ui.why_line(r["detail_json"])}</td>'
            f'<td>{tcell}</td><td>{ui.contact_cell(flags[r["crd"]])}</td>'
            f'<td>{who}</td><td>{add}</td></tr>')
    empty = ('<tr><td colspan="9" class="empty">No firms match. Clear a filter, or '
             'open <a href="?view=rules">How it is scored</a> to see what this list '
             'requires.</td></tr>')
    states = _states(c, key)
    tier_opts = "".join(ui.opt(str(t[1]), tier, f"Tier {t[1]}")
                        for t in p["tiers"] if str(t[1]) != "-")
    stat_opts = "".join(ui.opt(s, stat, s.capitalize()) for s in ui.STATUS_OPTIONS)
    pages = max(1, -(-total // per))
    prev = f'<a href="/lists/{key}?{qs}&page={page_n-1}">Previous</a>' if page_n > 1 else ""
    nxt = f'<a href="/lists/{key}?{qs}&page={page_n+1}">Next</a>' if page_n < pages else ""
    return f"""
<form class="filters" method="get" action="/lists/{key}">
<label>Search<input type="search" name="q" value="{esc(q)}" placeholder="Firm, city or CRD"></label>
<label>Tier<select name="tier">{ui.opt("", tier, "All tiers")}{tier_opts}
<option value="-"{" selected" if tier == "-" else ""}>Below the tiers</option></select></label>
<label>State<select name="st">{ui.opt("", st, "All states")}{"".join(ui.opt(s, st, s) for s in states)}</select></label>
<label>Owner<select name="owner">{ui.opt("", owner, "Anyone")}{ui.opt("none", owner, "Unclaimed")}</select></label>
<label>Status<select name="stat">{ui.opt("", stat, "Any")}{ui.opt("open", stat, "Still open")}{stat_opts}</select></label>
<label>Signal<select name="sig">{ui.opt("", sig, "Any")}{ui.opt("1", sig, "New in 60 days")}</select></label>
<label>Reach<select name="reach">{ui.opt("", reach, "Any")}{ui.opt("email", reach, "Has a real email")}</select></label>
<label>Sort<select name="sort">{"".join(ui.opt(k, sort, v) for k, v in SORTS.items())}</select></label>
<button class="primary" type="submit">Apply</button>
<a class="btn ghost" href="/lists/{key}">Clear</a>
</form>
<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin:10px 0 0;flex-wrap:wrap">
<div class="small soft"><b style="color:var(--ink)">{total:,}</b> firms</div>
<form method="post" action="/views/save" class="acts">
<input type="hidden" name="page" value="list:{key}"><input type="hidden" name="qs" value="{esc(qs)}">
<input type="text" name="name" placeholder="Name this view to save it" style="min-width:210px">
<button type="submit" class="sm">Save view</button></form>
</div>
<table><thead><tr><th>#</th><th>Firm</th><th>Tier</th><th>Score</th><th>Why it scores</th>
<th>New signal</th><th>Reach</th><th>Owner</th><th></th></tr></thead>
<tbody>{"".join(body) or empty}</tbody></table>
<div class="pager">{prev} Page {page_n} of {pages} {nxt}</div>"""


def _disq(c, key, q, page_n, per):
    where = "p.product=? AND p.status='disqualified'"
    args: list = [key]
    if q:
        where += " AND (f.legal_name ILIKE ? OR f.crd=?)"
        args += [f"%{q}%", q]
    rows = c.execute(f"""SELECT p.crd, p.reason, f.legal_name, f.city, f.state, f.raum
        FROM product_score p JOIN firm_current f ON f.crd=p.crd WHERE {where}
        ORDER BY f.raum DESC NULLS LAST LIMIT ? OFFSET ?""",
                     args + [per, (page_n - 1) * per]).fetchall()
    body = "".join(
        f'<tr class="go" data-href="/firm/{esc(r["crd"])}?p={key}"><td><div class="firm">'
        f'<a href="/firm/{esc(r["crd"])}?p={key}">{escn(r["legal_name"])}</a></div>'
        f'<div class="meta">{ui.firm_meta(r)}</div></td>'
        f'<td class="why">{esc(r["reason"])}</td></tr>' for r in rows)
    return (f'<p class="lede" style="margin:14px 0 4px">Firms that passed the gates '
            f'and were then removed. They stay visible so nobody calls them by '
            f'mistake, and so a wrong call can be spotted.</p>'
            f'<table><thead><tr><th>Firm</th><th>Why it was removed</th></tr></thead>'
            f'<tbody>{body or "<tr><td colspan=2 class=empty>None.</td></tr>"}</tbody></table>')


def rules_html(key: str) -> str:
    """The product's scoring table, straight from config/products.yml."""
    p = products.product(key)
    gates = "".join(f'<div class="gate"><span class="k">Must pass</span>{esc(g["label"])}</div>'
                    for g in p.get("gates", []))
    gates += "".join(f'<div class="gate"><span class="k bad">Removed if</span>{esc(g["label"])}</div>'
                     for g in p.get("disqualifiers", []))
    rows = []
    for cr in p["criteria"]:
        lv = "".join(f'<div class="lv"><b>{int(pts)}</b>{esc(label)}</div>'
                     for pts, label in cr.get("levels", []))
        hand = ('<div class="hand">An SDR can set this on the firm page</div>'
                if cr.get("manual") else "")
        rows.append(f'<tr><td style="width:36%"><b>{esc(cr["label"])}</b>{hand}</td>'
                    f'<td class="wt" style="width:90px">{cr["weight"]}%</td><td>{lv}</td></tr>')
    pens = "".join(f'<div class="gate"><span class="k bad">Minus {pe["points"]}</span>'
                   f'{esc(pe["label"])}</div>' for pe in p.get("penalties", []))
    sigs = "".join(f'<div class="gate"><span class="k">Shown</span>{esc(s["label"])}</div>'
                   for s in p.get("signals", []))
    rules = "".join(f'<div class="gate"><span class="k warnc">Rule</span>{esc(r)}</div>'
                    for r in p.get("rules", []))
    tiers = "".join(f'<div class="gate"><span class="k">{thr}+</span>'
                    f'{tier_chip(str(lb))}&nbsp; {esc(act)}</div>'
                    for thr, lb, act in p["tiers"])
    note = f'<p class="lede">{esc(p["note"].strip())}</p>' if p.get("note") else ""
    return f"""<div class="rules">
<p class="lede" style="margin-top:16px">Every criterion earns 0 to 100 points, then counts
for its weight. A firm&rsquo;s score is the sum, out of 100. The same table drives the
score, the reasons on every row and the breakdown on every firm page.</p>{note}
<h3>Gates</h3>{gates or '<p class="muted">None</p>'}
<h3>Scored criteria</h3>
<table><thead><tr><th>Criterion</th><th>Weight</th><th>Points for each level</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
{f"<h3>Penalties</h3>{pens}" if pens else ""}
{f"<h3>Shown, not scored</h3>{sigs}" if sigs else ""}
{f"<h3>Rules</h3>{rules}" if rules else ""}
<h3>What the score means</h3>{tiers}</div>"""


@router.get("/lists/{key}/export.csv")
def export_list(key: str, tier: str = "", q: str = "", st: str = "", owner: str = "",
                stat: str = "", sig: str = "", reach: str = ""):
    p = _key_or_404(key)
    if p is None:
        return RedirectResponse("/", status_code=303)
    c = conn()
    where, args = _where(key, tier, q, st, owner, stat, sig, reach)
    rows = c.execute(f"""
        SELECT p.rank, p.crd, f.legal_name, f.city, f.state, f.raum, f.website, f.phone,
               p.score, p.tier, p.detail_json, s.owner, s.status,
               (SELECT MIN(value) FROM firm_contact_info i WHERE i.crd=p.crd
                 AND i.kind='email') AS filed_email
        {BASE.format(where=where)} ORDER BY p.rank LIMIT 50000""", args).fetchall()
    c.close()
    crits = [cr["key"] for cr in p["criteria"]]
    labels = {cr["key"]: cr["label"] for cr in p["criteria"]}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["rank", "crd", "firm", "city", "state", "aum", "website", "phone",
                "filed_email", "score", "tier", "pitch"] +
               [f"{labels[k]} (points)" for k in crits] + ["owner", "status"])
    for r in rows:
        d = json.loads(r["detail_json"] or "{}")
        pts = {cp["key"]: cp["points"] for cp in d.get("components", [])}
        w.writerow([r["rank"], r["crd"], r["legal_name"], r["city"], r["state"], r["raum"],
                    r["website"], r["phone"], r["filed_email"], r["score"], r["tier"],
                    d.get("pitch", "")] + [pts.get(k) for k in crits] +
                   [r["owner"], r["status"]])
    name = f"{key}_list.csv"
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/lists/{key}/export.xlsx")
def export_contacts(key: str, tier: str = "", q: str = "", st: str = "", owner: str = "",
                    stat: str = "", sig: str = "", reach: str = ""):
    """The mail-merge sheet for exactly the firms on screen."""
    from .firms_view import STATUS_LABEL, WORKLIST
    p = _key_or_404(key)
    if p is None:
        return RedirectResponse("/", status_code=303)
    c = conn()
    where, args = _where(key, tier, q, st, owner, stat, sig, reach)
    scope = (f"AND fc.crd IN (SELECT p.crd FROM product_score p JOIN firm_current f"
             f" ON f.crd=p.crd LEFT JOIN firm_status s ON s.crd=p.crd WHERE {where})")
    rows = c.execute(WORKLIST.format(extra=scope), args).fetchall()
    ranks = {r["crd"]: (r["rank"], r["tier"], r["score"]) for r in c.execute(
        "SELECT crd, rank, tier, score FROM product_score WHERE product=?"
        " AND status='scored'", (key,))}
    c.close()
    headers = ["Rank", "Tier", "Score", "Firm", "CRD", "State", "Person", "Role",
               "Email", "Email source", "Email status", "Phone"]
    out = []
    for r in rows:
        rk, tr, scr = ranks.get(r["crd"], (None, None, None))
        out.append([rk or "", tr or "", scr if scr is not None else "",
                    r["legal_name"] or "", r["crd"], r["state"] or "",
                    r["person"] or "", r["role"] or "", r["email"] or "",
                    r["source"] or "", STATUS_LABEL.get(r["status"], r["status"]),
                    r["person_phone"] or r["firm_phone"] or ""])
    out.sort(key=lambda x: (x[0] == "", x[0] if x[0] != "" else 0))
    data = xlsx.write_sheet(headers, out, sheet_name="Contacts")
    return Response(data, media_type=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        headers={"Content-Disposition": f'attachment; filename="{key}_contacts.xlsx"'})
