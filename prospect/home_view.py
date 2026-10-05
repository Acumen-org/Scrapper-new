"""Home: what Bellwether knows, what moved, and where to start.

Read top to bottom it answers, without a click:

  1. How big is what we see? The adviser universe, the assets it manages, the
     people working in it, and how many of them we can actually reach.
  2. How do the product lists stand? Each list's size, the shape of its
     scores, how much of the scoring rests on known data, and its best firms.
  3. What just moved? Advisors joining and leaving firms on the lists, new
     registrations, asset jumps, custodian moves, newest first.
  4. Where are they? Firms on the lists by state.
  5. How complete is the data, and what is working on it right now?
  6. What is mine? My pipeline, my firms, the firms I watch.

Everything heavy is computed once every two minutes and shared, so the page
renders in milliseconds however many people open it.
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from . import ai, products, ui
from .webapp import (FAMILY_COLOUR, TYPE_LABEL, conn, current_account, current_owner,
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
        contact_point WHERE kind='email' AND person_key != '' AND is_role=0 AND source != 'pattern'
        AND verify_status NOT IN ('invalid','no_mail_server')) x""") or 0
    d["verified"] = _one(c, "SELECT COUNT(*) FROM contact_point WHERE kind='email'"
                            " AND verify_status='valid'") or 0
    d["candidates"] = _one(c, "SELECT COUNT(*) FROM contact_point WHERE kind='email'"
                              " AND source='pattern' AND verify_status IN ('unverified','queued')") or 0
    d["direct"] = _one(c, "SELECT COUNT(*) FROM contact_point WHERE kind='phone'"
                          " AND person_key != ''") or 0
    d["phones"] = _one(c, "SELECT COUNT(*) FROM contact_point WHERE kind='phone'") or 0
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
        ("A named person's email", _one(c, """SELECT COUNT(DISTINCT c.crd) FROM contact_point c
            JOIN firm_scope s ON s.crd=c.crd WHERE c.kind='email' AND c.person_key != ''
            AND c.is_role=0 AND c.source != 'pattern'
            AND c.verify_status NOT IN ('invalid','no_mail_server')""") or 0, sc),
        ("A verified email", _one(c, """SELECT COUNT(DISTINCT c.crd) FROM contact_point c
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


def _greeting() -> str:
    h = datetime.now().hour
    part = "morning" if h < 12 else "afternoon" if h < 18 else "evening"
    name = (current_owner() or "").split(" ")[0]
    return f"Good {part}{', ' + esc(name) if name else ''}"


def _kpi(n, label, sub="", href=None) -> str:
    inner = (f'<div class="n">{n}</div><div class="l">{esc(label)}</div>'
             + (f'<div class="d">{sub}</div>' if sub else ""))
    return f'<a class="kpi" href="{href}">{inner}</a>' if href else f'<div class="kpi">{inner}</div>'


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
        _kpi(_compact(d["firms"]), "Adviser firms tracked",
             f'{d["firms_sec"]:,} SEC, {d["firms"] - d["firms_sec"]:,} state', "/firms"),
        _kpi(money(d["aum"]), "Assets they manage", "Regulatory AUM, latest filings"),
        _kpi(f'{d["scope"]:,}', "On a product list", "Scored for PHH, AcuBooth or Glynac", "/firms?on=any"),
        _kpi(_compact(d["people"]), "People tracked",
             f'{d["hires_12m"] or 0:,} job moves into firms in 12 months', "/people"),
        _kpi(f'{d["named_email"]:,}', "Named people with an email",
             f'{d["verified"]:,} verified by mail server', "/people?reach=email"),
        _kpi(f'{d["direct"]:,}', "Direct phone lines", f'{d["phones"]:,} numbers in all', "/people?reach=phone"),
        _kpi(f'{d["signals_30"]:,}', "Signals in 30 days", "Hires, asset jumps, custody moves", "/signals"),
    ])

    rows = []
    for L in d["lists"]:
        p = L["p"]
        colour = FAMILY_COLOUR.get(p["family"], "#888")
        top = "".join(
            f'<div class="small"><a href="/firm/{esc(t["crd"])}?p={L["key"]}">{escn(t["legal_name"])}</a>'
            f' <span class="muted">{t["score"]:.0f}</span></div>' for t in L["top"])
        rows.append(
            f'<div class="prodrow"><div><div class="pn"><span class="dotc" style="background:{colour}"></span>'
            f'<a href="/lists/{L["key"]}">{esc(p["name"])}</a></div><div class="pa">{esc(p["audience"])}</div></div>'
            f'<div><div class="big" style="font-size:24px">{L["n"]:,}</div><div class="tiny muted">firms ranked</div></div>'
            f'<div>{ui.histogram(L["scores"])}<div class="tiny muted">{L["hi"]:,} score 60 or more</div></div>'
            f'<div><div class="meter{" amber" if L["cov"] < 70 else ""}"><i style="width:{L["cov"]:.0f}%"></i></div>'
            f'<div class="tiny muted" style="margin-top:4px">{L["cov"]:.0f}% of scoring on known data'
            f'{" . " + str(L["fresh"]) + " with new signals" if L["fresh"] else ""}</div></div>'
            f'<div>{top or "<span class=muted small>Scores appear after the first scoring run</span>"}</div></div>')
    lists_html = "".join(rows)

    # Feed: people moves and signals merged.
    feed = []
    for m in d["moves"]:
        joined = m["kind"] == "joined"
        other = escn(m["other_org_name"]) if m.get("other_org_name") else ""
        txt = (f'<b>{esc(m["person"] or "Someone")}</b> {"joined" if joined else "left"} '
               f'<a href="/firm/{esc(m["crd"])}#hiring">{escn(m["legal_name"])}</a>'
               + (f' <span class="muted">{"from" if joined else "for"} {other}</span>' if other else ""))
        feed.append((m["d"], f'<div class="it"><div class="ic {"in" if joined else "out"}">'
                             f'{"+" if joined else "-"}</div><div>{txt}</div>'
                             f'<div class="tiny muted nowrap">{esc(ui.ago(m["d"]))}</div></div>'))
    for s in d["sigs"]:
        feed.append((s["d"], f'<div class="it"><div class="ic">&#8599;</div><div>'
                             f'<a href="/firm/{esc(s["crd"])}">{escn(s["legal_name"])}</a> '
                             f'<span class="chip lead">{esc(TYPE_LABEL.get(s["trigger_type"], s["trigger_type"]))}</span>'
                             f'<div class="meta">{esc(s["description"])}</div></div>'
                             f'<div class="tiny muted nowrap">{esc(ui.ago(s["d"]))}</div></div>'))
    feed.sort(key=lambda x: x[0] or "", reverse=True)
    feed_html = ("".join(x[1] for x in feed[:16]) or
                 '<p class="empty">Nothing new in the last 60 days. The weekly SEC pull and '
                 'the people feed add movements here as they happen.</p>')

    hire_rows = "".join(
        f'<div class="r"><a href="/firm/{esc(h["crd"])}#hiring">{escn(h["legal_name"])}</a>'
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
            WHERE s.owner=? ORDER BY s.updated_at DESC LIMIT 8""", (me,)).fetchall()
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
        f'<tr class="go" data-href="/firm/{esc(r["crd"])}"><td><div class="firm">'
        f'<a href="/firm/{esc(r["crd"])}">{escn(r["legal_name"])}</a></div>'
        f'<div class="meta">{ui.firm_meta(r)}</div></td>'
        f'<td><span class="chip">{esc(r["status"] or "claimed")}</span></td>'
        f'<td>{score_cell(r["best_score"], r["best_coverage"], show_cov=False)}'
        f'<div class="meta">{esc(ui.product_name(r["best_product"]))}</div></td></tr>' for r in mine)
    mine_html = (f'<table class="tight"><tbody>{mine_html}</tbody></table>' if mine_html else
                 '<p class="empty">Nothing claimed yet. Set a status on any firm and it lands '
                 'here under your name.</p>')
    watch_html = "".join(
        f'<tr class="go" data-href="/firm/{esc(r["crd"])}"><td><div class="firm">'
        f'<a href="/firm/{esc(r["crd"])}">{escn(r["legal_name"])}</a></div>'
        f'<div class="meta">{esc(r["last_desc"] or "No change since you started watching")}</div></td>'
        f'<td class="small muted nowrap">{esc(ui.ago(r["last_date"]))}</td></tr>' for r in watched)
    watch_html = (f'<table class="tight"><tbody>{watch_html}</tbody></table>' if watch_html else
                  '<p class="empty">Star a firm on its page to follow it here.</p>')
    live_html = "".join(
        f'<div class="job"><span class="dotc" style="background:'
        f'{"var(--ok)" if j["state"] == "running" else "var(--amber)"}"></span>'
        f'{esc(j["label"])}<span class="muted small">'
        f'{" . " + format(j["backlog"], ",") + " to go" if j.get("backlog") else ""}</span></div>'
        for j in live[:6]) or '<p class="muted small">Everything is caught up.</p>'

    ai_ready = ai.configured()
    sugs = ["Which PHH Fund I firms in Texas hired advisors this year?",
            "Glynac firms on Microsoft 365 that use Black Diamond",
            "AcuBooth firms over $500M with a verified email",
            "Who left large firms on our lists in the last 60 days?"]
    sug_html = "".join(f'<a class="chip line" href="/ask?{qs_join(q=s)}">{esc(s)}</a>' for s in sugs)
    askbar = f"""<form class="askbar" method="get" action="/ask">
<canvas data-orb="breathing" data-size="32" data-px="34" data-tint="#d9d4ca" aria-label="Bellwether AI"></canvas>
<input name="q" placeholder="Ask Bellwether about any firm, person or market" autocomplete="off">
<button class="primary" type="submit">Ask</button></form>
<div class="row" style="margin-top:10px;max-width:880px">{sug_html}</div>
{"" if ai_ready else '<p class="hint muted small" style="margin-top:8px">Bellwether AI answers in plain English once an admin connects an AI provider; until then it finds firms by name.</p>'}"""

    fresh = []
    if d.get("feed_date"):
        fresh.append(f"SEC adviser feed of {esc(d['feed_date'])}")
    fresh.append(f"{d['brochures']:,} brochures read")
    fresh.append(f"{d['sites']:,} firm websites read")
    if d.get("scored_at"):
        fresh.append(f"scores computed {esc(ui.ago(d['scored_at']))}")

    body = f"""<div class="pg wide">
<div class="head"><div><h1 class="hello">{_greeting()}</h1>
<div class="lede">Every registered investment adviser in the US, the people who work at them,
how to reach them, and how well each fits PHH, AcuBooth and Glynac.</div>{askbar}</div></div>
<section class="s" style="margin-top:22px;padding-top:0;border-top:0"><div class="kpis">{kpis}</div></section>
<section class="s"><div class="s-head"><h2>Product lists</h2>
<span class="more">One ranked list per product. Hatched bars and amber figures mark scores resting on missing data.</span></div>
{lists_html}</section>
<section class="s"><div class="cols-21">
<div><div class="s-head"><h2>What moved</h2><a class="more" href="/signals">All signals</a></div>
<div class="feed">{feed_html}</div></div>
<div><div class="s-head"><h2>Hiring now</h2><a class="more" href="/firms?sort=hires&on=any">More</a></div>
<div class="hbars">{hire_rows or '<p class="empty">Hiring data appears after the people feed loads.</p>'}</div>
<div class="s-head" style="margin-top:30px"><h2>Where they are</h2></div>{_map(d["by_state"])}
<p class="meta">Firms on a product list by state. Click a state to see them.</p></div>
</div></section>
<section class="s"><div class="cols-21">
<div><div class="s-head"><h2>How complete the data is</h2>
<span class="more">Across the {d["scope"]:,} firms on a list</span></div>{cov_rows}
<p class="meta" style="margin-top:10px">{d["candidates"]:,} more addresses are pattern guesses
waiting for their mail server to confirm them.</p></div>
<div><div class="s-head"><h2>Working now</h2></div><div class="live">{live_html}</div>
<div class="s-head" style="margin-top:26px"><h2>Team pipeline</h2></div><div class="funnel">{funnel}</div></div>
</div></section>
<section class="s"><div class="cols-2">
<div><div class="s-head"><h2>Your firms</h2><a class="more" href="/firms?owner=me">All of them</a></div>{mine_html}</div>
<div><div class="s-head"><h2>Watching</h2></div>{watch_html}</div>
</div></section>
<p class="meta" style="margin-top:34px">{" . ".join(fresh)}</p>
</div>"""
    return page("Home", "home", body, orbs=True)
