"""Home: the first screen of the day.

Answers three questions in order, without a single click:

  1. How do the product lists stand? One row per product, tiers as numbers
     that open the list already filtered.
  2. Who should I call first? The best firms on each list with something
     fresh to say, each with the reason in its own words.
  3. What is mine? Firms I own, and firms I am watching that just moved.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from . import products, ui
from .webapp import (escn, TYPE_LABEL, conn, current_owner, esc, money, page, score_cell,
                     signal_cutoff, tier_chip)

router = APIRouter()

HOME_CSS = """
.hello{font:600 34px/1.15 Georgia,serif;letter-spacing:-.025em;margin:0}
.plist td{vertical-align:middle}
.plist .pn{font-weight:600;font-size:15px}
.plist .pa{color:var(--faint);font-size:12.5px;margin-top:2px}
.plist .tn{font:600 19px "Segoe UI",system-ui,sans-serif;font-variant-numeric:tabular-nums;
text-decoration:none;color:var(--ink)}
.plist .tn:hover{color:var(--red-hi)}
.plist .tn.zero{color:var(--faint);font-weight:400}
.plist .dot{display:inline-block;width:8px;height:8px;border-radius:99px;
margin-right:9px;vertical-align:1px}
.pick td{vertical-align:top}
.two{display:grid;grid-template-columns:1fr 1fr;gap:40px}
@media (max-width:1100px){.two{grid-template-columns:1fr}}
.fresh{font-size:12.5px;color:var(--faint);margin-top:34px;padding-top:14px;
border-top:1px solid var(--rule)}
"""


def _greeting() -> str:
    h = datetime.now().hour
    part = "morning" if h < 12 else "afternoon" if h < 18 else "evening"
    name = (current_owner() or "").split(" ")[0]
    return f"Good {part}{', ' + esc(name) if name else ''}"


@router.get("/", response_class=HTMLResponse)
def home(type: str = "", product: str = "", state: str = ""):
    # The inbox used to live at /. Old links with its filters land on Signals.
    if type or product or state:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(f"/signals?{ui_qs(type=type, product=product, state=state)}",
                                status_code=307)
    c = conn()
    cutoff = signal_cutoff()
    keys = products.product_keys()

    tiers: dict = {k: {} for k in keys}
    for r in c.execute("SELECT product, tier, COUNT(*) n FROM product_score"
                       " WHERE status='scored' GROUP BY product, tier"):
        tiers.setdefault(r["product"], {})[r["tier"]] = r["n"]
    sig: dict = {}
    for r in c.execute("""SELECT p.product, COUNT(DISTINCT t.crd) n
        FROM trigger_event t JOIN product_score p ON p.crd=t.crd
        LEFT JOIN trigger_action a ON a.trigger_id=t.id
        WHERE p.status='scored' AND p.tier IN ('A','B') AND t.suppressed=0
          AND a.state IS NULL AND t.detected_date >= ?
        GROUP BY p.product""", (cutoff,)):
        sig[r["product"]] = r["n"]

    prow = []
    for k in keys:
        p = products.product(k)
        t = tiers.get(k, {})
        total = sum(t.values())
        colour = {"PHH": "#c65454", "AcuBooth": "#cfa95c", "Glynac": "#63aa7c"}[p["family"]]

        def tn(label):
            n = t.get(label, 0)
            return (f'<a class="tn{" zero" if not n else ""}" '
                    f'href="/lists/{k}?tier={label}">{n:,}</a>')
        tier_cells = "".join(f'<td class="num">{tn(str(tt[1]))}</td>'
                             for tt in p["tiers"] if str(tt[1]) in ("A", "B", "C"))
        tier_cells += '<td class="num"></td>' * (3 - sum(
            1 for tt in p["tiers"] if str(tt[1]) in ("A", "B", "C")))
        s = sig.get(k, 0)
        prow.append(
            f'<tr class="go" data-href="/lists/{k}"><td><div class="pn">'
            f'<span class="dot" style="background:{colour}"></span>'
            f'<a href="/lists/{k}">{esc(p["name"])}</a></div>'
            f'<div class="pa">{esc(p["audience"])}</div></td>'
            f'{tier_cells}<td class="num">{total:,}</td>'
            f'<td class="num">{f"<a href=/signals?product={k}>{s:,}</a>" if s else "<span class=muted>0</span>"}</td></tr>')
    plist = (f'<table class="plist"><thead><tr><th>Product list</th>'
             f'<th class="num">Tier A</th><th class="num">Tier B</th>'
             f'<th class="num">Tier C</th><th class="num">Scored</th>'
             f'<th class="num" title="Tier A and B firms with a new signal in the '
             f'last 60 days">New signals</th></tr></thead>'
             f'<tbody>{"".join(prow)}</tbody></table>')

    # Call first: per product, the best A/B firms nobody else has claimed,
    # freshest signal first. Three each, so every product is represented.
    me = current_owner()
    picks = []
    for k in keys:
        rows = c.execute("""SELECT * FROM (
            SELECT p.crd, p.score, p.tier, p.detail_json, f.legal_name, f.city, f.state,
                   f.raum, s.owner, s.status,
                   (SELECT MAX(t.detected_date) FROM trigger_event t
                     LEFT JOIN trigger_action a ON a.trigger_id=t.id
                     WHERE t.crd=p.crd AND t.suppressed=0 AND a.state IS NULL
                       AND t.detected_date >= ?) AS fresh
            FROM product_score p
            JOIN firm_current f ON f.crd=p.crd
            LEFT JOIN firm_status s ON s.crd=p.crd
            WHERE p.product=? AND p.status='scored' AND p.tier IN ('A','B')
              AND (s.owner IS NULL OR s.owner='' OR s.owner=?)
              AND COALESCE(s.status,'') NOT IN ('disqualified','customer')) x
            ORDER BY (fresh IS NULL), tier, score DESC
            LIMIT 3""", (cutoff, k, me)).fetchall()
        for r in rows:
            picks.append((k, dict(r)))
    flags = ui.contact_flags(c, [r["crd"] for _, r in picks])
    trig_desc = {}
    if picks:
        ph = ",".join("?" * len(picks))
        for r in c.execute(f"""SELECT DISTINCT ON (crd) crd, trigger_type, description,
                detected_date FROM trigger_event
                WHERE crd IN ({ph}) AND suppressed=0 AND detected_date >= ?
                ORDER BY crd, detected_date DESC""",
                           tuple(r["crd"] for _, r in picks) + (cutoff,)):
            trig_desc[r["crd"]] = r
    pick_rows = []
    for k, r in picks:
        t = trig_desc.get(r["crd"])
        tcell = (f'<span class="chip lead">{esc(TYPE_LABEL.get(t["trigger_type"], t["trigger_type"]))}</span>'
                 f'<div class="meta">{esc(t["detected_date"])}</div>'
                 if t else '<span class="muted small">none new</span>')
        pick_rows.append(
            f'<tr class="go pick" data-href="/firm/{esc(r["crd"])}?p={k}">'
            f'<td><div class="firm"><a href="/firm/{esc(r["crd"])}?p={k}">'
            f'{escn(r["legal_name"])}</a></div><div class="meta">{ui.firm_meta(r)}</div></td>'
            f'<td><span class="chip">{esc(products.product(k)["name"])}</span></td>'
            f'<td>{tier_chip(r["tier"])}</td><td>{score_cell(r["score"], r["tier"])}</td>'
            f'<td class="why">{ui.why_line(r["detail_json"])}</td>'
            f'<td>{tcell}</td><td>{ui.contact_cell(flags[r["crd"]])}</td></tr>')
    picks_html = (f'<table><thead><tr><th>Firm</th><th>List</th><th>Tier</th>'
                  f'<th>Score</th><th>Why</th><th>New signal</th><th>Reach</th>'
                  f'</tr></thead><tbody>{"".join(pick_rows)}</tbody></table>'
                  if pick_rows else
                  '<p class="empty">No tier A or B firms yet. The product lists fill '
                  'in after the first scoring run on System.</p>')

    mine = []
    if me:
        mine = c.execute("""
            SELECT s.crd, s.status, s.updated_at, f.legal_name, f.state, f.city, f.raum,
                   sc.best_product, sc.best_tier, sc.best_score
            FROM firm_status s JOIN firm_current f ON f.crd=s.crd
            LEFT JOIN firm_scope sc ON sc.crd=s.crd
            WHERE s.owner=? ORDER BY s.updated_at DESC LIMIT 12""", (me,)).fetchall()
    mine_rows = "".join(
        f'<tr class="go" data-href="/firm/{esc(r["crd"])}"><td><div class="firm">'
        f'<a href="/firm/{esc(r["crd"])}">{escn(r["legal_name"])}</a></div>'
        f'<div class="meta">{ui.firm_meta(r)}</div></td>'
        f'<td><span class="chip">{esc(r["status"] or "claimed")}</span></td>'
        f'<td>{tier_chip(r["best_tier"]) if r["best_tier"] else ""} '
        f'<span class="small soft">{esc(ui.product_name(r["best_product"])) if r["best_product"] else ""}</span></td></tr>'
        for r in mine)
    mine_html = (f'<table><tbody>{mine_rows}</tbody></table>' if mine_rows else
                 '<p class="empty">Nothing claimed yet. Set a status on any firm page '
                 'and it lands here under your name.</p>')

    watched = c.execute("""
        SELECT w.crd, f.legal_name, f.state, f.city, f.raum,
               (SELECT description FROM trigger_event t WHERE t.crd=w.crd
                 AND t.suppressed=0 ORDER BY detected_date DESC LIMIT 1) AS last_desc,
               (SELECT MAX(detected_date) FROM trigger_event t WHERE t.crd=w.crd
                 AND t.suppressed=0) AS last_date
        FROM firm_watch w JOIN firm_current f ON f.crd=w.crd
        ORDER BY last_date DESC NULLS LAST LIMIT 10""").fetchall()
    watch_rows = "".join(
        f'<tr class="go" data-href="/firm/{esc(r["crd"])}"><td><div class="firm">'
        f'<a href="/firm/{esc(r["crd"])}">{escn(r["legal_name"])}</a></div>'
        f'<div class="meta">{ui.firm_meta(r)}</div></td>'
        f'<td class="why">{esc(r["last_desc"] or "No change since you started watching")}'
        f'<div class="meta">{esc(r["last_date"] or "")}</div></td></tr>' for r in watched)
    watch_html = (f'<table><tbody>{watch_rows}</tbody></table>' if watch_rows else
                  '<p class="empty">Star a firm on its page to follow it here.</p>')

    scope = c.execute("SELECT COUNT(*) n FROM firm_scope").fetchone()["n"]
    fresh_bits = []
    try:
        feed = c.execute("SELECT published_at FROM snapshot WHERE source_key='adv_feed'"
                         " ORDER BY id DESC LIMIT 1").fetchone()
        if feed:
            fresh_bits.append(f"SEC adviser feed of {esc(feed['published_at'])}")
        b = c.execute("SELECT COUNT(*) n FROM brochure WHERE status='ok'").fetchone()["n"]
        fresh_bits.append(f"{b:,} brochures read")
        m = c.execute("SELECT COUNT(*) n FROM firm_mail_platform").fetchone()["n"]
        fresh_bits.append(f"{m:,} email platforms checked")
        sc = c.execute("SELECT MAX(computed_at) t FROM product_score").fetchone()["t"]
        if sc:
            fresh_bits.append(f"scores computed {esc(sc[:16].replace('T', ' '))} UTC")
    except Exception:
        c.rollback()
    c.close()

    body = f"""<div class="pg">
<div class="head"><div><h1 class="hello">{_greeting()}</h1>
<div class="lede"><b>{scope:,}</b> firms are on at least one product list.
Open a list to work it, or start with the firms below.</div></div></div>
<section class="s"><div class="s-head"><h2>Product lists</h2>
<span class="more">Tier numbers open the list filtered to that tier</span></div>
{plist}</section>
<section class="s"><div class="s-head"><h2>Call first</h2>
<span class="more">Best unclaimed tier A and B firms on each list, newest signal first</span></div>
{picks_html}</section>
<section class="s"><div class="two">
<div><div class="s-head"><h2>Your firms</h2><a class="more" href="/firms?owner={esc(me)}">All of them</a></div>{mine_html}</div>
<div><div class="s-head"><h2>Watching</h2></div>{watch_html}</div>
</div></section>
<p class="fresh">{" &middot; ".join(fresh_bits)} &middot; <a href="/health">System</a></p>
</div>"""
    return page("Home", "home", body, HOME_CSS)


def ui_qs(**kw) -> str:
    from .webapp import qs_join
    return qs_join(**kw)
