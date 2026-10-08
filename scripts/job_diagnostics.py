"""Read-only aggregate runtime checks; never emit contacts, settings or secrets."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from prospect import db


def _scrub(text):
    text = re.sub(r'[A-Za-z0-9_~+/.=-]{32,}', '[long value omitted]', str(text or ''))
    return re.sub(r'[\w.+-]+@[\w-]+(\.[\w-]+)+', '[address]', text)[:300]


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
