"""Product lists: one ranked, explainable list per product.

Each list is the scoring rules for one product applied to every firm. Three
views of the same product:

  Ranked        the firms that passed the gates, best score first. Every score
                shows how much of it rests on data Bellwether holds, and what
                is missing, so a well-founded 70 and a 70 built on guesses
                never look alike.
  Disqualified  firms that passed the gates and were then removed, with why.
  Scoring       the rules in force, rendered from the live config. Admins and
                the product's owner can change them here: weights, levels,
                thresholds, gates, switching a factor off, and adding a new
                factor from any data field Bellwether holds. Every save is
                validated, kept in history, and rescored within seconds.

Filters compose, the view can be saved, and both exports carry exactly what
is on screen.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import subprocess
import sys

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from . import config, procs, products, ui, users, xlsx
from .webapp import (TYPE_LABEL, conn, current_account, current_owner, esc, escn,
                     missing_chip, money, page, qs_join, score_cell, signal_cutoff)

router = APIRouter()

SORTS = {"score": "Best score", "potential": "Best potential", "signal": "Newest signal",
         "aum": "Largest", "hires": "Most hiring", "name": "Name"}


def _key_or_none(key: str):
    if key not in products.product_keys():
        return None
    return products.product(key)


def _where(key, q, st, owner, stat, sig, reach, cov, status="scored"):
    where = ["p.product=?", "p.status=?"]
    args: list = [key, status]
    if q:
        where.append("(f.legal_name ILIKE ? OR f.business_name ILIKE ? OR f.crd=?"
                     " OR f.city ILIKE ?)")
        args += [f"%{q}%", f"%{q}%", q, f"%{q}%"]
    if st:
        where.append("f.state=?")
        args.append(st)
    if owner == "none":
        where.append("(s.owner IS NULL OR s.owner='')")
    elif owner == "me":
        where.append("s.owner=?")
        args.append(current_owner())
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
        where.append("EXISTS (SELECT 1 FROM contact_point x WHERE x.crd=p.crd AND x.kind='email'"
                     " AND x.person_key != '' AND x.is_role=0 AND x.source != 'pattern'"
                     " AND x.verify_status NOT IN ('invalid','no_mail_server'))")
    elif reach == "verified":
        where.append("EXISTS (SELECT 1 FROM contact_point x WHERE x.crd=p.crd AND x.kind='email'"
                     " AND x.verify_status='valid')")
    elif reach == "phone":
        where.append("EXISTS (SELECT 1 FROM contact_point x WHERE x.crd=p.crd AND x.kind='phone'"
                     " AND x.person_key != '')")
    if cov == "full":
        where.append("p.coverage >= 90")
    elif cov == "most":
        where.append("p.coverage >= 70")
    elif cov == "gaps":
        where.append("p.coverage < 70")
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


def can_edit(key: str) -> bool:
    return users.can_edit_product(current_account(), products.product(key)["family"])


@router.get("/lists/{key}", response_class=HTMLResponse)
def product_list(key: str, view: str = Query("ranked"), q: str = Query(""),
                 st: str = Query(""), owner: str = Query(""), stat: str = Query(""),
                 sig: str = Query(""), reach: str = Query(""), cov: str = Query(""),
                 sort: str = Query("score"), page_n: int = Query(1, ge=1, alias="page"),
                 per: int = Query(50, ge=10, le=200), msg: str = Query(""),
                 err: str = Query("")):
    p = _key_or_none(key)
    if p is None:
        return RedirectResponse("/", status_code=303)
    if view == "rules":
        view = "scoring"
    c = conn()
    stats = c.execute("""SELECT COUNT(*) n, AVG(coverage) cov,
        COUNT(*) FILTER (WHERE score >= 60) hi,
        COUNT(*) FILTER (WHERE coverage >= 90) full_n FROM product_score
        WHERE product=? AND status='scored'""", (key,)).fetchone()
    dq_n = c.execute("SELECT COUNT(*) n FROM product_score WHERE product=?"
                     " AND status='disqualified'", (key,)).fetchone()["n"]
    fresh_n = c.execute("""SELECT COUNT(DISTINCT p.crd) n FROM product_score p
        JOIN trigger_event t ON t.crd=p.crd LEFT JOIN trigger_action a ON a.trigger_id=t.id
        WHERE p.product=? AND p.status='scored' AND t.suppressed=0 AND a.state IS NULL
          AND t.detected_date >= ?""", (key, signal_cutoff())).fetchone()["n"]

    qs = qs_join(q=q, st=st, owner=owner, stat=stat, sig=sig, reach=reach, cov=cov,
                 sort=sort if sort != "score" else "")

    def tab(v, label, n=None):
        cls = "on" if view == v else ""
        cnt = f' <span class="muted">{n:,}</span>' if n is not None else ""
        return f'<a class="{cls}" href="/lists/{key}?view={v}">{label}{cnt}</a>'

    strip = (f'<div class="strip">'
             f'<a href="/lists/{key}"><div class="n">{stats["n"] or 0:,}</div><div class="l">Firms ranked</div></a>'
             f'<a href="/lists/{key}?cov=full" class="{"on" if cov == "full" else ""}"><div class="n">{stats["full_n"] or 0:,}</div>'
             f'<div class="l">Scored on 90%+ known data</div></a>'
             f'<div class="k"><div class="n">{stats["cov"] or 0:.0f}%</div><div class="l">Average coverage</div></div>'
             f'<div class="k"><div class="n">{stats["hi"] or 0:,}</div><div class="l">Score 60 or more</div></div>'
             f'<a class="{"on" if sig else ""}" href="/lists/{key}?sig=1"><div class="n">{fresh_n:,}</div>'
             f'<div class="l">With a new signal</div></a></div>')

    if view == "scoring":
        body_main = scoring_html(key, msg, err)
    elif view == "disqualified":
        body_main = _disq(c, key, q, page_n, per)
    else:
        body_main = _ranked(c, key, p, q, st, owner, stat, sig, reach, cov, sort, page_n,
                            per, qs)
    c.close()

    note = (f'<details class="source-help"><summary>About this list</summary><p>{esc(p["note"].strip())}</p></details>'
            if p.get("note") and view == "ranked" else "")
    exp = ""
    if view == "ranked":
        exp = (f'<a class="btn" href="/lists/{key}/export.csv?{qs}" data-noprefetch>Export list</a>'
               f'<a class="btn" href="/lists/{key}/export.xlsx?{qs}" data-noprefetch>Export contacts</a>')
    scoring_tab = "Scoring" + (" (edit)" if can_edit(key) else "")
    body = f"""<div class="pg wide">
<div class="crumb"><a href="/">Home</a> / Product lists</div>
<div class="head"><div><h1>{esc(p["name"])}</h1>
<div class="lede">{esc(p["audience"])}</div></div>
<div class="acts">{exp}</div></div>
{strip}
<div style="margin:18px 0 4px" class="seg">{tab("ranked", "Ranked")}{tab("disqualified", "Disqualified", dq_n)}{tab("scoring", scoring_tab)}</div>
{note}
{body_main}
</div>"""
    return page(p["name"], f"list:{key}", body, js=ADD_JS)


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
function wsum(){
  var t=0; document.querySelectorAll('input[data-w]').forEach(function(i){t+=parseFloat(i.value)||0;});
  var el=document.getElementById('wsum'); if(!el) return;
  el.textContent=(Math.round(t*10)/10)+'%';
  el.className='wsum'+(Math.abs(t-100)>0.01?' badsum':'');
}
function spread(){
  var ins=[].slice.call(document.querySelectorAll('input[data-w]')).filter(function(i){return (parseFloat(i.value)||0)>0;});
  var t=ins.reduce(function(a,i){return a+(parseFloat(i.value)||0);},0); if(!t) return;
  var acc=0; ins.forEach(function(i,k){var v=k<ins.length-1?Math.round((parseFloat(i.value)||0)/t*1000)/10:Math.round((100-acc)*10)/10; acc+=v; i.value=v;});
  wsum();
}
document.addEventListener('input',function(e){ if(e.target.matches('input[data-w]')) wsum(); });
document.addEventListener('DOMContentLoaded',wsum);
"""


def _ranked(c, key, p, q, st, owner, stat, sig, reach, cov, sort, page_n, per, qs):
    where, args = _where(key, q, st, owner, stat, sig, reach, cov)
    base = BASE.format(where=where)
    total = c.execute(f"SELECT COUNT(*) n {base}", args).fetchone()["n"]
    have_people = bool(c.execute("SELECT to_regclass('firm_people_stats') t").fetchone()["t"])
    order = {"signal": "fresh DESC NULLS LAST, p.rank",
             "potential": "p.potential DESC NULLS LAST, p.rank",
             "aum": "f.raum DESC NULLS LAST", "name": "f.legal_name",
             "hires": ("(SELECT hires_12m FROM firm_people_stats ps WHERE ps.crd=p.crd)"
                       " DESC NULLS LAST, p.rank") if have_people else "p.rank",
             }.get(sort, "p.rank")
    rows = c.execute(f"""
        SELECT p.crd, p.rank, p.score, p.coverage, p.potential, p.missing, p.detail_json,
               f.legal_name, f.city, f.state, f.raum, s.owner, s.status,
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
               f'<select name="list_id" style="min-width:0;padding:3px 7px;font-size:12px" '
               f'onchange="addToList(this)" title="Add to one of your saved lists">'
               f'<option value="">+ list</option>{listopts}'
               f'<option value="__new">New list...</option></select></form>')
        body.append(
            f'<tr class="go" data-href="/firm/{esc(r["crd"])}?p={key}">'
            f'<td class="rank">{r["rank"] or ""}</td>'
            f'<td><div class="firm"><a href="/firm/{esc(r["crd"])}?p={key}">'
            f'{escn(r["legal_name"] or "(unnamed)")}</a></div>'
            f'<div class="meta">{ui.firm_meta(r)}</div></td>'
            f'<td>{score_cell(r["score"], r["coverage"], r["potential"])}'
            f'<div style="margin-top:5px">{missing_chip(r["missing"], 2)}</div></td>'
            f'<td class="why">{ui.why_line(r["detail_json"])}</td>'
            f'<td>{tcell}</td><td>{ui.contact_cell(flags[r["crd"]])}</td>'
            f'<td>{who}</td><td>{add}</td></tr>')
    empty = ('<tr><td colspan="8" class="empty">No firms match. Clear a filter, or open '
             f'<a href="/lists/{key}?view=scoring">Scoring</a> to see what this list requires.</td></tr>')
    states = _states(c, key)
    stat_opts = "".join(ui.opt(s, stat, s.capitalize()) for s in ui.STATUS_OPTIONS)
    pages = max(1, -(-total // per))
    prev = f'<a href="/lists/{key}?{qs}&page={page_n-1}">Previous</a>' if page_n > 1 else ""
    nxt = f'<a href="/lists/{key}?{qs}&page={page_n+1}">Next</a>' if page_n < pages else ""
    return f"""
<form class="filters" method="get" action="/lists/{key}">
<label>Search<input type="search" name="q" value="{esc(q)}" placeholder="Firm, city or CRD"></label>
<label>State<select name="st">{ui.opt("", st, "All states")}{"".join(ui.opt(s, st, s) for s in states)}</select></label>
<label>Data<select name="cov">{ui.opt("", cov, "Any coverage")}{ui.opt("full", cov, "90%+ known")}{ui.opt("most", cov, "70%+ known")}{ui.opt("gaps", cov, "Big gaps")}</select></label>
<label>Owner<select name="owner">{ui.opt("", owner, "Anyone")}{ui.opt("me", owner, "Mine")}{ui.opt("none", owner, "Unclaimed")}</select></label>
<label>Status<select name="stat">{ui.opt("", stat, "Any")}{ui.opt("open", stat, "Still open")}{stat_opts}</select></label>
<label>Signal<select name="sig">{ui.opt("", sig, "Any")}{ui.opt("1", sig, "New in 60 days")}</select></label>
<label>Reach<select name="reach">{ui.opt("", reach, "Any")}{ui.opt("email", reach, "A named person's email")}{ui.opt("verified", reach, "A verified email")}{ui.opt("phone", reach, "A direct line")}</select></label>
<label>Sort<select name="sort">{"".join(ui.opt(k, sort, v) for k, v in SORTS.items())}</select></label>
<button class="primary" type="submit">Apply</button>
<a class="btn ghost" href="/lists/{key}">Clear</a>
</form>
<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin:10px 0 0;flex-wrap:wrap">
<div class="legend"><span><b style="color:var(--ink)">{total:,}</b> firms</span>
<span><i style="background:var(--ink)"></i>earned on known data</span>
<span><i style="background:var(--hatch)"></i>unknown, could still be earned</span></div>
<form method="post" action="/views/save" class="acts">
<input type="hidden" name="page" value="list:{key}"><input type="hidden" name="qs" value="{esc(qs)}">
<input type="text" name="name" placeholder="Name this view to save it" style="min-width:210px">
<button type="submit" class="sm">Save view</button></form>
</div>
<table><thead><tr><th>#</th><th>Firm</th><th>Score</th><th>Why it scores</th>
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
    return (f'<p class="lede" style="margin:14px 0 4px">Firms that passed the gates and were '
            f'then removed. They stay visible so nobody calls them by mistake, and so a wrong '
            f'call can be spotted.</p>'
            f'<table><thead><tr><th>Firm</th><th>Why it was removed</th></tr></thead>'
            f'<tbody>{body or "<tr><td colspan=2 class=empty>None.</td></tr>"}</tbody></table>')


# ------------------------------------------------------------------ scoring

def _num(v) -> str:
    if v is None:
        return ""
    f = float(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _thr_hint(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return money(f) if f >= 10_000 else ""


GATE_PARAMS = {"min": "Minimum", "max": "Maximum", "min_avg_client": "Minimum average client"}


def scoring_html(key: str, msg: str = "", err: str = "") -> str:
    p = products.product(key)
    edit = can_edit(key)
    ed = products.edited(key)
    who = (f'<p class="meta">Last changed by {esc(ed["updated_by"] or "someone")} on '
           f'{esc((ed["updated_at"] or "")[:16].replace("T", " "))} UTC. The shipped defaults '
           f'are in config/products.yml.</p>' if ed else
           '<p class="meta">These are the shipped defaults from config/products.yml.</p>')
    flash = ""
    if msg:
        flash = f'<div class="note good">{esc(msg)}</div>'
    if err:
        flash = f'<div class="note bad">{esc(err)}</div>'
    intro = ('<p class="lede" style="margin-top:16px">Every factor earns 0 to 100 points, then counts '
             'for its weight. A firm&rsquo;s score is the sum, out of 100. A factor Bellwether '
             'has no data for yet earns nothing and is shown as missing, with the share of '
             'the weight it represents, so a score never rises because something is '
             'unknown. Firms are ranked by score, then by how much of it rests on known data.</p>')

    dis = "" if edit else " disabled"
    gates = []
    for i, g in enumerate(p.get("gates", [])):
        params = "".join(
            f'<label class="inline">{esc(GATE_PARAMS[k])} <input type="number" step="any" '
            f'name="g-{i}-{k}" value="{esc(_num(g[k]))}"{dis} style="width:150px">'
            f'<span class="muted small">{esc(_thr_hint(g[k]))}</span></label>'
            for k in GATE_PARAMS if k in g)
        gates.append(f'<div class="gline" style="padding:8px 0"><b>Must pass</b> '
                     f'{esc(g["label"])}<div class="row" style="margin-top:6px">{params}</div></div>')
    for i, g in enumerate(p.get("disqualifiers", [])):
        on = not g.get("off")
        gates.append(f'<div class="gline" style="padding:8px 0"><b class="x">Removed if</b> '
                     f'{esc(g["label"])} <label class="inline" style="display:inline-flex;margin-left:10px">'
                     f'<input type="checkbox" name="d-{i}-on" value="1"{" checked" if on else ""}{dis}> '
                     f'in force</label></div>')

    rows = []
    for i, cr in enumerate(p["criteria"]):
        kind = cr.get("kind")
        lv_html = ""
        if cr.get("bands"):
            lv_html = "".join(
                f'<div class="lv"><span class="muted small" style="width:60px">from</span>'
                f'<input type="number" step="any" name="c-{i}-b-{j}-thr" value="{esc(_num(thr))}"{dis}'
                f' style="width:130px"><span class="muted small" style="width:58px">{esc(_thr_hint(thr))}</span>'
                f'<input type="number" step="any" name="c-{i}-b-{j}-pts" value="{esc(_num(pts))}"{dis}>'
                f'<span class="muted small">points</span></div>'
                for j, (thr, pts) in enumerate(cr["bands"]))
        if kind == "flag":
            lv_html = (f'<div class="lv"><span class="muted small" style="width:60px">yes</span>'
                       f'<input type="number" step="any" name="c-{i}-yes" value="{esc(_num(cr.get("yes", 100)))}"{dis}>'
                       f'<span class="muted small" style="width:40px;margin-left:10px">no</span>'
                       f'<input type="number" step="any" name="c-{i}-no" value="{esc(_num(cr.get("no", 0)))}"{dis}></div>')
        levels = ""
        if cr.get("levels") and not cr.get("bands"):
            levels = "".join(
                f'<div class="lv"><input type="number" step="any" name="c-{i}-l-{j}-pts" value="{esc(_num(pts))}"{dis}>'
                f'<input type="text" name="c-{i}-l-{j}-label" value="{esc(label)}"{dis}></div>'
                for j, (pts, label) in enumerate(cr["levels"]))
        elif cr.get("levels"):
            levels = "".join(f'<div class="lv muted small"><b style="width:30px;color:var(--ink)">'
                             f'{int(pts)}</b> {esc(label)}</div>' for pts, label in cr["levels"])
        source = ""
        if kind == "field":
            source = f'<div class="meta">Data field: {esc(products.FIELDS[cr["field"]][0])}</div>'
        elif kind == "flag":
            source = f'<div class="meta">Yes or no: {esc(products.flag_label(cr["field"]))}</div>'
        manual = (f'<label class="inline small" style="margin-top:6px"><input type="checkbox" '
                  f'name="c-{i}-manual" value="1"{" checked" if cr.get("manual") else ""}{dis}> '
                  f'Can be set by hand on a firm page</label>')
        remove = ""
        if kind in ("field", "flag") and edit:
            remove = (f'<label class="inline small"><input type="checkbox" name="c-{i}-remove" '
                      f'value="1"> Remove this factor</label>')
        off = float(cr.get("weight", 0)) <= 0
        rows.append(
            f'<tr{" style=opacity:.55" if off else ""}><td style="width:30%">'
            f'<input type="text" name="c-{i}-label" value="{esc(cr["label"])}"{dis} style="width:100%;font-weight:600">'
            f'{source}{manual}{remove}</td>'
            f'<td style="width:120px"><div class="wc"><input type="number" step="any" min="0" max="100" data-w '
            f'name="c-{i}-weight" value="{esc(_num(cr["weight"]))}"{dis}>%</div>'
            f'{"<div class=meta>off</div>" if off else ""}</td>'
            f'<td><div class="lvls">{lv_html}{levels}</div></td></tr>')
    pens = "".join(
        f'<div class="gline"><b class="x">Minus</b> <input type="number" step="any" name="p-{i}-points" '
        f'value="{esc(_num(pe["points"]))}"{dis} style="width:70px"> {esc(pe["label"])}</div>'
        for i, pe in enumerate(p.get("penalties", [])))

    glob = ""
    if p["family"] == "PHH":
        cf = products.cfg()
        glob = (f'<h3 style="margin-top:22px">PHH settings shared by the three PHH lists</h3>'
                f'<label style="max-width:520px">Focus states (two-letter codes, comma separated)'
                f'<input type="text" name="phh_focus_states" value="{esc(", ".join(cf.get("phh_focus_states") or []))}"{dis}></label>'
                f'<label style="max-width:520px;margin-top:10px">Custodians that can hold private investments'
                f'<input type="text" name="major_custodians" value="{esc(", ".join(cf.get("major_custodians") or []))}"{dis}></label>')

    add = ""
    hist = ""
    actions = ""
    if edit:
        fopts = "".join(f'<option value="field:{k}">{esc(v[0])} (number)</option>'
                        for k, v in products.FIELDS.items())
        fopts += "".join(f'<option value="flag:{k}">{esc(v[0])} (yes or no)</option>'
                         for k, v in products.FLAGS.items())
        products._load_vocab()
        fopts += "".join(f'<option value="flag:tag:{k}">Brochure mentions {esc(v.lower())}</option>'
                         for k, v in sorted(products.TAG_LABELS.items(), key=lambda x: x[1]))
        add = f"""<section class="s"><h2>Add a factor</h2>
<p class="lede">Any number Bellwether holds about a firm can become a factor scored by bands, and
any yes or no as two levels. It starts with weight 0; give it a weight and take that weight from
other factors so the total stays 100.</p>
<form method="post" action="/lists/{key}/scoring/add" class="row" style="margin-top:10px">
<select name="field" style="max-width:420px">{fopts}</select>
<input type="text" name="label" placeholder="Name it (optional)" style="min-width:240px">
<button type="submit">Add factor</button></form></section>"""
        h = products.history(key)
        if h:
            hist = ('<section class="s"><h2>History</h2><table class="tight"><tbody>' + "".join(
                f'<tr><td class="small nowrap">{esc((x["updated_at"] or "")[:16].replace("T", " "))}</td>'
                f'<td class="small">{esc(x["updated_by"] or "")}</td>'
                f'<td class="small soft">{esc(x["note"] or "")}</td></tr>' for x in h)
                + '</tbody></table></section>')
        actions = f"""<div class="row" style="margin-top:18px;position:sticky;bottom:0;background:var(--bg);padding:12px 0;border-top:1px solid var(--rule)">
<span class="muted small">Weights total</span><span id="wsum" class="wsum">100%</span>
<button type="button" class="ghost sm" onclick="spread()">Scale to 100</button>
<span class="spacer"></span>
<input type="text" name="note" placeholder="What changed and why" style="min-width:300px">
<button type="submit" class="primary">Save and rescore</button></div>"""
        reset = (f'<form method="post" action="/lists/{key}/scoring/reset" style="margin-top:10px" '
                 f'onsubmit="return confirm(\'Put this product back to the shipped defaults?\')">'
                 f'<button type="submit" class="ghost sm">Reset to the shipped defaults</button></form>'
                 if ed else "")
        actions += reset
    else:
        who += ('<p class="meta">Only admins and this product&rsquo;s owner can change these rules.</p>')

    return f"""<div class="rules editor">{flash}{intro}{who}
<form method="post" action="/lists/{key}/scoring">
<h3 style="margin-top:22px">Gates</h3>{"".join(gates) or '<p class="muted">None</p>'}
<h3 style="margin-top:22px">Scored factors</h3>
<table><thead><tr><th>Factor</th><th>Weight</th><th>Points for each level</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
{f"<h3 style='margin-top:22px'>Penalties</h3>{pens}" if pens else ""}
{glob}
{actions}
</form>{add}{hist}</div>"""


def _float(form, name, default=None):
    v = form.get(name)
    if v in (None, ""):
        return default
    try:
        return float(v)
    except ValueError:
        raise ValueError(f"{name} is not a number") from None


def _clean(v: float):
    return int(v) if float(v) == int(v) else v


def _rescore() -> None:
    log = open(config.DATA_DIR / "weekly.log", "ab")
    subprocess.Popen([sys.executable, "-m", "scripts.score_products"], cwd=str(config.ROOT),
                     stdout=log, stderr=log, creationflags=procs.SPAWN_FLAGS)


@router.post("/lists/{key}/scoring")
async def scoring_save(key: str, request: Request):
    if _key_or_none(key) is None:
        return RedirectResponse("/", status_code=303)
    if not can_edit(key):
        return RedirectResponse(f"/lists/{key}?view=scoring&err=Only+admins+and+this+"
                                f"product%27s+owner+can+change+its+scoring", status_code=303)
    form = await request.form()
    p = copy.deepcopy(products.product(key))
    try:
        for i, g in enumerate(p.get("gates", [])):
            for k in GATE_PARAMS:
                if k in g:
                    g[k] = _clean(_float(form, f"g-{i}-{k}", g[k]))
        for i, g in enumerate(p.get("disqualifiers", [])):
            if form.get(f"d-{i}-on"):
                g.pop("off", None)
            else:
                g["off"] = True
        kept = []
        for i, cr in enumerate(p["criteria"]):
            if form.get(f"c-{i}-remove"):
                continue
            cr["label"] = (form.get(f"c-{i}-label") or cr["label"]).strip()[:80]
            w = _float(form, f"c-{i}-weight", cr["weight"])
            if w < 0 or w > 100:
                raise ValueError(f"{cr['label']}: weight must be between 0 and 100")
            cr["weight"] = _clean(w)
            if form.get(f"c-{i}-manual"):
                cr["manual"] = True
            else:
                cr.pop("manual", None)
            if cr.get("bands"):
                cr["bands"] = sorted(
                    ([_clean(_float(form, f"c-{i}-b-{j}-thr", thr)),
                      _clean(_float(form, f"c-{i}-b-{j}-pts", pts))]
                     for j, (thr, pts) in enumerate(cr["bands"])),
                    key=lambda b: -float(b[0]))
            if cr.get("kind") == "flag":
                cr["yes"] = _clean(_float(form, f"c-{i}-yes", cr.get("yes", 100)))
                cr["no"] = _clean(_float(form, f"c-{i}-no", cr.get("no", 0)))
            if cr.get("levels") and not cr.get("bands"):
                # Keep each condition's identity when points change order.
                cr["level_inputs"] = products.level_inputs(key, cr)
                cr["levels"] = [
                    [_clean(_float(form, f"c-{i}-l-{j}-pts", pts)),
                      (form.get(f"c-{i}-l-{j}-label") or label).strip()[:120]]
                     for j, (pts, label) in enumerate(cr["levels"])]
            for b in cr.get("bands", []):
                if not 0 <= float(b[1]) <= 100:
                    raise ValueError(f"{cr['label']}: points must be between 0 and 100")
            kept.append(cr)
        p["criteria"] = kept
        for i, pe in enumerate(p.get("penalties", [])):
            pe["points"] = _clean(_float(form, f"p-{i}-points", pe["points"]))
        note = (form.get("note") or "").strip()[:200]
        who = current_owner()
        if p["family"] == "PHH" and ("phh_focus_states" in form or "major_custodians" in form):
            g = {"phh_focus_states": [s.strip().upper() for s in
                                      (form.get("phh_focus_states") or "").split(",") if s.strip()],
                 "major_custodians": [s.strip() for s in
                                      (form.get("major_custodians") or "").split(",") if s.strip()]}
            cf = products.cfg()
            if (g["phh_focus_states"] != (cf.get("phh_focus_states") or [])
                    or g["major_custodians"] != (cf.get("major_custodians") or [])):
                products.save_product("_global", g, who, note or "PHH shared settings")
        products.save_product(key, p, who, note)
    except ValueError as e:
        return RedirectResponse(f"/lists/{key}?view=scoring&{qs_join(err=str(e))}",
                                status_code=303)
    _rescore()
    return RedirectResponse(f"/lists/{key}?view=scoring&{qs_join(msg='Saved. The list is being rescored now; it takes under a minute.')}",
                            status_code=303)


@router.post("/lists/{key}/scoring/add")
def scoring_add(key: str, field: str = Form(...), label: str = Form("")):
    if _key_or_none(key) is None or not can_edit(key):
        return RedirectResponse(f"/lists/{key}?view=scoring", status_code=303)
    p = copy.deepcopy(products.product(key))
    kind, _, name = field.partition(":")
    slug = "x_" + "".join(ch if ch.isalnum() else "_" for ch in name.lower())[:40]
    if any(cr["key"] == slug for cr in p["criteria"]):
        return RedirectResponse(f"/lists/{key}?view=scoring&{qs_join(err='That factor is already on this list.')}",
                                status_code=303)
    if kind == "field" and name in products.FIELDS:
        lab, typ, _ = products.FIELDS[name]
        bands = {"money": [[1e9, 100], [5e8, 75], [2.5e8, 50], [1e8, 25], [0, 0]],
                 "pct": [[0.6, 100], [0.4, 70], [0.2, 40], [0, 0]],
                 "int": [[25, 100], [10, 70], [5, 40], [1, 20], [0, 0]]}[typ]
        cr = {"key": slug, "kind": "field", "field": name, "label": label.strip() or lab,
              "weight": 0, "bands": bands}
    elif kind == "flag" and (name in products.FLAGS or name.startswith(("tag:", "web:"))):
        cr = {"key": slug, "kind": "flag", "field": name,
              "label": label.strip() or products.flag_label(name), "weight": 0,
              "yes": 100, "no": 0}
    else:
        return RedirectResponse(f"/lists/{key}?view=scoring", status_code=303)
    p["criteria"].append(cr)
    try:
        products.save_product(key, p, current_owner(), f"added factor: {cr['label']}")
    except ValueError as e:
        return RedirectResponse(f"/lists/{key}?view=scoring&{qs_join(err=str(e))}", status_code=303)
    return RedirectResponse(f"/lists/{key}?view=scoring&{qs_join(msg='Factor added with weight 0. Give it a weight and save.')}",
                            status_code=303)


@router.post("/lists/{key}/scoring/reset")
def scoring_reset(key: str):
    if _key_or_none(key) is None or not can_edit(key):
        return RedirectResponse(f"/lists/{key}?view=scoring", status_code=303)
    products.reset_product(key, current_owner())
    _rescore()
    return RedirectResponse(f"/lists/{key}?view=scoring&{qs_join(msg='Back to the shipped defaults; rescoring now.')}",
                            status_code=303)


# ------------------------------------------------------------------ exports

@router.get("/lists/{key}/export.csv")
def export_list(key: str, q: str = "", st: str = "", owner: str = "", stat: str = "",
                sig: str = "", reach: str = "", cov: str = ""):
    p = _key_or_none(key)
    if p is None:
        return RedirectResponse("/", status_code=303)
    c = conn()
    where, args = _where(key, q, st, owner, stat, sig, reach, cov)
    rows = c.execute(f"""
        SELECT p.rank, p.crd, f.legal_name, f.city, f.state, f.raum, f.website, f.phone,
               p.score, p.coverage, p.potential, p.missing, p.detail_json, s.owner, s.status
        {BASE.format(where=where)} ORDER BY p.rank LIMIT 50000""", args).fetchall()
    flags = ui.contact_flags(c, [r["crd"] for r in rows]) if len(rows) <= 5000 else {}
    c.close()
    crits = [cr["key"] for cr in p["criteria"] if float(cr.get("weight", 0)) > 0]
    labels = {cr["key"]: cr["label"] for cr in p["criteria"]}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["rank", "crd", "firm", "city", "state", "aum", "website", "main_phone",
                "score", "coverage_pct", "could_reach", "missing", "pitch",
                "named_people_with_email", "verified_emails"] +
               [f"{labels[k]} (points)" for k in crits] + ["owner", "status"])
    for r in rows:
        d = json.loads(r["detail_json"] or "{}")
        pts = {cp["key"]: (cp["points"] if cp.get("known", True) else "missing")
               for cp in d.get("components", [])}
        fl = flags.get(r["crd"], {})
        w.writerow([r["rank"], r["crd"], r["legal_name"], r["city"], r["state"], r["raum"],
                    r["website"], r["phone"], r["score"], r["coverage"], r["potential"],
                    (r["missing"] or "").replace("|", "; "), d.get("pitch", ""),
                    fl.get("personal", ""), fl.get("verified", "")] +
                   [pts.get(k) for k in crits] + [r["owner"], r["status"]])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{key}_list.csv"'})


@router.get("/lists/{key}/export.xlsx")
def export_contacts(key: str, q: str = "", st: str = "", owner: str = "", stat: str = "",
                    sig: str = "", reach: str = "", cov: str = ""):
    """The mail-merge sheet for exactly the firms on screen: one row per person
    and address, with where it came from and whether a mail server confirmed it."""
    from .firms_view import contacts_rows
    p = _key_or_none(key)
    if p is None:
        return RedirectResponse("/", status_code=303)
    c = conn()
    where, args = _where(key, q, st, owner, stat, sig, reach, cov)
    crds = [r["crd"] for r in c.execute(
        f"SELECT p.crd {BASE.format(where=where)} ORDER BY p.rank LIMIT 5000", args)]
    ranks = {r["crd"]: (r["rank"], r["score"], r["coverage"]) for r in c.execute(
        "SELECT crd, rank, score, coverage FROM product_score WHERE product=?"
        " AND status='scored'", (key,))}
    rows = contacts_rows(c, crds)
    c.close()
    headers = ["Rank", "Score", "Coverage %", "Firm", "CRD", "State", "Person", "Title",
               "Email", "Email status", "Source", "Confidence", "Phone"]
    out = []
    for r in rows:
        rk, sc, cv = ranks.get(r["crd"], (None, None, None))
        out.append([rk or "", sc if sc is not None else "", cv if cv is not None else "",
                    r["firm"], r["crd"], r["state"] or "", r["person"] or "",
                    r["title"] or "", r["email"] or "", r["status"] or "", r["source"] or "",
                    r["confidence"] or "", r["phone"] or ""])
    out.sort(key=lambda x: (x[0] == "", x[0] if x[0] != "" else 0))
    data = xlsx.write_sheet(headers, out, sheet_name="Contacts")
    return Response(data, media_type=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        headers={"Content-Disposition": f'attachment; filename="{key}_contacts.xlsx"'})
