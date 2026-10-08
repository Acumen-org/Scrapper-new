"""Home: what Bellwether knows, what moved, and where to start.

Four views keep the default page focused while retaining all intelligence:

  1. How big is what we see? The adviser universe, the assets it manages, the
     people working in it, and how many of them we can actually reach.
  2. How do the product lists stand? Each list's size, the shape of its
     scores, how much of the scoring rests on known data, and its best firms.
  3. What just moved? Advisors joining and leaving firms on the lists, new
     registrations, asset jumps, custodian moves, newest first.
  4. Where are they? Firms on the lists by state.
  5. How complete is the data, and what is working on it right now?
  6. What is mine? My pipeline, my firms, the firms I watch.

Heavy aggregates are cached and refreshed in the background.
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from . import ai, products, ui
from .webapp import (ICONS, TYPE_LABEL, conn, current_owner,
                     esc, escn, money, page, qs_join, score_cell, signal_cutoff)

router = APIRouter()

TILES = {
    "AK": (0, 0), "ME": (11, 0), "VT": (10, 1), "NH": (11, 1),
    "WA": (1, 2), "ID": (2, 2), "MT": (3, 2), "ND": (4, 2), "MN": (5, 2), "IL": (6, 2),
    "WI": (7, 2), "MI": (8, 2), "NY": (9, 2), "RI": (10, 2), "MA": (11, 2),
    "OR": (1, 3), "NV": (2, 3), "WY": (3, 3), "SD": (4, 3), "IA": (5, 3), "IN": (6, 3),
    "OH": (7, 3), "PA": (8, 3), "NJ": (9, 3), "CT": (10, 3),
    "CA": (1, 4), "UT": (2, 4), "CO": (3, 4), "NE": (4, 4), "MO": (5, 4), "KY": (6, 4),
    "WV": (7, 4), "VA": (8, 4), "MD": (9, 4), "DE": (10, 4),
    "AZ": (2, 5), "NM": (3, 5), "KS": (4, 5), "AR": (5, 5), "TN": (6, 5), "NC": (7, 5),
    "SC": (8, 5), "DC": (9, 5),
    "OK": (4, 6), "LA": (5, 6), "MS": (6, 6), "AL": (7, 6), "GA": (8, 6),
    "HI": (0, 7), "TX": (4, 7), "FL": (9, 7),
}

_CACHE: dict = {"t": 0.0, "data": None}
BIG_FIRM = 300   # people; wirehouses and national broker-dealers sit far above
_LOCK = threading.Lock()
TTL_S = 120.0


def _one(c, sql, args=()):
    try:
        r = c.execute(sql, args).fetchone()
        return list(r.values())[0] if r else None
    except Exception:
        c.rollback()
        return None


def _all(c, sql, args=()):
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    except Exception:
        c.rollback()
        return []


def _gather() -> dict:
    c = conn()
    d: dict = {}
    d["firms"] = _one(c, "SELECT COUNT(*) FROM firm_current WHERE is_era=0") or 0
    d["firms_sec"] = _one(c, "SELECT COUNT(*) FROM firm_current WHERE is_era=0 AND regulator='SEC'") or 0
    d["era"] = _one(c, "SELECT COUNT(*) FROM firm_current WHERE is_era=1") or 0
    d["aum"] = _one(c, "SELECT SUM(raum) FROM firm_current WHERE is_era=0") or 0
    d["scope"] = _one(c, "SELECT COUNT(*) FROM firm_scope") or 0
    d["people"] = _one(c, "SELECT COUNT(*) FROM person") or 0
    if not d["people"]:
        d["people"] = (_one(c, "SELECT COUNT(*) FROM schedule_a WHERE is_individual=1") or 0)
    d["hires_12m"] = _one(c, "SELECT COUNT(*) FROM people_event WHERE kind='joined'"
                             " AND event_date >= ?", ((date.today() - timedelta(days=365)).isoformat(),))
    d["named_email"] = _one(c, """SELECT COUNT(*) FROM (SELECT DISTINCT crd, person_key FROM
        usable_contact_point WHERE kind='email' AND person_key != '' AND is_role=0 AND source != 'pattern'
        AND verify_status NOT IN ('invalid','no_mail_server')) x""") or 0
    d["verified"] = _one(c, "SELECT COUNT(*) FROM usable_contact_point WHERE kind='email'"
                            " AND verify_status='valid'") or 0
    d["candidates"] = _one(c, "SELECT COUNT(*) FROM contact_point WHERE kind='email'"
                              " AND source='pattern' AND verify_status IN ('unverified','queued')") or 0
    d["direct"] = _one(c, "SELECT COUNT(*) FROM usable_contact_point WHERE kind='phone'"
                          " AND person_key != ''") or 0
    d["phones"] = _one(c, "SELECT COUNT(*) FROM usable_contact_point WHERE kind='phone'") or 0
    d["sites"] = _one(c, "SELECT COUNT(*) FROM web_enrich_state WHERE status='ok'") or 0
    d["brochures"] = _one(c, "SELECT COUNT(*) FROM brochure WHERE status='ok'") or 0
    d["signals_30"] = _one(c, "SELECT COUNT(*) FROM trigger_event WHERE suppressed=0"
                              " AND detected_date >= ?",
                           ((date.today() - timedelta(days=30)).isoformat(),)) or 0

    # Product lists: size, distribution, coverage, best firms, fresh signals.
    lists = []
    cutoff = signal_cutoff()
    for k in products.product_keys():
        p = products.product(k)
        st = c.execute("""SELECT COUNT(*) n, AVG(coverage) cov,
            COUNT(*) FILTER (WHERE score >= 60) hi FROM product_score
            WHERE product=? AND status='scored'""", (k,)).fetchone()
        scores = [r["score"] for r in c.execute(
            "SELECT score FROM product_score WHERE product=? AND status='scored'", (k,))]
        top = _all(c, """SELECT p.crd, p.score, p.coverage, f.legal_name FROM product_score p
            JOIN firm_current f ON f.crd=p.crd WHERE p.product=? AND p.status='scored'
            ORDER BY p.rank LIMIT 3""", (k,))
        fresh = _one(c, """SELECT COUNT(DISTINCT t.crd) FROM trigger_event t
            JOIN product_score p ON p.crd=t.crd AND p.product=? AND p.status='scored'
            WHERE t.suppressed=0 AND t.detected_date >= ?""", (k, cutoff)) or 0
        lists.append({"key": k, "p": p, "n": st["n"] or 0, "cov": st["cov"] or 0,
                      "hi": st["hi"] or 0, "scores": scores, "top": top, "fresh": fresh})
    d["lists"] = lists

    # What moved: people and signals on firms on the lists, newest first.
    since = (date.today() - timedelta(days=60)).isoformat()
    # Moves at the firms a salesperson would call: the giant broker-dealers on
    # some lists hire thousands a year, and would bury every move that matters.
    moves = _all(c, """SELECT e.event_date AS d, e.kind, e.crd, e.other_org_name, p.name AS person,
            f.legal_name FROM people_event e
        JOIN firm_scope s ON s.crd=e.crd
        JOIN firm_current f ON f.crd=e.crd
        LEFT JOIN firm_people_stats ps ON ps.crd=e.crd
        LEFT JOIN person p ON p.indvl_pk=e.indvl_pk
        WHERE e.event_date >= ? AND COALESCE(ps.headcount, 0) <= ?
        ORDER BY e.event_date DESC, s.priority DESC LIMIT 14""", (since, BIG_FIRM))
    sigs = _all(c, """SELECT t.detected_date AS d, t.trigger_type, t.description, t.crd, f.legal_name
        FROM trigger_event t JOIN firm_scope s ON s.crd=t.crd
        JOIN firm_current f ON f.crd=t.crd
        WHERE t.suppressed=0 AND t.detected_date >= ?
        ORDER BY t.detected_date DESC, s.priority DESC LIMIT 14""", (since,))
    d["moves"], d["sigs"] = moves, sigs
    d["hire_firms"] = _all(c, """SELECT ps.crd, ps.hires_12m, ps.net_12m, ps.headcount, f.legal_name,
            f.state FROM firm_people_stats ps JOIN firm_scope s ON s.crd=ps.crd
        JOIN firm_current f ON f.crd=ps.crd
        WHERE ps.hires_12m >= 2 AND ps.headcount <= ?
        ORDER BY ps.hires_12m DESC, s.priority DESC LIMIT 6""", (BIG_FIRM,))

    d["by_state"] = {r["state"]: r["n"] for r in _all(c, """SELECT f.state, COUNT(*) n
        FROM firm_scope s JOIN firm_current f ON f.crd=s.crd WHERE f.state IS NOT NULL
        GROUP BY f.state""")}

    sc = d["scope"] or 1
    d["coverage"] = [
        ("Brochure read", _one(c, "SELECT COUNT(*) FROM brochure b JOIN firm_scope s ON s.crd=b.crd WHERE b.status='ok'") or 0, sc),
        ("Website read", _one(c, "SELECT COUNT(*) FROM web_enrich_state w JOIN firm_scope s ON s.crd=w.crd WHERE w.status='ok'") or 0, sc),
        ("People roster", _one(c, "SELECT COUNT(*) FROM firm_people_stats p JOIN firm_scope s ON s.crd=p.crd WHERE p.headcount > 0") or 0, sc),
        ("A named person's email", _one(c, """SELECT COUNT(DISTINCT c.crd) FROM usable_contact_point c
            JOIN firm_scope s ON s.crd=c.crd WHERE c.kind='email' AND c.person_key != ''
            AND c.is_role=0 AND c.source != 'pattern'
            AND c.verify_status NOT IN ('invalid','no_mail_server')""") or 0, sc),
        ("A verified email", _one(c, """SELECT COUNT(DISTINCT c.crd) FROM usable_contact_point c
            JOIN firm_scope s ON s.crd=c.crd WHERE c.kind='email' AND c.verify_status='valid'""") or 0, sc),
        ("Email platform known", _one(c, """SELECT COUNT(*) FROM firm_mail_platform m
            JOIN firm_scope s ON s.crd=m.crd WHERE m.platform IN ('m365','google','other')""") or 0, sc),
    ]
    d["funnel"] = {r["status"]: r["n"] for r in _all(
        c, "SELECT status, COUNT(*) n FROM firm_status WHERE status IS NOT NULL GROUP BY status")}
    from . import jobs
    try:
        d["live"] = [{"label": j["job"].label, "state": j["state"], "backlog": j.get("backlog")}
                     for j in jobs.overview(c) if j["state"] in ("running", "queued")]
    except Exception:
        c.rollback()
        d["live"] = []
    try:
        from . import people_index
        d["reach"] = people_index.coverage(c)
    except Exception:
        c.rollback()
        d["reach"] = {}
    d["finds"] = _all(c, """SELECT cp.crd, cp.person_name, cp.kind, cp.source,
            COALESCE(cp.verified_at, cp.found_at) AS at, f.legal_name
        FROM contact_point cp JOIN firm_scope s ON s.crd=cp.crd JOIN firm_current f ON f.crd=cp.crd
        WHERE cp.person_key != '' AND ((cp.kind='email' AND cp.verify_status='valid')
              OR (cp.kind='linkedin' AND cp.verify_status='matched')
              OR (cp.kind='phone' AND cp.label IN ('direct','mobile')))
        ORDER BY COALESCE(cp.verified_at, cp.found_at) DESC LIMIT 8""")
    d["feed_date"] = _one(c, "SELECT published_at FROM snapshot WHERE source_key='adv_feed'"
                             " ORDER BY id DESC LIMIT 1")
    d["scored_at"] = _one(c, "SELECT MAX(computed_at) FROM product_score")
    c.close()
    return d


def _refresh() -> None:
    try:
        d = _gather()
        with _LOCK:
            _CACHE.update(t=time.monotonic(), data=d)
    finally:
        with _LOCK:
            _CACHE["busy"] = False


def data() -> dict:
    """Home's numbers. Stale numbers are served at once while fresh ones are
    worked out in the background, so nobody waits for the aggregates; only
    the very first call in a process computes them in line."""
    with _LOCK:
        have = _CACHE["data"]
        fresh = have is not None and time.monotonic() - _CACHE["t"] < TTL_S
        if have is not None and not fresh and not _CACHE.get("busy"):
            _CACHE["busy"] = True
            threading.Thread(target=_refresh, daemon=True, name="home-refresh").start()
    if have is not None:
        return have
    d = _gather()
    with _LOCK:
        _CACHE.update(t=time.monotonic(), data=d)
    return d


def _kpi(n, label, sub="", href=None, accent=False) -> str:
    inner = (f'<div class="l">{esc(label)}</div><div class="n">{n}</div>'
             + (f'<div class="d">{sub}</div>' if sub else ""))
    cls = "kpi accent" if accent else "kpi"
    return f'<a class="{cls}" href="{href}">{inner}</a>' if href else f'<div class="{cls}">{inner}</div>'


def _compact(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 10_000:
        return f"{n / 1e3:.0f}K"
    return f"{n:,}"


def _map(by_state: dict) -> str:
    top = max(by_state.values()) if by_state else 1
    cells = []
    import math
    for st, (x, y) in TILES.items():
        n = by_state.get(st, 0)
        a = 0.08 + 0.82 * (math.log1p(n) / math.log1p(top)) if n else 0
        bg = f"rgba(198,84,84,{a:.2f})" if n else "var(--raise)"
        cells.append(f'<a class="st" href="/firms?{qs_join(st=st, on="any")}" '
                     f'style="grid-column:{x + 1};grid-row:{y + 1};background:{bg}" '
                     f'title="{st}: {n:,} firms on a list">{st}</a>')
    return f'<div class="usmap">{"".join(cells)}</div>'


@router.get("/", response_class=HTMLResponse)
def home(type: str = "", product: str = "", state: str = ""):
    # The inbox used to live at /. Old links with its filters land on Signals.
    if type or product or state:
        return RedirectResponse(f"/signals?{qs_join(type=type, product=product, state=state)}",
                                status_code=307)
    d = data()
    me = current_owner()

    kpis = "".join([
        _kpi(_compact(d["firms"]), "Adviser firms",
             f'{money(d["aum"])} under management', "/firms"),
        _kpi(f'{d["scope"]:,}', "Ranked for our products", "on at least one list", "/firms?on=any"),
        _kpi(_compact(d["people"]), "People tracked",
             f'{d["hires_12m"] or 0:,} job moves in 12 months', "/people"),
        _kpi(f'{(d.get("reach") or {}).get("email", d["named_email"]):,}', "Reachable by email",
             f'{d["verified"]:,} addresses verified', "/people?view=email", accent=True),
        _kpi(f'{(d.get("reach") or {}).get("phone", d["phones"]):,}', "People with a phone",
             f'{(d.get("reach") or {}).get("direct", 0):,} direct lines', "/people?view=direct"),
        _kpi(f'{d["signals_30"]:,}', "Signals in 30 days", "filings, people, assets", "/signals"),
    ])

    cards = []
    for L in d["lists"]:
        p = L["p"]
        top = "".join(
            f'<a href="/firm/{esc(t["crd"])}?p={L["key"]}"><span>{escn(t["legal_name"])}</span>'
            f'<span class="{"ok" if t["score"] >= 60 else "muted"}">{t["score"]:.0f}</span></a>' for t in L["top"])
        cards.append(
            f'<div class="product-entry"><div class="product-summary">'
            f'<a class="product-name" href="/lists/{L["key"]}">{esc(p["name"])}</a>'
            f'<span><b>{L["n"]:,}</b> firms</span><span><b>{L["hi"]:,}</b> scoring 60+</span>'
            f'<span class="{"warnc" if L["cov"] < 70 else "soft"}">{L["cov"]:.0f}% known data</span>'
            f'<a class="more" href="/lists/{L["key"]}">Open list</a></div>'
            f'<details class="product-preview"><summary>Audience and leading firms</summary>'
            f'<p>{esc(p["audience"])}</p><div class="tops">{top or "<span class=muted>No scores yet</span>"}</div>'
            f'<p class="meta">{L["fresh"]:,} new signals</p></details></div>')
    lists_html = "".join(cards)

    # Feed: people moves and signals merged.
    feed = []
    for m in d["moves"]:
        joined = m["kind"] == "joined"
        other = escn(m["other_org_name"]) if m.get("other_org_name") else ""
        txt = (f'<b>{esc(m["person"] or "Someone")}</b> {"joined" if joined else "left"} '
               f'<a href="/firm/{esc(m["crd"])}#activity">{escn(m["legal_name"])}</a>'
               + (f' <span class="muted">{"from" if joined else "for"} {other}</span>' if other else ""))
        feed.append((m["d"], f'<div class="it"><div class="ic {"in" if joined else "out"}">'
                             f'{"+" if joined else "-"}</div><div>{txt}</div>'
                             f'<div class="when">{esc(ui.ago(m["d"]))}</div></div>'))
    for s in d["sigs"]:
        feed.append((s["d"], f'<div class="it"><div class="ic amb">{ICONS["bolt"]}</div><div>'
                             f'<a href="/firm/{esc(s["crd"])}"><b>{escn(s["legal_name"])}</b></a> '
                             f'<span class="chip lead">{esc(TYPE_LABEL.get(s["trigger_type"], s["trigger_type"]))}</span>'
                             f'<div class="meta">{esc(s["description"])}</div></div>'
                             f'<div class="when">{esc(ui.ago(s["d"]))}</div></div>'))
    feed.sort(key=lambda x: x[0] or "", reverse=True)
    feed_html = ("".join(x[1] for x in feed[:6]) or
                 '<p class="empty">No new signals in the last 60 days.</p>')

    hire_rows = "".join(
        f'<div class="r"><a href="/firm/{esc(h["crd"])}#activity">{escn(h["legal_name"])}</a>'
        f'<div class="b"><i style="width:{min(100, h["hires_12m"] / max(1, d["hire_firms"][0]["hires_12m"]) * 100):.0f}%;background:var(--ok)"></i></div>'
        f'<div class="num">+{h["hires_12m"]}</div></div>' for h in d["hire_firms"])

    cov_rows = "".join(
        f'<div class="mrow"><div>{esc(lbl)}</div><div class="meter{" amber" if n / of < .5 else ""}">'
        f'<i style="width:{n / of * 100:.1f}%"></i></div><div class="num">{n / of * 100:.0f}%</div></div>'
        for lbl, n, of in d["coverage"])

    funnel_order = ["new", "working", "meeting set", "qualified", "customer"]
    ftop = max([d["funnel"].get(s, 0) for s in funnel_order] + [1])
    funnel = "".join(
        f'<div class="r"><a href="/firms?{qs_join(stat=s)}">{esc(s.capitalize())}</a>'
        f'<div class="b"><i style="width:{d["funnel"].get(s, 0) / ftop * 100:.0f}%"></i></div>'
        f'<div class="num">{d["funnel"].get(s, 0):,}</div></div>' for s in funnel_order)

    # Personal panels: never cached, they are about the person looking.
    c = conn()
    mine = []
    if me:
        mine = c.execute("""
            SELECT s.crd, s.status, s.updated_at, f.legal_name, f.state, f.city, f.raum,
                   sc.best_product, sc.best_score, sc.best_coverage
            FROM firm_status s JOIN firm_current f ON f.crd=s.crd
            LEFT JOIN firm_scope sc ON sc.crd=s.crd
            WHERE s.owner=? ORDER BY s.updated_at DESC LIMIT 6""", (me,)).fetchall()
    watched = c.execute("""
        SELECT w.crd, f.legal_name, f.state, f.city, f.raum,
               (SELECT description FROM trigger_event t WHERE t.crd=w.crd
                 AND t.suppressed=0 ORDER BY detected_date DESC LIMIT 1) AS last_desc,
               (SELECT MAX(detected_date) FROM trigger_event t WHERE t.crd=w.crd
                 AND t.suppressed=0) AS last_date
        FROM firm_watch w JOIN firm_current f ON f.crd=w.crd
        ORDER BY last_date DESC NULLS LAST LIMIT 6""").fetchall()
    live = d.get("live") or []
    c.close()

    mine_html = "".join(
        f'<tr class="go" data-href="/firm/{esc(r["crd"])}"><td><div class="ent">{ui.mono(r["legal_name"], "sm")}<div>'
        f'<a class="t" href="/firm/{esc(r["crd"])}">{escn(r["legal_name"])}</a>'
        f'<div class="meta">{esc(r["status"] or "claimed").capitalize()} . {esc(ui.product_name(r["best_product"]))}</div></div></div></td>'
        f'<td class="num">{score_cell(r["best_score"], r["best_coverage"], show_cov=False)}</td></tr>' for r in mine)
    mine_html = (f'<table class="tight bare"><tbody>{mine_html}</tbody></table>' if mine_html else
                 '<div class="empty">No firms assigned. <a href="/firms">Find a firm</a></div>')
    watch_html = "".join(
        f'<tr class="go" data-href="/firm/{esc(r["crd"])}"><td><div class="ent">{ui.mono(r["legal_name"], "sm")}<div>'
        f'<a class="t" href="/firm/{esc(r["crd"])}">{escn(r["legal_name"])}</a>'
        f'<div class="meta">{esc(r["last_desc"] or "No change since you started watching")}</div></div></div></td>'
        f'<td class="small muted nowrap num">{esc(ui.ago(r["last_date"]))}</td></tr>' for r in watched)
    watch_html = (f'<table class="tight bare"><tbody>{watch_html}</tbody></table>' if watch_html else
                  '<div class="empty">Watch a firm to follow its activity here.</div>')

    # The contact discovery engine: how far it has got, what it is doing now,
    # and what it found most recently.
    rc = d.get("reach") or {}
    listed = rc.get("listed") or 0
    pct = (rc.get("listed_email", 0) / listed * 100) if listed else 0
    engine = "".join(
        f'<div class="e"><i class="{"run" if j["state"] == "running" else "wait"}"></i>'
        f'<span>{esc(j["label"])}</span><span class="m">'
        f'{format(j["backlog"], ",") + " to go" if j.get("backlog") else ("running" if j["state"] == "running" else "queued")}'
        f'</span></div>' for j in live[:6]) or '<p class="muted small">No jobs running.</p>'
    finds = "".join(
        f'<div class="it"><div class="ic in">{ICONS["check"]}</div><div><b>{esc(f_["person_name"] or "A shared inbox")}</b>'
        f' <span class="muted">at</span> <a href="/firm/{esc(f_["crd"])}#people">{escn(f_["legal_name"])}</a>'
        f'<div class="meta">{"Verified email" if f_["kind"] == "email" else "LinkedIn profile" if f_["kind"] == "linkedin" else "Phone line"}'
        f' . {esc(ui.SOURCE_LABEL.get(f_["source"], f_["source"]))}</div></div><div class="when">{esc(ui.ago(f_["at"]))}</div></div>'
        for f_ in (d.get("finds") or [])[:6])
    discovery = f"""<div class="card"><div class="card-head"><div><h2>Contact discovery</h2>
<div class="sub">People at firms on our lists with a usable email</div></div>
<a class="more" href="/people?on=any&amp;view=hunting">Still hunting</a></div>
<div class="row" style="align-items:flex-end;gap:14px"><div class="big">{pct:.1f}%</div>
<div class="small soft" style="padding-bottom:6px">{rc.get('listed_email', 0):,} of {listed:,} people</div></div>
<div class="meter" style="margin:18px 0"><i style="width:{pct:.1f}%"></i></div>
<div class="legend" style="margin-bottom:14px"><span><i style="background:var(--ok)"></i>email {rc.get('email', 0):,}</span>
<span>LinkedIn {rc.get('linkedin', 0):,}</span>
<span><i style="background:var(--soft)"></i>direct line {rc.get('direct', 0):,}</span></div>
<details class="detail-section"><summary>Discovery jobs</summary><div class="engine">{engine}</div></details>
{('<details class="detail-section"><summary>Latest finds</summary><div class="feed">' + finds + '</div></details>') if finds else ''}</div>"""

    ai_ready = ai.configured()
    askbar = f"""<form class="askbar" method="get" action="{'/ask' if ai_ready else '/firms'}">
<input name="q" aria-label="{'Ask Bellwether AI' if ai_ready else 'Search firms'}" placeholder="{'Ask Bellwether AI' if ai_ready else 'Search firms'}" autocomplete="off">
<button class="primary" type="submit">{'Ask' if ai_ready else 'Search'}</button></form>"""

    fresh = []
    if d.get("feed_date"):
        fresh.append(f"SEC adviser feed of {esc(d['feed_date'])}")
    fresh.append(f"{d['brochures']:,} brochures read")
    fresh.append(f"{d['sites']:,} firm websites read")
    if d.get("scored_at"):
        fresh.append(f"scores computed {esc(ui.ago(d['scored_at']))}")
    body = f"""<div class="pg dashboard">
<div class="dash-hero"><h1>Home</h1>{askbar}</div>
<nav class="workspace-tabs" data-workspace-tabs aria-label="Dashboard views">
<button type="button" data-panel="home-overview">Overview</button>
<button type="button" data-panel="home-products">Product lists</button>
<button type="button" data-panel="home-workspace">Your workspace</button>
<button type="button" data-panel="home-coverage">Data coverage</button></nav>
<section id="home-overview">
<div class="overview-totals"><a href="/firms"><strong>{d['firms']:,}</strong> adviser firms</a>
<a href="/people"><strong>{d['people']:,}</strong> people</a>
<a href="/signals"><strong>{d['signals_30']:,}</strong> signals <span>in 30 days</span></a></div>
<div class="overview-columns">
<section class="overview-section"><div class="s-head"><h2>Latest intelligence</h2><a href="/signals" class="more">All activity</a></div><div class="feed">{feed_html}</div></section>
<section class="overview-section"><div class="s-head"><h2>Hiring activity</h2><a href="/firms?sort=hires&amp;on=any" class="more">View firms</a></div><div class="hbars">{hire_rows or '<p class="empty">No recorded hiring activity.</p>'}</div><p class="meta">Joins in the past 12 months</p></section>
</div>
<div class="home-shortcuts"><a href="/people?view=email">{rc.get('email', d['named_email']):,} people with email</a>
<a href="/people?view=direct">{rc.get('direct', 0):,} people with a direct line</a>
<a href="#home-coverage">View data coverage</a></div>
</section>
<section id="home-products"><div class="product-index">{lists_html}</div></section>
<section id="home-workspace"><div class="overview-columns">
<div><section class="overview-section"><div class="s-head"><h2>Your firms</h2><a href="/firms?owner=me" class="more">View all</a></div>{mine_html}</section>
<section class="overview-section"><div class="s-head"><h2>Watching</h2><a href="/firms" class="more">Find firms</a></div>{watch_html}</section></div>
<section class="overview-section"><h2>Team pipeline</h2><div class="funnel">{funnel}</div></section></div></section>
<section id="home-coverage"><div class="kpis">{kpis}</div><div class="coverage-columns">
<div>{discovery}</div><section class="overview-section"><div class="s-head"><h2>Data coverage</h2><span class="meta">{d['scope']:,} ranked firms</span></div>{cov_rows}
<details class="detail-section"><summary>Firms by state</summary>{_map(d['by_state'])}</details></section></div>
<p class="meta freshness">{' · '.join(fresh)}</p></section>
</div>"""
    return page("Home", "home", body)
