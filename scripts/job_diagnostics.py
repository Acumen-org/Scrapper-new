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
            SUM(CASE WHEN checked_at >= to_char(NOW() - INTERVAL '1 day', 'YYYY-MM-DD') THEN 1 ELSE 0 END) day,
            COUNT(*) total FROM email_attempt WHERE status='valid'"""), default=str))
    print('Runtime brochures:', json.dumps(q("""SELECT status, COUNT(*) n,
            SUM(CASE WHEN COALESCE(text_chars,0) < 300 * GREATEST(COALESCE(pages,1),1) THEN 1 ELSE 0 END) thin,
            SUM(CASE WHEN COALESCE(text_chars,0) < 50 * GREATEST(COALESCE(pages,1),1) THEN 1 ELSE 0 END) image_only,
            SUM(COALESCE(pages,0)) pages FROM brochure GROUP BY 1 ORDER BY 2 DESC"""), default=str))
    print('Runtime scheduler:', json.dumps(q("SELECT * FROM scheduler_state"), default=str))
    print('Runtime feeds:', json.dumps(q("""SELECT source_key, MAX(captured_at) latest FROM snapshot
            GROUP BY 1 ORDER BY 1"""), default=str))
    print('Runtime runs:', json.dumps(q("""SELECT source_key, status, COUNT(*) n, MAX(started_at) latest
            FROM run_log WHERE started_at >= to_char(NOW() - INTERVAL '1 day', 'YYYY-MM-DD')
            GROUP BY 1, 2 ORDER BY 1, 2"""), default=str))


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
        _ai(conn)
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
