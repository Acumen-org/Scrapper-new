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

The Workspace tab holds status, owner, notes and saved lists. Bellwether AI
opens in a drawer scoped to this firm.

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
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from . import ai, contacts, products, roles, ui
from .webapp import (ICONS, STATUSES, TYPE_LABEL, caveat, conn, current_account, current_owner, esc,
                     escn, missing_chip, money, nice_name, page, score_cell)

router = APIRouter()

# CRDs are digits. Checked before a CRD goes into a redirect or a write, so a
# crafted path can never become part of a Location header or a stored row.
CRD_RE = re.compile(r"^[0-9]{1,12}$")

# Firm types that are not buyers for any product: shown in red on the badge.
OUT_TYPES = {"custodian", "wirehouse", "independent_bd", "bank_trust", "insurance"}

SOURCE_LABEL = ui.SOURCE_LABEL


def _pretty_name(filed: str) -> str:
    """Schedule A files names as 'LAST, FIRST, MIDDLE'. People read the other
    order."""
    from .names import person_name
    return person_name(filed)


def _yearpos(iso: str) -> float:
    y, m, d = int(iso[:4]), int(iso[5:7] or 1), int(iso[8:10] or 1)
    return y + (m - 1) / 12 + (d - 1) / 365


def aum_chart(history) -> str:
    """Regulatory AUM over time as an inline SVG with real axes: dollar
    gridlines, a year scale, and every filing as a hoverable point."""
    if len(history) < 2:
        return '<p class="muted small">At least two filings are needed to show asset history.</p>'
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
    """One email, phone or profile with where it came from and how sure we are."""
    src = SOURCE_LABEL.get(cp["source"], cp["source"])
    if cp["kind"] == "email":
        status = cp["verify_status"]
        label = contacts.VERIFY_LABEL.get(status, status)
        if status != "valid" and cp["source"] != "pattern":
            label = "Published"
        btn = ""
        if show_verify and status not in ("invalid", "no_mail_server", "valid"):
            btn = (f'<button class="sm ghost" data-post="/api/contact/{cp["id"]}/verify" '
                   f'data-busy="Checking" data-target=".verify-result-{cp["id"]}">Check</button>')
        role = ' <span class="muted small">shared inbox</span>' if cp["is_role"] else ""
        return (f'<div class="cline{" ok" if status == "valid" else ""}">{ICONS["mail"]}'
                f'<a href="mailto:{esc(cp["value"])}" title="{esc(cp["value"])}">{esc(cp["value"])}</a>{role}'
                f'<span id="vs{cp["id"]}" class="verify-result-{cp["id"]}"><span class="chip v-{esc(status)}" title="From {esc(src)};'
                f' confidence {cp["confidence"]}">{esc(label)}</span></span>{btn}</div>')
    if cp["kind"] == "linkedin":
        return (f'<div class="cline">{ICONS["linkedin"]}<a href="{esc(cp["value"])}" target="_blank" '
                f'rel="noopener" data-noprefetch>LinkedIn profile</a>'
                f'<span class="chip{" good" if cp["verify_status"] == "matched" else ""}" title="From {esc(src)}">'
                f'{"Matched" if cp["verify_status"] == "matched" else "Likely match"}</span></div>')
    lab = {"direct": "Direct", "mobile": "Mobile", "office": "Office", "main": "Main line",
           "toll_free": "Toll free"}.get(cp["label"] or "", "Phone")
    return (f'<div class="cline">{ICONS["phone"]}<a href="tel:{esc(cp["value"])}">{esc(cp["value"])}</a>'
            f'<span class="chip line" title="From {esc(src)}">{esc(lab)}</span></div>')


PEOPLE_CARDS_MAX = 60


def _person_card(person: dict, key: str, points: list, hunt: dict | None = None) -> str:
    name = person.get("name") or "Name unavailable"
    title = roles.clean_title(person.get("title") or "") or (
        "Officer" if person.get("is_officer") else "Registered representative")
    role = roles.classify(person.get("title") or "")
    chips = []
    if person.get("is_officer"):
        chips.append('<span class="chip">Officer</span>')
    for d in (person.get("designations") or [])[:3]:
        label = d if isinstance(d, str) else (d.get("name") or d.get("code") or "")
        if label and len(label) <= 12:
            chips.append(f'<span class="chip">{esc(label)}</span>')
    if person.get("has_disclosure"):
        chips.append('<span class="chip warn">Disclosure</span>')
    meta = []
    if person.get("since"):
        meta.append(f'Since {esc(person["since"][:7])}')
    if person.get("prior_firm"):
        pk = person.get("prior_firm_pk")
        prior = (f'<a href="/firm/{esc(pk)}">{escn(person["prior_firm"])}</a>'
                 if pk and str(pk).isdigit() else escn(person["prior_firm"]))
        meta.append(f"from {prior}")
    if person.get("branch_city"):
        meta.append(esc(" ".join(x for x in (nice_name(person["branch_city"]), person.get("branch_state") or "") if x)))
    # Best first: verified email, published email, direct line, office line, profile.
    order = {"email": 0, "phone": 1, "linkedin": 2}
    pts = sorted(points, key=lambda cp: (order.get(cp["kind"], 3), cp["verify_status"] != "valid",
                                         -(cp["confidence"] or 0)))
    lines = "".join(_contact_line(cp) for cp in pts[:5])
    if not any(cp["kind"] == "email" for cp in points):
        lines += _hunt_line(hunt)
    iapd = (f'<a class="more" href="{esc(person["iapd_link"])}" target="_blank" rel="noopener" '
            f'data-noprefetch>SEC record</a>' if person.get("iapd_link") else "")
    anchor = "p-" + re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")
    return (f'<article class="pcard" id="{anchor}"><div class="top">{ui.mono(name, "p")}<div>'
            f'<span class="nm">{esc(name)}</span><div class="ttl">{esc(title)}'
            f'</div><div class="meta">{" · ".join(meta)}</div></div>{iapd}</div>'
            f'{("<div class=pills>" + "".join(chips) + "</div>") if chips else ""}'
            f'<div class="reach">{lines}</div></article>')


# Hunt states that are settled for now: shown without the pulsing dot.
HUNT_SETTLED = {"exhausted", "accept_all_domain", "no_mail_server", "no_domain", "free_mail",
                "blocked", "unnamed", "unverifiable"}


def _hunt_line(hunt: dict | None) -> str:
    """Where the email hunt stands for one person, in words."""
    if not hunt:
        return ('<div class="hunt" title="Website, web search, every common pattern checked '
                'with the mail server, and AI research"><i></i>Finding a verified email</div>')
    tried = hunt.get("tried") or 0
    extra = f" . {tried} address{'es' if tried != 1 else ''} checked" if tried else ""
    when = ""
    if hunt.get("next_try_at") and hunt["state"] in HUNT_SETTLED:
        when = f" . trying again {ui.ago(hunt['next_try_at'])}"
    settled = hunt["state"] in HUNT_SETTLED
    return (f'<div class="hunt{" done" if settled else ""}" title="{esc(hunt.get("detail") or hunt["label"])}">'
            f'<i></i>{esc(hunt["label"])}{esc(extra)}{esc(when)}</div>')


def _people_section(c, crd: str, roster: list, cps_by_person: dict, web_people: list,
                    unmatched: list, stats: dict | None,
                    hunts: dict | None = None) -> tuple[str, list[str], dict]:
    """Every person as a card, the ones we can reach first. Returns the HTML,
    the top cards for the overview, and the counts for the header."""
    records, seen = [], set()
    for person in roster:
        records.append((person, f"i:{person['indvl_pk']}"))
    for person in web_people:
        records.append((dict(name=person["person_name"], title=person.get("title")), person["person_key"]))
    for person in unmatched:
        nm = _pretty_name(person["name"])
        records.append((dict(name=nm, title=person.get("title"), is_officer=True), contacts.name_key(nm)))
    uniq = []
    for person, key in records:
        if key in seen:
            continue
        seen.add(key)
        uniq.append((person, key))

    def rank(item):
        person, key = item
        pts = cps_by_person.get(key, [])
        has_mail = any(cp["kind"] == "email" for cp in pts)
        has_direct = any(cp["kind"] == "phone" and cp["label"] in ("direct", "mobile") for cp in pts)
        leader = roles.classify(person.get("title") or "") in ("leadership", "exec", "owner")
        return (not has_mail, not person.get("is_officer"), not leader, not has_direct,
                person.get("since") or "9999", person.get("name") or "")
    uniq.sort(key=rank)
    counts = {"people": len(uniq), "email": 0, "direct": 0, "linkedin": 0}
    for person, key in uniq:
        pts = cps_by_person.get(key, [])
        counts["email"] += any(cp["kind"] == "email" for cp in pts)
        counts["direct"] += any(cp["kind"] == "phone" and cp["label"] in ("direct", "mobile") for cp in pts)
        counts["linkedin"] += any(cp["kind"] == "linkedin" for cp in pts)
    hunts = hunts or {}
    cards = [_person_card(person, key, cps_by_person.get(key, []), hunts.get(key))
             for person, key in uniq[:PEOPLE_CARDS_MAX]]
    more = ""
    if len(uniq) > PEOPLE_CARDS_MAX:
        more = (f'<p class="small soft" style="margin-top:14px">Showing {PEOPLE_CARDS_MAX} of '
                f'{len(uniq):,}, reachable people first. <a href="/people?q={esc(crd)}">See everyone '
                f'at this firm in People</a>.</p>')
    if not cards:
        html = ('<div class="card empty"><b>No people on record yet</b>The SEC roster, Schedule A '
                'and the website reader fill this in; use Find contacts now to start.</div>')
    else:
        html = f'<div class="people-grid">{"".join(cards)}</div>{more}'
    return html, cards[:6], counts


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


def _hiring_glance(stats, series) -> str:
    """The overview's hiring card: the year's moves as three numbers, then the
    last decade as bars."""
    if not stats and not series:
        return '<p class="muted">Joins and departures appear once the SEC roster loads.</p>'
    out = ""
    if stats:
        net = (stats.get("hires_12m") or 0) - (stats.get("departures_12m") or 0)
        out = (f'<div class="facts" style="margin-bottom:14px"><div><div class="l">Joined, 12 months</div>'
               f'<div class="v ok">+{stats.get("hires_12m") or 0}</div></div>'
               f'<div><div class="l">Left, 12 months</div><div class="v bad">'
               f'{-(stats.get("departures_12m") or 0) or 0}</div></div>'
               f'<div><div class="l">Net</div><div class="v">{net:+d}</div></div></div>')
    if series:
        out += ui.year_bars(series[-8:], w=420, h=130)
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
    cps = rows("""SELECT * FROM usable_contact_point WHERE crd=?
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
    offices = rows("SELECT * FROM firm_office WHERE crd=? ORDER BY city LIMIT 60", (crd,))
    attempts = rows("SELECT COUNT(*) n, COUNT(DISTINCT person_key) p FROM email_attempt WHERE crd=?", (crd,))
    attempts = attempts[0] if attempts else None
    ftype = None
    try:
        from . import firmtype
        ftype = firmtype.get(c, crd)
    except Exception:
        c.rollback()

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

    # ---- signals timeline
    trow = []
    for t in trigs[:60]:
        kind = products.trigger_products().get(t["trigger_type"], {}).get("kind")
        chip = "dis" if kind == "disqualifier" else "lead"
        old = (" " + caveat("archive_as_of", "archive") if t["detected_date"] < "2025-01-01" else "")
        trow.append(f'<div class="ev"><div class="nowrap">{esc(t["detected_date"])}{old}'
                    f'<div class="meta">{esc(ui.ago(t["detected_date"]))}</div></div>'
                    f'<div><span class="chip {chip}">{esc(TYPE_LABEL.get(t["trigger_type"], t["trigger_type"]))}</span></div>'
                    f'<div class="why">{esc(t["description"])}'
                    f'{" <span class=chip>" + esc(t["state"]) + "</span>" if t["state"] else ""}</div></div>')
    trig_html = (f'<div class="timeline">{"".join(trow)}</div>' if trow else
                 '<p class="muted">No recorded signals yet. Bellwether watches every filing for changes.</p>')

    # ---- their own words
    points, seen_tp = [], set()
    for fam in ("phh", "acubooth", "glynac"):
        for tp in products.talking_points(d, fam):
            # One brochure sentence often supports several tags; show it once.
            if tp["text"] in seen_tp:
                continue
            seen_tp.add(tp["text"])
            points.append(dict(tp, fam=fam))
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
    refreshed = d.get('cust_refresh') or {}
    current_custody = 'Not checked yet'
    if refreshed.get('custodians'):
        current_custody = esc(', '.join(nice_name(x) for x in refreshed['custodians'].split('|')))
        at = refreshed.get('last_success_at') or (refreshed.get('fetched_at') if refreshed.get('status') == 'ok' else None)
        current_custody += f'<div class="meta">Current ADV, read {esc(ui.ago(at)) or "previously"}</div>'
    elif refreshed.get('status') == 'ok':
        current_custody = 'No custodian names found in the current ADV'
    if refreshed and refreshed.get('status') != 'ok':
        current_custody += '<div class="meta warnc">Latest refresh failed; retry scheduled.</div>'
    schwab = "-"
    if cust.get("schwab_share_reported") is not None:
        schwab = (caveat("schwab_share_reported",
                         f'{cust["schwab_share_reported"] * 100:.0f}% of reported')
                  + f' <span class="meta">as of {esc(cust.get("as_of_filing_date"))}</span>')
    mail_s = {"m365": "Microsoft 365", "google": "Google Workspace", "other": "Other provider",
              "none": "No mail server", "no_domain": "No domain on file",
              "unknown": "Not identifiable"}.get(mail.get("platform"), "Not checked yet")
    est = money((f["raum"] or 0) / f["clients_total"]) if f["clients_total"] else "-"
    website, site_host = "", ""
    if f["website"]:
        href = f["website"] if f["website"].lower().startswith("http") else "https://" + f["website"]
        site_host = f["website"].lower().replace("https://", "").replace("http://", "").rstrip("/")
        website = (f'<a href="{esc(href)}" target="_blank" rel="noopener" data-noprefetch>'
                   f'{esc(site_host)}</a>')
    firm_li = next((cp["value"] for cp in firm_level if cp["kind"] == "linkedin"), None)

    assets_html = f"""<div class="card"><div class="card-head"><h2>Assets over time</h2></div>{aum_chart(history)}</div>
<div class="facts" style="margin-top:12px">
<div><div class="l">High net worth clients</div><div class="v">{f['hnw_clients'] or 0:,} <small>{money(f['hnw_aum'])}, {hs:.0f}% of assets</small></div></div>
<div><div class="l">Other individuals</div><div class="v">{f['retail_clients'] or 0:,} <small>{money(f['retail_aum'])}</small></div></div>
<div><div class="l">All clients</div><div class="v">{f['clients_total'] or 0:,} <small>average {caveat('est_avg_client_size', est)}</small></div></div>
<div><div class="l">Discretionary</div><div class="v">{money(f['raum_disc'])} <small>of {money(f['raum'])}</small></div></div>
</div>
<dl class="kv" style="margin-top:18px"><dt>Services</dt><dd>{esc(", ".join(svc)) or "-"}</dd></dl>"""

    if funds:
        frows = "".join(
            f'<tr><td>{esc(z["fund_type"])}</td><td>{esc(z["fund_name"])}</td>'
            f'<td class="num">{money(z["gross_asset_value"])}</td><td class="num">{z["owners"] or 0}</td>'
            f'<td class="num">{money(z["minimum_investment"])}</td>'
            f'<td class="meta">{esc(z["d"])}</td></tr>' for z in funds)
        funds_html = ('<div class="table-scroll"><table class="tight"><thead><tr><th>Type</th><th>Fund</th><th class="num">Assets</th>'
                      '<th class="num">Investors</th><th class="num">Minimum</th>'
                      f'<th>As of</th></tr></thead><tbody>{frows}</tbody></table></div>')
    else:
        funds_html = '<p class="muted">No private funds recorded on Schedule D.</p>'
    seg = d["seg"]
    seg_html = ""
    if seg:
        seg_html = (f'<p class="why" style="margin-top:12px"><b>{esc(seg["segment"].capitalize())}</b>: '
                    f'{esc(seg["rationale"])} {caveat("archive_as_of", "archive")} as of '
                    f'{esc(seg["as_of_filing_date"])}</p>')
    h13 = sorted(d["h13f"].items(), key=lambda kv: -(kv[1]["value"] or 0))
    h13_html = ""
    if h13:
        h13_html = ('<h3 style="margin-top:22px">13F holdings of interest</h3><div class="table-scroll"><table class="tight"><tbody>'
                    + "".join(f'<tr><td><b>{esc(tk)}</b></td><td class="num">{money(h["value"])}</td>'
                              f'<td class="meta">{esc(h["quarter"])}</td></tr>' for tk, h in h13[:12])
                    + "</tbody></table></div>")
    invest_html = f"""<dl class="kv">
<dt>Custodians in latest ADV</dt><dd>{current_custody}</dd>
<dt>Primary custodian, archive</dt><dd>{esc(cust.get('primary_canonical') or '-')}{f", {cust.get('reported_custodians')} reported" if cust.get('reported_custodians') else ""}{f'<div class="meta">As of {esc(cust["as_of_filing_date"])}</div>' if cust.get('as_of_filing_date') else ''}</dd>
<dt>Schwab share</dt><dd>{schwab}</dd>
<dt>Files 13F</dt><dd>{"Yes" + (f' <span class="meta">CIK {esc(match["cik"])}, link confidence {match["confidence"]:.2f}</span>' if match else "") if d["files_13f"] else "No"}</dd>
</dl><h3 style="margin-top:22px">Private funds</h3>{funds_html}{seg_html}{h13_html}"""

    tech_html = f"""<dl class="kv">
<dt>Email platform</dt><dd>{esc(mail_s)}{f' <span class="meta">{esc(mail.get("evidence"))}</span>' if mail.get("evidence") else ""}</dd>
<dt>Reporting and CRM</dt><dd>{esc(", ".join(f"{k}" for k in plats)) or "Not found"}{''.join(f'<div class="meta">{esc(v)}</div>' for v in plats.values())}</dd>
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

    office_html = ""
    if offices:
        orows = "".join(
            f'<tr><td>{esc(nice_name(o.get("city") or ""))}{", " + esc(o["state"]) if o.get("state") else ""}</td>'
            f'<td class="meta">{esc(o.get("street") or "")}</td>'
            f'<td>{("<a href=" + chr(34) + "tel:" + esc(o["phone"]) + chr(34) + ">" + esc(o["phone"]) + "</a>") if o.get("phone") else "-"}</td></tr>'
            for o in offices)
        office_html = (f'<div class="card" style="margin-top:16px"><div class="card-head"><h2>Offices '
                       f'<span class="h-cnt">{len(offices)}</span></h2><span class="sub">From Form ADV '
                       f'Schedule D</span></div><table class="tight"><tbody>{orows}</tbody></table></div>')

    # ---- contacts: firm lines, sources, and the discovery engine's state
    fl_rows = "".join(_contact_line(cp) for cp in firm_level if cp["kind"] != "linkedin")
    n_em = sum(1 for cp in cps if cp["kind"] == "email")
    srcs = {}
    for cp in cps:
        for s_ in (cp["sources"] or cp["source"]).split(","):
            srcs[s_] = srcs.get(s_, 0) + 1
    src_line = ", ".join(f"{n} from {SOURCE_LABEL.get(k, k)}" for k, n in sorted(srcs.items(), key=lambda kv: -kv[1]))

    hunts = {}
    try:
        from . import hunt as _hunt
        hunts = _hunt.person_status(c, crd)
    except Exception:
        c.rollback()
    people_html, top_cards, pc = _people_section(c, crd, roster, cps_by_person, web_people,
                                                 unmatched, stats, hunts)
    c.close()
    hiring_html = _hiring_section(None, crd, stats, mv, series)
    hunting = max(0, pc["people"] - pc["email"])
    tried = (f'{attempts["n"]:,} candidate addresses checked with the mail server for '
             f'{attempts["p"]:,} people so far. ' if attempts and attempts["n"] else "")
    discovery_html = f"""<div class="contact-summary"><span><b>{pc["email"]:,} / {pc["people"]:,}</b> with email</span>
<span><b>{pc["direct"]:,}</b> direct lines</span><span><b>{pc["linkedin"]:,}</b> LinkedIn profiles</span>
<button class="primary sm" data-post="/api/firm/{esc(crd)}/hunt" data-busy="Starting">{ICONS["bolt"]}Find contacts now</button></div>
<details class="detail-section"><summary>Discovery, sources and contact tools</summary>
<p class="small soft">{hunting:,} people without email. {tried}Emails are labelled verified or published.</p>
<div>{fl_rows or '<p class="muted small">No firm-level phone or inbox on file.</p>'}</div>
<p class="small soft">{n_em} email address{"es" if n_em != 1 else ""}. {esc(src_line) or "No sources yet"}.</p>
<div class="row" style="margin-top:6px">
<button class="sm" data-post="/api/firm/{esc(crd)}/verify" data-busy="Starting">Re-check emails</button>
<button class="sm ghost" data-post="/api/firm/{esc(crd)}/crawl" data-busy="Starting">Re-read website</button></div>
<details class="adj" style="margin-top:12px"><summary>Add a contact you know</summary>
<form class="row" style="margin-top:10px" data-api-form action="/api/firm/{esc(crd)}/contact" method="post">
<label>Name<input type="text" name="name"></label><label>Title<input type="text" name="title"></label>
<label>Email<input type="email" name="email"></label><label>Phone<input type="text" name="phone"></label>
<button type="submit" class="sm" data-busy="Saving">Save contact</button></form></details>
<details class="adj" style="margin-top:6px"><summary>Website is wrong or missing</summary>
<form class="row" style="margin-top:10px" data-api-form action="/api/firm/{esc(crd)}/crawl" method="post">
<label>Website<input type="url" name="url" placeholder="https://their-site.com" required></label>
<button type="submit" class="sm" data-busy="Starting">Read website</button></form></details></details>"""

    # ---- identity
    profile_name = nice_name(f["business_name"] or f["legal_name"])
    place = ", ".join(x for x in (nice_name(f["city"] or ""), f["state"] or "") if x)
    tags = []
    if ftype and ftype.get("category"):
        core = bool(ftype.get("core"))
        ev = "; ".join(str(e) for e in (ftype.get("evidence") or [])[:3])
        tags.append(f'<span class="ftype {"core" if core else "out" if ftype["category"] in OUT_TYPES else ""}" title="{esc(ev)}"><i></i>'
                    f'{esc(ftype.get("label") or ftype["category"])}</span>')
    if f["is_era"]:
        tags.append('<span class="chip warn">Exempt reporting adviser</span>')
    cur_status = (fs["status"] or "") if fs else ""
    if cur_status:
        tags.append(f'<span class="chip lead">{esc(cur_status.capitalize())}'
                    f'{" . " + esc(fs["owner"]) if fs and fs["owner"] else ""}</span>')
    if trigs and trigs[0]["detected_date"] >= (datetime.now(timezone.utc).date().isoformat()[:4] + "-01-01"):
        tags.append(f'<a class="chip good" href="#activity">{len(trigs)} signal{"s" if len(trigs) != 1 else ""}</a>')
    growth = ""
    if len(history) >= 2 and history[0]["raum"]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).date().isoformat()
        yr_ago = [h for h in history if h["filing_date"] <= cutoff]
        base = (yr_ago[-1]["raum"] if yr_ago else history[0]["raum"]) or 0
        if base:
            pct = (history[-1]["raum"] - base) / base * 100
            growth = (f'<span class="{"up" if pct >= 0 else "dn"}">{pct:+.0f}%</span> '
                      f'{"in 12 months" if yr_ago else "since " + history[0]["filing_date"][:4]}')
    clients_s = f"{f['clients_total']:,}" if f["clients_total"] is not None else "-"
    team = (stats or {}).get("headcount", f["iar_count"])
    net = ((stats or {}).get("hires_12m") or 0) - ((stats or {}).get("departures_12m") or 0)
    stats_html = f"""<div class="stats">
<div><div class="l">Assets under management</div><div class="v">{money(f['raum'])}</div><div class="d">{growth or "&nbsp;"}</div></div>
<div><div class="l">Clients</div><div class="v">{clients_s}</div><div class="d">{hs:.0f}% of assets from high-net-worth clients</div></div>
<div><div class="l">Registered team</div><div class="v">{team if team is not None else '-'}</div><div class="d">{f'<span class="{"up" if net >= 0 else "dn"}">{net:+d}</span> net in 12 months' if stats else '&nbsp;'}</div></div>
</div>"""

    fit_cards = []
    for r in sorted(results.values(), key=lambda r: ({"scored": 0, "disqualified": 1, "gated": 2}[r.status], -r.score)):
        pn = esc(products.product(r.product)["name"])
        if r.status == "scored":
            rank = ranks.get(r.product)
            cov = f'{r.coverage:.0f}% known data'
            fit_cards.append(f'<a class="fit-summary" href="#fit-{r.product}"><span>{pn}'
                             f'<small class="{"warnc" if r.coverage < 99.5 else "muted"}">{cov}</small></span>'
                             f'<strong>{r.score:.0f}<small>/100</small></strong></a>')
        else:
            why = "Removed" if r.status == "disqualified" else "Not eligible"
            fit_cards.append(f'<a class="fit-summary" href="#fit-{r.product}"><span>{pn}</span>'
                             f'<span class="meta">{why}</span></a>')

    if ai.configured():
        if brief:
            from .api_view import brief_html
            brief_block = (f'<div id="brief">{brief_html(brief)}</div>'
                           f'<button class="sm ghost" data-post="/api/firm/{esc(crd)}/brief" '
                           f'data-busy="Writing" data-target="#brief">Rewrite</button>')
        else:
            brief_block = (f'<div id="brief"></div>'
                           f'<button class="sm" data-post="/api/firm/{esc(crd)}/brief" '
                           f'data-busy="Writing" data-target="#brief">{ICONS["spark"]}Write a brief</button>')
    else:
        brief_block = ""

    # ---- rail
    status_opts = "".join(
        f'<option value="{esc(s)}"{" selected" if s == cur_status else ""}>'
        f'{esc(s.capitalize() if s else "Not set")}</option>' for s in [""] + STATUSES)
    lists_chips = " ".join(f'<a class="chip" href="/firms?list={l["id"]}">{esc(l["name"])}</a>'
                           for l in in_lists)
    listopts = "".join(f'<option value="{l["id"]}">{esc(l["name"])}</option>'
                       for l in all_lists if l["id"] not in {x["id"] for x in in_lists})
    star = (ICONS["star"].replace('fill="none"', f'fill="{"currentColor" if watched else "none"}"')
            + ('Watching' if watched else 'Watch'))
    saved_note = ('<div class="note good" style="margin:0 0 14px">Saved.</div>' if saved else "")
    ai_state = ("Answers from this firm's records" if ai.enabled("ask") else
                "AI unavailable right now" if ai.configured() else
                "An admin needs to connect an AI provider")
    ai_rail = f"""<div class="card aipanel firm-chat" id="firm-ai" data-scope="firm:{esc(crd)}">
<div class="aihead"><canvas data-orb="breathing" data-size="32" data-px="30" data-tint="#f06b72" aria-hidden="true"></canvas>
<div><div class="t">Ask about {esc(profile_name)}</div><div class="s">{ai_state}</div></div></div>
<div class="ai-welcome" style="text-align:left;padding:0"><div class="aisugs">
<button type="button" data-question="Who should I contact first at this firm, and how?">Who should I contact first, and how?</button>
<button type="button" data-question="Which product fits this firm best and what evidence is missing?">Which product fits best, and what is missing?</button>
<button type="button" data-question="What changed at this firm recently?">What changed here recently?</button></div></div>
<div class="aimsgs" role="log" aria-live="polite"></div>
<form class="aiform composer"><label class="sr-only" for="firm-question">Ask about this firm</label>
<textarea id="firm-question" rows="1" maxlength="1500" placeholder="Ask anything about this firm" required></textarea>
<div class="cfoot"><span class="model">Enter to send</span><button class="send" type="submit" aria-label="Send">{ICONS["send"]}</button></div></form>
</div>"""
    rail = f"""<div class="workspace-forms"><section><h2>Pipeline</h2>
<form method="post" action="/firm/{esc(crd)}/status"><label>Status<select name="status">{status_opts}</select></label>
<label>Owner<input type="text" name="owner" value="{esc(fs['owner'] if fs else '')}" placeholder="Leave blank to assign yourself"></label>
<button type="submit" class="sm primary">Save</button></form></section>
<section><h2>Notes</h2><form method="post" action="/firm/{esc(crd)}/note">
<textarea name="note" aria-label="Firm notes" placeholder="Notes and next steps">{esc(note[0]['note'] if note else '')}</textarea>
<div class="row" style="margin-top:8px"><button class="sm" type="submit">Save note</button>
{f'<span class="meta">Saved {esc(ui.ago(note[0]["updated_at"]))}</span>' if note else ''}</div></form></section>
<section><h2>Saved lists</h2><div class="pills" style="margin-bottom:10px">{lists_chips or '<span class="muted small">No saved lists</span>'}</div>
<form method="post" action="/firms/addtolist"><input type="hidden" name="crd" value="{esc(crd)}"><input type="hidden" name="back" value="/firm/{esc(crd)}#workspace"><input type="hidden" name="new_name" value="">
<select name="list_id" aria-label="Add to saved list" onchange="addToList(this)" style="width:100%"><option value="">Add to a list</option>{listopts}<option value="__new">New list...</option></select></form></section></div>"""

    subline = []
    if place:
        subline.append(f'<span>{ICONS["pin"]}{esc(place)}</span>')
    if website:
        subline.append(f'<span>{ICONS["globe"]}{website}</span>')
    if f["phone"]:
        subline.append(f'<span>{ICONS["phone"]}<a href="tel:{esc(f["phone"])}">{esc(f["phone"])}</a></span>')
    if firm_li:
        subline.append(f'<span>{ICONS["linkedin"]}<a href="{esc(firm_li)}" target="_blank" rel="noopener" '
                       f'data-noprefetch>LinkedIn</a></span>')
    subline.append(f'<span class="muted">CRD {esc(crd)}{" . " + esc(f["regulator"]) if f["regulator"] else ""}</span>')
    signals_n = len(trigs)
    # The overview repeats the first cards; without their ids, so each id stays unique.
    overview_people = "".join(re.sub(r' id="(p-[^"]*|vs[0-9]+)"', "", x) for x in top_cards[:2]) or '<p class="empty">No people on record. <a href="#people">Find contacts</a></p>'
    recent = trow[:3]
    body = f"""<div class="pg firm-page">
<div class="firm-top"><div class="crumb"><a href="/firms">Firms</a><span class="sep">/</span><span>{esc(profile_name)}</span></div>
<div class="acts"><form method="post" action="/watch/{esc(crd)}"><input type="hidden" name="back" value="/firm/{esc(crd)}"><button type="submit">{star}</button></form>
<button type="button" class="primary" data-focus-ai>{ICONS["spark"]}Ask Bellwether AI</button></div></div>
{saved_note}
<header class="dhero"><div class="idrow">{ui.mono(profile_name, "lg")}<div>
<h1>{escn(f['legal_name'])}</h1>
<div class="sub">{"".join(subline)}</div>
<div class="tags">{"".join(tags)}</div></div></div>
{stats_html}</header>
<div class="dossier"><div>
<nav class="secnav" aria-label="Firm research"><a href="#overview">Overview</a>
<a href="#people">People &amp; contacts <span class="cnt">{pc['people']:,}</span></a>
<a href="#fit">Product fit</a><a href="#activity">Activity</a>
<a href="#assets">Assets &amp; funds</a><a href="#profile">Research</a><a href="#workspace">Workspace</a></nav>
<div class="dossier-tabs">
<section class="anchor" id="overview">
<div class="firm-overview-grid"><section class="overview-section"><div class="s-head"><h2>Key people</h2><a class="more" href="#people">All {pc['people']:,} people</a></div>
<div class="people-preview">{overview_people}</div></section>
<section class="overview-section"><div class="s-head"><h2>Product fit</h2><a class="more" href="#fit">Score details</a></div><div>{"".join(fit_cards)}</div></section></div>
<div class="firm-overview-grid overview-secondary"><section class="overview-section"><div class="s-head"><h2>Recent signals</h2><a class="more" href="#activity">All activity</a></div>
{('<div class="timeline">' + "".join(recent) + '</div>') if recent else '<p class="muted">No recorded signals.</p>'}</section>
<section class="overview-section"><div class="s-head"><h2>Hiring</h2><a class="more" href="#hiring">Details</a></div>
{_hiring_glance(stats, [])}</section></div>
{('<details class="detail-section"><summary>AI brief</summary>' + brief_block + '</details>') if brief_block else ''}
</section>
<section class="anchor" id="people">
<div class="dsec">{discovery_html}</div>
<div class="dsec">{people_html}</div>
</section>
<section class="anchor" id="fit"><div class="s-head"><h2>Product fit</h2><span class="meta">Missing factors score zero.</span></div>{_fit_section(crd, results, ranks, focus)}</section>
<section class="anchor" id="activity"><div class="card dsec" id="signals"><div class="card-head"><h2>Signals <span class="h-cnt">{signals_n}</span></h2></div>{trig_html}</div>
<div class="card dsec" id="hiring"><div class="card-head"><h2>Hiring and departures</h2></div>{hiring_html}</div></section>
<section class="anchor" id="assets">{assets_html}<div class="card" style="margin-top:16px"><div class="card-head"><h2>Investments</h2></div>{invest_html}</div></section>
<section class="anchor" id="profile"><div class="cols-2"><div class="card"><div class="card-head"><h2>Technology</h2></div>{tech_html}</div>
<div class="card" id="compliance"><div class="card-head"><h2>Compliance and registration</h2></div>{comp_html}</div></div>
<details class="detail-section"><summary>In their own words</summary>{tp_html}</details>
{('<div class="card" style="margin-top:16px"><div class="card-head"><h2>Firm type</h2><span class="sub">How Bellwether classified this firm</span></div><p><span class="ftype">' + esc(ftype.get("label") or ftype.get("category") or "") + '</span> <span class="meta">' + esc(str(ftype.get("source_label") or ftype.get("source") or "")) + (", confidence " + str(ftype.get("confidence")) if ftype.get("confidence") is not None else "") + '</span></p><ul class="evidence-list">' + "".join("<li>" + esc(str(e)) + "</li>" for e in (ftype.get("evidence") or [])) + '</ul></div>') if ftype and ftype.get("category") else ''}
{office_html}</section>
<section class="anchor" id="workspace">{rail}</section>
</div></div>
</div>
<dialog class="ai-drawer" id="firm-ai-drawer" aria-labelledby="firm-chat-title">
<div class="drawer-header"><h2 id="firm-chat-title">Bellwether AI</h2><button type="button" class="ghost" data-close-ai aria-label="Close firm conversation">Close</button></div>{ai_rail}</dialog>
</div>"""
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
    return RedirectResponse(f"/firm/{crd}?saved=1#workspace", status_code=303)


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
    return RedirectResponse(f"/firm/{crd}?saved=1#workspace", status_code=303)


@router.post("/firm/{crd}/emails")
def gen_emails(crd: str):
    """Older pages posted here to look for addresses; it now runs the whole
    discovery chain for the firm, which only ever shows verified addresses."""
    if not CRD_RE.match(crd):
        return RedirectResponse("/", status_code=303)
    from .api_view import api_hunt
    api_hunt(crd)
    return RedirectResponse(f"/firm/{crd}#people", status_code=303)
