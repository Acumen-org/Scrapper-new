"""Read-only aggregate runtime checks; never emit contacts, settings or secrets."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from prospect import db


def _scrub(text):
    text = re.sub(r'[A-Za-z0-9_~+/.=-]{32,}', '[long value omitted]', str(text or ''))
    return re.sub(r'[\w.+-]+@[\w-]+(\.[\w-]+)+', '[address]', text)[:300]


def _health(conn):
    """What each job last said, how the email hunt is moving, and whether the
    weekly SEC pull and the scheduler are alive. Counts and job summaries only."""
    def q(sql, args=()):
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        except Exception as exc:
            conn.rollback()
            return [{'unavailable': type(exc).__name__}]
    for row in q("SELECT kind, last_output FROM auto_task WHERE last_output IS NOT NULL ORDER BY kind"):
        print('Runtime job output:', row.get('kind'), _scrub(row.get('last_output')))
    for row in q("""SELECT started_at, finished_at, status, rows_in, rows_out, message FROM run_log
            WHERE source_key='email_hunt' ORDER BY started_at DESC LIMIT 6"""):
        print('Runtime hunt run:', json.dumps({k: (_scrub(v) if k == 'message' else v)
                                               for k, v in row.items()}, default=str))
    print('Runtime hunt states:', json.dumps(q(
        "SELECT state, COUNT(*) n FROM email_hunt GROUP BY 1 ORDER BY 2 DESC"), default=str))
    print('Runtime hunt firms:', json.dumps(q("""SELECT state, COUNT(*) n,
            SUM(CASE WHEN next_try_at <= to_char(NOW(), 'YYYY-MM-DD"T"HH24:MI:SS') THEN 1 ELSE 0 END) due
            FROM email_hunt_firm GROUP BY 1 ORDER BY 2 DESC"""), default=str))
    print('Runtime hunt found:', json.dumps(q("""SELECT
            SUM(CASE WHEN checked_at >= to_char(NOW() - INTERVAL '1 day', 'YYYY-MM-DD') THEN 1 ELSE 0 END) last_day,
            COUNT(*) total FROM email_attempt WHERE status='valid'"""), default=str))
    print('Runtime brochures:', json.dumps(q("""SELECT status, COUNT(*) n,
            SUM(CASE WHEN COALESCE(text_chars,0) < 300 * GREATEST(COALESCE(pages,1),1) THEN 1 ELSE 0 END) thin,
            SUM(CASE WHEN COALESCE(text_chars,0) < 50 * GREATEST(COALESCE(pages,1),1) THEN 1 ELSE 0 END) image_only,
            SUM(COALESCE(pages,0)) pages FROM brochure GROUP BY 1 ORDER BY 2 DESC"""), default=str))
    # Throughput: every recorded run in the last day, per source.
    for row in q("""SELECT source_key, COUNT(*) n,
            SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) ok,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed,
            SUM(CASE WHEN status NOT IN ('ok','failed') THEN 1 ELSE 0 END) other,
            ROUND(AVG(EXTRACT(EPOCH FROM (finished_at::timestamptz - started_at::timestamptz)))) avg_s,
            ROUND(MAX(EXTRACT(EPOCH FROM (finished_at::timestamptz - started_at::timestamptz)))) max_s,
            ROUND(SUM(EXTRACT(EPOCH FROM (finished_at::timestamptz - started_at::timestamptz)))) busy_s,
            SUM(rows_in) rows_in, SUM(rows_out) rows_out
            FROM run_log WHERE started_at >= to_char(NOW() - INTERVAL '1 day', 'YYYY-MM-DD"T"HH24:MI:SS')
            GROUP BY 1 ORDER BY busy_s DESC NULLS LAST"""):
        print('Runtime throughput:', json.dumps(row, default=str))
    try:
        from prospect import jobs, settings
        for j in jobs.overview(conn):
            print('Runtime backlog:', json.dumps({
                'kind': j['job'].kind, 'lane': j['job'].lane, 'state': j['state'],
                'backlog': j['backlog'], 'done': j['done'], 'total': j['total'],
                'recent': j.get('recent'), 'fails': j.get('fails'), 'runs': j.get('runs'),
                'args': list(j['job'].args), 'timeout_s': j['job'].timeout_s,
                'every_hours': j['job'].every_hours}, default=str))
        print('Runtime settings:', json.dumps({k: settings.get(k) for k in (
            'verify.engine', 'verify.per_minute', 'crawl.max_pages', 'crawl.recrawl_days',
            'crawl.contact_retry_days', 'crawl.use_browser', 'ai.daily_limit')}))
    except Exception as exc:
        conn.rollback()
        print('Runtime backlog: unavailable', type(exc).__name__, str(exc)[:120])
    # Per slice, from the worker's own record: how busy each lane was, and how
    # long, how heavy and how reliable each job's slices are.
    for row in q("""SELECT lane, COUNT(*) slices, ROUND(SUM(seconds)) busy_s,
            ROUND(100.0 * SUM(seconds) / GREATEST(EXTRACT(EPOCH FROM (NOW() - MIN(started_at)::timestamptz)), 1)) busy_pct
            FROM job_run WHERE started_at >= to_char(NOW() - INTERVAL '1 day', 'YYYY-MM-DD"T"HH24:MI:SS')
            GROUP BY 1 ORDER BY 1"""):
        print('Runtime lane use:', json.dumps(row, default=str))
    for row in q("""SELECT kind, COUNT(*) slices, ROUND(AVG(seconds)) avg_s, ROUND(MAX(seconds)) max_s,
            MAX(peak_mb) peak_mb, SUM(CASE WHEN status='timeout' THEN 1 ELSE 0 END) timeouts,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failures
            FROM job_run WHERE started_at >= to_char(NOW() - INTERVAL '1 day', 'YYYY-MM-DD"T"HH24:MI:SS')
            GROUP BY 1 ORDER BY 1"""):
        print('Runtime job speed:', json.dumps(row, default=str))
    print('Runtime ocr:', json.dumps(q("""SELECT ocr_status, ocr_method, COUNT(*) n FROM brochure
            WHERE ocr_status IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC"""), default=str))
    try:
        from prospect import ocr as _ocr, settings as _settings, verify as _verify
        print('Runtime engines:', json.dumps({
            'ocr_engine': _ocr.engine(), 'tesseract': _ocr.tesseract_cmd(),
            'per_minute_setting_stored': _settings.is_set('verify.per_minute'),
            'per_minute_now': _verify._per_minute()}))
    except Exception as exc:
        conn.rollback()
        print('Runtime engines: unavailable', type(exc).__name__)
    print('Runtime scanned:', json.dumps(q("""SELECT crd, version_id, pages, text_chars FROM brochure
            WHERE status='ok' AND COALESCE(text_chars,0) < 50 * GREATEST(COALESCE(pages,1),1)
            ORDER BY pages LIMIT 5"""), default=str))
    print('Runtime scheduler:', json.dumps(q("SELECT * FROM scheduler_state"), default=str))
    print('Runtime lanes:', json.dumps(q("SELECT lane, beat_at, job FROM worker_lane ORDER BY lane"),
                                       default=str))
    for row in q("SELECT kind, level, title, detail, since FROM job_alert ORDER BY level, since"):
        print('Runtime alert:', json.dumps({k: _scrub(v) for k, v in row.items()}, default=str))
    for row in q("""SELECT at, kind, action, detail FROM job_event
            ORDER BY id DESC LIMIT 10"""):
        print('Runtime watchdog:', json.dumps({k: _scrub(v) for k, v in row.items()}, default=str))
    print('Runtime feeds:', json.dumps(q("""SELECT source_key, MAX(captured_at) latest FROM snapshot
            GROUP BY 1 ORDER BY 1"""), default=str))
    print('Runtime runs:', json.dumps(q("""SELECT source_key, status, COUNT(*) n, MAX(started_at) latest
            FROM run_log WHERE started_at >= to_char(NOW() - INTERVAL '1 day', 'YYYY-MM-DD')
            GROUP BY 1, 2 ORDER BY 1, 2"""), default=str))


def _shapes(conn):
    """The layout of the SEC files on disk: the Base_A archive's columns and
    one firm element of each weekly feed, tag and attribute names only."""
    try:
        import gzip
        from prospect import config
        from lxml import etree
        r = conn.execute("SELECT rel_path FROM snapshot WHERE source_key='adv_filing_crosswalk'"
                         " ORDER BY id DESC LIMIT 1").fetchone()
        if r:
            p = config.SNAPSHOT_DIR / r["rel_path"]
            with open(p, "rb") as fh:
                gz = fh.read(2) == b"\x1f\x8b"
            fh = gzip.open(p, "rt", errors="replace") if gz else open(p, "rt", errors="replace")
            with fh:
                head = fh.readline().strip().lstrip("﻿")
            cols = [c.strip().strip('"') for c in head.split(",")]
            print("Runtime archive columns:", len(cols), json.dumps(cols[:243]))
        for key in ("adv_feed", "adv_state_feed"):
            r = conn.execute("SELECT rel_path FROM snapshot WHERE source_key=? ORDER BY id DESC LIMIT 1",
                             (key,)).fetchone()
            if not r:
                continue
            p = config.SNAPSHOT_DIR / r["rel_path"]
            opener = gzip.open if str(p).endswith(".gz") else open
            with opener(p, "rb") as fh:
                for _, el in etree.iterparse(fh, tag="Firm"):
                    out = {}
                    for ch in el:
                        if ch.tag == "FormInfo":
                            continue
                        out[ch.tag] = {"attrs": sorted(ch.attrib),
                                       "children": sorted({g.tag for g in ch.iter() if g is not ch})[:20],
                                       "sample": [dict(g.attrib) for g in ch.iter() if g is not ch and g.attrib][:3]}
                    p1 = el.find("./FormInfo/Part1A")
                    if p1 is not None:
                        for item in ("Item5A", "Item5B", "Item5C"):
                            x = p1.find(item)
                            out["Part1A/" + item] = dict(x.attrib) if x is not None else None
                    if any(k not in ("Info", "MainAddr", "MailingAddr", "Filing") for k in out):
                        print("Runtime feed shape:", key, json.dumps(out)[:1500])
                        break
                    el.clear()
    except Exception as exc:
        conn.rollback()
        print("Runtime feed shape: unavailable", type(exc).__name__, str(exc)[:160])


def _ai(conn):
    """Which model is connected and how its calls went: counts, times and the
    latest errors. The key is reported only as set or not, the base URL only
    as its host."""
    try:
        from prospect import ai, settings
        base = settings.get('ai.base_url') or ''
        print('Runtime ai config:', json.dumps({
            'provider': ai.provider(), 'model_smart': ai.model('smart'),
            'model_fast': ai.model('fast'), 'key_set': bool(settings.get('ai.api_key')),
            'base_host': urlsplit(base).netloc if base else '',
            'features': settings.get_list('ai.features'),
            'daily_limit': settings.get_int('ai.daily_limit', 400),
            'used_today': ai.calls_today()}))
        rows = conn.execute("""SELECT feature, provider, model, ok, COUNT(*) AS n,
            ROUND(AVG(ms)) AS avg_ms, MAX(ms) AS max_ms, ROUND(AVG(out_tokens)) AS avg_out
            FROM ai_call WHERE at >= to_char(NOW() - INTERVAL '2 day', 'YYYY-MM-DD')
            GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC LIMIT 20""").fetchall()
        for row in rows:
            print('Runtime ai calls:', json.dumps(dict(row), default=str))
        for row in conn.execute("""SELECT at, feature, provider, model, ms, error FROM ai_call
                WHERE ok=0 ORDER BY id DESC LIMIT 8""").fetchall():
            print('Runtime ai failure:', row['at'], row['feature'], row['provider'],
                  row['model'], row['ms'], _scrub(row['error']))
    except Exception as exc:
        conn.rollback()
        print('Runtime ai config:', json.dumps({'unavailable': type(exc).__name__}))


def _growth(conn):
    """Growth and open roles, as counts."""
    def one(sql):
        try:
            r = conn.execute(sql).fetchone()
            return dict(r) if r else {}
        except Exception as exc:
            conn.rollback()
            return {'unavailable': type(exc).__name__}
    print('Runtime growth:', json.dumps(one("""SELECT COUNT(*) AS firms,
        COUNT(*) FILTER (WHERE period_from IS NOT NULL) AS compared,
        COUNT(*) FILTER (WHERE team_growth > 0) AS growing_team,
        COUNT(*) FILTER (WHERE aum_growth >= 0.1) AS assets_up_10,
        COUNT(*) FILTER (WHERE clients_growth IS NOT NULL) AS with_clients,
        COUNT(*) FILTER (WHERE peer_support IS NOT NULL) AS with_peer,
        COUNT(*) FILTER (WHERE departures_senior > 0) AS senior_departures,
        COUNT(*) FILTER (WHERE new_locations > 0) AS new_offices,
        COUNT(*) FILTER (WHERE new_states > 0) AS new_states,
        MAX(computed_at) AS computed_at FROM firm_growth"""), default=str))
    print('Runtime history staff:', json.dumps(one("""SELECT COUNT(*) AS rows,
        COUNT(employees) AS with_staff, COUNT(clients) AS with_clients FROM firm_history"""),
        default=str))
    try:
        for r in conn.execute("""SELECT status, COUNT(*) AS n, MAX(checked_at) AS latest,
                COUNT(searched_at) AS searched FROM firm_careers GROUP BY 1 ORDER BY 2 DESC"""):
            print('Runtime careers:', json.dumps(dict(r), default=str))
        for r in conn.execute("""SELECT source, function, COUNT(*) AS n FROM firm_job_posting
                WHERE active=1 GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 20"""):
            print('Runtime open roles:', json.dumps(dict(r), default=str))
        for r in conn.execute("""SELECT ats, COUNT(*) AS n FROM firm_careers WHERE ats IS NOT NULL
                GROUP BY 1 ORDER BY 2 DESC LIMIT 12"""):
            print('Runtime tracking systems:', json.dumps(dict(r), default=str))
        for r in conn.execute("""SELECT title FROM firm_job_posting WHERE active=1
                ORDER BY last_seen DESC LIMIT 25"""):
            print('Runtime role sample:', _scrub(r['title']))
    except Exception as exc:
        conn.rollback()
        print('Runtime careers:', json.dumps({'unavailable': type(exc).__name__}))
    print('Runtime ats list:', json.dumps(one("""SELECT COUNT(*) AS scored,
        COUNT(*) FILTER (WHERE score >= 60) AS strong, ROUND(AVG(coverage)) AS avg_coverage,
        MAX(score) AS top FROM product_score WHERE product='ats' AND status='scored'"""),
        default=str))


def _list_speed():
    """Render each product list's first page as the app does and time every
    statement it runs, slowest first. Reads only."""
    import time
    from prospect import webapp, lists_view
    real_connect = db.connect
    log = []

    class Timed:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, args=()):
            t = time.perf_counter()
            try:
                return self._c.execute(sql, args)
            finally:
                log.append((time.perf_counter() - t, " ".join(str(sql).split())[:150]))

        def __getattr__(self, name):
            return getattr(self._c, name)

    def timed_connect(*a, **k):
        return Timed(real_connect(*a, **k))

    token = webapp.CURRENT_ACCOUNT.set({"login": "diagnostics", "role": "admin", "name": "Diagnostics",
                                        "active": 1, "families": []})
    db.connect = timed_connect
    lists_view.conn = timed_connect
    try:
        for key in ("ats", "glynac", "phh_fund"):
            log.clear()
            t = time.perf_counter()
            try:
                lists_view.product_list(key, view="ranked", q="", st="", owner="", stat="", sig="",
                                        reach="", cov="", size="", cat="", hiring="", sort="score",
                                        page_n=1, per=50, msg="", err="")
                status = "ok"
            except Exception as exc:
                status = type(exc).__name__ + ": " + str(exc)[:120]
            total = time.perf_counter() - t
            print("Runtime list speed:", json.dumps({"list": key, "seconds": round(total, 2),
                  "sql_seconds": round(sum(x[0] for x in log), 2), "statements": len(log),
                  "status": status}))
            for secs, sql in sorted(log, reverse=True)[:6]:
                print("Runtime list sql:", key, f"{secs:.2f}s", sql)
    finally:
        db.connect = real_connect
        lists_view.conn = webapp.conn
        webapp.CURRENT_ACCOUNT.reset(token)


def _scoring(conn):
    """The scoring rules each list runs on, and whether every stored score was
    computed under them: the weights in force against the saved edit, and the
    config stamp on the score rows against the current one."""
    from prospect import products

    def q(sql, args=()):
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        except Exception as exc:
            conn.rollback()
            return [{'unavailable': type(exc).__name__}]
    products.reload()
    print('Runtime scoring stamp:', products.stamp())
    print('Runtime scoring edits:', json.dumps(q(
        "SELECT product, updated_at FROM scoring_config ORDER BY product"), default=str))
    stored = {r['product']: json.loads(r['body']) for r in conn.execute(
        "SELECT product, body FROM scoring_config").fetchall()}
    for key in products.product_keys():
        live = products.product(key)
        weights = {cr['key']: cr.get('weight') for cr in live['criteria']}
        saved = {cr['key']: cr.get('weight') for cr in (stored.get(key) or {}).get('criteria', [])}
        print('Runtime scoring weights:', key, json.dumps(
            [[cr['label'], cr.get('weight')] for cr in live['criteria']]))
        if saved and saved != weights:
            print('Runtime scoring mismatch:', key, json.dumps({'saved': saved, 'in_force': weights}))
    for row in q("""SELECT product, status, COUNT(*) n, MIN(computed_at) oldest, MAX(computed_at) newest,
            COUNT(DISTINCT config_stamp) stamps FROM product_score GROUP BY 1, 2 ORDER BY 1, 2"""):
        print('Runtime scoring rows:', json.dumps(row, default=str))
    for row in q("""SELECT product, config_stamp, COUNT(*) n FROM product_score
            GROUP BY 1, 2 ORDER BY 1, 3 DESC"""):
        print('Runtime scoring row stamps:', json.dumps(row, default=str))
    for row in q("""SELECT id, product, note, updated_at, body FROM scoring_history
            WHERE product='ats' ORDER BY id DESC LIMIT 8"""):
        body = json.loads(row.pop('body') or '{}')
        row['weights'] = [[cr.get('key'), cr.get('weight')] for cr in body.get('criteria', [])]
        print('Runtime scoring history:', json.dumps(row, default=str))
    for row in q("""SELECT started_at, finished_at, status, message FROM run_log
            WHERE source_key IN ('rescore', 'score_products') ORDER BY started_at DESC LIMIT 5"""):
        print('Runtime scoring runs:', json.dumps({k: (_scrub(v) if k == 'message' else v)
                                                   for k, v in row.items()}, default=str))


def _consistency(conn):
    """The same quantity as each screen counts it, side by side, so figures
    that should agree can be checked against each other. Counts only."""
    from datetime import date, timedelta

    def one(sql, args=()):
        try:
            r = conn.execute(sql, args).fetchone()
            return dict(r) if r else {}
        except Exception as exc:
            conn.rollback()
            return {'unavailable': type(exc).__name__}
    print('Runtime counts people:', json.dumps(one("""SELECT
        (SELECT COUNT(*) FROM person) AS person_rows,
        (SELECT COUNT(*) FROM people_index) AS index_rows,
        (SELECT COUNT(DISTINCT indvl_pk) FROM people_index) AS index_people,
        (SELECT COUNT(*) FROM people_index WHERE has_email=1) AS index_email,
        (SELECT COUNT(*) FROM people_index WHERE email_status='valid') AS index_verified,
        (SELECT COUNT(*) FROM people_index WHERE title IS NOT NULL) AS index_titled,
        (SELECT COUNT(DISTINCT (crd, person_key)) FROM contact_point WHERE person_key LIKE 'n:%') AS site_people,
        (SELECT COUNT(DISTINCT (crd, person_key)) FROM contact_point WHERE person_key LIKE 'n:%'
            AND title IS NOT NULL AND title != '') AS site_people_titled""")))
    print('Runtime counts firms:', json.dumps(one("""SELECT
        (SELECT COUNT(*) FROM firm_current WHERE is_era=0) AS firms,
        (SELECT COUNT(*) FROM firm_scope) AS scope,
        (SELECT COUNT(DISTINCT crd) FROM product_score WHERE status='scored') AS on_any_list,
        (SELECT COUNT(DISTINCT crd) FROM product_score WHERE status='scored' AND product != 'ats') AS on_non_ats,
        (SELECT COUNT(*) FROM product_score WHERE product='ats' AND status='scored' AND score >= 50) AS ats_50""")))
    cutoff = (date.today() - timedelta(days=60)).isoformat()
    for key in ('ats', 'glynac', 'phh_fund'):
        print('Runtime counts list:', key, json.dumps(one("""SELECT
            (SELECT COUNT(*) FROM product_score WHERE product=? AND status='scored') AS kpi_n,
            (SELECT COUNT(*) FROM product_score p JOIN firm_current f ON f.crd=p.crd
                WHERE p.product=? AND p.status='scored') AS table_n,
            (SELECT COUNT(*) FROM product_score p WHERE p.product=? AND p.status='scored' AND EXISTS (
                SELECT 1 FROM trigger_event t LEFT JOIN trigger_action a ON a.trigger_id=t.id
                WHERE t.crd=p.crd AND t.suppressed=0 AND a.state IS NULL AND t.detected_date >= ?)) AS list_fresh,
            (SELECT COUNT(DISTINCT t.crd) FROM trigger_event t JOIN product_score p ON p.crd=t.crd
                AND p.product=? AND p.status='scored' WHERE t.suppressed=0 AND t.detected_date >= ?) AS home_fresh,
            (SELECT COUNT(*) FROM product_score p WHERE p.product=? AND p.status='scored' AND EXISTS (
                SELECT 1 FROM usable_contact_point x WHERE x.crd=p.crd AND x.kind='email'
                AND x.person_key != '' AND x.is_role=0)) AS list_reach,
            (SELECT COUNT(*) FROM product_score p WHERE p.product=? AND p.status='scored' AND EXISTS (
                SELECT 1 FROM usable_contact_point x WHERE x.crd=p.crd AND x.kind='email'
                AND x.person_key != '' AND x.is_role=0 AND x.source != 'pattern'
                AND x.verify_status NOT IN ('invalid','no_mail_server'))) AS home_reach""",
            (key, key, key, cutoff, key, cutoff, key, key)), default=str))


def _page_speed():
    """Render Settings, People and Home as the app does and time each, with
    its slowest statements. Reads only (the settings overview asks the mail
    engine and browser whether they are available, as the page does)."""
    import time
    from prospect import webapp, settings_view, people_view, home_view
    real_connect = db.connect
    log = []

    class Timed:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, args=()):
            t = time.perf_counter()
            try:
                return self._c.execute(sql, args)
            finally:
                log.append((time.perf_counter() - t, " ".join(str(sql).split())[:150]))

        def __getattr__(self, name):
            return getattr(self._c, name)

    def timed_connect(*a, **k):
        return Timed(real_connect(*a, **k))

    mods = (settings_view, people_view, home_view)
    token = webapp.CURRENT_ACCOUNT.set({"login": "diagnostics", "role": "admin", "name": "Diagnostics",
                                        "active": 1, "families": []})
    db.connect = timed_connect
    for m in mods:
        m.conn = timed_connect
    pages = [
        ("settings", lambda: settings_view.overview(msg="")),
        ("settings_jobs", lambda: settings_view.jobs_page(msg="")),
        ("people", lambda: people_view.people_page(q="", st="", on="", view="", role="", joined="",
                                                   desig="", disc="", size="", cat="", sort="",
                                                   page_n=1, per=50, reach="", officers="", cfp="")),
        ("home_data", home_view._gather),
    ]
    try:
        for name, fn in pages:
            log.clear()
            t = time.perf_counter()
            try:
                fn()
                status = "ok"
            except Exception as exc:
                status = type(exc).__name__ + ": " + str(exc)[:120]
            total = time.perf_counter() - t
            print("Runtime page speed:", json.dumps({"page": name, "seconds": round(total, 2),
                  "sql_seconds": round(sum(x[0] for x in log), 2), "statements": len(log),
                  "status": status}))
            for secs, sql in sorted(log, reverse=True)[:6]:
                print("Runtime page sql:", name, f"{secs:.2f}s", sql)
    finally:
        db.connect = real_connect
        for m in mods:
            m.conn = webapp.conn
        webapp.CURRENT_ACCOUNT.reset(token)


def main():
    conn = db.connect()
    try:
        conn.execute('SET TRANSACTION READ ONLY')
        jobs = conn.execute("""SELECT kind, desired_state, last_status, last_run_at,
            next_run_at, running_since, runs, force FROM auto_task
            ORDER BY kind""").fetchall()
        for row in jobs:
            print('Runtime jobs:', json.dumps(dict(row)))
        releases = conn.execute("""SELECT version, applied_at, evaluated_firms
            FROM app_release ORDER BY applied_at DESC LIMIT 1""").fetchall()
        print('Runtime release:', json.dumps([dict(row) for row in releases]))
        _health(conn)
        _shapes(conn)
        _ai(conn)
        _growth(conn)
        _scoring(conn)
        _consistency(conn)
        _list_speed()
        _page_speed()
        ready =conn.execute("""SELECT COUNT(*) AS n FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='firm_refresh'
              AND column_name IN ('detail','last_success_at')""").fetchone()['n']
        if ready != 2:
            print('Runtime custodians:', json.dumps({'ready': False}))
            return
        filings = conn.execute("""SELECT status, COUNT(*) AS total,
            SUM(CASE WHEN fetched_at >= to_char(NOW() - INTERVAL '1 day',
                'YYYY-MM-DD"T"HH24:MI:SS') THEN 1 ELSE 0 END) AS attempted_last_day,
            SUM(CASE WHEN custodians IS NOT NULL AND custodians != '' THEN 1 ELSE 0 END)
                AS with_custodians,
            MAX(fetched_at) AS latest_attempt, MAX(last_success_at) AS latest_success
            FROM firm_refresh GROUP BY status ORDER BY status""").fetchall()
        print('Runtime custodians:', json.dumps([dict(row) for row in filings]))
        failures = conn.execute("""SELECT CASE
            WHEN detail LIKE 'Filing exceeded%' THEN 'filing_timeout'
            WHEN detail LIKE 'SEC filing unavailable%' THEN 'source_unavailable'
            WHEN detail LIKE 'PDF exceeds%' THEN 'size_limit'
            WHEN detail LIKE 'PDF could not be read%' THEN 'pdf_parse'
            WHEN detail LIKE 'Filing reader stopped%' THEN 'reader_exit'
            ELSE 'legacy_or_unspecified' END AS reason, COUNT(*) AS total
            FROM firm_refresh WHERE status != 'ok' GROUP BY 1 ORDER BY 2 DESC""").fetchall()
        print('Runtime custodian retries:', json.dumps([dict(row) for row in failures]))
    finally:
        conn.rollback()
        conn.close()


if __name__ == '__main__':
    main()
