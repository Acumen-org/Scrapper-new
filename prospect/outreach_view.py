"""Outreach: the mass-email worklist and its Excel export.

One row per PERSON, not per firm: a decision maker with an email and a phone,
ready to drop into a mail-merge tool. It unions three sources in trust order:

  filed at firm    an email the firm printed in its brochure (real)
  their website    a person-to-email pair scraped from the firm's own site (real)
  inferred         a pattern guess built from how the firm addresses its other
                   people, checked against the mail domain (a guess, labelled)

Every row carries how the address was obtained and the domain check result, so a
guess is never mistaken for a confirmed address. Phone is the person's own where
the website gave one, else the firm's main office line.

The Excel button exports exactly the filtered set. Nothing is hidden from the
export that is not hidden from the screen.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, Response

from . import xlsx
from .webapp import PAGE_CSS, conn, esc, nav

router = APIRouter()

# The worklist query. Person-level contacts, best source first, with the firm's
# scored-list membership so the outreach can be aimed.
WORKLIST = """
WITH people AS (
    -- real person-to-email pairs scraped from the firm's own website: an actual
    -- named person with their actual address
    SELECT w.crd, w.person, w.title AS role, w.email,
           'their website' AS source, 'domain_accepts_mail' AS status, 3 AS trust
    FROM web_contact w WHERE w.person IS NOT NULL AND w.email IS NOT NULL
    UNION ALL
    -- inferred per-person guesses (pattern column records HOW; status the check)
    SELECT ce.crd, ce.name AS person, sa.title AS role, ce.email,
           'inferred: ' || ce.pattern AS source, ce.status, 1 AS trust
    FROM contact_email ce
    LEFT JOIN schedule_a sa ON sa.crd=ce.crd AND sa.is_individual=1
       AND UPPER(sa.name) LIKE '%' || UPPER(substr(ce.name, instr(ce.name,' ')+1)) || '%'
    UNION ALL
    -- firm-level inbox from a brochure. Real, but NOT a person's address, so it
    -- is labelled a firm inbox rather than pinned to a random officer's name.
    SELECT f.crd, NULL AS person, 'firm inbox' AS role, f.value AS email,
           'filed at firm' AS source, 'domain_accepts_mail' AS status, 2 AS trust
    FROM firm_contact_info f WHERE f.kind='email'
)
SELECT p.crd, fc.legal_name, fc.state, fc.phone AS firm_phone,
       p.person, p.role, p.email, p.source, p.status, MAX(p.trust) AS trust,
       (ta.crd IS NOT NULL) AS is_tier_a,
       (tc.crd IS NOT NULL) AS is_tier_c,
       (ov.crd IS NOT NULL) AS is_intersection,
       (SELECT wc.phone FROM web_contact wc
        WHERE wc.crd=p.crd AND wc.person=p.person AND wc.phone IS NOT NULL
        LIMIT 1) AS person_phone
FROM people p
JOIN firm_current fc ON fc.crd=p.crd
LEFT JOIN tier_a_rank ta ON ta.crd=p.crd AND ta.in_working_list=1
LEFT JOIN (SELECT crd FROM tier_c_score WHERE rank<=100) tc ON tc.crd=p.crd
LEFT JOIN firm_overlay ov ON ov.crd=p.crd AND ov.phh_13f=1
WHERE fc.is_era=0 AND fc.raum>=25e6 AND fc.raum<500e6 {extra}
GROUP BY p.crd, p.email, COALESCE(p.person,'')
ORDER BY MAX(p.trust) DESC, is_tier_a DESC, fc.legal_name
"""


def _filters(product: str, real_only: str, live_only: str):
    extra, args = "", []
    if product == "PHH":
        extra += (" AND (ta.crd IS NOT NULL OR ov.crd IS NOT NULL)")
    elif product == "ACUBOOTH":
        extra += " AND tc.crd IS NOT NULL"
    if real_only:
        extra += " AND p.trust=3"
    if live_only:
        extra += " AND p.status='domain_accepts_mail'"
    return extra, args


def _rows(c, product: str, real_only: str, live_only: str, limit: int | None):
    extra, args = _filters(product, real_only, live_only)
    sql = WORKLIST.format(extra=extra)
    if limit:
        sql += f" LIMIT {int(limit)}"
    return c.execute(sql, args).fetchall()


PRODUCT_LABEL = {"": "All firms", "PHH": "Prairie Hill lists only",
                 "ACUBOOTH": "AcuBooth list only"}
STATUS_LABEL = {"domain_accepts_mail": "domain ok", "no_mail_server": "dead domain",
                "bad_syntax": "malformed", "queued": "unchecked", "candidate": "guess"}


@router.get("/outreach", response_class=HTMLResponse)
def outreach(product: str = Query(""), real: str = Query(""),
             live: str = Query(""), page: int = Query(1, ge=1),
             per: int = Query(100, ge=25, le=500)):
    c = conn()
    rows = _rows(c, product, real, live, None)
    total = len(rows)
    reals = sum(1 for r in rows if r["trust"] == 3)
    live_n = sum(1 for r in rows if r["status"] == "domain_accepts_mail")
    window = rows[(page - 1) * per: page * per]

    def opt(v, cur, label):
        return f'<option value="{esc(v)}"{" selected" if v==cur else ""}>{esc(label)}</option>'

    body = []
    for r in window:
        aim = []
        if r["is_intersection"]:
            aim.append('<span class="chip lead">intersection</span>')
        elif r["is_tier_a"]:
            aim.append('<span class="chip">tier A</span>')
        if r["is_tier_c"]:
            aim.append('<span class="chip">tier C</span>')
        chip = "lead" if r["status"] == "domain_accepts_mail" else (
            "dis" if r["status"] in ("no_mail_server", "bad_syntax") else "partial")
        src_chip = "lead" if r["trust"] == 3 else "partial"
        phone = r["person_phone"] or r["firm_phone"] or "-"
        body.append(
            f'<tr><td><a href="/firm/{esc(r["crd"])}">{esc(r["legal_name"] or "")}</a>'
            f'<div class="meta">CRD {esc(r["crd"])} &middot; {esc(r["state"] or "-")} '
            f'{" ".join(aim)}</div></td>'
            f'<td><b>{esc(r["person"] or "")}</b>'
            f'<div class="meta">{esc(r["role"] or "")}</div></td>'
            f'<td><a href="mailto:{esc(r["email"])}">{esc(r["email"])}</a>'
            f'<div class="meta"><span class="chip {src_chip}">{esc(r["source"])}</span> '
            f'<span class="chip {chip}">{esc(STATUS_LABEL.get(r["status"], r["status"]))}</span>'
            f'</div></td>'
            f'<td>{esc(phone)}</td></tr>')

    pages = max(1, -(-total // per))
    qs = f"product={product}&real={real}&live={live}"
    prev = f'<a href="/outreach?{qs}&page={page-1}">Previous</a>' if page > 1 else ""
    nxt = f'<a href="/outreach?{qs}&page={page+1}">Next</a>' if page < pages else ""
    c.close()

    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>Outreach</title><style>{PAGE_CSS}</style>
{nav("outreach")}
<header><h1>Outreach list</h1>
<div class="sub">One row per decision maker, with the best email and phone we
have. Real filed and scraped addresses first, then pattern guesses checked
against the mail domain. Export the filtered set as Excel for a mail merge.</div>
</header>
<div class="wrap">
<div class="warn">This mixes confirmed addresses with educated guesses, both
labelled. A guess on a live domain is worth sending to; treat a bounce as the
real verification. Never send to a <b>dead domain</b> row.</div>
<form class="filters" method="get">
<label>Scope<select name="product">
{"".join(opt(k, product, v) for k, v in PRODUCT_LABEL.items())}</select></label>
<label>Source<select name="real">{opt("", real, "Real and inferred")}
{opt("1", real, "Only real (filed or scraped)")}</select></label>
<label>Domain<select name="live">{opt("", live, "Any")}
{opt("1", live, "Only live mail domains")}</select></label>
<button class="primary" type="submit">Apply</button>
<a href="/outreach.xlsx?{qs}" style="align-self:center;font-size:13px">Export Excel</a>
</form>
<p class="meta" style="margin:0 0 12px">{total:,} people &middot; {reals:,} real
addresses &middot; {live_n:,} on live mail domains</p>
<table><thead><tr><th style="width:280px">Firm</th><th style="width:220px">Person</th>
<th>Email</th><th style="width:150px">Phone</th></tr></thead>
<tbody>{"".join(body) or
'<tr><td colspan="4" style="padding:26px;color:var(--faint)">No contacts match. '
'Run the website enrichment and email inference jobs to fill this in.</td></tr>'}
</tbody></table>
<div class="pager">{prev} Page {page} of {pages} &middot; {total:,} people {nxt}</div>
</div>""")


@router.get("/outreach.xlsx")
def outreach_xlsx(product: str = Query(""), real: str = Query(""),
                  live: str = Query("")):
    c = conn()
    rows = _rows(c, product, real, live, limit=50000)
    c.close()
    headers = ["Firm", "CRD", "State", "Person", "Role/title", "Email",
               "Email source", "Domain check", "Phone", "Lists"]
    out = []
    for r in rows:
        lists = []
        if r["is_intersection"]:
            lists.append("intersection")
        if r["is_tier_a"]:
            lists.append("tier A")
        if r["is_tier_c"]:
            lists.append("tier C")
        out.append([
            r["legal_name"] or "", r["crd"], r["state"] or "",
            r["person"] or "", r["role"] or "", r["email"] or "",
            r["source"] or "", STATUS_LABEL.get(r["status"], r["status"]),
            r["person_phone"] or r["firm_phone"] or "",
            ", ".join(lists)])
    data = xlsx.write_sheet(headers, out, sheet_name="Outreach")
    return Response(data, media_type=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        headers={"Content-Disposition": 'attachment; filename="outreach.xlsx"'})
