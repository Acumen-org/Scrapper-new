"""Apply the workspace release once and report only non-sensitive diagnostics."""
from __future__ import annotations

import time
from prospect import db, jobs, products, settings, msauth, ai, users

RELEASE = 'workspace-2026-10-glynac-v3-audit'


def main():
    c = db.connect()
    jobs.init(c)
    c.execute('CREATE TABLE IF NOT EXISTS app_release (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)')
    db.add_column(c, 'app_release', 'evaluated_firms', 'INTEGER')
    c.commit()
    applied = c.execute('SELECT 1 FROM app_release WHERE version=?', (RELEASE,)).fetchone()
    if not applied:
        from scripts import web_signals
        web_signals.main()
        started = time.monotonic()
        audit = {}
        counts = products.score_all(c, progress=lambda done, total: audit.update(done=done, total=total))
        assert audit.get('done') == audit.get('total'), 'Scoring did not finish the complete universe'
        print('All-firm rescoring complete:', counts, 'seconds:', round(time.monotonic()-started, 1), flush=True)
        requested = jobs.request_full_refresh(c)
        c.execute('INSERT INTO app_release (version, applied_at, evaluated_firms) VALUES (?,?,?) ON CONFLICT DO NOTHING',
                  (RELEASE, jobs.now_iso(), audit['done']))
        c.commit()
        print('Website and mail-platform refreshes queued:', requested, flush=True)
    total = c.execute('SELECT COUNT(*) n FROM firm_current').fetchone()['n']
    scored = c.execute('SELECT COUNT(DISTINCT crd) n FROM product_score').fetchone()['n']
    hidden = c.execute("SELECT COUNT(*) n FROM contact_point WHERE kind='email' AND verify_status!='valid'").fetchone()['n']
    visible_bad = c.execute("SELECT COUNT(*) n FROM usable_contact_point WHERE kind='email' AND verify_status!='valid'").fetchone()['n']
    evaluated = c.execute('SELECT evaluated_firms FROM app_release WHERE version=?', (RELEASE,)).fetchone()['evaluated_firms']
    print('Firm universe:', total, 'evaluated on release:', evaluated, 'passing at least one product gate:', scored)
    print('Internal email candidates:', hidden, 'unverified emails visible:', visible_bad)
    assert visible_bad == 0
    assert evaluated == total, 'The firm universe changed during release; run a fresh rescore'
    print('Microsoft configured:', msauth.configured())
    admin = users.effective({'login':'rahul.gopan@acumen-strategy.com', 'role':'user'})
    print('Designated Microsoft account receives admin:', users.is_admin(admin))
    print('AI provider configured:', ai.configured(), 'ask enabled:', ai.enabled('ask'))
    print('Reacher service configured:', bool(settings.get('verify.reacher_url')))
    print('Verification mode:', settings.get('verify.engine'))
    from prospect import verify
    verification = verify.engine_status(refresh=True)
    print('Verification connectivity:', {k: verification[k] for k in
          ('resolved', 'reacher_ok', 'port25_ok')}, flush=True)
    if ai.enabled('ask'):
        try:
            response = ai.complete('This is an application connectivity check. Reply with Ready.',
                                   [{'role':'user', 'content':'Check connection.'}],
                                   feature='ask', max_tokens=20)
            print('AI provider response received:', bool(response), flush=True)
        except Exception as exc:
            print('AI provider check failed:', type(exc).__name__, flush=True)
    print('Scheduled jobs:', len(jobs.JOBS), 'paused:', sum(s.get('desired_state')=='paused' for k,s in jobs.states(c).items() if k in jobs.BY_KIND))
    print('Jobs requiring a retry:', [k for k,s in jobs.states(c).items()
          if k in jobs.BY_KIND and s.get('last_status') in ('failed', 'timeout')])
    from prospect import config, procs
    print('Automatic enrichment worker alive:', bool(procs.alive_pid(config.DATA_DIR / 'autopilot.pid')))
    c.close()
    # Render the costly read path with the deployed database without creating an account.
    from prospect import webapp  # initialize routers before importing an individual view
    from prospect.people_view import people_page
    started = time.monotonic()
    people_page(q='', st='', on='', officers='', reach='ready', joined='', cfp='', disc='', sort='name', page_n=1, per=25)
    print('People page first render seconds:', round(time.monotonic()-started, 3))


if __name__ == '__main__':
    main()
