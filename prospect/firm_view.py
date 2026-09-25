"""Firm page: everything needed to decide whether to call, and what to say.

One continuous document with a rail beside it. The document answers, in
order: how does this firm fit each product and exactly why; what changed; what
it says in its own words; who to reach; and the facts underneath. The rail
holds what you act on: status, owner, notes, lists and the call prep.

Rules this page enforces:

  1. A score is always shown with its inputs. Every criterion row carries the
     evidence that set its level, so anyone can disagree with it and see why.
  2. A person's judgement is labelled as one. A manual level shows who set it,
     when, and what the filings alone would have said.
  3. Every archive-derived figure carries its as-of date.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from . import products, ui
from .webapp import (escn, nice_name, STATUSES, TYPE_LABEL, caveat, conn, current_owner, esc, money,
                     page, tier_chip)

router = APIRouter()

# CRDs are digits. Checked before a CRD goes into a redirect or a write, so a
# crafted path can never become part of a Location header or a stored row.
CRD_RE = re.compile(r"^[0-9]{1,12}$")

FIRM_CSS = """
.doc{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:44px;align-items:start}
@media (max-width:1180px){.doc{grid-template-columns:minmax(0,1fr)}}
.rail{position:sticky;top:22px;display:flex;flex-direction:column;gap:14px}
.rail .panel h3{margin-bottom:8px}
.rail form label{margin-bottom:8px}
.rail select,.rail input[type=text]{width:100%;min-width:0}
.facts{display:grid;grid-template-columns:1fr 1fr;gap:10px 14px}
.facts .n{font:600 18px "Segoe UI",sans-serif;font-variant-numeric:tabular-nums}
.facts .l{font-size:11px;color:var(--faint)}
.sub-line{color:var(--soft);font-size:13.5px;margin-top:8px}
.sub-line a{color:var(--soft)}
details.fit{border-top:1px solid var(--rule);padding:0}
details.fit:last-of-type{border-bottom:1px solid var(--rule)}
details.fit > summary{display:grid;grid-template-columns:22px minmax(150px,220px) 50px 110px 1fr;
align-items:center;gap:12px;padding:13px 2px}
details.fit > summary:hover{background:rgba(255,255,255,.02)}
details.fit > summary .caret{color:var(--faint);transition:transform .15s;font-size:11px}
details.fit[open] > summary .caret{transform:rotate(90deg)}
details.fit .pname{font-weight:600;font-size:15px}
details.fit .what{color:var(--soft);font-size:13px}
details.fit .inner{padding:4px 0 22px 34px}
.bd td{vertical-align:top;font-size:13px}
.bd .ev{color:var(--soft);line-height:1.45}
.bd .pts{font-variant-numeric:tabular-nums;font-weight:600}
.bd .man{color:var(--amber);font-size:11.5px;margin-top:4px}
.bd form{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
.adj > summary{display:inline-block;font-size:12px;color:var(--faint);margin-top:5px;
text-decoration:underline;text-decoration-color:var(--rule2);text-underline-offset:3px}
.adj > summary:hover{color:var(--red-hi)}
.bd form select{min-width:0;max-width:270px;font-size:12.5px;padding:4px 7px}
.bd form input{min-width:0;width:170px;font-size:12.5px;padding:4px 7px}
.gline{font-size:12.5px;color:var(--soft);padding:2px 0}
.gline b{color:var(--ok);font-weight:600}
.gline b.x{color:var(--red-hi)}
.tp{padding:10px 0;border-bottom:1px solid var(--rule)}
.tp:last-child{border-bottom:0}
.tp .q{font:italic 14.5px/1.55 Georgia,serif;color:var(--ink)}
.reach{display:flex;gap:10px;align-items:baseline;padding:6px 0;font-size:13.5px;
border-bottom:1px solid var(--rule)}
.reach:last-child{border-bottom:0}
pre.prep{white-space:pre-wrap;background:var(--side);border-radius:8px;padding:12px;
font:12px/1.55 ui-monospace,Consolas,monospace;max-height:280px;overflow:auto;margin:10px 0 0}
.inputs{display:grid;grid-template-columns:repeat(4,1fr);gap:18px;margin:8px 0 10px}
.inputs .n{font:600 18px "Segoe UI",sans-serif;font-variant-numeric:tabular-nums}
.inputs .l{font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.07em}
"""


def _pretty_name(filed: str) -> str:
    """Schedule A files names as 'LAST, FIRST, MIDDLE'. People read the other
    order."""
    parts = [p.strip() for p in filed.split(",") if p.strip()]
    if len(parts) >= 2:
        return " ".join(parts[1:] + parts[:1]).title()
    return filed.title()


def _yearpos(iso: str) -> float:
    y, m, d = int(iso[:4]), int(iso[5:7] or 1), int(iso[8:10] or 1)
    return y + (m - 1) / 12 + (d - 1) / 365


def aum_chart(history) -> str:
    """Regulatory AUM over time as an inline SVG with real axes: dollar
    gridlines, a year scale, and every filing as a hoverable point."""
    if len(history) < 2:
        return ('<p class="muted small">Only one filing on record, so there is no '
                'trajectory to draw yet. The weekly feed adds a point whenever the '
                'firm files.</p>')
    vals = [h["raum"] for h in history]
    xs = [_yearpos(h["filing_date"]) for h in history]
    lo, hi = min(vals), max(vals)
    x0, x1 = xs[0], xs[-1]
    W, H, PADL, PADR, PADT, PADB = 680, 150, 8, 74, 10, 22
    rngy = (hi - lo) or 1
    rngx = (x1 - x0) or 1

    def X(x): return PADL + (x - x0) / rngx * (W - PADL - PADR)
    def Y(v): return PADT + (1 - (v - lo) / rngy) * (H - PADT - PADB)

    pts = " ".join(f"{X(x):.1f},{Y(v):.1f}" for x, v in zip(xs, vals))
    area = (f"{X(xs[0]):.1f},{H-PADB} " + pts + f" {X(xs[-1]):.1f},{H-PADB}")
    grid = []
    for v in (lo, (lo + hi) / 2, hi):
        y = Y(v)
        grid.append(f'<line x1="{PADL}" y1="{y:.1f}" x2="{W-PADR}" y2="{y:.1f}" '
                    f'stroke="var(--rule)" stroke-width="1"/>'
                    f'<text x="{W-PADR+6}" y="{y+3.5:.1f}" fill="var(--faint)" '
                    f'font-size="10.5" font-family="Segoe UI">{money(v)}</text>')
    step = max(1, round(rngx / 6))
    ticks = []
    yr = int(x0) + (1 if x0 % 1 > 0.5 else 0)
    while yr <= x1:
        if yr >= x0:
            tx = X(yr)
            ticks.append(f'<line x1="{tx:.1f}" y1="{H-PADB}" x2="{tx:.1f}" '
                         f'y2="{H-PADB+4}" stroke="var(--rule2)" stroke-width="1"/>'
                         f'<text x="{tx:.1f}" y="{H-6}" fill="var(--faint)" '
                         f'font-size="10.5" font-family="Segoe UI" '
                         f'text-anchor="middle">{yr}</text>')
        yr += step
    dots = "".join(
        f'<circle cx="{X(x):.1f}" cy="{Y(v):.1f}" r="2.6" fill="var(--ok)" '
        f'opacity=".85"><title>{esc(h["filing_date"])}: {money(v)}</title></circle>'
        for x, v, h in zip(xs, vals, history))
    growth = ""
    if vals[0]:
        pct = (vals[-1] - vals[0]) / vals[0] * 100
        growth = (f' &middot; {pct:+.0f}% over the span'
                  if abs(pct) >= 1 else " &middot; roughly flat")
    return (
        f'<div class="meta" style="margin-bottom:6px">{len(history)} filings from '
        f'{esc(history[0]["filing_date"][:4])} to {esc(history[-1]["filing_date"][:4])}. '
        f'Now {money(vals[-1])}{growth}. Hover a point for its filing.</div>'
        f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:{H}px;display:block">'
        f'{"".join(grid)}{"".join(ticks)}'
        f'<polygon points="{area}" fill="var(--ok)" opacity=".07"/>'
        f'<polyline points="{pts}" fill="none" stroke="var(--ok)" '
        f'stroke-width="1.8"/>{dots}'
        f'<circle cx="{X(xs[-1]):.1f}" cy="{Y(vals[-1]):.1f}" r="3.6" '
        f'fill="var(--ok)"/></svg>')


COPY_JS = """
function copyPrep(){
  var t = document.getElementById('prep').innerText;
  var done = function(){
    var c = document.getElementById('copied');
    c.textContent = 'Copied';
    setTimeout(function(){ c.textContent = ''; }, 2000);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(t).then(done, fallback);
  } else { fallback(); }
  function fallback(){
    var r = document.createRange();
    r.selectNode(document.getElementById('prep'));
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(r);
    try { document.execCommand('copy'); done(); } catch (e) {}
    window.getSelection().removeAllRanges();
  }
}
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


def _fit_section(crd: str, results: dict, ranks: dict, focus: str) -> str:
    """One expandable row per product, best first; the focused one open."""
    order = sorted(results.values(), key=lambda r: (
        r.product != focus, {"scored": 0, "disqualified": 1, "gated": 2}[r.status],
        -r.score))
    out = []
    for i, r in enumerate(order):
        p = products.product(r.product)
        is_open = r.product == focus or (not focus and i == 0 and r.status == "scored")
        if r.status == "scored":
            rank = ranks.get(r.product)
            head = (f'{tier_chip(r.tier)}<span class="score {r.tier if r.tier in ("A", "B", "C") else ""}">'
                    f'<span>{r.score:.0f}</span><span class="b"><i style="width:{r.score:.0f}%">'
                    f'</i></span></span><span class="what">{esc(r.action)}'
                    f'{f" &middot; #{rank:,} on the list" if rank else ""}'
                    f'{" &middot; " + esc(r.pitch) if r.pitch else ""}</span>')
        elif r.status == "disqualified":
            head = (f'<span class="chip dis">removed</span><span></span>'
                    f'<span class="what">{esc(r.reason)}</span>')
        else:
            head = (f'<span class="tier">-</span><span class="muted small">not eligible</span>'
                    f'<span class="what">{esc(r.reason)}</span>')
        inner = _breakdown(crd, r) if r.status == "scored" else _gates_html(r)
        out.append(
            f'<details class="fit" id="fit-{r.product}"{" open" if is_open else ""}>'
            f'<summary><span class="caret">&#9654;</span>'
            f'<span class="pname">{esc(p["name"])}</span>{head}</summary>'
            f'<div class="inner">{inner}</div></details>')
    return "".join(out)


def _gates_html(r) -> str:
    lines = "".join(
        f'<div class="gline"><b class="{"" if g["passed"] else "x"}">'
        f'{"Pass" if g["passed"] else ("Removed" if g.get("disqualifier") else "Fails")}</b> '
        f'{esc(g["label"])}: <span class="muted">{esc(g["evidence"])}</span></div>'
        for g in r.gates)
    return lines or '<p class="muted small">No gate information.</p>'


def _breakdown(crd: str, r) -> str:
    p = products.product(r.product)
    crit_cfg = {c["key"]: c for c in p["criteria"]}
    rows = []
    for comp in r.components:
        cfg = crit_cfg[comp["key"]]
        man = ""
        form = ""
        if comp["manual"]:
            ov = comp["override"]
            if ov:
                man = (f'<div class="man">Set by {esc(ov["by"] or "someone")} on '
                       f'{esc(ov["at"])}{": " + esc(ov["note"]) if ov["note"] else ""}. '
                       f'Filings alone: {ov["computed"]:.0f} ({esc(ov["computed_evidence"])})</div>')
            opts = "".join(
                f'<option value="{pts}"{" selected" if ov and float(pts) == comp["points"] else ""}>'
                f'{int(pts)}: {esc(lbl)}</option>' for pts, lbl in cfg["levels"])
            first = ('<option value="">Use the filings</option>' if ov else
                     '<option value="">Set a level from what you know</option>')
            form = (f'<details class="adj"{" open" if False else ""}><summary>'
                    f'{"Change" if ov else "Adjust"}</summary>'
                    f'<form method="post" action="/firm/{esc(crd)}/level">'
                    f'<input type="hidden" name="product" value="{esc(r.product)}">'
                    f'<input type="hidden" name="criterion" value="{esc(comp["key"])}">'
                    f'<select name="points" title="Set this level from what you know">'
                    f'{first}{opts}</select>'
                    f'<input type="text" name="note" placeholder="Why (optional)" '
                    f'value="{esc(ov["note"]) if ov else ""}">'
                    f'<button class="sm" type="submit">Save</button></form></details>')
        rows.append(
            f'<tr><td style="width:24%"><b>{esc(comp["label"])}</b>{man}</td>'
            f'<td class="ev">{esc(comp["evidence"])}'
            f'<div class="meta">Level: {esc(comp["level"])}</div>{form}</td>'
            f'<td class="num pts" style="width:62px">{comp["points"]:.0f}</td>'
            f'<td class="num muted" style="width:56px">{comp["weight"]}%</td>'
            f'<td class="num pts" style="width:62px">+{comp["contrib"]:.1f}</td></tr>')
    for pen in r.penalties:
        rows.append(f'<tr><td><b class="bad">{esc(pen["label"])}</b></td>'
                    f'<td class="ev">{esc(pen["evidence"])}</td><td></td><td></td>'
                    f'<td class="num pts bad">-{pen["points"]}</td></tr>')
    rows.append(f'<tr><td colspan="4" class="num muted">Score</td>'
                f'<td class="num pts" style="font-size:16px">{r.score:.1f}</td></tr>')
    gates = "".join(
        f'<div class="gline"><b>Pass</b> {esc(g["label"])}: '
        f'<span class="muted">{esc(g["evidence"])}</span></div>' for g in r.gates)
    sig = ""
    if r.signals:
        sig = ('<h3 style="margin-top:16px">Talking points and flags</h3>' + "".join(
            f'<div class="gline"><span class="chip">{esc(s["label"])}</span> '
            f'{esc(s["text"])}</div>' for s in r.signals[:8]))
    rules = "".join(f'<div class="note plain small">{esc(x)}</div>'
                    for x in p.get("rules", []))
    return (f'<table class="bd"><thead><tr><th>Criterion</th><th>Evidence</th>'
            f'<th class="num">Points</th><th class="num">Weight</th>'
            f'<th class="num">Adds</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
            f'<h3 style="margin-top:16px">Gates passed</h3>{gates}{sig}{rules}'
            f'<p class="small muted" style="margin-top:10px">'
            f'<a href="/lists/{r.product}?view=rules">How {esc(p["name"])} is scored</a></p>')


def call_prep(f, results, trigs, points, reach_lines, officers) -> str:
    """Plain text, paste-ready. Carries the reasons, not just the scores."""
    L = [f"{nice_name(f['legal_name'])}  (CRD {f['crd']})"]
    if f["business_name"] and f["business_name"] != f["legal_name"]:
        L.append(f"dba {f['business_name']}")
    L.append(" ".join(x for x in (f["city"], f["state"], f["website"]) if x))
    hs = (f["hnw_aum"] or 0) / f["raum"] * 100 if f["raum"] else 0
    L.append(f"{money(f['raum'])} AUM, {f['iar_count'] or 0} advisors, "
             f"{f['hnw_clients'] or 0} HNW clients ({hs:.0f}% of assets)")
    for r in sorted(results.values(), key=lambda r: -r.score):
        if r.status != "scored":
            continue
        p = products.product(r.product)
        L.append("")
        L.append(f"{p['name']}: tier {r.tier}, score {r.score:.0f} ({r.action})")
        if r.pitch:
            L.append(f"  Angle: {r.pitch}")
        for c in r.top_reasons(3):
            if c["contrib"] > 0:
                L.append(f"  {c['label']}: {c['evidence']}")
    if trigs:
        L.append("")
        L.append("What changed:")
        for t in trigs[:5]:
            L.append(f"  {t['detected_date']}  {t['description']}")
    if points:
        L.append("")
        L.append("In their own words:")
        for tp in points[:6]:
            L.append(f"  {tp['label']}: \"{tp['text']}\"")
    if reach_lines:
        L.append("")
        L.append("How to reach them:")
        L.extend(reach_lines)
    if officers:
        L.append("")
        L.append("Who runs it (Schedule A):")
        for o in officers[:6]:
            L.append(f"  {_pretty_name(o['name'])}: {o['title'] or ''}")
    return "\n".join(L)


@router.get("/firm/{crd}", response_class=HTMLResponse)
def firm_detail(crd: str, p: str = Query(""), saved: str = Query("")):
    c = conn()
    f = c.execute("SELECT * FROM firm_current WHERE crd=?", (crd,)).fetchone()
    if f is None:
        c.close()
        return page("Not found", "firms",
                    f'<div class="pg"><h1>No firm with CRD {esc(crd)}</h1>'
                    f'<p class="lede"><a href="/firms">Search firms</a></p></div>',
                    status=404)

    feats = products.load_features(c, [crd])
    d = feats[crd]
    results = products.evaluate_all(d)
    ranks = {r["product"]: r["rank"] for r in c.execute(
        "SELECT product, rank FROM product_score WHERE crd=? AND status='scored'", (crd,))}

    def rows(sql, args=()):
        try:
            return c.execute(sql, args).fetchall()
        except Exception:
            c.rollback()
            return []

    trigs = rows("""SELECT t.*, a.state FROM trigger_event t
        LEFT JOIN trigger_action a ON a.trigger_id = t.id
        WHERE t.crd = ? AND t.suppressed=0 ORDER BY t.detected_date DESC""", (crd,))
    funds = rows("""
        WITH latest AS (SELECT MAX(fc.filing_date) d FROM sched_d_7b1 s
          JOIN filing_crd fc ON fc.filing_id = s.filing_id WHERE s.crd = ?)
        SELECT s.fund_type, s.fund_name, s.gross_asset_value, s.owners,
               s.minimum_investment, fc.filing_date d FROM sched_d_7b1 s
        JOIN filing_crd fc ON fc.filing_id = s.filing_id, latest
        WHERE s.crd = ? AND fc.filing_date = latest.d
        ORDER BY s.gross_asset_value DESC NULLS LAST""", (crd, crd))
    note = rows("SELECT note FROM firm_note WHERE crd=?", (crd,))
    fs = rows("SELECT * FROM firm_status WHERE crd=?", (crd,))
    fs = fs[0] if fs else None
    officers = rows("""SELECT * FROM schedule_a WHERE crd=? AND is_individual=1
                       ORDER BY (control_person!='Y'), name LIMIT 20""", (crd,))
    reps = rows("SELECT * FROM contact WHERE crd=? ORDER BY title LIMIT 14", (crd,))
    emails = rows("SELECT * FROM contact_email WHERE crd=? ORDER BY status, email", (crd,))
    filed_info = rows("SELECT * FROM firm_contact_info WHERE crd=? ORDER BY kind, id", (crd,))
    web_people = rows("""SELECT * FROM web_contact WHERE crd=? AND person IS NOT NULL
                         ORDER BY id LIMIT 20""", (crd,))
    web_firm = rows("""SELECT * FROM web_contact WHERE crd=? AND person IS NULL
                       ORDER BY (email IS NULL), id LIMIT 6""", (crd,))
    history = rows("""SELECT filing_date, raum FROM firm_history
                      WHERE crd=? AND raum IS NOT NULL ORDER BY filing_date""", (crd,))
    watched = bool(rows("SELECT 1 FROM firm_watch WHERE crd=?", (crd,)))
    bro = rows("SELECT * FROM brochure WHERE crd=?", (crd,))
    bro = bro[0] if bro else None
    match = rows("SELECT * FROM adv_13f_match WHERE crd=? ORDER BY confidence DESC LIMIT 1",
                 (crd,))
    match = match[0] if match else None
    in_lists = rows("""SELECT u.id, u.name FROM user_list u JOIN user_list_item i
                       ON i.list_id=u.id WHERE i.crd=? ORDER BY u.name""", (crd,))
    all_lists = rows("SELECT id, name FROM user_list ORDER BY name")
    c.close()

    focus = p if p in products.product_keys() else ""
    hs = (f["hnw_aum"] or 0) / f["raum"] * 100 if f["raum"] else 0

    # ---- why now
    trow = []
    for t in trigs[:30]:
        kind = products.trigger_products().get(t["trigger_type"], {}).get("kind")
        chip = "dis" if kind == "disqualifier" else "lead"
        old = (" " + caveat("archive_as_of", "archive")
               if t["detected_date"] < "2025-01-01" else "")
        trow.append(f'<tr><td style="white-space:nowrap">{esc(t["detected_date"])}{old}</td>'
                    f'<td><span class="chip {chip}">'
                    f'{esc(TYPE_LABEL.get(t["trigger_type"], t["trigger_type"]))}</span></td>'
                    f'<td class="why">{esc(t["description"])}</td>'
                    f'<td>{esc(t["state"] or "")}</td></tr>')
    trig_html = (f'<table><tbody>{"".join(trow)}</tbody></table>' if trow else
                 '<p class="muted">Nothing has changed at this firm since tracking began: '
                 'no assets jump, no custodian move, no advisors added. Steady is a '
                 'finding, not missing data.</p>')

    # ---- talking points: the firm's own words, grouped by product family
    points = []
    for fam in ("phh", "acubooth", "glynac"):
        points += [dict(tp, fam=fam) for tp in products.talking_points(d, fam)]
    fam_name = {"phh": "PHH", "acubooth": "AcuBooth", "glynac": "Glynac"}
    tp_html = "".join(
        f'<div class="tp"><span class="chip">{esc(fam_name[tp["fam"]])}</span> '
        f'<b class="small">{esc(tp["label"])}</b>'
        f'<div class="q">{"&ldquo;" + esc(tp["text"]) + "&rdquo;" if tp["kind"] == "brochure" else esc(tp["text"])}</div></div>'
        for tp in points)
    if not tp_html:
        tp_html = ('<p class="muted">' + (
            "The brochure does not use any of the product vocabulary (covered calls, "
            "alternatives, real estate, 1031, reporting platforms)."
            if bro and bro["status"] == "ok" else
            "Brochure not read yet; the brochure job on System works through the "
            "product lists best first.") + "</p>")
    if bro:
        tp_html += (f'<p class="meta" style="margin-top:8px">From the Part 2A brochure '
                    f'{esc(bro["brochure_name"] or "")} filed {esc(bro["date_submitted"] or "?")}. '
                    f'Deterministic phrase matching, no model calls.</p>')

    # ---- reach and people
    def _key(v: str) -> str:
        v = (v or "").strip().lower()
        return re.sub(r"\D", "", v) if "@" not in v else v

    reach, seen, reach_lines = [], set(), []
    if f["phone"]:
        seen.add(_key(f["phone"]))
        reach.append(f'<div class="reach"><span class="chip lead">filed</span>'
                     f'<b>{esc(f["phone"])}</b><span class="meta">main office, Form ADV</span></div>')
        reach_lines.append(f"  {f['phone']} (main office, Form ADV)")
    for r in filed_info:
        if _key(r["value"]) in seen:
            continue
        seen.add(_key(r["value"]))
        val = (f'<a href="mailto:{esc(r["value"])}">{esc(r["value"])}</a>'
               if r["kind"] == "email" else f'<b>{esc(r["value"])}</b>')
        reach.append(f'<div class="reach"><span class="chip lead">filed</span>{val}'
                     f'<span class="meta">printed in their brochure</span></div>')
        reach_lines.append(f"  {r['value']} ({r['kind']}, their brochure)")
    for r in web_firm:
        val = r["email"] or r["phone"]
        if not val or _key(val) in seen:
            continue
        seen.add(_key(val))
        shown = (f'<a href="mailto:{esc(val)}">{esc(val)}</a>' if r["email"]
                 else f"<b>{esc(val)}</b>")
        reach.append(f'<div class="reach"><span class="chip">their site</span>{shown}'
                     f'<span class="meta">from their own website</span></div>')
        reach_lines.append(f"  {val} (their website)")
    reach_html = "".join(reach) or '<p class="muted">No phone or email on file yet.</p>'

    web_by_name = {}
    for w in web_people:
        web_by_name.setdefault(w["person"].lower(), w)
        bits = " / ".join(x for x in (w["email"], w["phone"]) if x)
        if bits:
            reach_lines.append(f"  {bits} ({w['person']}, their website)")

    def person_extra(name: str) -> str:
        w = web_by_name.get(name.lower())
        if not w:
            return ""
        bits = []
        if w["email"]:
            bits.append(f'<a href="mailto:{esc(w["email"])}">{esc(w["email"])}</a>')
        if w["phone"]:
            bits.append(esc(w["phone"]))
        return (f'<div class="meta">{" &middot; ".join(bits)} '
                f'<span class="chip lead">their site</span></div>') if bits else ""

    prow = "".join(
        f'<tr><td><b>{esc(_pretty_name(o["name"]))}</b>{person_extra(_pretty_name(o["name"]))}</td>'
        f'<td class="soft small">{esc(nice_name(o["title"] or ""))}'
        f'{" &middot; control person" if o["control_person"] == "Y" else ""}'
        f'<div class="meta">Schedule A, as of {esc(o["as_of"] or "archive")}</div></td></tr>'
        for o in officers)
    officer_names = {_pretty_name(o["name"]).lower() for o in officers}
    prow += "".join(
        f'<tr><td><b>{esc(r["name"])}</b>{person_extra(r["name"])}</td>'
        f'<td class="soft small">{esc(r["title"] or "")}</td></tr>'
        for r in reps if r["name"].lower() not in officer_names)
    erows = ""
    if emails:
        chipfor = {"domain_accepts_mail": "lead", "no_mail_server": "dis",
                   "bad_syntax": "dis", "queued": "warn"}
        label = {"domain_accepts_mail": "domain ok", "no_mail_server": "dead domain",
                 "bad_syntax": "malformed", "queued": "unchecked", "candidate": "guess"}
        erows = ('<h3 style="margin-top:16px">Guessed addresses</h3>' + "".join(
            f'<div class="reach"><span class="chip {chipfor.get(e["status"], "")}">'
            f'{esc(label.get(e["status"], e["status"]))}</span>'
            f'<a href="mailto:{esc(e["email"])}">{esc(e["email"])}</a>'
            f'<span class="meta">{esc(e["name"] or "")}</span></div>' for e in emails))
    people_html = (
        (f'<table><tbody>{prow}</tbody></table>' if prow else
         '<p class="muted">Nobody on file yet: no Schedule A roster (state-registered '
         'firms are not in the SEC archive) and no reps in the individual feed.</p>')
        + erows +
        f'<form method="post" action="/firm/{esc(crd)}/emails" style="margin-top:12px">'
        f'<button type="submit" class="sm" title="One best-guess email per officer, '
        f'checked against the domain with a free DNS lookup">Guess emails for these people'
        f'</button></form>'
        f'<p class="meta">A guess uses the pattern the firm uses for its own people. '
        f'<b>domain ok</b> means the domain takes mail, not that the mailbox exists.</p>')

    # ---- profile
    x = d["extra"] or {}
    mail = d["mail"] or {}
    plats = products.platform_evidence(d)
    mkt = [lab for key, lab in (("ad_performance", "performance results"),
                                ("ad_testimonials", "testimonials"),
                                ("ad_endorsements", "endorsements"),
                                ("ad_ratings", "third-party ratings"),
                                ("ad_hypothetical", "hypothetical performance"),
                                ("ad_predecessor", "predecessor performance")) if x.get(key)]
    socials = json.loads(x.get("social_hosts") or "[]") if x else []
    svc = [lab for key, lab in (("svc_financial_planning", "financial planning"),
                                ("svc_portfolio_individuals", "portfolio management for individuals"),
                                ("svc_portfolio_institutions", "institutional portfolios"),
                                ("svc_pooled_vehicles", "pooled vehicles"),
                                ("svc_selects_advisers", "selects other advisers")) if x.get(key)]
    cust = d["cust"] or {}
    schwab = "-"
    if cust.get("schwab_share_reported") is not None:
        schwab = (caveat("schwab_share_reported",
                         f'{cust["schwab_share_reported"] * 100:.0f}% of reported')
                  + f' <span class="meta">as of {esc(cust.get("as_of_filing_date"))}</span>')
    mail_s = {"m365": "Microsoft 365", "google": "Google Workspace", "other": "Other provider",
              "none": "No mail server", "no_domain": "No domain on file",
              "unknown": "Not identifiable"}.get(mail.get("platform"), "Not checked yet")
    est = money((f["raum"] or 0) / f["clients_total"]) if f["clients_total"] else "-"
    profile = f"""<dl class="kv">
<dt>High net worth</dt><dd>{f['hnw_clients'] or 0} clients &middot; {money(f['hnw_aum'])} &middot; <b>{hs:.0f}%</b> of assets</dd>
<dt>Other individuals</dt><dd>{f['retail_clients'] or 0} clients &middot; {money(f['retail_aum'])}</dd>
<dt>Average client</dt><dd>{caveat('est_avg_client_size', est)} across {f['clients_total'] or 0} clients</dd>
<dt>Services</dt><dd>{esc(", ".join(svc)) or "-"}</dd>
<dt>Custodian</dt><dd>{esc(cust.get('primary_canonical') or '-')}{f", {cust.get('reported_custodians')} reported" if cust.get('reported_custodians') else ""}</dd>
<dt>Schwab share</dt><dd>{schwab}</dd>
<dt>Email platform</dt><dd>{esc(mail_s)}{f' <span class="meta">{esc(mail.get("evidence"))}</span>' if mail.get("evidence") else ""}</dd>
<dt>Reporting platform</dt><dd>{esc(", ".join(plats)) or "Not found"}</dd>
<dt>Advertising (Item 5.L)</dt><dd>{esc(", ".join(mkt)) or ("None reported" if x else "-")}</dd>
<dt>Social media</dt><dd>{esc(", ".join(socials)) or "None listed"}</dd>
<dt>Files 13F</dt><dd>{"Yes" + (f' <span class="meta">CIK {esc(match["cik"])}, link confidence {match["confidence"]:.2f}</span>' if match else "") if d["files_13f"] else "No"}</dd>
<dt>Registration</dt><dd>{esc(f['firm_type'] or '')}, since {esc(f['registered_date'] or '-')}; last filed {esc(f['filing_date'] or '-')}</dd>
<dt>Disclosures</dt><dd>{"<span class='bad'>Discloses a disciplinary event (Item 11)</span>" if f["disciplinary"] == "Y" else "None reported"}</dd>
</dl>"""

    # ---- funds and real estate
    if funds:
        frows = "".join(
            f'<tr><td>{esc(z["fund_type"])}</td><td>{esc(z["fund_name"])}</td>'
            f'<td class="num">{money(z["gross_asset_value"])}</td><td class="num">{z["owners"] or 0}</td>'
            f'<td class="num">{money(z["minimum_investment"])}</td>'
            f'<td class="meta">{esc(z["d"])}</td></tr>' for z in funds)
        funds_html = ('<table><thead><tr><th>Type</th><th>Fund</th><th class="num">Assets</th>'
                      '<th class="num">Investors</th><th class="num">Minimum</th>'
                      f'<th>As of</th></tr></thead><tbody>{frows}</tbody></table>')
    else:
        funds_html = ('<p class="muted">No private funds on Schedule D. That is typical: '
                      'most advisers this size run none.</p>')
    seg = d["seg"]
    seg_html = ""
    if seg:
        seg_html = (
            '<div class="inputs">'
            f'<div><div class="n">{money(seg["total_gav"])}</div><div class="l">Real estate fund assets</div></div>'
            f'<div><div class="n">{(seg["raum_ratio"] or 0) * 100:.1f}%</div><div class="l">Share of firm assets</div></div>'
            f'<div><div class="n">{seg["total_owners"] or 0}</div><div class="l">Investors</div></div>'
            f'<div><div class="n">{money(seg["min_investment"])}</div><div class="l">Minimum</div></div></div>'
            f'<p class="why"><b>{esc(seg["segment"].capitalize())}</b>: {esc(seg["rationale"])} '
            f'{caveat("archive_as_of", "archive")} as of {esc(seg["as_of_filing_date"])}</p>')

    # ---- rail
    cur_status = fs["status"] if fs else ""
    status_opts = "".join(
        f'<option value="{esc(s)}"{" selected" if s == cur_status else ""}>'
        f'{esc(s.capitalize() if s else "Not set")}</option>' for s in [""] + STATUSES)
    lists_chips = " ".join(f'<a class="chip" href="/firms?list={l["id"]}">{esc(l["name"])}</a>'
                           for l in in_lists)
    listopts = "".join(f'<option value="{l["id"]}">{esc(l["name"])}</option>'
                       for l in all_lists if l["id"] not in {x["id"] for x in in_lists})
    prep = call_prep(f, results, trigs, [tp for tp in points if tp["kind"] == "brochure"],
                     reach_lines, officers)
    star = "&#9733; Watching" if watched else "&#9734; Watch"
    website = (f'<a href="{esc(f["website"] if f["website"].lower().startswith("http") else "https://" + f["website"])}" '
               f'target="_blank" rel="noopener">{esc(f["website"].lower().replace("https://", "").replace("http://", "").rstrip("/"))}</a>') if f["website"] else ""
    crumb = (f'<a href="/lists/{focus}">{esc(products.product(focus)["name"])}</a>'
             if focus else '<a href="/firms">Firms</a>')
    saved_note = ('<div class="note plain" style="margin:0 0 14px">Saved. The scores below '
                  'already reflect it.</div>' if saved else "")
    scored_n = sum(1 for r in results.values() if r.status == "scored")

    body = f"""<div class="pg">
<div class="crumb"><a href="/">Home</a> / {crumb}</div>
<div class="head"><div><h1>{escn(f['legal_name'])}</h1>
<div class="sub-line">{escn(f['business_name']) + " &middot; " if f['business_name'] and f['business_name'] != f['legal_name'] else ""}
{esc(" ".join(z for z in (nice_name(f['city']), f['state']) if z))} &middot; {money(f['raum'])} AUM &middot;
CRD {esc(crd)}{" &middot; " + website if website else ""}{" &middot; " + esc(f["phone"]) if f["phone"] else ""}</div></div>
<div class="acts">
<form method="post" action="/watch/{esc(crd)}"><input type="hidden" name="back" value="/firm/{esc(crd)}">
<button type="submit" class="{"primary" if watched else ""}">{star}</button></form>
<button type="button" class="primary" onclick="copyPrep()">Copy call prep</button>
<span id="copied" class="ok small"></span></div></div>
{saved_note}
<div class="doc"><div>
<section class="s" id="fit"><div class="s-head"><h2>Fit</h2>
<span class="more">On {scored_n} of {len(results)} product lists. Open a product for the full reasoning.</span></div>
{_fit_section(crd, results, ranks, focus)}</section>
<section class="s" id="now"><div class="s-head"><h2>Why now</h2></div>{trig_html}</section>
<section class="s" id="words"><div class="s-head"><h2>In their own words</h2>
<span class="more">Brochure language and holdings to open with</span></div>{tp_html}</section>
<section class="s" id="people"><div class="s-head"><h2>People and how to reach them</h2></div>
{reach_html}<div style="margin-top:14px">{people_html}</div></section>
<section class="s" id="profile"><div class="s-head"><h2>Profile</h2></div>{profile}</section>
<section class="s" id="aum"><div class="s-head"><h2>Assets over time</h2></div>{aum_chart(history)}</section>
<section class="s" id="funds"><div class="s-head"><h2>Private funds</h2></div>{funds_html}{seg_html}</section>
</div>
<aside class="rail">
<div class="panel"><div class="facts">
<div><div class="n">{money(f['raum'])}</div><div class="l">Assets</div></div>
<div><div class="n">{hs:.0f}%</div><div class="l">High net worth</div></div>
<div><div class="n">{f['iar_count'] or 0}</div><div class="l">Advisors</div></div>
<div><div class="n">{f['hnw_clients'] or 0}</div><div class="l">HNW clients</div></div>
</div></div>
<div class="panel"><h3>Status and owner</h3>
<form method="post" action="/firm/{esc(crd)}/status">
<label>Status<select name="status">{status_opts}</select></label>
<label>Owner<input type="text" name="owner" value="{esc(fs['owner'] if fs else '')}" placeholder="Leave blank to claim it yourself"></label>
<button class="primary sm" type="submit">Save</button></form>
<p class="meta">Meetings and customers raise the relationship points on every list.</p></div>
<div class="panel"><h3>Notes</h3>
<form method="post" action="/firm/{esc(crd)}/note">
<textarea name="note" placeholder="Call notes, context, next step">{esc(note[0]['note'] if note else '')}</textarea>
<button class="sm" type="submit" style="margin-top:8px">Save note</button></form></div>
<div class="panel"><h3>Saved lists</h3>{lists_chips or '<span class="muted small">Not on any list</span>'}
<form method="post" action="/firms/addtolist" style="margin-top:10px">
<input type="hidden" name="crd" value="{esc(crd)}"><input type="hidden" name="back" value="/firm/{esc(crd)}">
<input type="hidden" name="new_name" value="">
<select name="list_id" onchange="addToList(this)"><option value="">Add to a list</option>{listopts}
<option value="__new">New list...</option></select></form></div>
<div class="panel"><h3>Call prep</h3><pre class="prep" id="prep">{esc(prep)}</pre></div>
</aside></div></div>"""
    return page(nice_name(f["legal_name"]) or crd, f"list:{focus}" if focus else "firms", body,
                FIRM_CSS, js=COPY_JS)


@router.post("/firm/{crd}/level")
def set_level(crd: str, product: str = Form(...), criterion: str = Form(...),
              points: str = Form(""), note: str = Form("")):
    if product not in products.product_keys() or not CRD_RE.match(crd):
        return RedirectResponse("/", status_code=303)
    c = conn()
    try:
        products.set_override(c, crd, product, criterion,
                              float(points) if points.strip() else None,
                              note.strip()[:200], current_owner())
    except (ValueError, KeyError):
        pass
    c.close()
    return RedirectResponse(f"/firm/{crd}?p={product}&saved=1#fit-{product}",
                            status_code=303)


@router.post("/firm/{crd}/note")
def save_note(crd: str, note: str = Form("")):
    if not CRD_RE.match(crd):
        return RedirectResponse("/", status_code=303)
    c = conn()
    c.execute("INSERT INTO firm_note (crd,note,updated_at) VALUES (?,?,?)"
              " ON CONFLICT(crd) DO UPDATE SET note=excluded.note,"
              " updated_at=excluded.updated_at",
              (crd, note, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    c.commit()
    c.close()
    return RedirectResponse(f"/firm/{crd}?saved=1", status_code=303)


@router.post("/firm/{crd}/status")
def save_status(crd: str, status: str = Form(""), owner: str = Form("")):
    if not CRD_RE.match(crd):
        return RedirectResponse("/", status_code=303)
    # Claiming a firm without naming an owner means you.
    if status and not owner.strip():
        owner = current_owner()
    c = conn()
    c.execute("INSERT INTO firm_status (crd,status,owner,updated_at) VALUES (?,?,?,?)"
              " ON CONFLICT(crd) DO UPDATE SET status=excluded.status,"
              " owner=excluded.owner, updated_at=excluded.updated_at",
              (crd, status or None, owner or None,
               datetime.now(timezone.utc).isoformat(timespec="seconds")))
    c.commit()
    # Status feeds the relationship criteria, so the lists move with it.
    products.rescore_firm(c, crd)
    c.close()
    return RedirectResponse(f"/firm/{crd}?saved=1", status_code=303)


from prospect.mailcheck import BAD_EMAIL_DOMAINS  # noqa: E402,F401  single source


@router.post("/firm/{crd}/emails")
def gen_emails(crd: str):
    """One best-guess email per decision maker and rep, checked on the spot
    with a local DNS lookup, so the verdict is on the page when it reloads."""
    if not CRD_RE.match(crd):
        return RedirectResponse("/", status_code=303)
    from prospect import emailguess, mailcheck
    c = conn()
    firm_pat, fallback = emailguess.observed(c)
    domain = emailguess.domain_for(c, crd)
    c.execute("DELETE FROM contact_email WHERE crd=? AND pattern IS NOT NULL"
              " AND pattern != 'filed'", (crd,))
    people = []
    for r in c.execute("""SELECT name, title FROM schedule_a
                          WHERE crd=? AND is_individual=1 LIMIT 12""", (crd,)):
        people.append((emailguess.pretty(r["name"]), r["title"]))
    for r in c.execute("SELECT name, title FROM contact WHERE crd=? LIMIT 8", (crd,)):
        people.append((r["name"], r["title"]))
    mx = mailcheck.has_mx(domain) if domain else False
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seen = set()
    for full, title in people:
        np = emailguess.name_parts(full)
        if not np or np in seen:
            continue
        seen.add(np)
        guess = emailguess.best_email(full, crd, firm_pat, fallback, domain)
        if not guess:
            continue
        addr, label = guess
        if not mailcheck.valid_syntax(addr):
            status = "bad_syntax"
        elif mx is True:
            status = "domain_accepts_mail"
        elif mx is False:
            status = "no_mail_server"
        else:
            status = "queued"
        c.execute("INSERT OR IGNORE INTO contact_email"
                  " (crd,name,title,email,pattern,status,checked_at)"
                  " VALUES (?,?,?,?,?,?,?)", (crd, full, title, addr, label, status, now))
    c.commit()
    c.close()
    return RedirectResponse(f"/firm/{crd}#people", status_code=303)
