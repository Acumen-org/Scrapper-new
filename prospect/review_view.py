"""Unified review queue: one queue, a type filter, separate counts per type.

Two job types share it, per the design decision:

  match_13f          Uncertain ADV-to-13F links. A wrong link puts a holding the
                     firm does not own into a call opener, so nothing below the
                     auto threshold merges without a human.

  brochure_negation  Sentences where a tag phrase shares a sentence with a
                     negation term. Until reviewed the tag stays PRESENT at
                     damped confidence, never absent: an unreviewed row must not
                     silently delete a signal.

Rows are ranked so the ones that change a decision surface first; the tail can
sit unreviewed indefinitely without blocking anything downstream.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from .webapp import PAGE_CSS, conn, current_owner, esc, nav

router = APIRouter()

REVIEW_CSS = PAGE_CSS + """
.q{font-size:13px;color:#5b6570}
.sent{background:var(--side);border:1px solid var(--rule);border-radius:8px;
padding:7px 10px;font-size:13px;margin-top:5px}
.conf{font-variant-numeric:tabular-nums}
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# A firm someone will actually call: on the tier A working list, in the
# intersection, or in the tier C top 100. Review effort spent anywhere else is
# effort the safe defaults already cover.
CALLED = """(SELECT crd FROM tier_a_rank WHERE in_working_list=1
             UNION SELECT o.crd FROM firm_overlay o
                   JOIN tier_a_rank t ON t.crd=o.crd WHERE o.phh_13f=1
             UNION SELECT crd FROM tier_c_score WHERE rank<=100)"""


@router.get("/review", response_class=HTMLResponse)
def review_queue(kind: str = Query(""), page: int = Query(1, ge=1),
                 per: int = Query(40, ge=10, le=200), scope: str = Query("called")):
    c = conn()
    if scope not in ("called", "all"):
        scope = "called"

    n_match = c.execute(
        "SELECT COUNT(*) n FROM adv_13f_match WHERE status='review'").fetchone()["n"]
    n_neg = c.execute(
        "SELECT COUNT(*) n FROM brochure_negation WHERE status='open'").fetchone()["n"]
    n_match_c = c.execute(
        f"SELECT COUNT(*) n FROM adv_13f_match WHERE status='review'"
        f" AND crd IN {CALLED}").fetchone()["n"]
    n_neg_c = c.execute(
        f"SELECT COUNT(*) n FROM brochure_negation WHERE status='open'"
        f" AND crd IN {CALLED}").fetchone()["n"]

    m_scope = f" AND m.crd IN {CALLED}" if scope == "called" else ""
    n_scope = f" AND n.crd IN {CALLED}" if scope == "called" else ""

    rows_html = []
    total = 0
    if kind in ("", "match_13f"):
        matches = c.execute(f"""
            SELECT m.*, f.legal_name, f.raum,
                   (SELECT COUNT(*) FROM holding_13f h WHERE h.cik=m.cik) AS held
            FROM adv_13f_match m LEFT JOIN firm_current f ON f.crd=m.crd
            WHERE m.status='review'{m_scope}
            ORDER BY (SELECT COUNT(*) FROM holding_13f h WHERE h.cik=m.cik) DESC,
                     m.confidence DESC
            LIMIT ? OFFSET ?""",
            (per, (page - 1) * per) if kind == "match_13f" else (per, 0)).fetchall()
        total += n_match
        for m in matches:
            impact = (f'{m["held"]} target holdings ride on this link'
                      if m["held"] else "no target holdings affected yet")
            rows_html.append(f"""<tr>
<td><span class="chip info">13F match</span></td>
<td><a href="/firm/{esc(m['crd'])}">{esc(m['name_adv'] or m['legal_name'] or '')}</a>
  <div class="meta">CRD {esc(m['crd'])} &middot; state {esc(m['state_adv'] or '-')}</div></td>
<td>{esc(m['name_edgar'] or '')}
  <div class="meta">CIK {esc(m['cik'])} &middot; state {esc(m['state_edgar'] or '-')}
  &middot; {esc(m['match_basis'])}</div></td>
<td class="conf">{m['confidence']:.2f}
  <div class="meta">{esc(impact)}</div></td>
<td><form method="post" action="/review/match" class="act">
  <input type="hidden" name="crd" value="{esc(m['crd'])}">
  <input type="hidden" name="cik" value="{esc(m['cik'])}">
  <button name="decision" value="confirmed">Same firm</button>
  <button name="decision" value="denied">Different firm</button></form></td></tr>""")

    if kind in ("", "brochure_negation"):
        negs = c.execute(f"""
            SELECT n.*, f.legal_name, t.confidence
            FROM brochure_negation n
            LEFT JOIN firm_current f ON f.crd=n.crd
            LEFT JOIN brochure_tag t ON t.crd=n.crd AND t.tag=n.tag
            WHERE n.status='open'{n_scope}
            ORDER BY COALESCE(t.confidence,0) DESC
            LIMIT ? OFFSET ?""",
            (per, (page - 1) * per) if kind == "brochure_negation" else (per, 0)).fetchall()
        total += n_neg
        for n in negs:
            rows_html.append(f"""<tr>
<td><span class="chip partial">Negation</span></td>
<td><a href="/firm/{esc(n['crd'])}">{esc(n['legal_name'] or '')}</a>
  <div class="meta">CRD {esc(n['crd'])}</div></td>
<td><b>{esc(n['tag'])}</b> via "{esc(n['phrase'])}"
  <div class="sent">{esc(n['sentence'] or '')}</div></td>
<td class="conf">{'' if n['confidence'] is None else f"{n['confidence']:.2f}"}
  <div class="meta">tag currently PRESENT at damped confidence</div></td>
<td><form method="post" action="/review/negation" class="act">
  <input type="hidden" name="nid" value="{n['id']}">
  <button name="decision" value="negation_confirmed"
    title="The sentence really does say they do not do this">Real negation</button>
  <button name="decision" value="tag_confirmed"
    title="The tag stands at full confidence">Tag stands</button></form></td></tr>""")

    if kind:
        prevl = (f'<a href="/review?kind={kind}&scope={scope}&page={page-1}">Previous</a>'
                 if page > 1 else "")
        nxtl = (f'<a href="/review?kind={kind}&scope={scope}&page={page+1}">Next</a>'
                if len(rows_html) >= per else "")
        pager_html = f'<div class="pager">{prevl} Page {page} {nxtl}</div>'
    else:
        pager_html = ""

    def tab(v, label, count):
        sel = ' style="font-weight:700;text-decoration:none"' if kind == v else ""
        return f'<a href="/review?kind={v}&scope={scope}"{sel}>{label} ({count})</a>'

    worth = n_match_c + n_neg_c
    if scope == "called":
        scope_line = (f'Showing the <b>{worth}</b> items that sit on a firm someone '
                      f'will actually call (tier A working list, the intersection, '
                      f'or the tier C top 100). The other '
                      f'{n_match + n_neg - worth:,} are held safely by the defaults '
                      f'and never need a human. '
                      f'<a href="/review?kind={esc(kind)}&scope=all">Show everything '
                      f'anyway</a>')
        counts = (n_match_c, n_neg_c)
    else:
        scope_line = (f'Showing all {n_match + n_neg:,} open items. '
                      f'<a href="/review?kind={esc(kind)}&scope=called">Back to the '
                      f'{worth} that matter</a>')
        counts = (n_match, n_neg)

    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>System</title><style>{REVIEW_CSS}
.systabs a{{font-size:14px;text-decoration:none;color:var(--soft);padding:6px 2px;margin-right:16px}}
.systabs a.on{{color:var(--red-hi);font-weight:700;border-bottom:2px solid var(--red)}}</style>
{nav("system")}
<header><h1>System</h1>
<div class="sub">You never have to clear the review queue. An unreviewed row
always defaults to the safe reading; reviewing just sharpens the firms you are
about to call.</div></header>
<div class="wrap">
<p class="systabs"><a href="/health">Pipeline health</a><a href="/review" class="on">Review queue</a></p>
<p style="font-size:13px">
{tab('', 'All', sum(counts))} &middot;
{tab('match_13f', '13F matches', counts[0])} &middot;
{tab('brochure_negation', 'Brochure negations', counts[1])}</p>
<div class="warn">{scope_line}</div>
<p class="meta" style="margin:0 0 12px">A confirmed 13F match becomes eligible for
holdings ingest on the next run; a wrong one would put someone else&rsquo;s
holdings in a call opener, which is why nothing merges without a human. An
unreviewed negation never removes a tag, it only holds its confidence lower.</p>
<table><thead><tr><th style="width:110px">Type</th><th style="width:250px">Adviser</th>
<th>Evidence</th><th style="width:150px">Confidence</th>
<th style="width:210px">Decision</th></tr></thead>
<tbody>{''.join(rows_html) or
'<tr><td colspan="5" style="padding:26px;color:var(--faint)">Queue is empty.</td></tr>'}</tbody>
</table>
{pager_html}
</div>""")


@router.post("/review/match")
def decide_match(crd: str = Form(...), cik: str = Form(...),
                 decision: str = Form(...)):
    if decision not in ("confirmed", "denied"):
        return RedirectResponse("/review", status_code=303)
    c = conn()
    c.execute("UPDATE adv_13f_match SET status=?, reviewed_by=?, reviewed_at=?"
              " WHERE crd=? AND cik=?", (decision, current_owner() or "bd", _now(), crd, cik))
    c.commit()
    return RedirectResponse("/review?kind=match_13f", status_code=303)


@router.post("/review/negation")
def decide_negation(nid: int = Form(...), decision: str = Form(...)):
    if decision not in ("negation_confirmed", "tag_confirmed"):
        return RedirectResponse("/review", status_code=303)
    c = conn()
    row = c.execute("SELECT crd, tag FROM brochure_negation WHERE id=?", (nid,)).fetchone()
    c.execute("UPDATE brochure_negation SET status=?, decided_by=?, decided_at=?"
              " WHERE id=?", (decision, current_owner() or "bd", _now(), nid))
    if row:
        open_left = c.execute(
            "SELECT COUNT(*) n FROM brochure_negation WHERE crd=? AND tag=?"
            " AND status='open'", (row["crd"], row["tag"])).fetchone()["n"]
        if decision == "tag_confirmed" and open_left == 0:
            # every ambiguous sentence for this tag resolved in the tag's favour:
            # lift the damp
            c.execute("UPDATE brochure_tag SET negation_damp=1.0,"
                      " confidence=MIN(1.0, confidence/0.6)"
                      " WHERE crd=? AND tag=? AND negation_damp<1.0",
                      (row["crd"], row["tag"]))
        elif decision == "negation_confirmed":
            # a human read the sentence as a real negation: mark the tag as an
            # explicit negative signal (present=0 means "states they do NOT")
            c.execute("UPDATE brochure_tag SET present=0, confidence=0.9"
                      " WHERE crd=? AND tag=?", (row["crd"], row["tag"]))
    c.commit()
    return RedirectResponse("/review?kind=brochure_negation", status_code=303)
