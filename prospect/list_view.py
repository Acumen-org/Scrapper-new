"""Firm list and pipeline health.

The firm list is a second route to the same detail page the inbox reaches, with
server-side filtering and pagination because 17,105 registered firms will not
render client side.

Pipeline health exists to make a silent failure loud. A run that did nothing and
reported success is the worst outcome this system can produce, so skipped runs,
flagged row-count movements and stale snapshots are all shown as distinct states
rather than folded into a green tick.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from .webapp import PAGE_CSS, caveat, conn, esc, money, nav

# the state dropdown scans 23k rows and its answer changes weekly; cache it
_STATES: dict = {"t": 0.0, "v": []}

router = APIRouter()

LIST_CSS = PAGE_CSS  # tokens and semantic classes live in the shared sheet

BANDS = {
    "": ("All AUM", None, None),
    "25-100": ("25 to 100M", 25e6, 100e6),
    "100-250": ("100 to 250M", 100e6, 250e6),
    "250-500": ("250 to 500M", 250e6, 500e6),
}


@router.get("/firms", response_class=HTMLResponse)
def firm_list(
    q: str = Query(""),
    st: str = Query(""),
    band: str = Query(""),
    seg: str = Query(""),
    trig: str = Query(""),
    stat: str = Query(""),
    owner: str = Query(""),
    sort: str = Query("raum"),
    page: int = Query(1, ge=1),
    per: int = Query(50, ge=10, le=200),
):
    c = conn()
    where = ["f.is_era = 0", "f.raum >= 25e6", "f.raum < 500e6"]
    args: list = []
    if q:
        where.append("(f.legal_name LIKE ? OR f.business_name LIKE ? OR f.crd = ?)")
        args += [f"%{q}%", f"%{q}%", q]
    if st:
        where.append("f.state = ?"); args.append(st)
    if band and band in BANDS and BANDS[band][1] is not None:
        where.append("f.raum >= ? AND f.raum < ?")
        args += [BANDS[band][1], BANDS[band][2]]
    if seg:
        where.append("r.segment = ?"); args.append(seg)
    if stat:
        where.append("fs.status = ?"); args.append(stat)
    if owner:
        where.append("fs.owner LIKE ?"); args.append(f"%{owner}%")
    if trig == "open":
        where.append("EXISTS (SELECT 1 FROM trigger_event t LEFT JOIN trigger_action a"
                     " ON a.trigger_id = t.id WHERE t.crd = f.crd AND t.suppressed = 0"
                     " AND a.state IS NULL)")

    order = {"raum": "f.raum DESC", "hnw": "hnwshare DESC",
             "name": "f.legal_name", "triggers": "trigs DESC"}.get(sort, "f.raum DESC")

    base = f"""FROM firm_current f
        LEFT JOIN re_segment r ON r.crd = f.crd
        LEFT JOIN firm_custodian_profile p ON p.crd = f.crd
        LEFT JOIN firm_status fs ON fs.crd = f.crd
        WHERE {' AND '.join(where)}"""
    total = c.execute(f"SELECT COUNT(*) n {base}", args).fetchone()["n"]
    rows = c.execute(f"""
        SELECT f.crd, f.legal_name, f.state, f.raum, f.hnw_clients, f.hnw_aum,
               f.iar_count, f.clients_total,
               CASE WHEN f.raum > 0 THEN 1.0 * COALESCE(f.hnw_aum,0) / f.raum END AS hnwshare,
               r.segment, fs.status AS wstatus, fs.owner AS wowner,
               p.primary_canonical AS cust, p.schwab_share_reported AS sshare,
               p.as_of_filing_date AS sasof,
               (SELECT COUNT(*) FROM trigger_event t LEFT JOIN trigger_action a
                 ON a.trigger_id = t.id
                WHERE t.crd = f.crd AND t.suppressed = 0 AND a.state IS NULL) AS trigs
        {base} ORDER BY {order} LIMIT ? OFFSET ?""",
        args + [per, (page - 1) * per]).fetchall()

    import time as _time
    if _time.monotonic() - _STATES["t"] > 60:
        _STATES["v"] = [r["state"] for r in c.execute(
            "SELECT DISTINCT state FROM firm_current WHERE state IS NOT NULL"
            " AND is_era=0 ORDER BY state") if r["state"]]
        _STATES["t"] = _time.monotonic()
    states = _STATES["v"]

    def opt(v, cur, label):
        return f'<option value="{esc(v)}"{" selected" if v == cur else ""}>{esc(label)}</option>'

    body = []
    for r in rows:
        segch = (f'<span class="seg {r["segment"]}">{esc(r["segment"])}</span>'
                 if r["segment"] else "")
        sch = "-"
        if r["sshare"] is not None:
            sch = caveat("schwab_share_reported", f'{r["sshare"] * 100:.0f}%')
        body.append(
            f'<tr><td><a href="/firm/{esc(r["crd"])}">{esc(r["legal_name"] or "(unnamed)")}</a>'
            f'<div class="meta">CRD {esc(r["crd"])}</div></td>'
            f'<td>{esc(r["state"] or "-")}</td>'
            f'<td class="num">{money(r["raum"])}</td>'
            f'<td class="num">{(r["hnwshare"] or 0) * 100:.0f}%</td>'
            f'<td class="num">{r["hnw_clients"] or 0}</td>'
            f'<td class="num">{r["iar_count"] or 0}</td>'
            f'<td>{esc(r["cust"] or "-")}</td><td class="num">{sch}</td>'
            f'<td>{segch}</td>'
            f'<td>{esc(r["wstatus"] or "")}'
            f'{("<div class=meta>" + esc(r["wowner"]) + "</div>") if r["wowner"] else ""}</td>'
            f'<td class="num">{r["trigs"] or ""}</td></tr>')

    pages = max(1, -(-total // per))
    qs = (f"q={q}&st={st}&band={band}&seg={seg}&trig={trig}&stat={stat}"
          f"&owner={owner}&sort={sort}&per={per}")
    prev = f'<a href="/firms?{qs}&page={page-1}">Previous</a>' if page > 1 else ""
    nxt = f'<a href="/firms?{qs}&page={page+1}">Next</a>' if page < pages else ""

    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>Firm list</title><style>{LIST_CSS}</style>
{nav("firms")}
<header><h1>Firm list</h1><div class="sub">{total:,} in-band registered advisers
&middot; exempt reporting advisers excluded &middot; filtered and paged server side</div></header>
<div class="wrap">
<p class="back" style="font-size:13px"><a href="/">&larr; Trigger inbox</a> &middot;
<a href="/health">Pipeline health</a></p>
<form class="filters" method="get">
<label>Search<input type="text" name="q" value="{esc(q)}" placeholder="name or CRD"></label>
<label>State<select name="st">{opt("", st, "All states")}
{"".join(opt(s, st, s) for s in states)}</select></label>
<label>AUM band<select name="band">
{"".join(opt(k, band, v[0]) for k, v in BANDS.items())}</select></label>
<label>Real estate<select name="seg">{opt("", seg, "Any")}
{"".join(opt(s, seg, s) for s in ("prospect", "competitor", "sponsor", "ambiguous", "unraised"))}
</select></label>
<label>Triggers<select name="trig">{opt("", trig, "Any")}{opt("open", trig, "Has open trigger")}
</select></label>
<label>Status<select name="stat">{opt("", stat, "Any")}
{"".join(opt(x, stat, x) for x in ("new", "working", "meeting set", "qualified",
 "disqualified", "customer"))}</select></label>
<label>Owner<input type="text" name="owner" value="{esc(owner)}" placeholder="anyone"></label>
<label>Sort<select name="sort">
{"".join(opt(k, sort, v) for k, v in [("raum", "RAUM"), ("hnw", "HNW share"),
 ("triggers", "Open triggers"), ("name", "Name")])}</select></label>
<button class="primary" type="submit">Apply</button>
<a href="/firms" style="align-self:center;font-size:13px">Reset</a>
<a href="/firms.csv?{qs}" style="align-self:center;font-size:13px">Export CSV</a>
</form>
<table><thead><tr><th>Firm</th><th style="width:50px">St</th>
<th class="num" style="width:90px">RAUM</th><th class="num" style="width:70px">HNW %</th>
<th class="num" style="width:70px">HNW cl</th><th class="num" style="width:60px">IARs</th>
<th style="width:140px">Custodian</th><th class="num" style="width:80px">Schwab</th>
<th style="width:110px">Real estate</th><th style="width:110px">Status</th>
<th class="num" style="width:70px">Triggers</th>
</tr></thead><tbody>{"".join(body) or
'<tr><td colspan="11" style="padding:26px;color:var(--faint)">Nothing matches these filters.</td></tr>'}
</tbody></table>
<div class="pager">{prev} Page {page} of {pages} &middot; {total:,} firms {nxt}</div>
</div>""")


@router.get("/firms.csv")
def firms_csv(q: str = Query(""), st: str = Query(""), band: str = Query(""),
              city: str = Query(""),
              seg: str = Query(""), trig: str = Query(""), sort: str = Query("raum"),
              per: int = Query(5000)):
    """Export the current filtered view. Shaped for manual import into Twenty."""
    import csv
    import io
    c = conn()
    where = ["f.is_era = 0", "f.raum >= 25e6", "f.raum < 500e6"]
    args: list = []
    if q:
        where.append("(f.legal_name LIKE ? OR f.crd = ?)"); args += [f"%{q}%", q]
    if st:
        where.append("f.state = ?"); args.append(st)
    if city:
        where.append("UPPER(f.city) = UPPER(?)"); args.append(city)
    if band in BANDS and BANDS[band][1] is not None:
        where.append("f.raum >= ? AND f.raum < ?"); args += [BANDS[band][1], BANDS[band][2]]
    if seg:
        where.append("r.segment = ?"); args.append(seg)
    rows = c.execute(f"""
        SELECT f.crd, f.legal_name, f.business_name, f.website, f.phone,
               ce.email AS filed_email, f.city, f.state,
               f.raum, f.hnw_clients, f.hnw_aum, f.iar_count, f.clients_total,
               r.segment AS re_segment, r.as_of_filing_date AS re_as_of,
               p.primary_canonical AS custodian, p.schwab_share_reported,
               p.as_of_filing_date AS custodian_as_of,
               fs.status AS work_status, fs.owner AS work_owner
        FROM firm_current f LEFT JOIN re_segment r ON r.crd = f.crd
        LEFT JOIN firm_custodian_profile p ON p.crd = f.crd
        LEFT JOIN firm_status fs ON fs.crd = f.crd
        LEFT JOIN (SELECT crd, MIN(value) AS email FROM firm_contact_info
                   WHERE kind='email' GROUP BY crd) ce ON ce.crd = f.crd
        WHERE {' AND '.join(where)} LIMIT ?""", args + [per]).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    cols = list(rows[0].keys()) if rows else ["crd"]
    w.writerow(cols)
    for r in rows:
        w.writerow([r[k] for k in cols])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="firms.csv"'})


@router.get("/health", response_class=HTMLResponse)
def health_view(msg: str = Query("")):
    c = conn()
    snaps = c.execute("SELECT * FROM snapshot ORDER BY id").fetchall()
    runs = c.execute("SELECT * FROM run_log ORDER BY id DESC LIMIT 25").fetchall()
    today = date.today()

    srow = []
    for s in snaps:
        pub = (s["published_at"] or "").replace("/", "-")
        srow.append(
            f'<tr><td>{esc(s["source_key"])}</td><td>{esc(s["published_at"])}</td>'
            f'<td class="num">{s["bytes"]:,}</td>'
            f'<td style="font-family:Consolas,monospace;font-size:12px;color:var(--faint)">{esc(s["sha256"][:16])}</td>'
            f'<td>{esc(s["captured_at"][:10])}</td></tr>')

    rrow = []
    for r in runs:
        cls = {"ok": "ok", "failed": "bad", "skipped": "warnc",
               "running": "warnc"}.get(r["status"], "")
        flag = ' <b class="warnc">FLAGGED</b>' if r["flagged"] else ""
        delta = ""
        if r["pct_change"] is not None:
            d = r["pct_change"]
            delta = f'<span class="{"bad" if abs(d) > 5 else ""}">{d:+.1f}%</span>'
        rrow.append(
            f'<tr class="hrow"><td>{esc(r["source_key"])}</td><td>{esc(r["stage"])}</td>'
            f'<td class="{cls}"><b>{esc(r["status"])}</b>{flag}</td>'
            f'<td class="num">{r["rows_out"] if r["rows_out"] is not None else "-"}</td>'
            f'<td class="num">{delta}</td>'
            f'<td>{esc((r["finished_at"] or "")[:16])}</td>'
            f'<td style="font-size:12px;color:var(--soft)">{esc((r["message"] or "")[:110])}</td></tr>')

    def safe(sql):
        try:
            return c.execute(sql).fetchone()["n"]
        except Exception:
            return None

    counts = {t: safe(f"SELECT COUNT(*) n FROM {t}") for t in
              ("firm", "trigger_event", "sched_d_5k3", "sched_d_7b1", "filing_crd",
               "re_segment", "firm_custodian_profile", "tier_a_rank", "tier_c_score",
               "edgar_13f_filer", "holding_13f", "form_d", "brochure", "brochure_tag",
               "source_schema")}
    crow = "".join(f'<tr><td>{k}</td><td class="num">{v:,}</td></tr>'
                   for k, v in counts.items() if v is not None)

    # coverage and failure rates the spec calls out by name
    extra = []
    f_ok = safe("SELECT COUNT(*) n FROM filing_13f WHERE status='ok'")
    f_bad = safe("SELECT COUNT(*) n FROM filing_13f WHERE status='parse_failed'")
    if f_ok is not None:
        rate = (f_bad or 0) / max((f_ok or 0) + (f_bad or 0), 1) * 100
        cls = "bad" if rate > 5 else "ok"
        extra.append(f'<tr><td>13F parse failure rate</td>'
                     f'<td class="num {cls}">{rate:.1f}%</td></tr>')
    b_ok = safe("SELECT COUNT(*) n FROM brochure WHERE status='ok'")
    band = safe("SELECT COUNT(*) n FROM firm_current WHERE is_era=0 AND raum>=25e6 AND raum<500e6")
    if b_ok is not None and band:
        extra.append(f'<tr><td>Brochure coverage of band</td>'
                     f'<td class="num">{b_ok:,} / {band:,} '
                     f'({b_ok/band*100:.1f}%)</td></tr>')
    rq_m = safe("SELECT COUNT(*) n FROM adv_13f_match WHERE status='review'")
    rq_n = safe("SELECT COUNT(*) n FROM brochure_negation WHERE status='open'")
    if rq_m is not None or rq_n is not None:
        extra.append(f'<tr><td><a href="/review">Review queue open</a></td>'
                     f'<td class="num">{(rq_m or 0) + (rq_n or 0):,} '
                     f'({rq_m or 0} matches, {rq_n or 0} negations)</td></tr>')
    cusip = c.execute("SELECT MAX(finished_at) t FROM run_log WHERE source_key='cusip_map'"
                      " AND status='ok'").fetchone()["t"]
    if cusip:
        age = (today - date.fromisoformat(cusip[:10])).days
        cls = "bad" if age > 92 else "ok"
        extra.append(f'<tr><td>CUSIP map last verified</td>'
                     f'<td class="num {cls}">{age} days ago'
                     f'{" (quarterly re-verify DUE)" if age > 92 else ""}</td></tr>')
    crow += "".join(extra)

    feed = c.execute("SELECT published_at FROM snapshot WHERE source_key='adv_feed'"
                     " ORDER BY id DESC LIMIT 1").fetchone()
    stale = ""
    if feed:
        try:
            m, d, y = feed["published_at"].split("/")
            age = (today - date(int(y), int(m), int(d))).days
            cls = "bad" if age > 10 else "ok"
            stale = (f'<p class="{cls}"><b>Latest adviser feed snapshot is {age} days old.</b> '
                     f'The feed publishes weekly and the upstream manifest keeps only the '
                     f'current file, so a missed week is lost permanently.</p>')
        except Exception:
            pass

    # autopilot jobs, with their controls
    try:
        tasks = c.execute("SELECT * FROM auto_task ORDER BY kind").fetchall()
    except Exception:
        tasks = []
    labels = {"brochures": ("Brochure coverage",
                            "Downloads and tags every in-band firm's Part 2A "
                            "brochure. Why: brochures carry the firm's own words "
                            "on covered calls, alternatives and real estate, plus "
                            "its contact details. Runs until complete, then only "
                            "picks up newly filed brochures."),
              "contact_extract": ("Contact extraction",
                                  "Reads the first pages of each cached brochure "
                                  "for the emails and phone numbers the firm "
                                  "itself filed. Why: these are the only contact "
                                  "details that are real rather than guessed. No "
                                  "network use at all."),
              "web_enrich": ("Website enrichment",
                             "Reads each firm's own website (homepage plus its "
                             "team and contact pages) for people, titles, emails "
                             "and phone numbers. Why: no filing carries an "
                             "individual's email or direct line; the firm's own "
                             "site is where that lives. Every page is cached and "
                             "never refetched."),
              "firm_refresh": ("Flagged-firm refresh",
                               "Pulls the current ADV PDF for firms flagged on "
                               "custody questions and reads today's custodian "
                               "names. Why: the bulk custodian source ends "
                               "December 2024, and the Schwab signal should not "
                               "rest on stale data."),
              "email_verify": ("Email domain checks",
                               "Tests guessed addresses for valid syntax and "
                               "whether their domain publishes a mail server, "
                               "using a local DNS lookup. Free, instant, no "
                               "account and no third party. It tells you an "
                               "address is worthless, never that a mailbox "
                               "exists: only sending mail proves that."),
              "cusip_verify": ("CUSIP re-verify",
                               "Re-observes the 20 target security identifiers "
                               "against real filings when the map is over 90 "
                               "days old. Why: issuers rename and share classes "
                               "change CUSIPs, and a stale identifier looks "
                               "identical to a ticker nobody holds.")}
    arows = []
    for t in tasks:
        name, blurb = labels.get(t["kind"], (t["kind"], ""))
        pct = (t["progress"] or 0) / (t["total"] or 1) * 100 if t["total"] else 0
        running = t["desired_state"] == "running"
        state = ('<span class="chip lead">running</span>' if running
                 else '<span class="chip">paused</span>')
        btn = ("pause", "Pause") if running else ("start", "Start")
        arows.append(f"""<tr>
<td><b>{esc(name)}</b><div class="meta">{esc(blurb)}</div></td>
<td>{state}</td>
<td style="min-width:220px">
 <div style="background:var(--card2);border-radius:6px;height:8px;overflow:hidden">
  <div style="width:{pct:.1f}%;height:8px;background:var(--ok)"></div></div>
 <div class="meta">{esc(t["message"] or "not started")}</div></td>
<td><form method="post" action="/admin/task/{esc(t["kind"])}/{btn[0]}">
 <button type="submit" class="{'primary' if not running else ''}">{btn[1]}</button>
 </form></td></tr>""")
    if arows:
        auto_html = ('<h2 style="font-size:12px;text-transform:uppercase;'
                     'letter-spacing:.08em;color:var(--faint)">Autopilot</h2>'
                     '<table style="margin-bottom:16px"><thead><tr><th>Job</th>'
                     '<th style="width:90px">State</th><th>Progress</th>'
                     '<th style="width:90px"></th></tr></thead><tbody>'
                     + "".join(arows) + "</tbody></table>")
    else:
        auto_html = ""

    nsnap = len([s for s in snaps if s["source_key"] == "adv_feed"])
    fwd = ('<p class="warnc"><b>Forward-looking triggers are not yet live.</b> '
           f'Only {nsnap} adviser feed snapshot held; diffing needs two. '
           'New registration, AUM jump and IAR growth begin working after the next '
           'weekly capture.</p>') if nsnap < 2 else ""

    # The automatic weekly pull: state of the scheduler that runs it, so
    # "is this thing taking care of itself" is answerable at a glance.
    try:
        sched = c.execute("SELECT * FROM scheduler_state WHERE id=1").fetchone()
    except Exception:
        sched = None
    if sched and sched["last_check"]:
        mins = (datetime.now(timezone.utc)
                - datetime.fromisoformat(sched["last_check"])).total_seconds() / 60
        alive = mins < 45
        beat = (f'<span class="ok">on</span>, last checked {mins:.0f} min ago'
                if alive else
                f'<span class="bad">stalled</span>, last checked {mins/60:.1f} h ago '
                f'(restarts with Bellwether)')
        why = esc(sched["message"] or "")
        started = (f' Last automatic pull started {esc(sched["last_started"][:16])}.'
                   if sched["last_started"] else "")
        sched_html = (f'<p><b>Automatic weekly pull:</b> {beat}. {why}.{started} '
                      f'The SEC keeps no archive of this file, so the scheduler '
                      f'pulls as soon as a new week is due, including catching up '
                      f'the moment the PC comes back on after a missed week.</p>')
    else:
        sched_html = ('<p><b>Automatic weekly pull:</b> <span class="warnc">not yet '
                      'checked in</span>. It starts with Bellwether and first '
                      'checks about 20 seconds after launch.</p>')

    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>Pipeline health</title><style>{LIST_CSS}</style>
{nav("health")}
<header><h1>Pipeline health</h1><div class="sub">The one screen that answers: is
the data current, is anything broken, and what is running right now. Red means
failed or stale. Amber means flagged or due. No color means healthy.</div></header>
<div class="wrap">

<div class="card" style="margin-bottom:14px">{sched_html}{stale}{fwd}
<form method="post" action="/admin/run-weekly" style="margin:8px 0 0">
<button type="submit">Run weekly cycle now</button>
<span style="font-size:12.5px;color:var(--soft);margin-left:10px">
Manual trigger of the same cycle the scheduler runs: snapshot &rarr; firms &rarr;
triggers &rarr; rescore &rarr; brochure slice &rarr; CUSIP check when due.
Rarely needed now that the pull is automatic.</span>
{'<b style="color:var(--amber);margin-left:10px">A cycle is already running.</b>' if msg == 'already-running' else ''}
{'<b style="color:var(--ok);margin-left:10px">Started.</b>' if msg == 'started' else ''}
</form></div>

<h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--faint)">
Background jobs</h2>
<p class="meta" style="margin:0 0 10px">Long-running work, each with why it exists
and its own Start and Pause. Pausing takes effect within seconds and loses
nothing; a paused job resumes exactly where it stopped.</p>
{auto_html}

<h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--faint)">
Recent runs</h2>
<p class="meta" style="margin:0 0 10px">Every pipeline stage writes its outcome
here before and after it works, so a failure can never look like success. FLAGGED
means the SEC changed something upstream; the message says what.</p>
<table><thead><tr><th>Source</th><th>Stage</th><th>Status</th><th class="num">Rows</th>
<th class="num">Delta</th><th>Finished</th><th>Message</th></tr></thead>
<tbody>{"".join(rrow)}</tbody></table>

<div class="grid" style="display:grid;grid-template-columns:1.4fr 1fr;gap:14px;margin-top:20px">
<div><h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--faint)">
Immutable snapshots</h2>
<p class="meta" style="margin:0 0 10px">The raw SEC files, content-addressed and
never overwritten. Any past score can be recomputed from these exactly.</p>
<table><thead><tr><th>Source</th><th>Published</th><th class="num">Bytes</th>
<th>SHA-256</th><th>Captured</th></tr></thead><tbody>{"".join(srow)}</tbody></table></div>
<div><h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--faint)">
Record counts and coverage</h2>
<p class="meta" style="margin:0 0 10px">Derived tables and the coverage rates the
design treats as decision points. <a href="/health.json">Raw JSON</a>.</p>
<table><tbody>{crow}</tbody></table></div>
</div></div>""")
