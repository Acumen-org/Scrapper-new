"""Enrichment: where Bellwether's facts come from, and adding more sources.

Three tabs, for admins and product owners:

  Sources         every source Bellwether already reads (SEC feeds, filings,
                  brochures, DNS), and the directories and websites the team
                  adds. Paste one or many addresses; Bellwether probes them
                  straight away, says whether its generic reader can extract
                  advisers from them or whether a source needs a custom
                  adapter (and why), then crawls them on their schedule and
                  matches what it finds to firms and people.
  Firm websites   each firm's own website, read for its people, titles,
                  emails, direct lines and vCards: coverage, recent reads,
                  failures, and reading one firm on demand.
  Verification    how many addresses are confirmed, and a box to check any
                  list of addresses right now.
"""

from __future__ import annotations

import json
import subprocess
import sys

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import config, contacts, procs, ui
from .webapp import conn, current_owner, esc, escn, page, qs_join

router = APIRouter()

TABS = [("sources", "Sources", "/enrichment"), ("websites", "Firm websites", "/enrichment/websites"),
        ("verify", "Email verification", "/enrichment/verify")]

BUILTIN = [
    ("adv_feed", "SEC investment adviser feed", "https://reports.adviserinfo.sec.gov/reports/CompilationReports/",
     "Weekly", "Every SEC-registered adviser: size, clients, advisors, custody, marketing, main phone, website."),
    ("adv_state_feed", "State-registered adviser feed", "https://reports.adviserinfo.sec.gov/reports/CompilationReports/",
     "Weekly", "The same for advisers registered with the states, mostly under $100M."),
    ("ia_indvl_feed", "SEC individual adviser feed (IAPD)", "https://adviserinfo.sec.gov",
     "Weekly", "Every registered rep: where they work, since when, where they were before, exams, designations, disclosures. The source of the people roster and hiring."),
    ("schedule_a_b", "Form ADV Schedule A and B", "https://www.sec.gov/foia-services/frequently-requested-documents/form-adv-data",
     "Once, archived", "Owners and executive officers with their filed titles."),
    ("schedule_d_archive", "Form ADV Schedule D archive", "https://www.sec.gov/foia-services/frequently-requested-documents/form-adv-data",
     "Once, archived", "Custodians, private funds and their service providers, 2011 to 2024."),
    ("brochures", "Form ADV Part 2A brochures", "https://adviserinfo.sec.gov",
     "Continuous", "The firm's own words on what it does, and the emails and phones it printed."),
    ("firm_refresh", "Current ADV filings (PDF)", "https://reports.adviserinfo.sec.gov/reports/ADV/",
     "Weekly", "Today's custodian names for firms that report custody."),
    ("13f", "SEC EDGAR 13F filings", "https://www.sec.gov/edgar",
     "Quarterly", "Holdings of the securities each product cares about."),
    ("form_d", "SEC Form D", "https://www.sec.gov/edgar", "As filed", "Private offerings and their owners."),
    ("mail_platform", "Public DNS mail records", "DNS", "Every 90 days",
     "Microsoft 365 or Google, from MX and SPF records; mail servers for verification."),
]

KINDS = {"directory": "Directory or listing of advisers", "website": "A single website",
         "search": "Search results page"}


def tabs(active: str) -> str:
    return ('<div class="seg" style="margin:4px 0 18px">' + "".join(
        f'<a class="{"on" if k == active else ""}" href="{href}">{esc(label)}</a>'
        for k, label, href in TABS) + '</div>')


def frame(title: str, tab: str, lede: str, inner: str, msg: str = "", err: str = "") -> HTMLResponse:
    flash = (f'<div class="note good">{esc(msg)}</div>' if msg else "") + \
            (f'<div class="note bad">{esc(err)}</div>' if err else "")
    body = (f'<div class="pg"><div class="head"><div><h1>{esc(title)}</h1>'
            f'{("<p class=lede>" + lede + "</p>") if lede else ""}</div></div>{tabs(tab)}{flash}{inner}</div>')
    return page(title, "enrichment", body)


def _bg(args: list[str], log: str = "directories.log") -> None:
    f = open(config.DATA_DIR / log, "ab")
    subprocess.Popen([sys.executable, "-m", *args], cwd=str(config.ROOT), stdout=f, stderr=f,
                     creationflags=procs.SPAWN_FLAGS)


def _directory():
    try:
        from . import directory
        return directory
    except ImportError:
        return None


@router.get("/enrichment", response_class=HTMLResponse)
def sources(msg: str = Query(""), err: str = Query("")):
    c = conn()
    last = {}
    for r in c.execute("""SELECT DISTINCT ON (source_key) source_key, status, finished_at,
            rows_out FROM run_log ORDER BY source_key, id DESC"""):
        last[r["source_key"]] = r
    alias = {"brochures": "brochure", "13f": "filing_13f", "form_d": "form_d",
             "mail_platform": "mail_platform", "firm_refresh": "firm_refresh",
             "ia_indvl_feed": "people"}
    brow = []
    for key, name, url, cad, what in BUILTIN:
        r = last.get(key) or last.get(alias.get(key, ""))
        when = (f'{esc(ui.ago(r["finished_at"]))} <span class="{"ok" if r["status"] == "ok" else "warnc"}">'
                f'{esc(r["status"])}</span>' if r and r["finished_at"] else '<span class="muted">-</span>')
        link = (f'<a class="muted small" href="{esc(url)}" target="_blank" rel="noopener" '
                f'data-noprefetch>{esc(url.replace("https://", ""))}</a>' if url.startswith("http") else
                f'<span class="muted small">{esc(url)}</span>')
        brow.append(f'<tr><td style="width:30%"><b>{esc(name)}</b><div>{link}</div></td>'
                    f'<td class="why">{esc(what)}</td><td class="small nowrap">{esc(cad)}</td>'
                    f'<td class="small nowrap">{when}</td></tr>')

    d = _directory()
    srows = []
    if d:
        try:
            for s in d.list_sources(c):
                verdict = ""
                if s.get("needs_adapter"):
                    verdict = (f'<span class="chip warn" title="{esc(s.get("adapter_note") or "")}">'
                               f'needs a custom adapter</span>')
                elif s.get("probe_json"):
                    verdict = '<span class="chip lead">generic reader works</span>'
                stats = (f'{s.get("records_found") or 0:,} records, {s.get("firms_matched") or 0:,} firms matched, '
                         f'{s.get("emails_found") or 0:,} emails, {s.get("phones_found") or 0:,} phones')
                srows.append(
                    f'<tr class="go" data-href="/enrichment/source/{s["id"]}"><td style="width:30%">'
                    f'<a class="firm" href="/enrichment/source/{s["id"]}">{esc(s["name"])}</a>'
                    f'<div class="meta">{esc((s.get("urls") or "").splitlines()[0] if s.get("urls") else "")}'
                    f'{" and more" if len((s.get("urls") or "").splitlines()) > 1 else ""}</div></td>'
                    f'<td>{verdict} <span class="chip">{esc(s.get("status") or "")}</span>'
                    f'<div class="meta">{esc(stats)}</div></td>'
                    f'<td class="small">{esc(ui.ago(s.get("last_run_at")) or "not yet")}'
                    f'<div class="meta">every {s.get("schedule_days") or 7} days</div></td></tr>')
        except Exception:
            c.rollback()
    c.close()
    kinds = "".join(ui.opt(k, "directory", v) for k, v in KINDS.items())
    add = f"""<section class="s"><h2>Add directories or websites</h2>
<p class="lede">Add source URLs. Sites that need a custom script are flagged after testing.</p>
<details class="source-help"><summary>Supported sources</summary>
<p>Advisor directories, association listings and conference pages. Search forms, login walls
and blocked pages may need a custom adapter. Check each site's terms of use before adding it.</p></details>
<form method="post" action="/enrichment/add" style="max-width:820px">
<label>Name<input type="text" name="name" placeholder="For example: NAPFA advisor directory, Texas" required></label>
<label style="margin-top:10px">Addresses, one per line<textarea name="urls" required placeholder="https://..."></textarea></label>
<div class="row" style="margin-top:10px">
<label>Type<select name="kind">{kinds}</select></label>
<label>Read again every<select name="schedule_days">{ui.opt("1", "7", "day")}{ui.opt("7", "7", "week")}{ui.opt("30", "7", "month")}{ui.opt("0", "7", "only once")}</select></label>
<label>Pages at most<input type="number" name="max_pages" value="200" min="1" max="5000"></label>
<label class="inline" style="margin-top:16px"><input type="checkbox" name="robots" value="1" checked> Obey the site's robots rules</label>
</div>
<button class="primary" type="submit" style="margin-top:12px">Add and test</button></form></section>"""
    inner = (f'<section class="s" style="border-top:0;padding-top:0"><div class="s-head"><h2>Your sources</h2></div>'
             f'<table><tbody>{"".join(srows) or "<tr><td class=empty>None yet. Add one below.</td></tr>"}</tbody></table></section>'
             f'{add}'
             f'<section class="s"><div class="s-head"><h2>Built-in sources</h2></div>'
             f'<table><thead><tr><th>Source</th><th>What it gives</th><th>How often</th><th>Last run</th></tr></thead>'
             f'<tbody>{"".join(brow)}</tbody></table></section>')
    return frame("Enrichment", "sources", "", inner, msg, err)


@router.post("/enrichment/add")
async def add_source(request: Request):
    form = await request.form()
    d = _directory()
    if not d:
        return RedirectResponse("/enrichment?err=The+directory+reader+is+not+installed.", status_code=303)
    name = (form.get("name") or "").strip()[:120]
    urls = [u.strip() for u in (form.get("urls") or "").splitlines() if u.strip()]
    urls = [u if u.lower().startswith("http") else "https://" + u for u in urls][:200]
    if not name or not urls:
        return RedirectResponse("/enrichment?err=Give+it+a+name+and+at+least+one+address.", status_code=303)
    try:
        days = int(form.get("schedule_days") or 7)
        max_pages = max(1, min(5000, int(form.get("max_pages") or 200)))
    except ValueError:
        days, max_pages = 7, 200
    c = conn()
    try:
        sid = d.add_source(c, name, urls, form.get("kind") or "directory", days,
                           bool(form.get("robots")), max_pages, current_owner())
    finally:
        c.close()
    _bg(["scripts.crawl_directories", "--probe-id", str(sid)])
    return RedirectResponse(f"/enrichment/source/{sid}?msg=Added.+Testing+it+now%3B+the+verdict+"
                            f"appears+here+within+a+minute.", status_code=303)


@router.get("/enrichment/source/{sid}", response_class=HTMLResponse)
def source_page(sid: int, msg: str = Query(""), err: str = Query(""), only: str = Query("")):
    d = _directory()
    if not d:
        return RedirectResponse("/enrichment", status_code=303)
    c = conn()
    s = d.get_source(c, sid)
    if not s:
        c.close()
        return RedirectResponse("/enrichment", status_code=303)
    recs = d.records(c, sid, limit=200, matched=(True if only == "matched" else None))
    c.close()
    probe = {}
    try:
        probe = json.loads(s.get("probe_json") or "{}")
    except ValueError:
        pass
    if s.get("needs_adapter"):
        verdict = (f'<div class="note"><b>Needs a custom adapter.</b> {esc(s.get("adapter_note") or "")}'
                   f'</div>')
    elif probe:
        verdict = (f'<div class="note good"><b>The generic reader works here.</b> '
                   f'{esc(probe.get("explanation") or "")}</div>')
    else:
        verdict = '<div class="note plain">Not tested yet. The test runs right after a source is added.</div>'
    sample = ""
    if probe.get("sample"):
        sample = ('<h3 style="margin-top:14px">What the test read</h3><table class="tight"><tbody>' + "".join(
            f'<tr><td>{esc(x.get("person_name") or "")}</td><td>{esc(x.get("firm_name") or "")}</td>'
            f'<td>{esc(x.get("email") or "")}</td><td>{esc(x.get("phone") or "")}</td></tr>'
            for x in probe["sample"][:6]) + '</tbody></table>')
    out = []
    for r in recs:
        match = (f'<a href="/firm/{esc(r["matched_crd"])}">matched</a>' if r.get("matched_crd")
                 else '<span class="muted">no match</span>')
        out.append(
            f'<tr><td>{esc(r.get("person_name") or "")}<div class="meta">{esc(r.get("title") or "")}</div></td>'
            f'<td>{esc(r.get("firm_name") or "")}<div class="meta">{esc(r.get("city") or "")} '
            f'{esc(r.get("state") or "")}</div></td>'
            f'<td class="small">{esc(r.get("email") or "")}<div class="meta">{esc(r.get("phone") or "")}</div></td>'
            f'<td>{match}<div class="meta">{esc(r.get("match_method") or "")}</div></td></tr>')
    rows = "".join(out)
    status = s.get("status") or "active"
    toggle = ("resume", "Resume") if status == "paused" else ("pause", "Pause")
    inner = f"""{verdict}
<div class="row" style="margin:6px 0 18px">
<form method="post" action="/enrichment/source/{sid}/run"><button class="primary sm" type="submit">Read it now</button></form>
<form method="post" action="/enrichment/source/{sid}/probe"><button class="sm" type="submit">Test again</button></form>
<form method="post" action="/enrichment/source/{sid}/{toggle[0]}"><button class="sm ghost" type="submit">{toggle[1]}</button></form>
<form method="post" action="/enrichment/source/{sid}/delete" onsubmit="return confirm('Remove this source? Contacts already found stay.')">
<button class="sm ghost" type="submit">Remove</button></form></div>
<dl class="kv"><dt>Addresses</dt><dd class="small">{"<br>".join(esc(u) for u in (s.get("urls") or "").splitlines()[:20])}</dd>
<dt>Type</dt><dd>{esc(KINDS.get(s.get("kind"), s.get("kind") or ""))}</dd>
<dt>Schedule</dt><dd>{"once" if not s.get("schedule_days") else "every " + str(s.get("schedule_days")) + " days"}; last read {esc(ui.ago(s.get("last_run_at")) or "never")}</dd>
<dt>Found</dt><dd>{s.get("records_found") or 0:,} records; {s.get("firms_matched") or 0:,} matched to firms; {s.get("people_found") or 0:,} people; {s.get("emails_found") or 0:,} emails; {s.get("phones_found") or 0:,} phones</dd>
<dt>Added by</dt><dd>{esc(s.get("created_by") or "")} {esc(ui.ago(s.get("created_at")))}</dd></dl>
{sample}
<section class="s"><div class="s-head"><h2>Records</h2>
<div class="seg"><a class="{"on" if only != "matched" else ""}" href="/enrichment/source/{sid}">All</a>
<a class="{"on" if only == "matched" else ""}" href="/enrichment/source/{sid}?only=matched">Matched to a firm</a></div></div>
<table class="tight"><thead><tr><th>Person</th><th>Firm</th><th>Contact</th><th>Match</th></tr></thead>
<tbody>{rows or '<tr><td colspan="4" class="empty">Nothing read yet.</td></tr>'}</tbody></table></section>"""
    return frame(s["name"], "sources", "A directory or website the team added.", inner, msg, err)


@router.post("/enrichment/source/{sid}/{action}")
def source_action(sid: int, action: str):
    d = _directory()
    if not d:
        return RedirectResponse("/enrichment", status_code=303)
    c = conn()
    try:
        if action == "delete":
            d.delete_source(c, sid)
            return RedirectResponse("/enrichment?msg=Removed.", status_code=303)
        if action in ("pause", "resume"):
            d.update_source(c, sid, status="paused" if action == "pause" else "active")
            return RedirectResponse(f"/enrichment/source/{sid}", status_code=303)
    finally:
        c.close()
    if action == "run":
        _bg(["scripts.crawl_directories", "--source-id", str(sid)])
        return RedirectResponse(f"/enrichment/source/{sid}?msg=Reading+it+now.+Records+appear+as+they+are+found.",
                                status_code=303)
    if action == "probe":
        _bg(["scripts.crawl_directories", "--probe-id", str(sid)])
        return RedirectResponse(f"/enrichment/source/{sid}?msg=Testing+it+now.", status_code=303)
    return RedirectResponse(f"/enrichment/source/{sid}", status_code=303)


# ------------------------------------------------------------------ websites

@router.get("/enrichment/websites", response_class=HTMLResponse)
def websites(msg: str = Query(""), err: str = Query("")):
    c = conn()

    def one(sql, args=()):
        try:
            r = c.execute(sql, args).fetchone()
            return list(r.values())[0] if r else 0
        except Exception:
            c.rollback()
            return 0

    total = one("""SELECT COUNT(*) FROM firm_current f
                   WHERE f.website IS NOT NULL AND f.website != ''""")
    read = one("SELECT COUNT(*) FROM web_enrich_state w")
    ok = one("SELECT COUNT(*) FROM web_enrich_state w WHERE w.status='ok'")
    pages = one("SELECT COUNT(*) FROM web_page")
    people = one("""SELECT COUNT(DISTINCT crd || person_key) FROM contact_point
                    WHERE source IN ('website','vcard','ai') AND person_key != ''""")
    emails = one("""SELECT COUNT(*) FROM contact_point WHERE kind='email' AND is_role=0
                    AND person_key != '' AND source IN ('website','vcard','ai')""")
    phones = one("""SELECT COUNT(*) FROM contact_point WHERE kind='phone' AND person_key != ''
                    AND source IN ('website','vcard','ai')""")
    recent = []
    try:
        recent = c.execute("""SELECT w.*, f.legal_name, f.website FROM web_enrich_state w
            JOIN firm_current f ON f.crd=w.crd ORDER BY w.scanned_at DESC LIMIT 30""").fetchall()
    except Exception:
        c.rollback()
    c.close()
    rrows = "".join(
        f'<tr class="go" data-href="/firm/{esc(r["crd"])}#contacts"><td><a class="firm" href="/firm/{esc(r["crd"])}#contacts">'
        f'{escn(r["legal_name"])}</a><div class="meta">{esc(r["website"] or "")}</div></td>'
        f'<td><span class="chip {"lead" if r["status"] == "ok" else "warn"}">{esc(r["status"])}</span></td>'
        f'<td class="num">{r["pages"]}</td><td class="num">{r["people"]}</td><td class="num">{r["emails"]}</td>'
        f'<td class="small">{esc(ui.ago(r["scanned_at"]))}</td></tr>' for r in recent)
    pct = (read / total * 100) if total else 0
    inner = f"""<div class="kpis" style="margin-bottom:18px">
<div class="kpi"><div class="n">{pct:.0f}%</div><div class="l">Firm websites read</div><div class="d">{read:,} of {total:,}; {ok:,} answered</div></div>
<div class="kpi"><div class="n">{pages:,}</div><div class="l">Pages read</div></div>
<div class="kpi"><div class="n">{people:,}</div><div class="l">People found on sites</div></div>
<div class="kpi"><div class="n">{emails:,}</div><div class="l">Personal emails found</div></div>
<div class="kpi"><div class="n">{phones:,}</div><div class="l">Direct lines found</div></div></div>
<p class="lede">Websites, team pages and vCards supply sourced contacts. Only verified emails appear in the directory. Firms with missing contacts are revisited automatically.</p>
<form class="row" method="post" action="/enrichment/websites/read" style="margin:12px 0 20px">
<input type="text" name="crd" placeholder="CRD of a firm" style="min-width:160px">
<input type="url" name="url" placeholder="Website, if different (optional)" style="min-width:300px">
<button type="submit" class="primary">Read it now</button></form>
<table><thead><tr><th>Recently read</th><th>Result</th><th class="num">Pages</th><th class="num">People</th>
<th class="num">Emails</th><th>When</th></tr></thead><tbody>{rrows or '<tr><td colspan="6" class="empty">No website read yet.</td></tr>'}</tbody></table>"""
    return frame("Firm websites", "websites", "Each firm's own website, read for its people and how to reach them.",
                 inner, msg, err)


@router.post("/enrichment/websites/read")
def websites_read(crd: str = Form(""), url: str = Form("")):
    crd = crd.strip()
    if not crd.isdigit():
        return RedirectResponse("/enrichment/websites?err=Give+the+firm%27s+CRD+number.", status_code=303)
    args = ["scripts.web_enrich", "--crd", crd]
    if url.strip():
        args += ["--url", url.strip()]
    _bg(args, "web_enrich.log")
    return RedirectResponse(f"/firm/{crd}#contacts", status_code=303)


# ------------------------------------------------------------------ verification

@router.get("/enrichment/verify", response_class=HTMLResponse)
def verify_tab(msg: str = Query(""), err: str = Query("")):
    return _verify_page(msg=msg, err=err)


def _verify_page(results: list | None = None, msg: str = "", err: str = "",
                 pasted: str = "") -> HTMLResponse:
    c = conn()
    counts = {r["verify_status"]: r["n"] for r in c.execute(
        "SELECT verify_status, COUNT(*) n FROM contact_point WHERE kind='email' GROUP BY 1")}
    personal_valid = c.execute("""SELECT COUNT(*) n FROM contact_point WHERE kind='email'
        AND verify_status='valid' AND is_role=0""").fetchone()["n"]
    c.close()
    try:
        from . import verify
        es = verify.engine_status()
        eng = (f'Engine in use: <b>{esc(es["resolved"])}</b>; outbound port 25 '
               f'{"open" if es.get("port25_ok") else "closed" if es.get("port25_ok") is False else "not tested"}'
               f'{"; Reacher answering" if es.get("reacher_ok") else ""}.')
    except Exception:
        eng = "The verification engine did not load."
    total = sum(counts.values()) or 1
    bars = "".join(
        f'<div class="mrow"><div><span class="chip v-{esc(k)}">{esc(contacts.VERIFY_LABEL.get(k, k))}</span></div>'
        f'<div class="meter{" red" if k in ("invalid", "no_mail_server") else " amber" if k in ("risky", "catch_all") else ""}">'
        f'<i style="width:{n / total * 100:.1f}%"></i></div><div class="num">{n:,}</div></div>'
        for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    res_html = ""
    if results:
        res_html = ('<section class="s"><h2>Results</h2><table class="tight"><tbody>' + "".join(
            f'<tr><td class="mono">{esc(r["email"])}</td><td><span class="chip v-{esc(r["status"])}">'
            f'{esc(contacts.VERIFY_LABEL.get(r["status"], r["status"]))}</span></td>'
            f'<td class="small soft">{esc(r.get("reason") or "")}</td></tr>' for r in results)
            + '</tbody></table></section>')
    inner = f"""<p class="lede">{eng} Verified means the firm&rsquo;s mail server accepted that exact
mailbox and turned away a made-up one at the same domain. Accept-all domains say yes to everything,
so their answers prove nothing and are never shown as verified. The verification job checks every
address by itself, personal addresses first, and re-checks after 90 days.</p>
<div class="kpis" style="margin-bottom:16px"><div class="kpi"><div class="n">{counts.get("valid", 0):,}</div>
<div class="l">Verified addresses</div><div class="d">{personal_valid:,} of them named people</div></div>
<div class="kpi"><div class="n">{counts.get("unverified", 0) + counts.get("queued", 0):,}</div><div class="l">Waiting to be checked</div></div>
<div class="kpi"><div class="n">{counts.get("invalid", 0) + counts.get("no_mail_server", 0):,}</div><div class="l">Known to bounce</div></div></div>
{bars}
<div class="row" style="margin:16px 0"><form method="post" action="/settings/jobs/email_verify/run">
<button class="sm" type="submit">Run the verification job now</button></form></div>
<section class="s"><h2>Check addresses now</h2>
<p class="lede">Paste up to 15 addresses, one per line or separated by commas. Nothing is stored.</p>
<form method="post" action="/enrichment/verify" style="max-width:640px">
<textarea name="emails" placeholder="jane.doe@firm.com">{esc(pasted)}</textarea>
<button class="primary" type="submit" style="margin-top:10px">Check</button></form></section>{res_html}"""
    return frame("Email verification", "verify", "Confirming that an address will reach a person.",
                 inner, msg, err)


@router.post("/enrichment/verify", response_class=HTMLResponse)
def verify_now(emails: str = Form("")):
    import re
    addrs = [a.strip() for a in re.split(r"[\s,;]+", emails or "") if "@" in a][:15]
    if not addrs:
        return _verify_page(err="Paste at least one email address.")
    try:
        from . import verify
        results = verify.check_many(addrs)
    except Exception as e:
        return _verify_page(err=f"Verification failed: {e}", pasted=emails)
    return _verify_page(results=results, pasted=emails)
