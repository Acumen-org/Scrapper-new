"""Firm detail: the click-through target from the inbox, structured as call prep.

Two rules this page exists to enforce:

  1. A classification is shown with its inputs beside it. Fund gross asset value,
     RAUM ratio, investor count and minimum investment sit in one row above the
     verdict, so anyone can disagree with the call and see exactly what drove it.
     A wrong classification must never be invisible.

  2. Every archive-derived figure carries its as-of date, not just Schwab share.
     The historical archive ends 2024-12-31 and every firm has amended since.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from .webapp import (CAVEATS, PAGE_CSS, TYPE_LABEL, caveat, conn, current_owner,
                     esc, money, nav)

router = APIRouter()

STATUSES = ["new", "working", "meeting set", "qualified", "disqualified", "customer"]

DETAIL_CSS = PAGE_CSS + """
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
.card h2{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.09em;
color:var(--faint);font-weight:600}
.reach{display:flex;gap:9px;align-items:baseline;padding:5px 0;font-size:13.5px;
border-bottom:1px solid var(--rule)}
.reach:last-of-type{border-bottom:0}
.reach .meta{margin-top:0}
.whyempty{color:var(--faint);font-size:13px;line-height:1.6;margin:2px 0 0}
.kv{display:grid;grid-template-columns:150px 1fr;gap:5px 12px;font-size:13.5px}
.kv dt{color:var(--soft)}
.kv dd{margin:0}
.inputs{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:10px 0 4px}
.inputs div{border:1px solid var(--rule);padding:8px 10px}
.inputs .n{font-size:19px;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.inputs .l{font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--faint)}
.verdict{padding:9px 12px;border-left:3px solid var(--ok);background:var(--ok-bg);
font-size:13.5px;margin-top:8px;border-radius:0 8px 8px 0}
.verdict.sponsor,.verdict.competitor{border-left-color:var(--red);background:var(--red-bg)}
.asof{font-size:11px;color:var(--faint);white-space:nowrap}
textarea{width:100%;min-height:90px}
pre.prep{white-space:pre-wrap;background:var(--side);border:1px solid var(--rule);
border-radius:8px;padding:12px;font:12.5px/1.55 ui-monospace,Consolas,monospace;
max-height:340px;overflow:auto}
.back{font-size:13px}
"""


def _pretty_name(filed: str) -> str:
    """Schedule A files names as 'LAST, FIRST, MIDDLE'. People read the other
    order."""
    parts = [p.strip() for p in filed.split(",") if p.strip()]
    if len(parts) >= 2:
        return " ".join(parts[1:] + parts[:1]).title()
    return filed.title()


def _yearpos(iso: str) -> float:
    """A date as a fractional year, for honest x positions."""
    y, m, d = int(iso[:4]), int(iso[5:7] or 1), int(iso[8:10] or 1)
    return y + (m - 1) / 12 + (d - 1) / 365


def aum_chart(history) -> str:
    """Regulatory AUM over time as an inline SVG with axes.

    Every mark answers a question: the y gridlines say what the dollars are,
    the year scale says when, the dots are the actual filings (hover one for
    its date and value), and the label on the right is where the firm is now."""
    if len(history) < 2:
        return ('<p style="color:var(--faint);font-size:13px">Only one filing on '
                'record, so there is no trajectory to draw yet. The weekly feed '
                'adds a point whenever the firm files.</p>')
    vals = [h["raum"] for h in history]
    xs = [_yearpos(h["filing_date"]) for h in history]
    lo, hi = min(vals), max(vals)
    x0, x1 = xs[0], xs[-1]
    W, H, PADL, PADR, PADT, PADB = 640, 150, 8, 74, 10, 22
    rngy = (hi - lo) or 1
    rngx = (x1 - x0) or 1

    def X(x): return PADL + (x - x0) / rngx * (W - PADL - PADR)
    def Y(v): return PADT + (1 - (v - lo) / rngy) * (H - PADT - PADB)

    pts = " ".join(f"{X(x):.1f},{Y(v):.1f}" for x, v in zip(xs, vals))
    area = (f"{X(xs[0]):.1f},{H-PADB} " + pts + f" {X(xs[-1]):.1f},{H-PADB}")

    # Y gridlines at low, middle, high; labels on the right where the eye
    # lands after following the line.
    grid = []
    for v in (lo, (lo + hi) / 2, hi):
        y = Y(v)
        grid.append(f'<line x1="{PADL}" y1="{y:.1f}" x2="{W-PADR}" y2="{y:.1f}" '
                    f'stroke="var(--rule)" stroke-width="1"/>'
                    f'<text x="{W-PADR+6}" y="{y+3.5:.1f}" fill="var(--faint)" '
                    f'font-size="10.5" font-family="Segoe UI">{money(v)}</text>')

    # Year ticks: at most ~7 labels however long the span is.
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

    # Every filing is a real, hoverable point.
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
        f'<div class="meta" style="margin-bottom:6px">Regulatory AUM as filed, '
        f'{len(history)} filings from {esc(history[0]["filing_date"][:4])} to '
        f'{esc(history[-1]["filing_date"][:4])}. Now {money(vals[-1])}{growth}. '
        f'Hover a point for its filing.</div>'
        f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:{H}px;display:block">'
        f'{"".join(grid)}{"".join(ticks)}'
        f'<polygon points="{area}" fill="var(--ok)" opacity=".07"/>'
        f'<polyline points="{pts}" fill="none" stroke="var(--ok)" '
        f'stroke-width="1.8"/>{dots}'
        f'<circle cx="{X(xs[-1]):.1f}" cy="{Y(vals[-1]):.1f}" r="3.6" '
        f'fill="var(--ok)"/></svg>')


def call_prep(f, seg, prof, trigs, funds) -> str:
    """Plain text, paste-ready. Every archive figure carries its as-of date."""
    L = [f"{f['legal_name']}  (CRD {f['crd']})"]
    if f["business_name"] and f["business_name"] != f["legal_name"]:
        L.append(f"dba {f['business_name']}")
    L.append(" ".join(x for x in (f["city"], f["state"], f["website"]) if x))
    L.append("")
    L.append(f"RAUM {money(f['raum'])}  ({money(f['raum_disc'])} discretionary)")
    hs = (f["hnw_aum"] or 0) / f["raum"] * 100 if f["raum"] else 0
    L.append(f"High net worth: {f['hnw_clients'] or 0} clients, "
             f"{money(f['hnw_aum'])} ({hs:.0f}% of RAUM)")
    L.append(f"Other individuals: {f['retail_clients'] or 0} clients, {money(f['retail_aum'])}")
    L.append(f"{f['iar_count'] or 0} adviser reps, {f['total_employees'] or 0} employees")
    L.append(f"Last ADV filing {f['filing_date']}, registered {f['registered_date']}")
    if f["disciplinary"] == "Y":
        L.append("NOTE: discloses a disciplinary event at Item 11")

    if prof:
        L.append("")
        line = f"Custodian: {prof['primary_canonical'] or 'unknown'}"
        if prof["schwab_share_reported"] is not None:
            line += (f", Schwab {prof['schwab_share_reported'] * 100:.0f}% of REPORTED "
                     f"custodian assets (as of {prof['as_of_filing_date']})")
        L.append(line)
        L.append("  Caveat: only custodians at 10%+ of SMA assets are reported, so this is an")
        L.append("  upper bound. Schwab presence indicates the late-2026 institutional")
        L.append("  opportunity, not accounts sellable today.")

    if seg:
        L.append("")
        L.append(f"Real estate classification: {seg['segment'].upper()} "
                 f"(as of {seg['as_of_filing_date']})")
        L.append(f"  {seg['rationale']}")
        L.append(f"  fund {money(seg['total_gav'])} / {(seg['raum_ratio'] or 0) * 100:.1f}% of "
                 f"RAUM / {seg['total_owners'] or 0} investors / min "
                 f"{money(seg['min_investment'])}")

    if funds:
        L.append("")
        L.append("Private funds advised:")
        for x in funds:
            L.append(f"  {x['fund_type']}: {x['fund_name']} - {money(x['gross_asset_value'])}, "
                     f"{x['owners'] or 0} investors, min {money(x['minimum_investment'])} "
                     f"(as of {x['d']})")

    if trigs:
        L.append("")
        L.append("Trigger history:")
        for t in trigs:
            L.append(f"  {t['detected_date']}  {t['description']}")

    L.append("")
    L.append("Schedule D figures derive from the SEC historical archive ending 2024-12-31.")
    L.append("The firm has filed at least one amendment since.")
    return "\n".join(L)


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
"""


@router.get("/firm/{crd}", response_class=HTMLResponse)
def firm_detail(crd: str):
    c = conn()
    f = c.execute("SELECT * FROM firm_current WHERE crd=?", (crd,)).fetchone()
    if f is None:
        return HTMLResponse(f"<p>No firm with CRD {esc(crd)}</p>", status_code=404)

    seg = c.execute("SELECT * FROM re_segment WHERE crd=?", (crd,)).fetchone()
    prof = c.execute("SELECT * FROM firm_custodian_profile WHERE crd=?", (crd,)).fetchone()
    trigs = c.execute("""SELECT t.*, a.state FROM trigger_event t
        LEFT JOIN trigger_action a ON a.trigger_id = t.id
        WHERE t.crd = ? ORDER BY t.detected_date DESC""", (crd,)).fetchall()
    funds = c.execute("""
        WITH latest AS (SELECT MAX(fc.filing_date) d FROM sched_d_7b1 s
          JOIN filing_crd fc ON fc.filing_id = s.filing_id WHERE s.crd = ?)
        SELECT s.*, fc.filing_date d FROM sched_d_7b1 s
        JOIN filing_crd fc ON fc.filing_id = s.filing_id, latest
        WHERE s.crd = ? AND fc.filing_date = latest.d
        ORDER BY s.gross_asset_value DESC""", (crd, crd)).fetchall()
    note = c.execute("SELECT note FROM firm_note WHERE crd=?", (crd,)).fetchone()
    fs = c.execute("SELECT * FROM firm_status WHERE crd=?", (crd,)).fetchone()
    cur_status = fs["status"] if fs else ""
    status_opts = "".join(
        f'<option value="{esc(x)}"{" selected" if x == cur_status else ""}>{esc(x)}</option>'
        for x in [""] + STATUSES)

    def maybe(sql, args=()):
        try:
            return c.execute(sql, args).fetchall()
        except Exception:
            return []

    holdings = maybe("""
        SELECT ticker, quarter, MAX(value_usd) v, MAX(shares) sh
        FROM holding_13f WHERE crd=? GROUP BY ticker, quarter
        ORDER BY ticker, quarter""", (crd,))
    match = maybe("""SELECT * FROM adv_13f_match WHERE crd=?
                     ORDER BY confidence DESC LIMIT 1""", (crd,))
    match = match[0] if match else None
    tags = maybe("""SELECT * FROM brochure_tag WHERE crd=?
                    ORDER BY present DESC, confidence DESC""", (crd,))
    contacts = maybe("""SELECT * FROM contact WHERE crd=? ORDER BY title LIMIT 14""", (crd,))
    emails = maybe("""SELECT * FROM contact_email WHERE crd=? ORDER BY status, email""", (crd,))
    filed_info = maybe("""SELECT * FROM firm_contact_info WHERE crd=?
                          ORDER BY kind, id""", (crd,))
    scanned = bool(maybe("SELECT 1 FROM contact_scan WHERE crd=?", (crd,)))
    officers = maybe("""SELECT * FROM schedule_a WHERE crd=? AND is_individual=1
                        ORDER BY (control_person!='Y'), name LIMIT 20""", (crd,))
    web_people = maybe("""SELECT * FROM web_contact WHERE crd=? AND person IS NOT NULL
                          ORDER BY id LIMIT 20""", (crd,))
    web_firm = maybe("""SELECT * FROM web_contact WHERE crd=? AND person IS NULL
                        ORDER BY (email IS NULL), id LIMIT 6""", (crd,))
    history = maybe("""SELECT filing_date, raum FROM firm_history
                       WHERE crd=? AND raum IS NOT NULL
                       ORDER BY filing_date""", (crd,))
    watched = bool(maybe("SELECT 1 FROM firm_watch WHERE crd=?", (crd,)))
    bro = maybe("SELECT * FROM brochure WHERE crd=?", (crd,))
    bro = bro[0] if bro else None

    hs = (f["hnw_aum"] or 0) / f["raum"] * 100 if f["raum"] else 0

    def asof(d):
        return f'<span class="asof">as of {esc(d)}</span>' if d else ""

    if seg:
        cls = seg["segment"] if seg["segment"] in ("sponsor", "competitor") else ""
        seg_html = (
            '<div class="inputs">'
            f'<div><div class="n">{money(seg["total_gav"])}</div>'
            '<div class="l">Fund gross asset value</div></div>'
            f'<div><div class="n">{(seg["raum_ratio"] or 0) * 100:.1f}%</div>'
            '<div class="l">Share of RAUM</div></div>'
            f'<div><div class="n">{seg["total_owners"] or 0}</div>'
            '<div class="l">Investors</div></div>'
            f'<div><div class="n">{money(seg["min_investment"])}</div>'
            '<div class="l">Minimum investment</div></div>'
            '</div>'
            f'<div class="verdict {cls}"><b>{esc(seg["segment"].upper())}</b>: '
            f'{esc(seg["rationale"])}<br>{caveat("archive_as_of", "archive-derived")} '
            f'{asof(seg["as_of_filing_date"])}</div>')
    else:
        seg_html = ('<p class="whyempty">This firm advises no real estate fund, '
                    'so it was never a candidate for the sponsor-or-prospect '
                    'classification. For Prairie Hill that is the common and '
                    'often better case: no competing product of their own.</p>')

    trows = "".join(
        f'<tr><td style="white-space:nowrap">{esc(t["detected_date"])}</td>'
        f'<td><span class="chip">'
        f'{esc(TYPE_LABEL.get(t["trigger_type"], t["trigger_type"]))}</span></td>'
        f'<td>{esc(t["description"])}</td>'
        f'<td class="pri">{(t["priority"] or 0):+.2f}</td>'
        f'<td>{esc(t["state"] or "")}</td></tr>' for t in trigs) or \
        ('<tr><td colspan="5" class="whyempty">Nothing has changed at this firm '
         'since tracking began: no AUM jump, no custodian move, no headcount '
         'growth. Steady is a finding, not missing data.</td></tr>')

    # An empty section must say WHY it is empty, or it reads as broken. Most
    # firms this size run no private funds; that is information, not a gap.
    if funds:
        frows = "".join(
            f'<tr><td>{esc(x["fund_type"])}</td><td>{esc(x["fund_name"])}</td>'
            f'<td>{money(x["gross_asset_value"])}</td><td>{x["owners"] or 0}</td>'
            f'<td>{money(x["minimum_investment"])}</td><td>{asof(x["d"])}</td></tr>'
            for x in funds)
        funds_html = ('<table><thead><tr><th>Type</th><th>Fund</th>'
                      '<th>Gross asset value</th><th>Investors</th>'
                      '<th>Minimum</th><th>As of</th></tr></thead>'
                      f'<tbody>{frows}</tbody></table>')
    else:
        funds_html = ('<p class="whyempty">No private funds on Schedule D 7.B(1). '
                      'That is typical: only about one in five firms this size '
                      'advises any private fund. For Prairie Hill it means the '
                      'illiquid-fund conversation starts from zero here.</p>')

    schwab = "-"
    if prof and prof["schwab_share_reported"] is not None:
        schwab = (caveat("schwab_share_reported",
                         f'{prof["schwab_share_reported"] * 100:.0f}% of reported')
                  + " " + asof(prof["as_of_filing_date"]))

    website = (f'<a href="{esc(f["website"])}" target="_blank" rel="noopener">'
               f'{esc(f["website"])}</a>') if f["website"] else "-"
    disc = ('&middot; <b style="color:#9e3b2e">discloses a disciplinary event</b>'
            if f["disciplinary"] == "Y" else "")
    est = money((f["raum"] or 0) / f["clients_total"]) if f["clients_total"] else "-"

    # ---- 13F holdings card. Match confidence is shown honestly next to the data.
    if holdings:
        by_ticker: dict[str, list] = {}
        for h in holdings:
            by_ticker.setdefault(h["ticker"], []).append(h)
        hrows = []
        for tk, seq in sorted(by_ticker.items()):
            seq.sort(key=lambda x: x["quarter"])
            latest = seq[-1]
            arc = " &rarr; ".join(f'{x["quarter"]} {money(x["v"])}' for x in seq)
            opened = (len(seq) == 1 and len({s["quarter"] for s in holdings}) > 1)
            hrows.append(
                f'<tr><td><b>{esc(tk)}</b></td>'
                f'<td class="num">{money(latest["v"])}</td>'
                f'<td class="num">{latest["sh"] or "-"}</td>'
                f'<td style="font-size:12px">{arc}</td>'
                f'<td>{"recently opened" if opened else ""}</td></tr>')
        conf_note = ""
        if match:
            band = ("auto-accepted" if match["status"] == "auto"
                    else esc(match["status"]))
            conf_note = (f'<p class="asof">ADV-to-13F link: {esc(match["name_edgar"] or "")} '
                         f'(CIK {esc(match["cik"])}), confidence {match["confidence"]:.2f}, '
                         f'{band}, basis {esc(match["match_basis"])}. A wrong link would put '
                         f'holdings the firm does not own into this page.</p>')
        holdings_html = (f'{conf_note}<table><thead><tr><th>Ticker</th>'
                         '<th class="num">Latest value</th><th class="num">Shares</th>'
                         '<th>By quarter</th><th></th></tr></thead>'
                         f'<tbody>{"".join(hrows)}</tbody></table>')
    else:
        holdings_html = ('<p style="color:var(--faint)">No target-security 13F holdings. '
                         'Absence is not a negative signal: the 13F threshold is $100M '
                         'in listed equities and most firms this size never file.</p>')

    # ---- brochure tags card, verbatim snippet next to each tag
    if tags:
        trows2 = []
        for t in tags:
            state = ("<b style='color:var(--red-hi)'>states they do NOT</b>" if not t["present"]
                     else (f'{t["confidence"]:.2f}'
                           + (' <span class="asof">damped, negation pending review</span>'
                              if (t["negation_damp"] or 1.0) < 1.0 else "")))
            trows2.append(
                f'<tr><td><b>{esc(t["tag"])}</b>'
                f'<div class="meta">via "{esc(t["best_phrase"] or "")}", {t["hits"]} hits'
                f'{", Item " + str(t["section_item"]) if t["section_item"] else ""}</div></td>'
                f'<td class="num">{state}</td>'
                f'<td style="font-size:12.5px">&ldquo;{esc(t["best_snippet"] or "")}&rdquo;</td></tr>')
        bro_meta = (f'<p class="asof">Brochure: {esc(bro["brochure_name"] or "")}, filed '
                    f'{esc(bro["date_submitted"] or "?")}, {bro["pages"] or "?"} pages. '
                    'Deterministic phrase matching, no model calls.</p>') if bro else ""
        tags_html = (f'{bro_meta}<table><thead><tr><th style="width:220px">Tag</th>'
                     '<th class="num" style="width:150px">Confidence</th>'
                     '<th>Supporting sentence, verbatim</th></tr></thead>'
                     f'<tbody>{"".join(trows2)}</tbody></table>')
    elif bro:
        tags_html = (f'<p class="whyempty">Brochure processed ({esc(bro["status"])}); '
                     'none of the vocabulary phrases (covered calls, real estate, '
                     'alternatives, held-away accounts) appear in it. The firm '
                     'simply does not talk about these things in its Part 2A.</p>')
    else:
        tags_html = ('<p class="whyempty">Brochure not fetched yet. The brochure '
                     'coverage job on <a href="/health">Pipeline health</a> works '
                     'through all in-band firms; this one is still in the queue.</p>')

    # AUM trajectory: a real chart, not a decorative line. Time on the x axis
    # (filings are not evenly spaced, so index position lied about pace),
    # dollar gridlines on the y, a year scale underneath, and every filing
    # point hoverable with its date and value.
    spark_html = aum_chart(history)

    # How to reach them: real filed details first, guesses clearly second.
    # Order of trust: the firm's own brochure, then its Form ADV, then pattern
    # guesses, which stay visually subordinate because they are guesses.
    reach = []
    if f["phone"]:
        reach.append(f'<div class="reach"><span class="chip lead">filed</span>'
                     f'<b>{esc(f["phone"])}</b>'
                     f'<span class="meta">main office, Form ADV</span></div>')
    for r in filed_info:
        label = "email" if r["kind"] == "email" else "phone"
        val = (f'<a href="mailto:{esc(r["value"])}">{esc(r["value"])}</a>'
               if label == "email" else f'<b>{esc(r["value"])}</b>')
        ctx = esc((r["context"] or "")[:70])
        reach.append(f'<div class="reach"><span class="chip lead">filed</span>'
                     f'{val}<span class="meta" title="{ctx}">brochure, page 1-3'
                     f'{", " + ctx if ctx else ""}</span></div>')
    filed_vals = {f["phone"]} | {r["value"] for r in filed_info}
    for r in web_firm:
        val = r["email"] or r["phone"]
        if not val or val in filed_vals:
            continue
        filed_vals.add(val)
        shown = (f'<a href="mailto:{esc(val)}">{esc(val)}</a>'
                 if r["email"] else f"<b>{esc(val)}</b>")
        reach.append(f'<div class="reach"><span class="chip">their site</span>'
                     f'{shown}<span class="meta">from the firm&rsquo;s own '
                     f'website</span></div>')
    if not filed_info:
        reach_note = ("No email or phone beyond the main line appears in the "
                      "first pages of this firm's brochure." if scanned else
                      "Brochure not yet scanned for contact details; the "
                      "contact extraction job fills this in.")
        reach.append(f'<p class="meta" style="margin:4px 0 0">{reach_note}</p>')
    reach_html = "".join(reach)

    # Leadership first: Schedule A names who runs and owns the firm, with the
    # title as filed. Reps from the individual feed follow. Web-found details
    # (email, phone, title from the firm's own site) attach to matching names.
    web_by_name = {}
    for w in web_people:
        web_by_name.setdefault(w["person"].lower(), w)

    def person_extra(name: str) -> str:
        w = web_by_name.get(name.lower())
        if not w:
            return ""
        bits = []
        if w["email"]:
            bits.append(f'<a href="mailto:{esc(w["email"])}">{esc(w["email"])}</a>')
        if w["phone"]:
            bits.append(esc(w["phone"]))
        if not bits:
            return ""
        return (f'<div class="meta">{" &middot; ".join(bits)} '
                f'<span class="chip lead" style="margin-left:4px">their site</span></div>')

    orows = ""
    if officers:
        def own(o):
            c_ = " &middot; control person" if o["control_person"] == "Y" else ""
            return f'{esc(o["title"] or "")}{c_}'
        orows = "".join(
            f'<tr><td><b>{esc(_pretty_name(o["name"]))}</b>{person_extra(_pretty_name(o["name"]))}</td>'
            f'<td style="font-size:12.5px;color:var(--soft)">{own(o)}'
            f'<div class="meta">Schedule A, as of {esc(o["as_of"] or "archive")}</div></td></tr>'
            for o in officers)

    if contacts or officers:
        crows = "".join(
            f'<tr><td><b>{esc(p["name"])}</b>{person_extra(p["name"])}</td>'
            f'<td style="font-size:12.5px;color:var(--soft)">{esc(p["title"] or "")}</td></tr>'
            for p in contacts
            # a rep who is also an officer already has a better row above
            if not officers or p["name"].lower() not in
            {_pretty_name(o["name"]).lower() for o in officers})
        crows = orows + crows
        erows = ""
        if emails:
            chipfor = {"valid": "lead", "invalid": "dis", "queued": "partial",
                       "candidate": "", "unverifiable": "partial", "error": "dis"}
            erows = "<div style='margin-top:10px'>" + "".join(
                f'<div style="display:flex;gap:8px;align-items:center;padding:3px 0;'
                f'font-size:12.5px"><span class="chip {chipfor.get(e["status"], "")}">'
                f'{esc(e["status"])}</span> {esc(e["email"])}'
                f'<span class="meta">{esc(e["name"] or "")}</span></div>'
                for e in emails) + "</div>"
        people_html = (
            f'<table><tbody>{crows}</tbody></table>{erows}'
            f'<form method="post" action="/firm/{esc(crd)}/emails" style="margin-top:10px;'
            f'display:flex;gap:8px">'
            f'<button type="submit">Guess email patterns</button>'
            f'<button type="submit" name="queue" value="1" '
            f'title="Marks candidates for the email verification autopilot job">'
            f'Queue for verification</button></form>'
            f'<p class="meta" style="margin-top:6px">Pattern guesses against the '
            f'firm&rsquo;s own mail domain (from its brochure when possible). '
            f'Guesses are never shown as real; only rows marked valid have been '
            f'individually verified, and accept-all domains can never verify.</p>')
    else:
        people_html = ('<p class="whyempty">Nobody on file yet: no Schedule A '
                       'roster in the archive (state-registered firms are not in '
                       'the SEC archive) and no reps in the individual feed. The '
                       'web enrichment job reads the firm&rsquo;s own site for '
                       'its team as coverage grows.</p>')

    star = ("&#9733; Watching" if watched else "&#9734; Watch")
    watch_html = (f'<form method="post" action="/watch/{esc(crd)}" style="display:inline">'
                  f'<input type="hidden" name="back" value="/firm/{esc(crd)}">'
                  f'<button type="submit" class="{"primary" if watched else ""}">'
                  f'{star}</button></form>')

    prep = call_prep(f, seg, prof, trigs, funds)
    reach_lines = []
    if f["phone"]:
        reach_lines.append(f"  {f['phone']} (main office, as filed on Form ADV)")
    for r in filed_info:
        src = "from the firm's brochure"
        reach_lines.append(f"  {r['value']} ({r['kind']}, {src})")
    for w in web_people:
        bits = " / ".join(x for x in (w["email"], w["phone"]) if x)
        if bits:
            reach_lines.append(f"  {bits} ({w['person']}, from the firm's website)")
    if reach_lines:
        prep += "\n\nHow to reach them (filed by the firm itself):\n" + \
                "\n".join(reach_lines)
    if officers:
        prep += ("\n\nWho runs it (Schedule A, as of "
                 f"{officers[0]['as_of'] or 'archive'}):\n") + "\n".join(
            f"  {_pretty_name(o['name'])}: {o['title'] or ''}"
            + (" (control person)" if o["control_person"] == "Y" else "")
            for o in officers[:6])
    if contacts:
        prep += "\n\nPeople (from the SEC individual adviser feed):\n" + "\n".join(
            f"  {p['name']}: {p['title']}" for p in contacts[:5])
    if holdings:
        prep += "\n\n13F target holdings (latest quarter, via matched CIK"
        if match:
            prep += f", link confidence {match['confidence']:.2f}"
        prep += "):\n" + "\n".join(
            f"  {tk}: " + ", ".join(f"{x['quarter']} {money(x['v'])}"
                                    for x in sorted(seq, key=lambda x: x['quarter']))
            for tk, seq in sorted(by_ticker.items()))
    if tags:
        prep += "\n\nBrochure language (their own words):\n" + "\n".join(
            f"  {t['tag']}{' (they state they do NOT)' if not t['present'] else ''}: "
            f"\"{t['best_snippet']}\"" for t in tags[:6])

    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>{esc(f['legal_name'])}</title><style>{DETAIL_CSS}</style>
{nav("firms")}
<header><h1>{esc(f['legal_name'])}</h1>
<div class="sub">CRD {esc(crd)} &middot; {esc(f['city'] or '')} {esc(f['state'] or '')}
&middot; {esc(f['firm_type'])} &middot; last filed {esc(f['filing_date'])} {disc}
&nbsp; {watch_html}</div></header>
<div class="wrap">
<p class="back"><a href="/">&larr; Trigger inbox</a> &middot;
<a href="/firms">Firm list</a></p>
<div class="grid">
  <div class="card"><h2>Identity and scale</h2><dl class="kv">
    <dt>Legal name</dt><dd>{esc(f['legal_name'])}</dd>
    <dt>Doing business as</dt><dd>{esc(f['business_name'] or '-')}</dd>
    <dt>Website</dt><dd>{website}</dd>
    <dt>Phone</dt><dd>{esc(f['phone'] or '-')}</dd>
    <dt>SEC number</dt><dd>{esc(f['sec_number'] or '-')}</dd>
    <dt>Registered</dt><dd>{esc(f['registered_date'] or '-')}</dd>
    <dt>RAUM</dt><dd>{money(f['raum'])} ({money(f['raum_disc'])} discretionary)</dd>
    <dt>Adviser reps</dt><dd>{f['iar_count'] or 0}</dd>
    <dt>Employees</dt><dd>{f['total_employees'] or 0}</dd>
  </dl></div>
  <div class="card"><h2>Client mix</h2><dl class="kv">
    <dt>High net worth</dt><dd><b>{f['hnw_clients'] or 0}</b> clients &middot;
      {money(f['hnw_aum'])} &middot; <b>{hs:.0f}%</b> of RAUM</dd>
    <dt>Other individuals</dt><dd>{f['retail_clients'] or 0} clients &middot;
      {money(f['retail_aum'])}</dd>
    <dt>Total clients</dt><dd>{f['clients_total'] or 0}
      <span class="asof">summed from Item 5.D</span></dd>
    <dt>Est. client size</dt><dd>{caveat('est_avg_client_size', est)}</dd>
    <dt>Custodian</dt><dd>{esc(prof['primary_canonical']) if prof else '-'}</dd>
    <dt>Schwab share</dt><dd>{schwab}</dd>
    <dt>Item 5.K(3) / 7.B</dt><dd>{esc(f['q5k3'] or '-')} / {esc(f['q7b'] or '-')}</dd>
  </dl></div>
</div>
<div class="grid">
  <div class="card"><h2>How to reach them</h2>{reach_html}</div>
  <div class="card"><h2>People</h2>{people_html}</div>
</div>
<div class="card" style="margin-top:14px"><h2>AUM trajectory</h2>{spark_html}</div>
<div class="card" style="margin-top:14px"><h2>Real estate classification, with the inputs it was made from</h2>
{seg_html}</div>
<div class="card" style="margin-top:14px"><h2>Private funds advised</h2>
{funds_html}</div>
<div class="card" style="margin-top:14px">
<h2>13F target holdings, with the link that produced them</h2>
{holdings_html}</div>
<div class="card" style="margin-top:14px">
<h2>Brochure tags, each with the firm's own sentence</h2>
{tags_html}</div>
<div class="card" style="margin-top:14px"><h2>Trigger history</h2>
<table><thead><tr><th style="width:100px">Date</th><th style="width:180px">Type</th>
<th>What happened</th><th style="width:80px">Priority</th>
<th style="width:90px">State</th></tr></thead><tbody>{trows}</tbody></table></div>
<div class="grid" style="margin-top:14px;grid-template-columns:1fr 1fr 1.4fr">
  <div class="card"><h2>Ownership and status</h2>
    <form method="post" action="/firm/{esc(crd)}/status" class="filters" style="border:0;padding:0;margin:0">
      <label>Status<select name="status">{status_opts}</select></label>
      <label>Owner<input type="text" name="owner" value="{esc(fs['owner'] if fs else '')}"
        placeholder="who works this firm"></label>
      <button class="primary" type="submit">Save</button>
    </form>
    <p class="meta" style="margin-top:8px">Status and owner live only in this tool;
    the CSV export carries them for manual import into Twenty.</p>
  </div>
  <div class="card"><h2>Notes</h2>
    <form method="post" action="/firm/{esc(crd)}/note">
      <textarea name="note" placeholder="Call notes, context, next step">{esc(note['note'] if note else '')}</textarea>
      <div style="margin-top:8px"><button class="primary" type="submit">Save note</button></div>
    </form></div>
  <div class="card"><h2>Call prep summary</h2>
    <button class="primary" onclick="copyPrep()">Copy to clipboard</button>
    <span id="copied" style="margin-left:9px;color:var(--ok);font-size:13px"></span>
    <pre class="prep" id="prep">{esc(prep)}</pre></div>
</div>
</div>
<script>{COPY_JS}</script>""")


@router.post("/firm/{crd}/note")
def save_note(crd: str, note: str = Form("")):
    c = conn()
    c.execute("INSERT INTO firm_note (crd,note,updated_at) VALUES (?,?,?)"
              " ON CONFLICT(crd) DO UPDATE SET note=excluded.note,"
              " updated_at=excluded.updated_at",
              (crd, note, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    c.commit()
    return RedirectResponse(f"/firm/{crd}", status_code=303)


@router.post("/firm/{crd}/status")
def save_status(crd: str, status: str = Form(""), owner: str = Form("")):
    # Claiming a firm without naming an owner means you: three people sharing a
    # queue need to see who took what, and the signed-in name is the truth.
    if status and not owner.strip():
        owner = current_owner()
    c = conn()
    c.execute("INSERT INTO firm_status (crd,status,owner,updated_at) VALUES (?,?,?,?)"
              " ON CONFLICT(crd) DO UPDATE SET status=excluded.status,"
              " owner=excluded.owner, updated_at=excluded.updated_at",
              (crd, status or None, owner or None,
               datetime.now(timezone.utc).isoformat(timespec="seconds")))
    c.commit()
    return RedirectResponse(f"/firm/{crd}", status_code=303)


# Domains a guessed employee address can never live on. Generating against one
# of these produced 27 confidently wrong @linkedin.com addresses, one of which a
# checker even blessed as valid. A guess needs the firm's own mail domain or it
# is worse than no guess.
BAD_EMAIL_DOMAINS = ("linkedin.", "facebook.", "twitter.", "x.com", "instagram.",
                     "youtube.", "tiktok.", "medium.", "vimeo.", "spotify.",
                     "pinterest.", "yelp.", "gmail.", "yahoo.", "hotmail.",
                     "outlook.", "aol.", "icloud.", "threads.")


def email_domain_for(c, crd: str) -> tuple[str | None, str]:
    """The firm's real mail domain and where it came from.

    Priority: the domain of an email address the firm itself printed in its
    brochure (ground truth), then the filed website (good since the social
    address fix), then nothing. Never a social or freemail domain."""
    import re as _re
    row = c.execute("""SELECT value FROM firm_contact_info
        WHERE crd=? AND kind='email' ORDER BY id LIMIT 1""", (crd,)).fetchone()
    if row:
        dom = row["value"].rsplit("@", 1)[-1].lower()
        if not any(b in dom for b in BAD_EMAIL_DOMAINS):
            return dom, "an email address in the firm's own brochure"
    f = c.execute("SELECT website FROM firm_current WHERE crd=?", (crd,)).fetchone()
    if f and f["website"]:
        m = _re.search(r"(?:https?://)?(?:www\.)?([a-z0-9.-]+\.[a-z]{2,})",
                       f["website"].lower())
        if m and not any(b in m.group(1) for b in BAD_EMAIL_DOMAINS):
            return m.group(1), "the firm's filed website"
    return None, ("no usable domain: the firm filed no website, or only a "
                  "social media page")


@router.post("/firm/{crd}/emails")
def gen_emails(crd: str, queue: str = Form("")):
    """Pattern-guess emails for the firm's top contacts against the firm's own
    mail domain. Candidates stay candidates until verification confirms them,
    and no candidate is ever generated on a domain the firm cannot receive
    mail at."""
    import re as _re
    c = conn()
    domain, _why = email_domain_for(c, crd)
    if domain:
        people = c.execute("SELECT name FROM contact WHERE crd=? LIMIT 6", (crd,)).fetchall()
        for p in people:
            parts = [x for x in _re.sub(r"[^a-z ]", "", p["name"].lower()).split() if x]
            if len(parts) < 2:
                continue
            first, last = parts[0], parts[-1]
            for pat, addr in (("first.last", f"{first}.{last}@{domain}"),
                              ("flast", f"{first[0]}{last}@{domain}"),
                              ("first", f"{first}@{domain}")):
                c.execute("INSERT OR IGNORE INTO contact_email"
                          " (crd,name,email,pattern,status) VALUES (?,?,?,?,'candidate')",
                          (crd, p["name"], addr, pat))
    if queue:
        c.execute("UPDATE contact_email SET status='queued'"
                  " WHERE crd=? AND status='candidate'", (crd,))
    c.commit()
    c.close()
    return RedirectResponse(f"/firm/{crd}", status_code=303)
