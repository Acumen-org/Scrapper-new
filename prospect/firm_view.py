"""The firm dossier: everything Bellwether knows about one adviser firm.

One page, read top to bottom or jumped through with the section bar that
stays at the top:

  Overview     who they are in numbers, the AI brief, and the three things
               that matter most right now
  Fit          each product's score with how much of it rests on known data,
               every factor's evidence, what is missing, and manual levels
  People       everyone registered there, officers first, with each person's
               role, tenure, prior firm, designations, and every email and
               direct line found, each with how sure we are of it
  Hiring       who joined and who left, from where and to where, year by year
  Contacts     the firm's own lines and inboxes, where every detail came from,
               and buttons to check emails or re-read the website now
  Signals      everything that changed, newest first
  Assets       assets over time, the client mix, services
  Investments  custodians, private funds, 13F holdings, the firm's own words
  Technology   email platform, reporting platform, website, social
  Compliance   who runs compliance, disclosures, registration

The rail holds what you act on: status and owner, notes, saved lists, and
Bellwether AI scoped to this firm.

Rules this page enforces:
  1. A score is always shown with its inputs and its coverage; missing data is
     marked as missing, never folded into the number.
  2. A person's judgement is labelled as one. A manual level shows who set it,
     when, and what the filings alone would have said.
  3. Every contact detail says where it came from and whether a mail server
     confirmed it. A guess is always called a guess.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from . import ai, contacts, products, roles, ui
from .webapp import (STATUSES, TYPE_LABEL, caveat, conn, current_account, current_owner, esc,
                     escn, missing_chip, money, nice_name, page, score_cell)

router = APIRouter()

# CRDs are digits. Checked before a CRD goes into a redirect or a write, so a
# crafted path can never become part of a Location header or a stored row.
CRD_RE = re.compile(r"^[0-9]{1,12}$")

SOURCE_LABEL = {"adv": "Form ADV", "brochure": "their brochure", "website": "their website",
                "vcard": "a vCard on their site", "directory": "a directory",
                "pattern": "the firm's email pattern", "ai": "AI reading of their site",
                "manual": "added by hand"}


def _pretty_name(filed: str) -> str:
    """Schedule A files names as 'LAST, FIRST, MIDDLE'. People read the other
    order."""
    parts = [p.strip() for p in (filed or "").split(",") if p.strip()]
    if len(parts) >= 2:
        return " ".join(parts[1:] + parts[:1]).title()
    return (filed or "").title()


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
    W, H, PADL, PADR, PADT, PADB = 680, 160, 8, 74, 10, 22
    rngy = (hi - lo) or 1
    rngx = (x1 - x0) or 1

    def X(x): return PADL + (x - x0) / rngx * (W - PADL - PADR)
    def Y(v): return PADT + (1 - (v - lo) / rngy) * (H - PADT - PADB)

    pts = " ".join(f"{X(x):.1f},{Y(v):.1f}" for x, v in zip(xs, vals))
    area = (f"{X(xs[0]):.1f},{H-PADB} " + pts + f" {X(xs[-1]):.1f},{H-PADB}")
    grid = []
    for v in (lo, (lo + hi) / 2, hi):
        y = Y(v)
        grid.append(f'<line class="grid" x1="{PADL}" y1="{y:.1f}" x2="{W-PADR}" y2="{y:.1f}" '
                    f'stroke-width="1"/><text x="{W-PADR+6}" y="{y+3.5:.1f}">{money(v)}</text>')
    step = max(1, round(rngx / 6))
    ticks = []
    yr = int(x0) + (1 if x0 % 1 > 0.5 else 0)
    while yr <= x1:
        if yr >= x0:
            tx = X(yr)
            ticks.append(f'<text x="{tx:.1f}" y="{H-6}" text-anchor="middle">{yr}</text>')
        yr += step
    dots = "".join(
        f'<circle cx="{X(x):.1f}" cy="{Y(v):.1f}" r="2.6" fill="var(--ok)" '
        f'opacity=".85"><title>{esc(h["filing_date"])}: {money(v)}</title></circle>'
        for x, v, h in zip(xs, vals, history))
    growth = ""
    if vals[0]:
        pct = (vals[-1] - vals[0]) / vals[0] * 100
        growth = (f' &middot; {pct:+.0f}% over the span' if abs(pct) >= 1 else " &middot; roughly flat")
    return (
        f'<div class="meta" style="margin-bottom:6px">{len(history)} filings from '
        f'{esc(history[0]["filing_date"][:4])} to {esc(history[-1]["filing_date"][:4])}. '
        f'Now {money(vals[-1])}{growth}. Hover a point for its filing.</div>'
        f'<svg class="chart" viewBox="0 0 {W} {H}" style="width:100%;height:{H}px;display:block">'
        f'{"".join(grid)}{"".join(ticks)}'
        f'<polygon points="{area}" fill="var(--ok)" opacity=".07"/>'
        f'<polyline points="{pts}" fill="none" stroke="var(--ok)" stroke-width="1.8"/>{dots}'
        f'<circle cx="{X(xs[-1]):.1f}" cy="{Y(vals[-1]):.1f}" r="3.6" fill="var(--ok)"/></svg>')


FIRM_JS = """
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


# ------------------------------------------------------------------ fit

def _fit_section(crd: str, results: dict, ranks: dict, focus: str) -> str:
    """One expandable row per product, best first; the focused one open."""
    order = sorted(results.values(), key=lambda r: (
        r.product != focus, {"scored": 0, "disqualified": 1, "gated": 2}[r.status], -r.score))
    out = []
    for i, r in enumerate(order):
        p = products.product(r.product)
        is_open = r.product == focus or (not focus and i == 0 and r.status == "scored")
        if r.status == "scored":
            rank = ranks.get(r.product)
            head = (f'{score_cell(r.score, r.coverage, r.potential)}'
                    f'<span class="small soft">{f"#{rank:,} on the list" if rank else ""}'
                    f'{" &middot; " + esc(r.pitch) if r.pitch else ""}'
                    f'<span style="margin-left:8px">{missing_chip("|".join(r.missing), 2)}</span></span>')
        elif r.status == "disqualified":
            head = (f'<span class="chip dis">removed</span>'
                    f'<span class="small soft">{esc(r.reason)}</span>')
        else:
            head = (f'<span class="muted small">not eligible</span>'
                    f'<span class="small soft">{esc(r.reason)}</span>')
        inner = _breakdown(crd, r) if r.status == "scored" else _gates_html(r)
        out.append(
            f'<details class="fit" id="fit-{r.product}"{" open" if is_open else ""}>'
            f'<summary><div class="fitrow"><span class="caret">&#9654;</span>'
            f'<span class="pname">{esc(p["name"])}</span>{head}</div></summary>'
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
                f'{int(pts)}: {esc(lbl)}</option>' for pts, lbl in cfg.get("levels", []))
            first = ('<option value="">Use the filings</option>' if ov else
                     '<option value="">Set a level from what you know</option>')
            form = (f'<details class="adj"><summary>{"Change" if ov else "Set it yourself"}</summary>'
                    f'<form method="post" action="/firm/{esc(crd)}/level">'
                    f'<input type="hidden" name="product" value="{esc(r.product)}">'
                    f'<input type="hidden" name="criterion" value="{esc(comp["key"])}">'
                    f'<select name="points">{first}{opts}</select>'
                    f'<input type="text" name="note" placeholder="Why (optional)" '
                    f'value="{esc(ov["note"]) if ov else ""}">'
                    f'<button class="sm" type="submit">Save</button></form></details>')
        known = comp.get("known", True)
        level = (f'<div class="meta">Level: {esc(comp["level"])}</div>' if known else
                 '<div class="meta warnc">Missing data: earns nothing until it is found</div>')
        pts = f'{comp["points"]:.0f}' if known else '<span class="warnc">?</span>'
        rows.append(
            f'<tr class="{"" if known else "unk"}"><td style="width:24%"><b>{esc(comp["label"])}</b>{man}</td>'
            f'<td class="ev">{esc(comp["evidence"])}{level}{form}</td>'
            f'<td class="num pts" style="width:62px">{pts}</td>'
            f'<td class="num muted" style="width:56px">{comp["weight"]:g}%</td>'
            f'<td class="num pts" style="width:62px">+{comp["contrib"]:.1f}</td></tr>')
    for pen in r.penalties:
        rows.append(f'<tr><td><b class="bad">{esc(pen["label"])}</b></td>'
                    f'<td class="ev">{esc(pen["evidence"])}</td><td></td><td></td>'
                    f'<td class="num pts bad">-{pen["points"]}</td></tr>')
    rows.append(f'<tr><td></td><td class="num muted">Score, on {r.coverage:.0f}% known data'
                f'{f" (could reach {r.potential:.0f})" if r.coverage < 99.5 else ""}</td><td></td><td></td>'
                f'<td class="num pts" style="font-size:16px">{r.score:.1f}</td></tr>')
    gates = "".join(
        f'<div class="gline"><b>Pass</b> {esc(g["label"])}: '
        f'<span class="muted">{esc(g["evidence"])}</span></div>' for g in r.gates)
    sig = ""
    if r.signals:
        sig = ('<h3 style="margin-top:16px">Talking points and flags</h3>' + "".join(
            f'<div class="gline"><span class="chip">{esc(s["label"])}</span> '
            f'{esc(s["text"])}</div>' for s in r.signals[:8]))
    rules = "".join(f'<div class="note plain small">{esc(x)}</div>' for x in p.get("rules", []))
    return (f'<table class="bd"><thead><tr><th>Factor</th><th>Evidence</th>'
            f'<th class="num">Points</th><th class="num">Weight</th>'
            f'<th class="num">Adds</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
            f'<h3 style="margin-top:16px">Gates passed</h3>{gates}{sig}{rules}'
            f'<p class="small muted" style="margin-top:10px">'
            f'<a href="/lists/{r.product}?view=scoring">How {esc(p["name"])} is scored</a></p>')


# ------------------------------------------------------------------ people

def _contact_line(cp, show_verify=True) -> str:
    """One email or phone with where it came from and how sure we are."""
    src = SOURCE_LABEL.get(cp["source"], cp["source"])
    if cp["kind"] == "email":
        status = cp["verify_status"]
        label = contacts.VERIFY_LABEL.get(status, status)
        if cp["source"] == "pattern" and status in ("unverified", "queued"):
            label = "Guess"
        btn = ""
        if show_verify and status not in ("valid", "invalid", "no_mail_server"):
            btn = (f' <button class="sm ghost" data-post="/api/contact/{cp["id"]}/verify" '
                   f'data-busy="Checking" data-target="#vs{cp["id"]}">Verify</button>')
        role = ' <span class="muted">shared inbox</span>' if cp["is_role"] else ""
        return (f'<span class="ctline"><a href="mailto:{esc(cp["value"])}">{esc(cp["value"])}</a>'
                f'<span id="vs{cp["id"]}"><span class="chip v-{esc(status)}" title="From '
                f'{esc(src)}; confidence {cp["confidence"]}">{esc(label)}</span></span>{role}{btn}</span>')
    lab = {"direct": "direct", "mobile": "mobile", "office": "office", "main": "main",
           "toll_free": "toll free"}.get(cp["label"] or "", "")
    return (f'<span class="ctline"><span>{esc(cp["value"])}</span>'
            f'<span class="muted small" title="From {esc(src)}">{esc(lab or "phone")}</span></span>')


def _people_section(c, crd: str, roster: list, cps_by_person: dict, web_people: list,
                    unmatched: list, stats: dict | None) -> str:
    titles = roles.lookup(c, [p.get("title") for p in roster if p.get("title")]
                          + [w["title"] for w in web_people if w.get("title")])
    rows = []

    def sort_key(p):
        role = titles.get(p.get("title"), ("", roles.classify(p.get("title"))))[1] \
            if p.get("title") else "zz"
        return (roles.rank(role) if p.get("title") else 99, p.get("since") or "9999")

    for p in sorted(roster, key=sort_key):
        key = f"i:{p['indvl_pk']}"
        cps = cps_by_person.get(key, [])
        title = p.get("title") or ""
        clean, role = (titles.get(title) if title else None) or ("", "")
        des = ", ".join(str(d if isinstance(d, str) else d.get("name", "")) for d in
                        (p.get("designations") or [])[:3])
        exams = ", ".join(str(e if isinstance(e, str) else e.get("code", "")) for e in
                          (p.get("exams") or [])[:5])
        flag = (' <span class="chip dis" title="Has a disclosure on the SEC record">disclosure</span>'
                if p.get("has_disclosure") else "")
        link = (f' <a class="muted small" href="{esc(p["iapd_link"])}" target="_blank" '
                f'rel="noopener" data-noprefetch>IAPD</a>' if p.get("iapd_link") else "")
        reach = "".join(f"<div>{_contact_line(cp)}</div>" for cp in cps) or \
            '<span class="muted small">no email or direct line yet</span>'
        since = p.get("since") or ""
        rows.append(
            f'<tr class="person"><td style="width:28%"><div class="nm">{esc(p["name"])}{flag}{link}</div>'
            f'<div class="meta">{esc(clean or "Registered rep")}'
            f'{" &middot; " + esc(roles.ROLE_LABEL.get(role, "")) if role and role != "other" else ""}</div></td>'
            f'<td class="small" style="width:15%">{esc(since[:7])}'
            f'<div class="meta">{esc(ui.ago(since))}</div></td>'
            f'<td class="small" style="width:17%">{escn(p.get("prior_firm")) if p.get("prior_firm") else "<span class=muted>-</span>"}'
            f'<div class="meta">{esc(des or exams)}</div></td>'
            f'<td><div class="ct">{reach}</div></td></tr>')
    roster_html = ""
    if rows:
        roster_html = (f'<table><thead><tr><th>Person</th><th>At the firm since</th>'
                       f'<th>Before</th><th>How to reach them</th></tr></thead>'
                       f'<tbody>{"".join(rows)}</tbody></table>')
    extra = ""
    if web_people:
        wr = []
        for w in web_people:
            cps = cps_by_person.get(w["person_key"], [])
            clean = (titles.get(w.get("title")) or (roles.clean_title(w.get("title")), ""))[0] \
                if w.get("title") else ""
            wr.append(f'<tr class="person"><td style="width:28%"><div class="nm">{esc(w["person_name"])}</div>'
                      f'<div class="meta">{esc(clean)}</div></td>'
                      f'<td><div class="ct">{"".join(f"<div>{_contact_line(cp)}</div>" for cp in cps)}</div></td></tr>')
        extra = (f'<h3 style="margin-top:22px">Also named on their website or in directories</h3>'
                 f'<p class="meta">Not registered with the SEC as advisers: operations, client '
                 f'service and other staff are often here.</p>'
                 f'<table><tbody>{"".join(wr)}</tbody></table>')
    off = ""
    if unmatched:
        off = (f'<h3 style="margin-top:22px">Owners and officers on Schedule A</h3>' + "".join(
            f'<div class="gline"><b style="color:var(--ink)">{esc(_pretty_name(o["name"]))}</b> '
            f'<span class="muted">{esc(roles.clean_title(o.get("title")))}</span></div>' for o in unmatched))
    if not (roster_html or extra or off):
        return ('<p class="muted">Nobody on file yet. The SEC individual feed lists every '
                'registered rep; website and directory reading add everyone else.</p>')
    head = ""
    if stats:
        head = (f'<div class="strip" style="margin-bottom:10px">'
                f'<div class="k"><div class="n">{stats.get("headcount") or 0}</div><div class="l">Registered now</div></div>'
                f'<div class="k"><div class="n">{stats.get("cfp_count") or 0}</div><div class="l">CFPs</div></div>'
                f'<div class="k"><div class="n">{(stats.get("avg_tenure_years") or 0):.1f}</div><div class="l">Average years at the firm</div></div>'
                f'<div class="k"><div class="n">{stats.get("broker_dual_count") or 0}</div><div class="l">Also broker registered</div></div>'
                f'<div class="k"><div class="n">{stats.get("disclosure_count") or 0}</div><div class="l">With a disclosure</div></div></div>')
    return head + roster_html + off + extra


def _hiring_section(c, crd: str, stats, mv, series) -> str:
    if not stats and not mv.get("joined") and not mv.get("left"):
        return ('<p class="muted">Joins and departures come from the SEC individual feed and '
                'appear once it has loaded.</p>')
    out = ""
    if stats:
        d12 = (stats.get("hires_12m") or 0) - (stats.get("departures_12m") or 0)
        out += (f'<div class="strip"><div class="k"><div class="n ok">+{stats.get("hires_12m") or 0}</div>'
                f'<div class="l">Joined in 12 months</div></div>'
                f'<div class="k"><div class="n bad">{-(stats.get("departures_12m") or 0) or 0}</div>'
                f'<div class="l">Left in 12 months</div></div>'
                f'<div class="k"><div class="n">{d12:+d}</div><div class="l">Net change</div></div>'
                f'<div class="k"><div class="n">+{stats.get("hires_prev_12m") or 0}</div>'
                f'<div class="l">Joined the 12 months before</div></div></div>')
    if series:
        out += f'<div style="margin:14px 0 6px">{ui.year_bars(series)}</div>'

    def lst(items, kind):
        if not items:
            return '<p class="muted small">None in the last two years.</p>'
        word = "from" if kind == "joined" else "now at"
        return "".join(
            f'<div class="it"><div class="ic {"in" if kind == "joined" else "out"}">'
            f'{"+" if kind == "joined" else "-"}</div><div><b>{esc(m["name"])}</b>'
            + (f' <span class="muted">{word} '
               + (f'<a href="/firm/{esc(m["other_org_pk"])}">{escn(m["other_org_name"])}</a>'
                  if m.get("other_org_pk") and str(m["other_org_pk"]).isdigit() else escn(m.get("other_org_name")))
               + '</span>' if m.get("other_org_name") else "")
            + f'</div><div class="tiny muted nowrap">{esc((m.get("date") or "")[:10])}</div></div>'
            for m in items[:12])
    out += (f'<div class="cols-2" style="margin-top:14px"><div><h3>Joined</h3><div class="feed">'
            f'{lst(mv.get("joined"), "joined")}</div></div><div><h3>Left</h3><div class="feed">'
            f'{lst(mv.get("left"), "left")}</div></div></div>')
    if stats:
        srcs = _json(stats.get("top_sources"))
        dests = _json(stats.get("top_destinations"))
        if srcs or dests:
            def fl(items):
                return ", ".join(f'{escn(x.get("org_name"))} ({x.get("n")})' for x in items[:5]) or "-"
            bits = []
            if srcs:
                bits.append(f"<b>Hires came from</b> {fl(srcs)}.")
            if dests:
                bits.append(f"<b>Leavers went to</b> {fl(dests)}.")
            out += f'<p class="small soft" style="margin-top:12px">{" ".join(bits)}</p>'
    return out


def _json(v):
    try:
        return json.loads(v) if isinstance(v, str) else (v or [])
    except ValueError:
        return []


# ------------------------------------------------------------------ page

@router.get("/firm/{crd}", response_class=HTMLResponse)
def firm_detail(crd: str, p: str = Query(""), saved: str = Query("")):
    if not CRD_RE.match(crd):
        return RedirectResponse("/firms", status_code=303)
    c = conn()
    f = c.execute("SELECT * FROM firm_current WHERE crd=?", (crd,)).fetchone()
    if f is None:
        c.close()
        return page("Not found", "firms",
                    f'<div class="pg"><h1>No firm with CRD {esc(crd)}</h1>'
                    f'<p class="lede"><a href="/firms">Search firms</a></p></div>', status=404)

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
        ORDER BY s.gross_asset_value DESC NULLS LAST LIMIT 40""", (crd, crd))
    note = rows("SELECT note, updated_at FROM firm_note WHERE crd=?", (crd,))
    fs = rows("SELECT * FROM firm_status WHERE crd=?", (crd,))
    fs = fs[0] if fs else None
    cps = rows("""SELECT * FROM contact_point WHERE crd=?
                  ORDER BY (person_key=''), (kind='phone'), (verify_status='valid') DESC,
                           confidence DESC, id""", (crd,))
    history = rows("""SELECT filing_date, raum FROM firm_history
                      WHERE crd=? AND raum IS NOT NULL ORDER BY filing_date""", (crd,))
    watched = bool(rows("SELECT 1 FROM firm_watch WHERE crd=?", (crd,)))
    bro = rows("SELECT * FROM brochure WHERE crd=?", (crd,))
    bro = bro[0] if bro else None
    match = rows("SELECT * FROM adv_13f_match WHERE crd=? ORDER BY confidence DESC LIMIT 1", (crd,))
    match = match[0] if match else None
    in_lists = rows("""SELECT u.id, u.name FROM user_list u JOIN user_list_item i
                       ON i.list_id=u.id WHERE i.crd=? ORDER BY u.name""", (crd,))
    all_lists = rows("SELECT id, name FROM user_list ORDER BY name")
    web_state = rows("SELECT * FROM web_enrich_state WHERE crd=?", (crd,))
    web_state = web_state[0] if web_state else None
    web_pages = rows("SELECT COUNT(*) n FROM web_page WHERE crd=?", (crd,))
    web_pages = web_pages[0]["n"] if web_pages else 0
    brief = rows("SELECT content, model, created_at FROM ai_note WHERE crd=? AND kind='brief'", (crd,))
    brief = dict(brief[0]) if brief else None

    # People, from the SEC roster when it has loaded, else Schedule A alone.
    roster, stats, mv, series, unmatched = [], None, {"joined": [], "left": []}, [], []
    try:
        from . import people
        roster = people.roster(c, crd)
        stats = people.stats(c, crd)
        mv = people.movements(c, crd, days=730)
        series = people.headcount_series(c, crd, years=10)
        unmatched = people.officers_unmatched(c, crd)
    except Exception:
        c.rollback()
    if not roster and not unmatched:
        unmatched = [dict(r) for r in rows("""SELECT name, title FROM schedule_a WHERE crd=?
            AND is_individual=1 ORDER BY (control_person!='Y'), name LIMIT 30""", (crd,))]

    cps_by_person: dict = {}
    firm_level, web_people = [], []
    roster_keys = {f"i:{p['indvl_pk']}" for p in roster}
    seen_web = set()
    for cp in cps:
        if cp["person_key"]:
            cps_by_person.setdefault(cp["person_key"], []).append(cp)
            if cp["person_key"] not in roster_keys and cp["person_key"] not in seen_web:
                seen_web.add(cp["person_key"])
                web_people.append(dict(cp))
        else:
            firm_level.append(cp)

    focus = p if p in products.product_keys() else ""
    hs = (f["hnw_aum"] or 0) / f["raum"] * 100 if f["raum"] else 0
    scored = sorted([r for r in results.values() if r.status == "scored"], key=lambda r: -r.score)
    best = scored[0] if scored else None
    flags = ui.contact_flags(c, [crd])[crd]

    # ---- signals timeline
    trow = []
    for t in trigs[:40]:
        kind = products.trigger_products().get(t["trigger_type"], {}).get("kind")
        chip = "dis" if kind == "disqualifier" else "lead"
        old = (" " + caveat("archive_as_of", "archive") if t["detected_date"] < "2025-01-01" else "")
        trow.append(f'<div class="ev"><div class="nowrap">{esc(t["detected_date"])}{old}'
                    f'<div class="meta">{esc(ui.ago(t["detected_date"]))}</div></div>'
                    f'<div><span class="chip {chip}">{esc(TYPE_LABEL.get(t["trigger_type"], t["trigger_type"]))}</span></div>'
                    f'<div class="why">{esc(t["description"])}'
                    f'{" <span class=chip>" + esc(t["state"]) + "</span>" if t["state"] else ""}</div></div>')
    trig_html = (f'<div class="timeline">{"".join(trow)}</div>' if trow else
                 '<p class="muted">Nothing has changed at this firm since tracking began: no '
                 'assets jump, no custodian move, no advisors added. Steady is a finding, not '
                 'missing data.</p>')

    # ---- their own words
    points = []
    for fam in ("phh", "acubooth", "glynac"):
        points += [dict(tp, fam=fam) for tp in products.talking_points(d, fam)]
    fam_name = {"phh": "PHH", "acubooth": "AcuBooth", "glynac": "Glynac"}
    tp_html = "".join(
        f'<div class="tp"><span class="chip">{esc(fam_name[tp["fam"]])}</span> '
        f'<b class="small">{esc(tp["label"])}</b>'
        f'<div class="quote">{"&ldquo;" + esc(tp["text"]) + "&rdquo;" if tp["kind"] == "brochure" else esc(tp["text"])}</div></div>'
        for tp in points)
    if not tp_html:
        tp_html = ('<p class="muted">' + (
            "The brochure does not use any of the product vocabulary (covered calls, "
            "alternatives, real estate, 1031, reporting platforms)."
            if bro and bro["status"] == "ok" else
            "Brochure not read yet; the brochure job reads the product lists best first.") + "</p>")
    if bro:
        tp_html += (f'<p class="meta" style="margin-top:8px">From the Part 2A brochure '
                    f'{esc(bro["brochure_name"] or "")} filed {esc(bro["date_submitted"] or "?")}.</p>')

    # ---- profile facts
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
    website = ""
    if f["website"]:
        href = f["website"] if f["website"].lower().startswith("http") else "https://" + f["website"]
        website = (f'<a href="{esc(href)}" target="_blank" rel="noopener" data-noprefetch>'
                   f'{esc(f["website"].lower().replace("https://", "").replace("http://", "").rstrip("/"))}</a>')

    assets_html = f"""{aum_chart(history)}
<dl class="kv" style="margin-top:18px">
<dt>High net worth</dt><dd>{f['hnw_clients'] or 0} clients &middot; {money(f['hnw_aum'])} &middot; <b>{hs:.0f}%</b> of assets</dd>
<dt>Other individuals</dt><dd>{f['retail_clients'] or 0} clients &middot; {money(f['retail_aum'])}</dd>
<dt>All clients</dt><dd>{f['clients_total'] or 0} &middot; average {caveat('est_avg_client_size', est)}</dd>
<dt>Discretionary</dt><dd>{money(f['raum_disc'])} of {money(f['raum'])}</dd>
<dt>Services</dt><dd>{esc(", ".join(svc)) or "-"}</dd>
</dl>"""

    if funds:
        frows = "".join(
            f'<tr><td>{esc(z["fund_type"])}</td><td>{esc(z["fund_name"])}</td>'
            f'<td class="num">{money(z["gross_asset_value"])}</td><td class="num">{z["owners"] or 0}</td>'
            f'<td class="num">{money(z["minimum_investment"])}</td>'
            f'<td class="meta">{esc(z["d"])}</td></tr>' for z in funds)
        funds_html = ('<table class="tight"><thead><tr><th>Type</th><th>Fund</th><th class="num">Assets</th>'
                      '<th class="num">Investors</th><th class="num">Minimum</th>'
                      f'<th>As of</th></tr></thead><tbody>{frows}</tbody></table>')
    else:
        funds_html = ('<p class="muted">No private funds on Schedule D. That is typical: most '
                      'advisers this size run none.</p>')
    seg = d["seg"]
    seg_html = ""
    if seg:
        seg_html = (f'<p class="why" style="margin-top:12px"><b>{esc(seg["segment"].capitalize())}</b>: '
                    f'{esc(seg["rationale"])} {caveat("archive_as_of", "archive")} as of '
                    f'{esc(seg["as_of_filing_date"])}</p>')
    h13 = sorted(d["h13f"].items(), key=lambda kv: -(kv[1]["value"] or 0))
    h13_html = ""
    if h13:
        h13_html = ('<h3 style="margin-top:20px">13F holdings of interest</h3><table class="tight"><tbody>'
                    + "".join(f'<tr><td><b>{esc(tk)}</b></td><td class="num">{money(h["value"])}</td>'
                              f'<td class="meta">{esc(h["quarter"])}</td></tr>' for tk, h in h13[:12])
                    + "</tbody></table>")
    invest_html = f"""<dl class="kv">
<dt>Primary custodian</dt><dd>{esc(cust.get('primary_canonical') or '-')}{f", {cust.get('reported_custodians')} reported" if cust.get('reported_custodians') else ""}</dd>
<dt>Schwab share</dt><dd>{schwab}</dd>
<dt>Files 13F</dt><dd>{"Yes" + (f' <span class="meta">CIK {esc(match["cik"])}, link confidence {match["confidence"]:.2f}</span>' if match else "") if d["files_13f"] else "No"}</dd>
</dl><h3 style="margin-top:20px">Private funds</h3>{funds_html}{seg_html}{h13_html}
<h3 style="margin-top:22px">In their own words</h3>{tp_html}"""

    tech_html = f"""<dl class="kv">
<dt>Email platform</dt><dd>{esc(mail_s)}{f' <span class="meta">{esc(mail.get("evidence"))}</span>' if mail.get("evidence") else ""}</dd>
<dt>Reporting platform</dt><dd>{esc(", ".join(f"{k}" for k in plats)) or "Not found"}{''.join(f'<div class="meta">{esc(v)}</div>' for v in plats.values())}</dd>
<dt>Website</dt><dd>{website or "None on file"}{f'<div class="meta">{web_pages} pages read, last {esc(ui.ago(web_state["scanned_at"]))}</div>' if web_state else ""}</dd>
<dt>Publishes</dt><dd>{"Blog or newsletter found" if "publishes" in d["web"] else "Nothing found"}</dd>
<dt>Advertising (Item 5.L)</dt><dd>{esc(", ".join(mkt)) or ("None reported" if x else "-")}</dd>
<dt>Social media</dt><dd>{esc(", ".join(socials)) or "None listed"}</dd>
</dl>"""

    cco = [o for o in d["officers"] if re.search(r"CHIEF COMPLIANCE|\bCCO\b", o["title"] or "", re.I)]
    comp_html = f"""<dl class="kv">
<dt>Compliance officer</dt><dd>{"; ".join(esc(_pretty_name(o["name"])) + " <span class=meta>" + esc(roles.clean_title(o["title"])) + "</span>" for o in cco) or "Not named on Schedule A"}</dd>
<dt>Firm disclosures</dt><dd>{"<span class='bad'>Discloses a disciplinary event (Item 11)</span>" if f["disciplinary"] == "Y" else "None reported"}</dd>
<dt>People with disclosures</dt><dd>{(stats or {}).get("disclosure_count") or 0} of {(stats or {}).get("headcount") or "?"} registered</dd>
<dt>Registration</dt><dd>{esc(f['firm_type'] or '')}, {esc(f['regulator'] or '')}, since {esc(f['registered_date'] or '-')}</dd>
<dt>Last ADV filed</dt><dd>{esc(f['filing_date'] or '-')} &middot; {d['filings_12m']} amendments in 12 months</dd>
<dt>SEC number</dt><dd>{esc(f['sec_number'] or '-')}</dd>
</dl>"""

    # ---- contacts (firm level and sources)
    fl_rows = "".join(f"<div class='reach' style='padding:7px 0;border-bottom:1px solid var(--rule)'>"
                      f"{_contact_line(cp)} <span class='meta'>from {esc(SOURCE_LABEL.get(cp['source'], cp['source']))}</span></div>"
                      for cp in firm_level)
    n_em = sum(1 for cp in cps if cp["kind"] == "email")
    srcs = {}
    for cp in cps:
        for s_ in (cp["sources"] or cp["source"]).split(","):
            srcs[s_] = srcs.get(s_, 0) + 1
    src_line = ", ".join(f"{n} from {SOURCE_LABEL.get(k, k)}" for k, n in sorted(srcs.items(), key=lambda kv: -kv[1]))
    contacts_html = f"""<div class="row" style="margin-bottom:12px">
<button class="sm" data-post="/api/firm/{esc(crd)}/verify" data-busy="Starting">Verify all emails</button>
<button class="sm" data-post="/api/firm/{esc(crd)}/crawl" data-busy="Starting">Re-read their website</button>
<form method="post" action="/firm/{esc(crd)}/emails" style="display:inline"><button class="sm ghost" type="submit"
 title="Build an address for each person from the pattern the firm uses">Fill in addresses from the firm&rsquo;s pattern</button></form>
</div>
{fl_rows or '<p class="muted">No firm-level phone or inbox on file.</p>'}
<p class="meta" style="margin-top:10px">{n_em} email address{"es" if n_em != 1 else ""} in all. {esc(src_line)}.
Verified means the firm&rsquo;s mail server accepted that exact mailbox and turned away a made-up one.</p>
<details class="adj" style="margin-top:10px"><summary>Add a contact you know</summary>
<form class="row" style="margin-top:8px" onsubmit="event.preventDefault();var b=this.querySelector('button');b.dataset.post='/api/firm/{esc(crd)}/contact';b.dataset.body=new URLSearchParams(new FormData(this)).toString();b.click();">
<input type="text" name="name" placeholder="Name"><input type="text" name="title" placeholder="Title">
<input type="email" name="email" placeholder="Email"><input type="text" name="phone" placeholder="Phone">
<button type="button" class="sm" data-busy="Saving">Save</button></form></details>
<details class="adj" style="margin-top:6px"><summary>Website is wrong or missing</summary>
<form class="row" style="margin-top:8px" onsubmit="event.preventDefault();var b=this.querySelector('button');b.dataset.post='/api/firm/{esc(crd)}/crawl';b.dataset.body=new URLSearchParams(new FormData(this)).toString();b.click();">
<input type="url" name="url" placeholder="https://their-site.com" style="min-width:260px">
<button type="button" class="sm" data-busy="Starting">Read this site</button></form></details>"""

    people_html = _people_section(c, crd, roster, cps_by_person, web_people,
                                  unmatched, stats)
    c.close()
    hiring_html = _hiring_section(None, crd, stats, mv, series)

    # ---- overview
    hl = []
    if best:
        hl.append(f'<div class="tp"><b>Best fit:</b> {esc(products.product(best.product)["name"])} '
                  f'at {best.score:.0f} on {best.coverage:.0f}% known data'
                  + (f'; the biggest unknown is {esc(best.missing[0].lower())}' if best.missing else "") + '.</div>')
    if trigs:
        hl.append(f'<div class="tp"><b>Latest change:</b> {esc(trigs[0]["description"])} '
                  f'<span class="muted">({esc(ui.ago(trigs[0]["detected_date"]))})</span></div>')
    if stats and (stats.get("hires_12m") or stats.get("departures_12m")):
        hl.append(f'<div class="tp"><b>Team:</b> {stats.get("hires_12m") or 0} joined and '
                  f'{stats.get("departures_12m") or 0} left in the last 12 months.</div>')
    reach_word = (f'{flags["verified"]} verified address{"es" if flags["verified"] != 1 else ""}'
                  if flags["verified"] else
                  f'{flags["personal"]} named people with an address' if flags["personal"] else
                  "no named contacts yet")
    hl.append(f'<div class="tp"><b>Reach:</b> {esc(reach_word)}'
              f'{", " + str(flags["direct"]) + " direct lines" if flags["direct"] else ""}.</div>')
    if ai.configured():
        if brief:
            from .api_view import brief_html
            brief_block = (f'<div id="brief">{brief_html(brief)}</div>'
                           f'<button class="sm ghost" data-post="/api/firm/{esc(crd)}/brief" '
                           f'data-busy="Writing" data-target="#brief">Rewrite the brief</button>')
        else:
            brief_block = (f'<div id="brief"><p class="muted">No brief yet.</p></div>'
                           f'<button class="sm" data-post="/api/firm/{esc(crd)}/brief" '
                           f'data-busy="Writing" data-target="#brief">Write an AI brief</button>')
    else:
        brief_block = ""
    overview_html = (f'<div class="cols-2"><div>{"".join(hl)}</div><div>{brief_block}</div></div>'
                     if brief_block else "".join(hl))

    # ---- rail
    cur_status = fs["status"] if fs else ""
    status_opts = "".join(
        f'<option value="{esc(s)}"{" selected" if s == cur_status else ""}>'
        f'{esc(s.capitalize() if s else "Not set")}</option>' for s in [""] + STATUSES)
    lists_chips = " ".join(f'<a class="chip" href="/firms?list={l["id"]}">{esc(l["name"])}</a>'
                           for l in in_lists)
    listopts = "".join(f'<option value="{l["id"]}">{esc(l["name"])}</option>'
                       for l in all_lists if l["id"] not in {x["id"] for x in in_lists})
    star = "&#9733; Watching" if watched else "&#9734; Watch"
    crumb = (f'<a href="/lists/{focus}">{esc(products.product(focus)["name"])}</a>'
             if focus else '<a href="/firms">Firms</a>')
    saved_note = ('<div class="note good" style="margin:0 0 14px">Saved. The scores below '
                  'already reflect it.</div>' if saved else "")
    sub_bits = [x for x in (escn(f["business_name"]) if f["business_name"] and f["business_name"] != f["legal_name"] else "",
                            esc(" ".join(z for z in (nice_name(f["city"]), f["state"]) if z)),
                            f"CRD {esc(crd)}", website, esc(f["phone"]) if f["phone"] else "") if x]
    tags = [f'<span class="chip line">{esc(f["regulator"] or "")}-registered</span>' if f["regulator"] else ""]
    for r in scored[:3]:
        tags.append(f'<a class="chip" href="#fit-{r.product}">{esc(products.product(r.product)["name"])} {r.score:.0f}</a>')
    if fs and fs["status"]:
        tags.append(f'<span class="chip warn">{esc(fs["status"])}{" . " + esc(fs["owner"]) if fs["owner"] else ""}</span>')
    if mail.get("platform") in ("m365", "google"):
        tags.append(f'<span class="chip line">{esc(mail_s)}</span>')
    for k in list(plats)[:1]:
        tags.append(f'<span class="chip line">{esc(k)}</span>')

    kp = [("Assets", money(f["raum"]), ui.spark([h["raum"] for h in history[-24:]]) if len(history) > 2 else ""),
          ("HNW share", f"{hs:.0f}%", f'{f["hnw_clients"] or 0} HNW clients'),
          ("People", str((stats or {}).get("headcount") or f["iar_count"] or 0),
           f'{(stats or {}).get("hires_12m") or 0} joined, {(stats or {}).get("departures_12m") or 0} left in 12m'
           if stats else f'{f["iar_count"] or 0} advisors (Item 5.B)'),
          ("Clients", f'{f["clients_total"] or 0:,}', f"average {est}"),
          ("Reach", f'{flags["personal"]}', f'named contacts, {flags["verified"]} verified')]
    kpis = "".join(f'<div class="kpi"><div class="n">{esc(v)}</div><div class="l">{esc(l)}</div>'
                   f'<div class="d">{s}</div></div>' for l, v, s in kp)

    nav_items = [("overview", "Overview", None), ("fit", "Fit", len(scored)),
                 ("people", "People", len(roster) or len(unmatched) or None),
                 ("hiring", "Hiring", None), ("contacts", "Contacts", n_em or None),
                 ("signals", "Signals", len(trigs) or None), ("assets", "Assets", None),
                 ("investments", "Investments", None), ("tech", "Technology", None),
                 ("compliance", "Compliance", None)]
    secnav = "".join(f'<a href="#{k}">{esc(lbl)}{f"<span class=cnt>{n}</span>" if n else ""}</a>'
                     for k, lbl, n in nav_items)

    ai_rail = f"""<div class="panel aipanel" data-scope="firm:{esc(crd)}">
<div class="aihead"><canvas data-orb="breathing" data-size="32" data-px="30" data-tint="#d9d4ca" aria-label="Bellwether AI"></canvas>
<div><div class="t">Ask about this firm</div><div class="s">Answers from everything on this page</div></div></div>
<div class="aisugs"><button type="button">Who should I contact first, and how?</button>
<button type="button">Which product fits best and why?</button>
<button type="button">What changed here recently?</button></div>
<div class="aimsgs"></div>
<form class="aiform"><input type="text" placeholder="Ask anything" autocomplete="off"><button class="sm primary" type="submit">Ask</button></form>
</div>"""

    body = f"""<div class="pg wide">
<div class="crumb"><a href="/">Home</a> / {crumb}</div>
<div class="hero"><div class="head" style="padding-bottom:0"><div><h1>{escn(f['legal_name'])}</h1>
<div class="sub">{" &middot; ".join(sub_bits)}</div></div>
<div class="acts">
<form method="post" action="/watch/{esc(crd)}"><input type="hidden" name="back" value="/firm/{esc(crd)}">
<button type="submit" class="{"primary" if watched else ""}">{star}</button></form>
<a class="btn" href="/ask?q={esc("Tell me about " + nice_name(f['legal_name']))}">Ask Bellwether</a></div></div>
<div class="tags">{"".join(t for t in tags if t)}</div></div>
{saved_note}
<div class="kpis" style="margin:8px 0 0">{kpis}</div>
<nav class="secnav">{secnav}</nav>
<div class="dossier"><div>
<section class="s anchor" id="overview">{overview_html}</section>
<section class="s anchor" id="fit"><div class="s-head"><h2>Fit</h2>
<span class="more">On {len(scored)} of {len(results)} lists. Hatched bars are data still missing.</span></div>
{_fit_section(crd, results, ranks, focus)}</section>
<section class="s anchor" id="people"><div class="s-head"><h2>People</h2>
<span class="more">SEC individual records, Schedule A, their website and directories</span></div>{people_html}</section>
<section class="s anchor" id="hiring"><div class="s-head"><h2>Hiring and departures</h2></div>{hiring_html}</section>
<section class="s anchor" id="contacts"><div class="s-head"><h2>Contacts and sources</h2></div>{contacts_html}</section>
<section class="s anchor" id="signals"><div class="s-head"><h2>Signals</h2></div>{trig_html}</section>
<section class="s anchor" id="assets"><div class="s-head"><h2>Assets and clients</h2></div>{assets_html}</section>
<section class="s anchor" id="investments"><div class="s-head"><h2>Investments</h2></div>{invest_html}</section>
<section class="s anchor" id="tech"><div class="s-head"><h2>Technology</h2></div>{tech_html}</section>
<section class="s anchor" id="compliance"><div class="s-head"><h2>Compliance and registration</h2></div>{comp_html}</section>
</div>
<aside class="rail">
<div class="panel"><h3>Status and owner</h3>
<form method="post" action="/firm/{esc(crd)}/status">
<label>Status<select name="status">{status_opts}</select></label>
<label>Owner<input type="text" name="owner" value="{esc(fs['owner'] if fs else '')}" placeholder="Leave blank to claim it yourself"></label>
<button class="primary sm" type="submit">Save</button></form>
<p class="meta">Meetings and customers raise the relationship points on every list.</p></div>
{ai_rail}
<div class="panel"><h3>Notes</h3>
<form method="post" action="/firm/{esc(crd)}/note">
<textarea name="note" placeholder="Call notes, context, next step">{esc(note[0]['note'] if note else '')}</textarea>
<button class="sm" type="submit" style="margin-top:8px">Save note</button>
{f'<span class="meta" style="margin-left:8px">saved {esc(ui.ago(note[0]["updated_at"]))}</span>' if note else ""}</form></div>
<div class="panel"><h3>Saved lists</h3>{lists_chips or '<span class="muted small">Not on any list</span>'}
<form method="post" action="/firms/addtolist" style="margin-top:10px">
<input type="hidden" name="crd" value="{esc(crd)}"><input type="hidden" name="back" value="/firm/{esc(crd)}">
<input type="hidden" name="new_name" value="">
<select name="list_id" onchange="addToList(this)" style="width:100%"><option value="">Add to a list</option>{listopts}
<option value="__new">New list...</option></select></form></div>
</aside></div></div>"""
    return page(nice_name(f["legal_name"]) or crd, f"list:{focus}" if focus else "firms", body,
                js=FIRM_JS, orbs=True)


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
    return RedirectResponse(f"/firm/{crd}?p={product}&saved=1#fit-{product}", status_code=303)


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


@router.post("/firm/{crd}/emails")
def gen_emails(crd: str):
    """A candidate address for each person at the firm who has none, from the
    pattern the firm uses (or the commonest pattern when it has published no
    personal address yet). Candidates are labelled as guesses until the
    verification job, or the Verify button, confirms them."""
    if not CRD_RE.match(crd):
        return RedirectResponse("/", status_code=303)
    from . import emailguess, mailcheck
    c = conn()
    firm_pat, fallback = emailguess.observed(c)
    domain = emailguess.domain_for(c, crd)
    if not domain or mailcheck.has_mx(domain) is False:
        c.close()
        return RedirectResponse(f"/firm/{crd}#contacts", status_code=303)
    have = {r["person_key"] for r in c.execute(
        "SELECT DISTINCT person_key FROM contact_point WHERE crd=? AND kind='email'"
        " AND person_key != ''", (crd,))}
    people_rows = []
    try:
        from . import people
        people_rows = [(p["name"], p.get("title"), f"i:{p['indvl_pk']}")
                       for p in people.roster(c, crd)]
    except Exception:
        c.rollback()
    for r in c.execute("SELECT name, title FROM schedule_a WHERE crd=? AND is_individual=1"
                       " LIMIT 30", (crd,)):
        full = emailguess.pretty(r["name"])
        people_rows.append((full, r["title"], contacts.name_key(full)))
    observed = crd in firm_pat
    for full, title, key in people_rows:
        if not key or key in have:
            continue
        guess = emailguess.best_email(full, crd, firm_pat, fallback, domain)
        if not guess:
            continue
        addr, label = guess
        if not mailcheck.valid_syntax(addr):
            continue
        have.add(key)
        contacts.upsert(c, crd, "email", addr, "pattern", person_key=key, person_name=full,
                        title=title, source_ref=label, confidence=60 if observed else 35,
                        is_role=False)
    c.commit()
    c.close()
    return RedirectResponse(f"/firm/{crd}#people", status_code=303)
