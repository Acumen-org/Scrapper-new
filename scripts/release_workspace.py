"""Apply the workspace release once and report only non-sensitive diagnostics."""
from __future__ import annotations

import time
from prospect import db, jobs, products, settings, msauth, ai, users

# Bumped when a release must re-read website signals and rescore every firm once.
# v3 classify: firm types, Microsoft Dynamics and Salesforce-built CRMs for Glynac.
RELEASE = 'workspace-2026-10-v3-classify'
# Releases that also re-crawl every website and mail record; the v3 one does
# not, because the October 5 release queued that full refresh already.
FULL_REFRESH = False


def main():
    issues = []
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
        requested = jobs.request_full_refresh(c) if FULL_REFRESH else 0
        # AI contact research is new in this release. A feature list saved in
        # Settings before it existed would keep it off, so add it once; the
        # daily AI limit still bounds what it can spend, and an admin can
        # untick it under Settings, AI.
        feats = settings.get_list('ai.features')
        if feats and 'research' not in feats:
            settings.set('ai.features', ','.join(feats + ['research']), by='release ' + RELEASE)
            print('AI contact research switched on in Settings, AI', flush=True)
        c.execute('INSERT INTO app_release (version, applied_at, evaluated_firms) VALUES (?,?,?) ON CONFLICT DO NOTHING',
                  (RELEASE, jobs.now_iso(), audit['done']))
        c.commit()
        print('Website and mail-platform refreshes queued:', requested, flush=True)
    total = c.execute('SELECT COUNT(*) n FROM firm_current').fetchone()['n']
    scored = c.execute('SELECT COUNT(DISTINCT crd) n FROM product_score').fetchone()['n']
    hidden = c.execute("SELECT COUNT(*) n FROM contact_point WHERE kind='email' AND verify_status!='valid'").fetchone()['n']
    # Addresses the firm itself published may show unconfirmed; a guess never may.
    visible_bad = c.execute("SELECT COUNT(*) n FROM usable_contact_point WHERE kind='email' AND source IN ('pattern','ai_web') AND verify_status!='valid'").fetchone()['n']
    evaluated = c.execute('SELECT evaluated_firms FROM app_release WHERE version=?', (RELEASE,)).fetchone()['evaluated_firms']
    print('Firm universe:', total, 'evaluated on release:', evaluated, 'passing at least one product gate:', scored)
    print('Internal email candidates:', hidden, 'unverified guesses visible:', visible_bad)
    assert visible_bad == 0
    assert evaluated == total, 'The firm universe changed during release; run a fresh rescore'
    print('Microsoft configured:', msauth.configured())
    admin = users.effective({'login':'rahul.gopan@acumen-strategy.com', 'role':'user'})
    print('Designated Microsoft account receives admin:', users.is_admin(admin))
    print('AI provider configured:', ai.configured(), 'ask enabled:', ai.enabled('ask'))
    print('AI provider and model:', ai.provider(), ai.model('smart'))
    print('Reacher service configured:', bool(settings.get('verify.reacher_url')))
    print('Verification mode:', settings.get('verify.engine'))
    from prospect import verify
    verification = verify.engine_status(refresh=True)
    print('Verification connectivity:', {k: verification[k] for k in
          ('resolved', 'reacher_ok', 'port25_ok')}, flush=True)
    if settings.get('verify.reacher_url'):
        import requests
        response = requests.post(settings.get('verify.reacher_url').rstrip('/') + '/v0/check_email',
                                 headers=verify._reacher_headers(),
                                 json={'to_email':'connectivity-check@example.invalid'}, timeout=30)
        working = response.status_code == 200 and bool(response.json().get('syntax'))
        print('Reacher API response received:', working, flush=True)
        if not working:
            issues.append('Reacher API')
    if ai.enabled('ask'):
        try:
            response = ai.complete('This is an application connectivity check. Reply with Ready.',
                                   [{'role':'user', 'content':'Check connection.'}],
                                   feature='ask', max_tokens=1000)
            print('AI provider response received:', bool(response), flush=True)
        except Exception as exc:
            print('AI provider check failed:', type(exc).__name__, flush=True)
            issues.append('AI provider')
    print('Scheduled jobs:', len(jobs.JOBS), 'paused:', sum(s.get('desired_state')=='paused' for k,s in jobs.states(c).items() if k in jobs.BY_KIND))
    print('Jobs requiring a retry:', [k for k,s in jobs.states(c).items()
          if k in jobs.BY_KIND and s.get('last_status') in ('failed', 'timeout')])
    for k,s in jobs.states(c).items():
        if k in jobs.BY_KIND and s.get('last_status') in ('failed', 'timeout'):
            import re
            reason = re.sub(r'https?://\S+', '[source URL]', s.get('message') or '')
            print('Job retry reason:', k, reason[:220])
    from prospect import config, procs
    print('Automatic enrichment worker alive:', bool(procs.alive_pid(config.DATA_DIR / 'autopilot.pid')))
    # Why AI calls fail and how far contact discovery has got, in aggregate
    # only: no addresses, names or keys are printed.
    import re as _re

    def _scrub(text):
        text = _re.sub(r'[A-Za-z0-9_~+/.=-]{32,}', '[long value omitted]', str(text or ''))
        return _re.sub(r'[\w.+-]+@[\w-]+(\.[\w-]+)+', '[address]', text)[:300]
    try:
        for r in c.execute('SELECT at, feature, provider, model, error FROM ai_call'
                           ' WHERE ok=0 ORDER BY id DESC LIMIT 3').fetchall():
            print('AI failure:', r['at'], r['feature'], r['provider'], r['model'], _scrub(r['error']))
        r = c.execute("SELECT COUNT(*) n, SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) good FROM ai_call"
                      " WHERE at >= to_char(NOW() - INTERVAL '1 day', 'YYYY-MM-DD')").fetchone()
        print('AI calls in the last day:', r['n'], 'succeeded:', r['good'] or 0)
    except Exception as exc:
        c.rollback()
        print('AI call log unavailable:', type(exc).__name__)
    try:
        rows = c.execute("SELECT verify_status s, COUNT(*) n FROM contact_point WHERE kind='email'"
                         " GROUP BY 1 ORDER BY 2 DESC").fetchall()
        print('Email statuses:', {r['s']: r['n'] for r in rows})
        r = c.execute("SELECT COUNT(*) n FROM contact_point WHERE kind='email' AND verified_at"
                      " >= to_char(NOW() - INTERVAL '1 day', 'YYYY-MM-DD')").fetchone()
        print('Emails checked in the last day:', r['n'])
        import json as _json
        from collections import Counter
        reasons = Counter()
        for r in c.execute("SELECT verify_detail d FROM contact_point WHERE kind='email'"
                           " AND verify_status IN ('unknown','risky','catch_all')"
                           " AND verify_detail IS NOT NULL ORDER BY verified_at DESC LIMIT 3000"):
            try:
                reason = _json.loads(r['d']).get('reason') or ''
            except Exception:
                reason = r['d'] or ''
            # One bucket per kind of answer, not per domain.
            reason = _re.sub(r'\b[\w-]+(\.[\w-]+)+\b', '[domain]', _scrub(reason))
            reasons[_re.sub(r'\d{3,}', 'N', reason)[:120]] += 1
        for reason, n in reasons.most_common(6):
            print('Unresolved reason:', n, reason)
        rows = c.execute("SELECT platform p, COUNT(*) n FROM firm_mail_platform GROUP BY 1"
                         " ORDER BY 2 DESC LIMIT 8").fetchall()
        print('Mail platforms:', {r['p']: r['n'] for r in rows})
        r = c.execute("SELECT COUNT(DISTINCT person_key) n FROM contact_point WHERE person_key != ''"
                      " AND kind='email' AND verify_status='valid'").fetchone()
        print('People with a verified email:', r['n'])
    except Exception as exc:
        c.rollback()
        print('Contact statistics unavailable:', type(exc).__name__)
    c.close()
    # Render the costly read path with the deployed database without creating an account.
    from prospect import webapp  # initialize routers before importing an individual view
    from prospect.people_view import people_page
    started = time.monotonic()
    people_page(q='', st='', on='', officers='', reach='ready', joined='', cfp='', disc='', sort='name', page_n=1, per=25)
    print('People page first render seconds:', round(time.monotonic()-started, 3))
    started = time.monotonic()
    people_page(q='', st='', on='', officers='', reach='ready', joined='', cfp='', disc='', sort='name', page_n=1, per=25)
    print('People page warm render seconds:', round(time.monotonic()-started, 3))
    if issues:
        raise SystemExit('Production checks requiring attention: ' + ', '.join(issues))


if __name__ == '__main__':
    main()
