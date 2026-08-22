"""Working lists: the ranked outputs the team actually dials from.

Four tabs, all reading the same scored tables the pipeline writes:

  working       tier A top 100 (PHH), own-vehicle and RE-adjacent flags visible
  intersection  tier A firms holding net lease names, sorted by position value,
                with the legacy/deliberate conviction call on every row
  tierc         tier C top 100 (AcuBooth pre-positioning; see the caveat banner)
  sponsors      competitor sponsors from the segmentation, intel not pipeline
"""

from __future__ import annotations

import yaml
from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from . import config
from .webapp import PAGE_CSS, caveat, conn, esc, money, nav

router = APIRouter()

_pc = yaml.safe_load((config.CONFIG_DIR / "scoring.yml").read_text(encoding="utf-8"))
PC = _pc.get("position_conviction", {"deliberate_min_names": 3,
                                     "deliberate_min_value": 500000,
                                     "legacy_max_value": 100000})


def conviction(names: int, value: int) -> str:
    if names >= PC["deliberate_min_names"] or value >= PC["deliberate_min_value"]:
        return "deliberate"
    if names == 1 and value < PC["legacy_max_value"]:
        return "possible legacy"
    return "moderate"


@router.get("/lists", response_class=HTMLResponse)
def lists(tab: str = Query("working"), st: str = Query(""),
          city: str = Query("")):
    c = conn()
    body = ""
    banner = ""

    if tab == "working":
        rows = c.execute("""
            SELECT t.*, f.legal_name, f.state, s.status, s.owner
            FROM tier_a_rank t JOIN firm_current f ON f.crd=t.crd
            LEFT JOIN firm_status s ON s.crd=t.crd
            WHERE t.in_working_list=1 ORDER BY t.rank""").fetchall()
        trs = []
        for r in rows:
            # Cautions, not decoration: each one names a reason this firm may
            # resist the pitch, and hovering it says so in a sentence.
            flags = []
            if r["fund_source"] == "own_vehicles":
                flags.append('<span class="chip dis" title="Every private fund '
                             'this firm advises is its own product. Firms that '
                             'manufacture funds rarely buy someone else&#39;s; '
                             'ranked down accordingly.">own vehicles</span>')
            elif r["fund_source"] == "mixed":
                flags.append('<span class="chip partial" title="Some of its '
                             'funds are its own, some are third party. Third '
                             'party appetite is proven but partial.">mixed</span>')
            if r["own_vehicle_re_adjacent"]:
                flags.append('<span class="chip dis" title="One of its own '
                             'vehicles is real estate or adjacent to it, so PHH '
                             'competes with the firm&#39;s own product.">'
                             'RE adjacent</span>')
            if not flags:
                flags.append('<span class="meta" title="No caution applies: its '
                             'private fund experience is with other people&#39;s '
                             'funds and none of its own products compete with '
                             'PHH.">clear</span>')
            mi = f"${r['max_min_investment']/1e3:,.0f}k" if r["max_min_investment"] else "-"
            trs.append(f"""<tr>
<td class="pri">{r['rank']}</td>
<td><a href="/firm/{esc(r['crd'])}">{esc(r['legal_name'] or '')}</a>
 <div class="meta">CRD {esc(r['crd'])} &middot; {esc(r['state'] or '-')}</div></td>
<td class="pri">{r['total_score']:.1f}
 <div class="meta">aum {r['hnw_aum_score']:.0f} / cnt {r['fund_count_score']:.0f} /
 min {r['min_investment_score']:.0f} / typ {r['fund_type_score']:.0f}</div></td>
<td class="pri">{money(r['hnw_aum'])}</td>
<td class="pri">{r['fund_count']}</td><td class="pri">{mi}</td>
<td>{esc(r['best_fund_type'] or '')}</td>
<td>{' '.join(flags)}</td>
<td>{esc(r['status'] or '')}{(' &middot; ' + esc(r['owner'])) if r['owner'] else ''}</td>
</tr>""")
        body = ("<table><thead><tr><th>#</th><th>Firm</th><th>Score</th><th>HNW</th>"
                "<th>Funds</th><th>Min</th><th>Best type</th><th>Cautions</th>"
                "<th>Status</th></tr></thead><tbody>" + "".join(trs) + "</tbody></table>")
        banner = (f"{len(rows)} firms. PHH working list: proven private fund appetite, "
                  "real HNW book, no competing real estate product. Component scores "
                  "shown so a rank can be argued with. Cautions mark firms likely to "
                  "resist the pitch: <b>own vehicles</b> means every fund they advise "
                  "is their own product, <b>mixed</b> means only some are, "
                  "<b>RE adjacent</b> means one of their own products competes with "
                  "PHH, and <b>clear</b> means none of that applies. Hover any "
                  "caution for the full sentence.")

    elif tab == "intersection":
        rows = c.execute("""
            SELECT o.*, t.rank, t.fund_source, t.max_min_investment,
                   f.legal_name, f.state, f.raum, f.hnw_aum
            FROM firm_overlay o JOIN tier_a_rank t ON t.crd=o.crd
            JOIN firm_current f ON f.crd=o.crd
            WHERE o.phh_13f=1
            ORDER BY COALESCE(o.phh_value,0) DESC""").fetchall()
        trs = []
        for r in rows:
            names = (r["phh_tickers"] or "").split(",") if r["phh_tickers"] else []
            conv = conviction(len(names), r["phh_value"] or 0)
            cls = {"deliberate": "lead", "possible legacy": "dis"}.get(conv, "")
            trs.append(f"""<tr>
<td><a href="/firm/{esc(r['crd'])}">{esc(r['legal_name'] or '')}</a>
 <div class="meta">CRD {esc(r['crd'])} &middot; {esc(r['state'] or '-')} &middot;
 tier A #{r['rank']} &middot; {esc(r['fund_source'] or '')}</div></td>
<td class="pri">{money(r['raum'])}</td><td class="pri">{money(r['hnw_aum'])}</td>
<td>{esc(r['phh_tickers'] or '')}</td>
<td class="pri">{money(r['phh_value'])}</td>
<td><span class="chip {cls}">{esc(conv)}</span></td></tr>""")
        body = ("<table><thead><tr><th>Firm</th><th>RAUM</th><th>HNW</th>"
                "<th>Holds</th><th>Position value</th><th>Conviction</th></tr></thead>"
                "<tbody>" + "".join(trs) + "</tbody></table>")
        banner = (f"{len(rows)} firms clear both gates: illiquidity tolerance proven by "
                  "private fund history AND real estate appetite shown in 13F holdings. "
                  "Sorted by position value because position size is the appetite "
                  "measure. Alisa works this list personally.")

    elif tab == "tierc":
        rows = c.execute("""
            SELECT t.*, f.legal_name, f.state, s.status, s.owner
            FROM tier_c_score t JOIN firm_current f ON f.crd=t.crd
            LEFT JOIN firm_status s ON s.crd=t.crd
            ORDER BY t.rank LIMIT 100""").fetchall()
        trs = []
        for r in rows:
            sw = (caveat("schwab_share_reported", f"{r['schwab_share']*100:.0f}%")
                  if r["schwab_share"] is not None else "-")
            trs.append(f"""<tr>
<td class="pri">{r['rank']}</td>
<td><a href="/firm/{esc(r['crd'])}">{esc(r['legal_name'] or '')}</a>
 <div class="meta">CRD {esc(r['crd'])} &middot; {esc(r['state'] or '-')}</div></td>
<td class="pri">{r['total_score']:.1f}</td>
<td class="pri">{money(r['hnw_aum'])}</td>
<td class="pri">{r['hnw_clients'] or 0}</td>
<td class="pri">{(r['clients_per_rep'] or 0):.0f}</td>
<td class="pri">{sw}</td>
<td>{esc(r['status'] or '')}{(' &middot; ' + esc(r['owner'])) if r['owner'] else ''}</td></tr>""")
        body = ("<table><thead><tr><th>#</th><th>Firm</th><th>Score</th><th>HNW</th>"
                "<th>HNW clients</th><th>Clients/rep</th><th>Schwab</th>"
                "<th>Status</th></tr></thead><tbody>" + "".join(trs) + "</tbody></table>")
        banner = ("AcuBooth pre-positioning, top 100 of 5,399. This ranks approach-worthiness "
                  "for the free holdings analysis offer, never sellable accounts: the pool is "
                  "held-away and self-directed accounts, which appear in no filing.")

    elif tab == "geo":
        # The dinner planner: 10 to 12 qualified firms within reach of one room.
        # Qualified = tier A, the intersection, or tier C scoring 70+.
        if not st:
            rows = c.execute("""
                SELECT f.state,
                       SUM(CASE WHEN ta.crd IS NOT NULL THEN 1 ELSE 0 END) tier_a,
                       SUM(CASE WHEN o.crd IS NOT NULL AND taa.crd IS NOT NULL
                           THEN 1 ELSE 0 END) inter,
                       SUM(CASE WHEN tc.total_score>=70 THEN 1 ELSE 0 END) tier_c
                FROM firm_current f
                LEFT JOIN tier_a_rank ta ON ta.crd=f.crd AND ta.in_working_list=1
                LEFT JOIN tier_a_rank taa ON taa.crd=f.crd
                LEFT JOIN firm_overlay o ON o.crd=f.crd AND o.phh_13f=1
                LEFT JOIN tier_c_score tc ON tc.crd=f.crd
                WHERE f.is_era=0 AND f.raum>=25e6 AND f.raum<500e6
                  AND f.state IS NOT NULL
                GROUP BY f.state
                HAVING tier_a+inter+tier_c > 0
                ORDER BY tier_a+inter DESC, tier_c DESC""").fetchall()
            trs = "".join(
                f'<tr><td><a href="/lists?tab=geo&st={esc(r["state"])}">'
                f'{esc(r["state"])}</a></td>'
                f'<td class="num">{r["tier_a"]}</td><td class="num">{r["inter"]}</td>'
                f'<td class="num">{r["tier_c"]}</td></tr>' for r in rows)
            body = ("<table><thead><tr><th>State</th><th class='num'>Tier A top 100</th>"
                    "<th class='num'>Intersection</th><th class='num'>Tier C 70+</th>"
                    "</tr></thead><tbody>" + trs + "</tbody></table>")
            banner = ("Pick a state, then a city. Qualified means tier A working list, "
                      "the intersection, or tier C scoring 70+. A good dinner needs 10 "
                      "to 12 qualified firms near one room.")
        elif not city:
            rows = c.execute("""
                SELECT UPPER(f.city) city, substr(f.postal_code,1,3) zip3,
                       SUM(CASE WHEN ta.crd IS NOT NULL THEN 1 ELSE 0 END) tier_a,
                       SUM(CASE WHEN o.crd IS NOT NULL AND taa.crd IS NOT NULL
                           THEN 1 ELSE 0 END) inter,
                       SUM(CASE WHEN tc.total_score>=70 THEN 1 ELSE 0 END) tier_c
                FROM firm_current f
                LEFT JOIN tier_a_rank ta ON ta.crd=f.crd AND ta.in_working_list=1
                LEFT JOIN tier_a_rank taa ON taa.crd=f.crd
                LEFT JOIN firm_overlay o ON o.crd=f.crd AND o.phh_13f=1
                LEFT JOIN tier_c_score tc ON tc.crd=f.crd
                WHERE f.is_era=0 AND f.raum>=25e6 AND f.raum<500e6
                  AND f.state=? AND f.city IS NOT NULL
                GROUP BY UPPER(f.city)
                HAVING tier_a+inter+tier_c > 0
                ORDER BY tier_a+inter DESC, tier_c DESC LIMIT 60""", (st,)).fetchall()
            trs = "".join(
                f'<tr><td><a href="/lists?tab=geo&st={esc(st)}&city='
                f'{esc(r["city"])}">{esc((r["city"] or "").title())}</a>'
                f'<div class="meta">zip {esc(r["zip3"] or "?")}xx</div></td>'
                f'<td class="num">{r["tier_a"]}</td><td class="num">{r["inter"]}</td>'
                f'<td class="num">{r["tier_c"]}</td>'
                f'<td class="num"><b>{r["tier_a"]+r["inter"]+r["tier_c"]}</b></td></tr>'
                for r in rows)
            body = (f'<p style="font-size:13px;margin:0 0 10px">'
                    f'<a href="/lists?tab=geo">&larr; all states</a></p>'
                    "<table><thead><tr><th>City</th><th class='num'>Tier A</th>"
                    "<th class='num'>Intersection</th><th class='num'>Tier C 70+</th>"
                    "<th class='num'>Qualified</th></tr></thead><tbody>" + trs +
                    "</tbody></table>")
            banner = f"Cities in {esc(st)} with at least one qualified firm."
        else:
            rows = c.execute("""
                SELECT f.crd, f.legal_name, f.city, f.postal_code, f.raum, f.hnw_aum,
                       ta.rank AS arank, o.phh_13f, o.phh_tickers,
                       tc.rank AS crank, tc.total_score AS cscore,
                       (SELECT 1 FROM tier_a_rank x WHERE x.crd=f.crd) AS in_tier_a,
                       fs.status, fs.owner
                FROM firm_current f
                LEFT JOIN tier_a_rank ta ON ta.crd=f.crd AND ta.in_working_list=1
                LEFT JOIN firm_overlay o ON o.crd=f.crd
                LEFT JOIN tier_c_score tc ON tc.crd=f.crd AND tc.total_score>=70
                LEFT JOIN firm_status fs ON fs.crd=f.crd
                WHERE f.is_era=0 AND f.raum>=25e6 AND f.raum<500e6
                  AND f.state=? AND UPPER(f.city)=UPPER(?)
                  AND (ta.crd IS NOT NULL OR o.phh_13f=1 OR tc.crd IS NOT NULL)
                ORDER BY (o.phh_13f IS NOT NULL AND o.phh_13f=1) DESC,
                         ta.rank IS NULL, ta.rank, tc.rank""", (st, city)).fetchall()
            trs = []
            for r in rows:
                why = []
                if r["phh_13f"] and (r["arank"] or r["in_tier_a"]):
                    why.append(f'<span class="chip lead">intersection '
                               f'{esc(r["phh_tickers"] or "")}</span>')
                elif r["phh_13f"]:
                    why.append(f'<span class="chip">holds '
                               f'{esc(r["phh_tickers"] or "")}</span>')
                if r["arank"]:
                    why.append(f'<span class="chip">tier A #{r["arank"]}</span>')
                if r["crank"]:
                    why.append(f'<span class="chip">tier C {r["cscore"]:.0f}</span>')
                trs.append(
                    f'<tr><td><a href="/firm/{esc(r["crd"])}">'
                    f'{esc(r["legal_name"] or "")}</a>'
                    f'<div class="meta">CRD {esc(r["crd"])} &middot; '
                    f'{esc((r["city"] or "").title())} {esc(r["postal_code"] or "")}'
                    f'</div></td>'
                    f'<td class="num">{money(r["raum"])}</td>'
                    f'<td class="num">{money(r["hnw_aum"])}</td>'
                    f'<td>{" ".join(why)}</td>'
                    f'<td>{esc(r["status"] or "")}'
                    f'{(" &middot; " + esc(r["owner"])) if r["owner"] else ""}</td></tr>')
            body = (f'<p style="font-size:13px;margin:0 0 10px">'
                    f'<a href="/lists?tab=geo&st={esc(st)}">&larr; {esc(st)} cities</a>'
                    f' &middot; <a href="/firms.csv?st={esc(st)}&city={esc(city)}">'
                    f'Export this dinner list as CSV</a></p>'
                    "<table><thead><tr><th>Firm</th><th class='num'>RAUM</th>"
                    "<th class='num'>HNW</th><th>Why qualified</th><th>Status</th>"
                    "</tr></thead><tbody>" + "".join(trs) + "</tbody></table>")
            banner = (f"{len(rows)} qualified firms in {esc(city.title())}, {esc(st)}. "
                      "Aim for 10 to 12 confirmed attendees; intersection firms first.")

    else:  # sponsors
        rows = c.execute("""
            SELECT r.*, f.legal_name, f.state,
                   d.investors fd_inv, d.total_sold, d.date_filed fd_date
            FROM re_segment r JOIN firm_current f ON f.crd=r.crd
            LEFT JOIN form_d d ON d.accession =
              (SELECT accession FROM form_d WHERE crd=r.crd
               ORDER BY date_filed DESC LIMIT 1)
            WHERE r.segment IN ('sponsor','competitor')
            ORDER BY r.total_gav DESC""").fetchall()
        trs = []
        for r in rows:
            fd = ""
            if r["fd_inv"] is not None:
                fd = (f"{r['fd_inv']} investors, {money(r['total_sold'])} raised "
                      f"(Form D {esc(r['fd_date'])})")
            trs.append(f"""<tr>
<td><a href="/firm/{esc(r['crd'])}">{esc(r['legal_name'] or '')}</a>
 <div class="meta">CRD {esc(r['crd'])} &middot; {esc(r['state'] or '-')}</div></td>
<td><span class="chip dis">{esc(r['segment'])}</span></td>
<td class="pri">{money(r['total_gav'])}</td>
<td class="pri">{r['total_owners'] or 0}</td>
<td class="pri">{money(r['min_investment'])}</td>
<td class="meta">{fd}</td></tr>""")
        body = ("<table><thead><tr><th>Firm</th><th>Segment</th><th>Fund GAV</th>"
                "<th>Investors</th><th>Min</th><th>Form D raise progress</th></tr></thead>"
                "<tbody>" + "".join(trs) + "</tbody></table>")
        banner = ("Competitive intelligence, not pipeline. Sponsors run their own funds; "
                  "Form D amendments show whose distribution is working. "
                  "All figures carry the archive as-of caveat.")

    def tabl(v, label):
        on = ' class="on"' if tab == v else ""
        return f'<a href="/lists?tab={v}"{on} style="margin-right:14px">{label}</a>'

    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>Working lists</title><style>{PAGE_CSS}
.tabs a{{font-size:14px;text-decoration:none;color:var(--soft);padding:6px 2px}}
.tabs a.on{{color:var(--red-hi);font-weight:700;border-bottom:2px solid var(--red)}}
</style>
{nav("lists")}
<header><h1>Working lists</h1>
<div class="sub">The ranked outputs, straight from the scoring tables</div></header>
<div class="wrap">
<p class="tabs">{tabl('working', 'Tier A working list')}{tabl('intersection', 'The intersection')}
{tabl('tierc', 'Tier C (AcuBooth)')}{tabl('geo', 'Geography')}{tabl('sponsors', 'Competitor sponsors')}</p>
<div class="warn">{banner}</div>
{body}
</div>""")
