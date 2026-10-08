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


SIZES = [("", "Any size"), ("100", "$100M+"), ("250", "$250M+"), ("500", "$500M+"),
         ("1000", "$1B+"), ("5000", "$5B+")]


def _has_table(c, name: str) -> bool:
    try:
        return bool(c.execute("SELECT to_regclass(?) t", (name,)).fetchone()["t"])
    except Exception:
        c.rollback()
        return False


def _categories() -> list[tuple[str, str]]:
    try:
        from . import firmtype
        return [(c["key"], c["label"]) for c in firmtype.categories()]
    except Exception:
        return []


def _where(key, q, st, owner, stat, sig, reach, cov, status="scored", size="", cat=""):
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
        where.append("EXISTS (SELECT 1 FROM usable_contact_point x WHERE x.crd=p.crd AND x.kind='email'"
                     " AND x.person_key != '' AND x.is_role=0)")
    elif reach == "verified":
        where.append("EXISTS (SELECT 1 FROM usable_contact_point x WHERE x.crd=p.crd AND x.kind='email'"
                     " AND x.verify_status='valid')")
    elif reach == "phone":
        where.append("EXISTS (SELECT 1 FROM usable_contact_point x WHERE x.crd=p.crd AND x.kind='phone'"
                     " AND x.person_key != '' AND x.label IN ('direct','mobile'))")
    elif reach == "linkedin":
        where.append("EXISTS (SELECT 1 FROM usable_contact_point x WHERE x.crd=p.crd AND x.kind='linkedin'"
                     " AND x.person_key != '')")
    elif reach == "none":
        where.append("NOT EXISTS (SELECT 1 FROM usable_contact_point x WHERE x.crd=p.crd AND x.kind='email'"
                     " AND x.person_key != '' AND x.is_role=0)")
    if cov == "full":
        where.append("p.coverage >= 90")
    elif cov == "most":
        where.append("p.coverage >= 70")
    elif cov == "gaps":
        where.append("p.coverage < 70")
    if size in dict(SIZES) and size:
        where.append("f.raum >= ?")
        args.append(int(size) * 1_000_000)
    if cat:
        where.append("EXISTS (SELECT 1 FROM firm_class fc WHERE fc.crd=p.crd AND fc.category=?)")
        args.append(cat)
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
                 size: str = Query(""), cat: str = Query(""),
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
    reach_n = 0
    try:
        reach_n = c.execute("""SELECT COUNT(DISTINCT p.crd) n FROM product_score p
            JOIN usable_contact_point x ON x.crd=p.crd AND x.kind='email' AND x.person_key != ''
             AND x.is_role=0 WHERE p.product=? AND p.status='scored'""", (key,)).fetchone()["n"]
    except Exception:
        c.rollback()

    qs = qs_join(q=q, st=st, owner=owner, stat=stat, sig=sig, reach=reach, cov=cov,
                 size=size, cat=cat, sort=sort if sort != "score" else "")

    def tab(v, label, n=None):
        cls = "on" if view == v else ""
        cnt = f'<span class="cnt">{n:,}</span>' if n is not None else ""
        return f'<a class="{cls}" href="/lists/{key}?view={v}">{label}{cnt}</a>'

    kpis = (f'<div class="kpis" style="margin-bottom:22px">'
            f'<a class="kpi" href="/lists/{key}"><div class="l">Firms ranked</div><div class="n">{stats["n"] or 0:,}</div>'
            f'<div class="d">passed every gate</div></a>'
            f'<a class="kpi{" accent" if sig else ""}" href="/lists/{key}?sig=1"><div class="l">New signal</div>'
            f'<div class="n">{fresh_n:,}</div><div class="d">in the last 60 days</div></a>'
            f'<a class="kpi" href="/lists/{key}?reach=email"><div class="l">Reachable</div><div class="n">{reach_n:,}</div>'
            f'<div class="d">a named person\'s email</div></a>'
            f'<div class="kpi"><div class="l">Score 60 or more</div><div class="n">{stats["hi"] or 0:,}</div>'
            f'<div class="d">strong fit</div></div>'
            f'<a class="kpi{" accent" if cov == "full" else ""}" href="/lists/{key}?cov=full"><div class="l">Data coverage</div>'
            f'<div class="n">{stats["cov"] or 0:.0f}%</div><div class="d">{stats["full_n"] or 0:,} scored on 90%+ known data</div></a>'
            f'</div>')

    if view == "scoring":
        body_main = scoring_html(key, msg, err)
    elif view == "disqualified":
        body_main = _disq(c, key, q, page_n, per)
    else:
        body_main = _ranked(c, key, p, q, st, owner, stat, sig, reach, cov, sort, page_n,
                            per, qs, size, cat)
    c.close()

    note = (f'<details class="source-help"><summary>How this list is built</summary><p>{esc(p["note"].strip())}</p></details>'
            if p.get("note") and view == "ranked" else "")
    exp = ""
    if view == "ranked":
        exp = (f'<a class="btn" href="/lists/{key}/export.csv?{qs}" data-noprefetch>Export list</a>'
               f'<a class="btn primary" href="/lists/{key}/export.xlsx?{qs}" data-noprefetch>Export contacts</a>')
    scoring_tab = "Scoring" + (" and weights" if can_edit(key) else "")
    from .webapp import FAMILY_COLOUR
    dot = FAMILY_COLOUR.get(p["family"], "#888")
    body = f"""<div class="pg wide">
<div class="crumb"><a href="/">Home</a><span class="sep">/</span>Product lists</div>
<div class="head"><div><h1><span class="dotc" style="background:{dot};width:12px;height:12px;border-radius:4px"></span>{esc(p["name"])}</h1>
<div class="lede">{esc(p["audience"])}</div></div>
<div class="acts">{exp}</div></div>
{kpis}
<nav class="tabs">{tab("ranked", "Ranked", stats["n"] or 0)}{tab("disqualified", "Removed", dq_n)}{tab("scoring", scoring_tab)}</nav>
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


def _reach_cell(f: dict) -> str:
    """How reachable the firm is, as pills: verified addresses, named people
    with an address, direct lines; otherwise, that Bellwether is still looking."""
    bits = []
    if f["verified"]:
        bits.append(f'<span class="pill ok" title="Addresses a mail server confirmed">'
                    f'{f["verified"]} verified</span>')
    elif f["personal"]:
        bits.append(f'<span class="pill" title="Named people with an address the firm published">'
                    f'{f["personal"]} named</span>')
    if f["direct"]:
        bits.append(f'<span class="pill" title="Direct or mobile lines tied to a person">'
                    f'{f["direct"]:,} direct line{"s" if f["direct"] != 1 else ""}</span>')
    elif f["phone"] and not bits:
        bits.append('<span class="pill" title="The main line the firm filed">Main line</span>')
    if not f["verified"] and not f["personal"]:
        bits.append('<span class="hunt" title="Website, web search, verified patterns and AI research '
                    'are working on this firm"><i></i>Hunting</span>')
    return f'<div class="pills">{"".join(bits)}</div>'


def _ranked(c, key, p, q, st, owner, stat, sig, reach, cov, sort, page_n, per, qs, size="", cat=""):
    where, args = _where(key, q, st, owner, stat, sig, reach, cov, size=size, cat=cat)
    base = BASE.format(where=where)
    total = c.execute(f"SELECT COUNT(*) n {base}", args).fetchone()["n"]
    have_people = _has_table(c, "firm_people_stats")
    have_cls = _has_table(c, "firm_class")
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
    trig, ftypes = {}, {}
    if crds:
        ph = ",".join("?" * len(crds))
        for r in c.execute(f"""SELECT DISTINCT ON (crd) crd, trigger_type, detected_date
                FROM trigger_event WHERE crd IN ({ph}) AND suppressed=0
                  AND detected_date >= ? ORDER BY crd, detected_date DESC""",
                           tuple(crds) + (signal_cutoff(),)):
            trig[r["crd"]] = r
        if have_cls:
            try:
                from . import firmtype
                ftypes = firmtype.get_many(c, crds) or {}
            except Exception:
                c.rollback()
    lists = c.execute("SELECT id, name FROM user_list ORDER BY name").fetchall()
    listopts = "".join(f'<option value="{l["id"]}">{esc(l["name"])}</option>' for l in lists)
    back = f"/lists/{key}?{qs}&page={page_n}"

    body = []
    for r in rows:
        t = trig.get(r["crd"])
        tcell = (f'<span class="chip lead">{esc(TYPE_LABEL.get(t["trigger_type"], t["trigger_type"]))}</span>'
                 f'<div class="meta">{esc(ui.ago(t["detected_date"]))}</div>' if t else
                 '<span class="dim small">None new</span>')
        tags = []
        low = next((pe for pe in ui.detail(r["detail_json"]).get("penalties", [])
                    if pe.get("key") == "firm_type"), None)
        if low:
            tags.append(f'<span class="chip warn" title="{esc(low["evidence"])}. '
                        f'{esc(_num(low["points"]))} points off.">Not a usual buyer</span>')
        ft = ftypes.get(r["crd"]) if ftypes else None
        if ft and ft.get("category"):
            tags.append(f'<span class="ftype">{esc(ft.get("short") or ft.get("label") or ft["category"])}</span>')
        if r["status"] or r["owner"]:
            tags.append(f'<span class="chip lead">{esc((r["status"] or "claimed").capitalize())}'
                        f'{" &middot; " + esc(r["owner"]) if r["owner"] else ""}</span>')
        add = (f'<form method="post" action="/firms/addtolist">'
               f'<input type="hidden" name="crd" value="{esc(r["crd"])}">'
               f'<input type="hidden" name="back" value="{esc(back)}">'
               f'<input type="hidden" name="new_name" value="">'
               f'<select name="list_id" class="listpick" style="height:28px;font-size:12px;min-width:0;width:92px" '
               f'onchange="addToList(this)" title="Add to one of your saved lists" aria-label="Add to a saved list">'
               f'<option value="">+ List</option>{listopts}'
               f'<option value="__new">New list...</option></select></form>')
        body.append(
            f'<tr class="go" data-href="/firm/{esc(r["crd"])}?p={key}">'
            f'<td class="rank">{r["rank"] or ""}</td>'
            f'<td><div class="ent">{ui.mono(r["legal_name"])}<div><a class="t" href="/firm/{esc(r["crd"])}?p={key}">'
            f'{escn(r["legal_name"] or "(unnamed)")}</a>'
            f'<div class="meta">{ui.firm_meta(r)}</div>'
            f'{("<div class=pills style=margin-top:6px>" + "".join(tags) + "</div>") if tags else ""}</div></div></td>'
            f'<td>{score_cell(r["score"], r["coverage"], r["potential"])}</td>'
            f'<td>{ui.why_cell(r["detail_json"], r["score"])}</td>'
            f'<td>{_reach_cell(flags[r["crd"]])}</td>'
            f'<td>{tcell}</td>'
            f'<td>{add}</td></tr>')
    empty = ('<tr><td colspan="7"><div class="empty"><b>No firms match these filters</b>Remove a '
             f'filter above, or open <a href="/lists/{key}?view=scoring">Scoring</a> to see what this '
             'list requires.</div></td></tr>')
    states = _states(c, key)
    pages = max(1, -(-total // per))
    prev = f'<a href="/lists/{key}?{qs}&page={page_n-1}">Previous</a>' if page_n > 1 else ""
    nxt = f'<a href="/lists/{key}?{qs}&page={page_n+1}">Next</a>' if page_n < pages else ""
    vals = dict(q=q, st=st, owner=owner, stat=stat, sig=sig, reach=reach, cov=cov, size=size,
                cat=cat, sort=sort)
    more = [("cov", "Data coverage", [("", "Any coverage"), ("full", "90%+ known"),
                                      ("most", "70%+ known"), ("gaps", "Big gaps")]),
            ("owner", "Owner", [("", "Anyone"), ("me", "Mine"), ("none", "Unclaimed")]),
            ("stat", "Status", [("", "Any status"), ("open", "Still open")]
             + [(s, s.capitalize()) for s in ui.STATUS_OPTIONS])]
    cats = _categories()
    if cats:
        more.append(("cat", "Firm type", [("", "Any firm type")] + cats))
    more.append(("sort", "Sort", list(SORTS.items())))
    bar = ui.filter_bar(f"/lists/{key}", vals, search=("q", "Search firm, city or CRD"),
                        quick=[("st", "State", [("", "All states")] + [(s, s) for s in states]),
                               ("size", "Size", SIZES),
                               ("reach", "Reach", [("", "Any reach"), ("email", "Named email"),
                                                   ("verified", "Verified email"),
                                                   ("phone", "Direct line"), ("linkedin", "LinkedIn"),
                                                   ("none", "No email yet")]),
                               ("sig", "Signal", [("", "Any signal"), ("1", "New signal, 60 days")])],
                        more=more, defaults={"sort": "score"})
    return f"""{bar}
<div class="resbar"><span><b>{total:,}</b> firm{"s" if total != 1 else ""}</span>
<span class="legend"><span><i style="background:linear-gradient(90deg,#9b9ba3,#d8d8de)"></i>earned</span>
<span><i style="background:var(--hatch)"></i>no data yet</span>
<span><i style="background:var(--raise3)"></i>not met</span>
<details class="save-view"><summary>Save view</summary><form method="post" action="/views/save">
<input type="hidden" name="page" value="list:{key}"><input type="hidden" name="qs" value="{esc(qs)}">
<input type="text" name="name" placeholder="Name this view" required><button type="submit" class="primary sm">Save</button></form></details></span></div>
<div class="table-scroll"><table class="ranked-table wide"><thead><tr><th>#</th><th>Firm</th><th>Score</th><th>Why it scores</th>
<th>Reach</th><th>New signal</th><th></th></tr></thead>
<tbody>{"".join(body) or empty}</tbody></table></div>
<div class="pager"><span>Page {page_n} of {pages:,}</span><span class="acts">{prev}{nxt}</span></div>"""


def _reason_group(reason: str) -> str:
    """The short name of a removal reason, for the counts at the top."""
    head = (reason or "").split(",")[0].split(":")[0].strip()
    return head or "Other"


def _disq(c, key, q, page_n, per):
    """Firms taken off the list, each with the full reason in plain words.
    Removal is kept for the sure cases: a type that can never buy the product,
    or a hard rule such as leaving Schwab; every other doubt only lowers a firm
    on the ranked list, where it stays visible."""
    from .webapp import is_admin
    p = products.product(key)
    where = "p.product=? AND p.status='disqualified'"
    args: list = [key]
    groups = {}
    for r in c.execute("SELECT reason FROM product_score WHERE product=? AND status='disqualified'",
                       (key,)):
        g = _reason_group(r["reason"])
        groups[g] = groups.get(g, 0) + 1
    if q:
        where += " AND (f.legal_name ILIKE ? OR f.crd=?)"
        args += [f"%{q}%", q]
    rows = c.execute(f"""SELECT p.crd, p.reason, f.legal_name, f.city, f.state, f.raum
        FROM product_score p JOIN firm_current f ON f.crd=p.crd WHERE {where}
        ORDER BY f.raum DESC NULLS LAST LIMIT ? OFFSET ?""",
                     args + [per, (page_n - 1) * per]).fetchall()
    admin = is_admin()
    body = []
    for r in rows:
        fix = ""
        if "a kind of firm that never buys" in (r["reason"] or "") and admin:
            fix = (f'<div class="meta" style="margin-top:6px"><a href="/settings/firmtypes?'
                   f'{qs_join(q=r["crd"])}">Wrong firm type? Correct it</a> and the firm comes back '
                   f'on the next rescore.</div>')
        body.append(
            f'<tr class="go" data-href="/firm/{esc(r["crd"])}?p={key}"><td><div class="ent">'
            f'{ui.mono(r["legal_name"])}<div><a class="t" href="/firm/{esc(r["crd"])}?p={key}">'
            f'{escn(r["legal_name"])}</a><div class="meta">{ui.firm_meta(r)}</div></div></div></td>'
            f'<td><div class="why" style="color:var(--ink)">{esc(r["reason"])}</div>{fix}</td></tr>')
    summary = "".join(f'<span class="pill">{esc(g)}<b style="margin-left:4px">{n:,}</b></span>'
                      for g, n in sorted(groups.items(), key=lambda kv: -kv[1]))
    lede = (f'<p class="lede" style="margin:4px 0 12px">A firm is removed from {esc(p["name"])} only '
            f'when Bellwether is very sure it fails a required rule: a kind of firm that can never '
            f'buy it (a custodian, a wirehouse, a bank or trust company, an insurer) or a hard rule '
            f'on the Scoring tab. Firms that are only a likely poor fit stay on the ranked list, '
            f'lower down and marked <span class="chip warn">Not a usual buyer</span>.</p>')
    return (lede + (f'<div class="pills" style="margin:0 0 14px">{summary}</div>' if summary else "")
            + f'<div class="table-scroll"><table><thead><tr><th style="width:38%">Firm</th>'
              f'<th>Why it was removed</th></tr></thead><tbody>'
            + ("".join(body) or '<tr><td colspan="2"><div class="empty"><b>Nothing removed</b>'
                                'Every firm that passed the gates is on the ranked list.</div></td></tr>')
            + '</tbody></table></div>')


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

    # Which kinds of firm this product sells to. Unchecked types are removed
    # from the list once Bellwether is sure enough of a firm's type; a firm
    # not classified yet is never removed for it.
    ftype_html = ""
    try:
        ft = products.firm_type_rule(key)
    except Exception:
        ft = None
    if ft:
        def choice(c):
            opts = "".join(f'<option value="{v}"{" selected" if c["choice"] == v else ""}>{t}</option>'
                           for v, t in (("allow", "Sells to"), ("lower", "Lower in the list"),
                                        ("remove", "Remove")))
            return (f'<label class="ftbox ft-{esc(c["choice"])}" title="{esc(c.get("treatment") or c.get("description") or "")}">'
                    f'<span>{esc(c["label"])}</span><select name="ft-{esc(c["key"])}"{dis}>{opts}</select></label>')
        boxes = "".join(choice(c) for c in ft["categories"])
        ftype_html = (f'<h3 style="margin-top:22px">Firm types for {esc(p["name"])}</h3>'
                      f'<p class="meta" style="margin:-4px 0 10px">Sells to: scored normally. '
                      f'Lower in the list: stays on the list, marked Not a usual buyer, with points off. '
                      f'Remove: taken off the list, with the reason, only when Bellwether is very sure '
                      f'of the type or a person set it. Firms not classified yet always stay.</p>'
                      f'<input type="hidden" name="ft-present" value="1">'
                      f'<div class="ftgrid">{boxes}</div>'
                      f'<div class="row" style="margin-top:12px;gap:18px">'
                      f'<label class="inline">Points off when lowered <input type="number" name="ft-penalty" '
                      f'min="0" max="100" value="{esc(_num(ft.get("penalty", 20)))}"{dis}></label>'
                      f'<label class="inline">Sure enough to lower <input type="number" name="ft-min_conf" '
                      f'min="0" max="100" value="{esc(_num(ft.get("min_confidence", 60)))}"{dis}>%</label>'
                      f'<label class="inline">Sure enough to remove <input type="number" name="ft-remove_conf" '
                      f'min="0" max="100" value="{esc(_num(ft.get("remove_confidence", 90)))}"{dis}>%</label></div>')

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
    extra_actions = ""
    if edit:
        fopts = "".join(f'<option value="field:{k}">{esc(v[0])} (number)</option>'
                        for k, v in products.FIELDS.items())
        fopts += "".join(f'<option value="flag:{k}">{esc(v[0])} (yes or no)</option>'
                         for k, v in products.FLAGS.items())
        products._load_vocab()
        fopts += "".join(f'<option value="flag:tag:{k}">{esc(products.flag_label("tag:" + k))}</option>'
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
        actions = f"""<div class="savebar">
<span class="muted small">Weights total</span><span id="wsum" class="wsum">100%</span>
<button type="button" class="ghost sm" onclick="spread()">Scale to 100</button>
<span class="spacer"></span>
<input type="text" name="note" placeholder="What changed and why" style="min-width:300px">
<label class="small"><input type="checkbox" name="refresh_all" value="1"> Refresh all firm websites and email platforms</label>
<button type="submit" class="primary">Save and rescore all firms</button></div>"""
        reset = (f'<form method="post" action="/lists/{key}/scoring/reset" style="margin-top:10px" '
                 f'onsubmit="return confirm(\'Put this product back to the shipped defaults?\')">'
                 f'<button type="submit" class="ghost sm">Reset to the shipped defaults</button></form>'
                 if ed else "")
        extra_actions = reset
        extra_actions += (f'<form method="post" action="/lists/{key}/rescore" class="rescore-all">'
                    '<p class="meta">Recheck every firm against every product, including firms currently outside the lists.</p>'
                    '<label class="small"><input type="checkbox" name="refresh_all" value="1"> Refresh all firm websites and email platforms</label>'
                    '<button type="submit">Rescore all firms now</button></form>')
    else:
        who += ('<p class="meta">Only admins and this product&rsquo;s owner can change these rules.</p>')

    return f"""<div class="rules editor">{flash}{intro}{who}
<form method="post" action="/lists/{key}/scoring">
<h3 style="margin-top:22px">Gates</h3>{"".join(gates) or '<p class="muted">None</p>'}
{ftype_html}
<h3 style="margin-top:22px">Scored factors</h3>
<table><thead><tr><th>Factor</th><th>Weight</th><th>Points for each level</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
{f"<h3 style='margin-top:22px'>Penalties</h3>{pens}" if pens else ""}
{glob}
{actions}
</form>{extra_actions}{add}{hist}</div>"""


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


def _rescore(refresh_all: bool = False) -> None:
    from . import jobs
    c = conn()
    try:
        jobs.init(c)
        if refresh_all:
            jobs.request_full_refresh(c)
        jobs.request_run(c, 'rescore')
    finally:
        c.close()


@router.post('/lists/{key}/rescore')
def rescore_all(key: str, refresh_all: str = Form("")):
    if _key_or_none(key) is None or not can_edit(key):
        return RedirectResponse('/', status_code=303)
    _rescore(bool(refresh_all))
    message = 'All firms and products queued for rescoring.'
    if refresh_all:
        message += ' Website and email-platform refreshes queued for every firm.'
    return RedirectResponse(f'/lists/{key}?view=scoring&{qs_join(msg=message)}', status_code=303)


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
        if form.get("ft-present"):
            ft_rule = products.firm_type_rule(key) or {"categories": []}
            picks = {c["key"]: form.get(f"ft-{c['key']}") or c["choice"] for c in ft_rule["categories"]}
            products.apply_firm_types(
                p, [k for k, v in picks.items() if v == "allow"], form.get("ft-min_conf"),
                remove=[k for k, v in picks.items() if v == "remove"],
                remove_confidence=form.get("ft-remove_conf"), penalty=form.get("ft-penalty"))
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
    _rescore(bool(form.get('refresh_all')))
    message = 'Saved. Every firm and product is queued for rescoring.'
    if form.get('refresh_all'):
        message += ' All firm websites and email platforms will be refreshed.'
    return RedirectResponse(f"/lists/{key}?view=scoring&{qs_join(msg=message)}",
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
                sig: str = "", reach: str = "", cov: str = "",
                size: str = "", cat: str = ""):
    p = _key_or_none(key)
    if p is None:
        return RedirectResponse("/", status_code=303)
    c = conn()
    where, args = _where(key, q, st, owner, stat, sig, reach, cov, size=size, cat=cat)
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
                    sig: str = "", reach: str = "", cov: str = "",
                size: str = "", cat: str = ""):
    """The mail-merge sheet for exactly the firms on screen: one row per person
    and address, with where it came from and whether a mail server confirmed it."""
    from .firms_view import contacts_rows
    p = _key_or_none(key)
    if p is None:
        return RedirectResponse("/", status_code=303)
    c = conn()
    where, args = _where(key, q, st, owner, stat, sig, reach, cov, size=size, cat=cat)
    crds = [r["crd"] for r in c.execute(
        f"SELECT p.crd {BASE.format(where=where)} ORDER BY p.rank LIMIT 5000", args)]
    ranks = {r["crd"]: (r["rank"], r["score"], r["coverage"]) for r in c.execute(
        "SELECT crd, rank, score, coverage FROM product_score WHERE product=?"
        " AND status='scored'", (key,))}
    rows = contacts_rows(c, crds)
    c.close()
    from .names import first_name
    headers = ["Rank", "Score", "Coverage %", "Firm", "CRD", "State", "Person", "first_name", "Title",
               "Email", "Email status", "Source", "Confidence", "Phone"]
    out = []
    for r in rows:
        rk, sc, cv = ranks.get(r["crd"], (None, None, None))
        out.append([rk or "", sc if sc is not None else "", cv if cv is not None else "",
                    r["firm"], r["crd"], r["state"] or "", r["person"] or "",
                    first_name(r["person"]) if r["person"] != "Shared inbox" else "",
                    r["title"] or "", r["email"] or "", r["status"] or "", r["source"] or "",
                    r["confidence"] or "", r["phone"] or ""])
    out.sort(key=lambda x: (x[0] == "", x[0] if x[0] != "" else 0))
    data = xlsx.write_sheet(headers, out, sheet_name="Contacts")
    return Response(data, media_type=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        headers={"Content-Disposition": f'attachment; filename="{key}_contacts.xlsx"'})
