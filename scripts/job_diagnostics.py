"""Read-only aggregate runtime checks; never emit contacts, settings or secrets."""
from __future__ import annotations

import json

from prospect import db


def main():
    conn = db.connect()
    try:
        conn.execute('SET TRANSACTION READ ONLY')
        jobs = conn.execute("""SELECT kind, desired_state, last_status, last_run_at,
            next_run_at, running_since, runs, force FROM auto_task
            WHERE kind IN ('firm_refresh','contact_search','email_hunt','email_verify',
                           'people_index','classify','rescore','ai_research') ORDER BY kind""").fetchall()
        for row in jobs:
            print('Runtime jobs:', json.dumps(dict(row)))
        releases = conn.execute("""SELECT version, applied_at, evaluated_firms
            FROM app_release ORDER BY applied_at DESC LIMIT 1""").fetchall()
        print('Runtime release:', json.dumps([dict(row) for row in releases]))
        ready = conn.execute("""SELECT COUNT(*) AS n FROM information_schema.columns
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
