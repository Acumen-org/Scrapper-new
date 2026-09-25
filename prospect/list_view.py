"""System: is the data current, is anything broken, and what is running.

Exists to make a silent failure loud. A run that did nothing and reported
success is the worst outcome this system can produce, so skipped runs, flagged
row-count movements and stale snapshots are distinct states rather than folded
into a green tick. Coverage is shown as plain numbers: how much of the product
universe each enrichment has reached, which is what decides how much to trust
a score that says "not found".
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from .webapp import conn, esc, page

router = APIRouter()

SYS_CSS = """
.prog{background:var(--raise2);border-radius:6px;height:6px;overflow:hidden;margin-top:4px}
.prog i{display:block;height:6px;background:var(--ok)}
.cols{display:grid;grid-template-columns:1.4fr 1fr;gap:36px}
@media (max-width:1100px){.cols{grid-template-columns:1fr}}
"""

JOBS = {
    "brochures": ("Brochure coverage",
                  "Downloads and tags each firm's Part 2A brochure, best-scored firms "
                  "first. The brochure is where a firm says, in its own words, what it "
                  "does: covered calls, alternatives, real estate, model portfolios, "
                  "its reporting platform."),
    "brochure_retag": ("Brochure re-tag",
                       "Re-reads brochures already held when the tag vocabulary grows, "
                       "from the saved text, with no refetch. Runs until every brochure "
                       "carries the current vocabulary."),
    "mail_platform": ("Email platform",
                      "Looks up each firm's public mail records to tell Microsoft 365 "
                      "from Google. Glynac's marketing module needs Microsoft 365. A free "
                      "DNS question, re-asked every 90 days."),
    "web_enrich": ("Website reading",
                   "Reads each firm's own website (home, team and contact pages) for "
                   "people, titles, emails and phones, and for the client login that "
                   "names its reporting platform."),
    "contact_extract": ("Brochure contacts",
                        "Reads the first pages of each brochure for the emails and "
                        "phones the firm itself printed. The only contact details that "
                        "are filed rather than guessed."),
    "infer_emails": ("Email inference",
                     "Builds a best-guess email for each officer on the product lists "
                     "from the pattern the firm uses for its own people, then checks "
                     "the domain."),
    "email_verify": ("Email domain checks",
                     "Checks guessed addresses for valid syntax and a mail server on "
                     "the domain. Says when an address cannot work, never that a "
                     "mailbox exists."),
    "firm_refresh": ("Custodian refresh",
                     "Pulls the current ADV PDF for firms flagged on custody questions "
                     "and reads today's custodian names, since the bulk custodian "
                     "source ends December 2024."),
    "cusip_verify": ("CUSIP re-verify",
                     "Re-observes the target security identifiers against real filings "
                     "when the map is over 90 days old."),
}


@router.get("/health", response_class=HTMLResponse)
def health_view(msg: str = Query("")):
    c = conn()
    today = date.today()

    def safe(sql, args=()):
        try:
            r = c.execute(sql, args).fetchone()
            return r["n"] if r else None
        except Exception:
            c.rollback()
            return None

    snaps = c.execute("SELECT * FROM snapshot ORDER BY id").fetchall()
    runs = c.execute("SELECT * FROM run_log ORDER BY id DESC LIMIT 25").fetchall()

    # ---- freshness
    fresh = []
    feed = c.execute("SELECT published_at FROM snapshot WHERE source_key='adv_feed'"
                     " ORDER BY id DESC LIMIT 1").fetchone()
    if feed:
        try:
            m, d, y = feed["published_at"].split("/")
            age = (today - date(int(y), int(m), int(d))).days
            cls = "bad" if age > 10 else "ok"
            fresh.append(f'<p><b class="{cls}">The SEC adviser feed is {age} days old.</b> '
                         f'It publishes weekly and keeps only the current file, so a '
                         f'missed week is lost for good.</p>')
        except Exception:
            pass
    try:
        sched = c.execute("SELECT * FROM scheduler_state WHERE id=1").fetchone()
    except Exception:
        c.rollback()
        sched = None
    if sched and sched["last_check"]:
        mins = (datetime.now(timezone.utc)
                - datetime.fromisoformat(sched["last_check"])).total_seconds() / 60
        beat = (f'<span class="ok">on</span>, last checked {mins:.0f} min ago'
                if mins < 45 else
                f'<span class="bad">stalled</span>, last checked {mins/60:.1f} h ago')
        started = (f' Last automatic pull started {esc(sched["last_started"][:16])}.'
                   if sched["last_started"] else "")
        fresh.append(f'<p><b>Automatic weekly pull:</b> {beat}. '
                     f'{esc(sched["message"] or "")}.{started}</p>')
    else:
        fresh.append('<p><b>Automatic weekly pull:</b> <span class="warnc">not yet '
                     'checked in</span>. It starts with Bellwether.</p>')
    scored_at = safe("SELECT COUNT(*) n FROM product_score")
    last_score = None
    try:
        last_score = c.execute("SELECT MAX(computed_at) t FROM product_score"
                               ).fetchone()["t"]
    except Exception:
        c.rollback()
    if last_score:
        fresh.append(f'<p><b>Product scores</b> last computed '
                     f'{esc(last_score[:16].replace("T", " "))} UTC, {scored_at or 0:,} '
                     f'rows across the lists.</p>')
    flash = {"already-running": ('warnc', "A cycle is already running."),
             "started": ('ok', "Weekly cycle started. Progress appears under Recent runs."),
             "rescoring": ('ok', "Recomputing the product lists; it takes seconds.")}
    fl = flash.get(msg)
    flash_html = f'<p class="{fl[0]}"><b>{esc(fl[1])}</b></p>' if fl else ""

    # ---- coverage: how far each enrichment has reached into the lists
    scope = safe("SELECT COUNT(*) n FROM firm_scope") or 0

    def cov(label, n, of=scope, href=None):
        pct = f"{(n or 0) / of * 100:.0f}%" if of else "-"
        inner = (f'<div class="n">{pct}</div><div class="l">{esc(label)}<br>'
                 f'{(n or 0):,} of {of:,}</div>')
        return (f'<a href="{href}">{inner}</a>' if href else f'<div class="k">{inner}</div>')

    cov_html = ('<div class="strip">'
                + f'<div class="k"><div class="n">{scope:,}</div><div class="l">Firms on a '
                  f'product list</div></div>'
                + cov("Brochure read", safe("SELECT COUNT(*) n FROM brochure b JOIN "
                                            "firm_scope s ON s.crd=b.crd WHERE b.status='ok'"))
                + cov("Email platform known", safe(
                    "SELECT COUNT(*) n FROM firm_mail_platform m JOIN firm_scope s ON "
                    "s.crd=m.crd WHERE m.platform IN ('m365','google','other')"))
                + cov("Website read", safe("SELECT COUNT(*) n FROM web_enrich_state w "
                                           "JOIN firm_scope s ON s.crd=w.crd"))
                + cov("Has a real email", safe(
                    "SELECT COUNT(DISTINCT s.crd) n FROM firm_scope s WHERE EXISTS "
                    "(SELECT 1 FROM firm_contact_info i WHERE i.crd=s.crd AND "
                    "i.kind='email') OR EXISTS (SELECT 1 FROM web_contact w WHERE "
                    "w.crd=s.crd AND w.email IS NOT NULL)"))
                + '</div>')

    # ---- jobs
    try:
        tasks = {t["kind"]: t for t in c.execute("SELECT * FROM auto_task")}
    except Exception:
        c.rollback()
        tasks = {}
    # Progress measured now, against the product lists, rather than whatever a
    # job last wrote: a paused job's message can be weeks old and describe a
    # universe that has since changed.
    import yaml
    from . import config as _config
    try:
        tag_ver = int(yaml.safe_load((_config.CONFIG_DIR / "brochure_tags.yml")
                                     .read_text(encoding="utf-8"))["config_version"])
    except Exception:
        tag_ver = 1
    live_sql = {
        "brochures": ("SELECT COUNT(*) n FROM brochure b JOIN firm_scope s ON s.crd=b.crd",
                      "SELECT COUNT(*) n FROM firm_scope", "firms on a list have a brochure record"),
        "brochure_retag": (f"SELECT COUNT(*) n FROM brochure WHERE status='ok'"
                           f" AND COALESCE(tag_version,1) >= {tag_ver}",
                           "SELECT COUNT(*) n FROM brochure WHERE status='ok'",
                           f"brochures on vocabulary v{tag_ver}"),
        "mail_platform": ("SELECT COUNT(*) n FROM firm_mail_platform m JOIN firm_scope s"
                          " ON s.crd=m.crd", "SELECT COUNT(*) n FROM firm_scope",
                          "firms on a list checked"),
        "web_enrich": ("SELECT COUNT(*) n FROM web_enrich_state w JOIN firm_scope s"
                       " ON s.crd=w.crd",
                       "SELECT COUNT(*) n FROM firm_current f JOIN firm_scope s ON"
                       " s.crd=f.crd WHERE f.website IS NOT NULL AND f.website != ''",
                       "websites of firms on a list read"),
        "contact_extract": ("SELECT COUNT(*) n FROM contact_scan",
                            "SELECT COUNT(*) n FROM brochure WHERE status='ok'",
                            "brochures scanned for contacts"),
    }
    arows = []
    for kind, (name, blurb) in JOBS.items():
        t = dict(tasks.get(kind) or {"desired_state": "paused", "progress": 0, "total": 0,
                                     "message": None})
        if kind in live_sql:
            done_n, total_n = safe(live_sql[kind][0]), safe(live_sql[kind][1])
            if total_n:
                t["progress"], t["total"] = min(done_n or 0, total_n), total_n
                if t["desired_state"] != "running" or not t["message"]:
                    t["message"] = (f"{min(done_n or 0, total_n):,} of {total_n:,} "
                                    f"{live_sql[kind][2]}")
        pct = (t["progress"] or 0) / (t["total"] or 1) * 100 if t["total"] else 0
        running = t["desired_state"] == "running"
        state = ('<span class="chip lead">running</span>' if running
                 else '<span class="chip">paused</span>')
        btn = ("pause", "Pause", "") if running else ("start", "Start", "primary")
        arows.append(
            f'<tr><td style="width:38%"><b>{esc(name)}</b><div class="meta">{esc(blurb)}</div></td>'
            f'<td>{state}</td><td style="min-width:220px"><div class="prog"><i style="width:{pct:.1f}%"></i></div>'
            f'<div class="meta">{esc(t["message"] or "not started")}</div></td>'
            f'<td class="num"><form method="post" action="/admin/task/{esc(kind)}/{btn[0]}">'
            f'<button type="submit" class="sm {btn[2]}">{btn[1]}</button></form></td></tr>')

    # ---- runs
    rrow = []
    for r in runs:
        cls = {"ok": "ok", "failed": "bad", "skipped": "warnc",
               "running": "warnc"}.get(r["status"], "")
        flag = ' <b class="warnc">FLAGGED</b>' if r["flagged"] else ""
        delta = ""
        if r["pct_change"] is not None:
            dd = r["pct_change"]
            delta = f'<span class="{"bad" if abs(dd) > 5 else ""}">{dd:+.1f}%</span>'
        rrow.append(
            f'<tr><td>{esc(r["source_key"])}</td><td>{esc(r["stage"])}</td>'
            f'<td class="{cls}"><b>{esc(r["status"])}</b>{flag}</td>'
            f'<td class="num">{r["rows_out"] if r["rows_out"] is not None else "-"}</td>'
            f'<td class="num">{delta}</td>'
            f'<td class="small">{esc((r["finished_at"] or "")[:16])}</td>'
            f'<td class="small soft">{esc((r["message"] or "")[:140])}</td></tr>')

    srow = "".join(
        f'<tr><td>{esc(s["source_key"])}</td><td>{esc(s["published_at"])}</td>'
        f'<td class="num">{s["bytes"]:,}</td>'
        f'<td class="small muted" style="font-family:Consolas,monospace">{esc(s["sha256"][:16])}</td>'
        f'<td class="small">{esc(s["captured_at"][:10])}</td></tr>' for s in snaps)

    counts = []
    for t in ("firm", "firm_scope", "product_score", "trigger_event", "brochure",
              "brochure_tag", "firm_mail_platform", "web_signal", "firm_adv_extra",
              "schedule_a", "sched_d_7b1", "holding_13f", "web_contact",
              "contact_email", "score_override"):
        n = safe(f"SELECT COUNT(*) n FROM {t}")
        if n is not None:
            counts.append(f'<tr><td>{t}</td><td class="num">{n:,}</td></tr>')
    rq_m = safe("SELECT COUNT(*) n FROM adv_13f_match WHERE status='review'")
    rq_n = safe("SELECT COUNT(*) n FROM brochure_negation WHERE status='open'")
    c.close()

    body = f"""<div class="pg">
<div class="head"><div><h1>System</h1>
<div class="lede">Whether the data is current, how far it reaches, and what is running.
Red means failed or stale, amber means flagged or due; no colour means healthy.</div></div>
<div class="acts">
<form method="post" action="/admin/rescore"><button type="submit" title="Recompute every product list from the data already held">Recompute scores</button></form>
<form method="post" action="/admin/run-weekly"><button type="submit" class="primary" title="Snapshot, triggers, rescore, brochure slice">Run weekly cycle now</button></form>
</div></div>
<div class="seg" style="margin-bottom:6px"><a class="on" href="/health">Health</a>
<a href="/review">Review queue <span class="muted">{(rq_m or 0) + (rq_n or 0):,}</span></a></div>
<section class="s">{flash_html}{"".join(fresh)}</section>
<section class="s"><div class="s-head"><h2>Coverage of the product lists</h2>
<span class="more">A criterion that says &ldquo;not found&rdquo; is only as good as this</span></div>{cov_html}</section>
<section class="s" id="jobs"><div class="s-head"><h2>Background jobs</h2>
<span class="more">Pausing loses nothing; a job resumes where it stopped</span></div>
<table><thead><tr><th>Job</th><th>State</th><th>Progress</th><th></th></tr></thead>
<tbody>{"".join(arows)}</tbody></table></section>
<section class="s"><div class="s-head"><h2>Recent runs</h2>
<span class="more">Every stage records its outcome before and after it works</span></div>
<table><thead><tr><th>Source</th><th>Stage</th><th>Status</th><th class="num">Rows</th>
<th class="num">Change</th><th>Finished</th><th>Message</th></tr></thead>
<tbody>{"".join(rrow)}</tbody></table></section>
<section class="s"><div class="cols">
<div><h3>Immutable snapshots</h3>
<table><thead><tr><th>Source</th><th>Published</th><th class="num">Bytes</th><th>SHA-256</th>
<th>Captured</th></tr></thead><tbody>{srow}</tbody></table></div>
<div><h3>Record counts</h3><table><tbody>{"".join(counts)}</tbody></table>
<p class="meta"><a href="/health.json">Raw JSON</a></p></div></div></section>
</div>"""
    return page("System", "system", body, SYS_CSS)
