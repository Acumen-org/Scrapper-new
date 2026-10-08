"""A live check of Bellwether AI against the connected model, run inside the
app container by the diagnostics workflow when asked (ai_check). It plans one
fixed question and streams one short answer, then prints the plan's filters
and the timings. Three model calls at most; no firm, person or key is printed.
"""
from __future__ import annotations

import json
import time

from prospect import ai, assistant

QUESTION = "Glynac firms in Texas on Microsoft 365 with at least $500M, largest first"


def main():
    if not ai.configured():
        print('Runtime ai check:', json.dumps({'configured': False}))
        return
    t0 = time.monotonic()
    try:
        plan = assistant._plan(QUESTION, [], 'diagnostics')
        print('Runtime ai check plan:', json.dumps({
            'ok': True, 'seconds': round(time.monotonic() - t0, 1), 'mode': plan['mode'],
            'filters': plan['filters'], 'sort': plan['sort']}))
    except ai.AIError as e:
        print('Runtime ai check plan:', json.dumps({
            'ok': False, 'seconds': round(time.monotonic() - t0, 1), 'error': str(e)}))
    t0, first, text = time.monotonic(), None, ''
    try:
        for piece in ai.stream('Answer in one plain sentence.',
                               [{'role': 'user', 'content': 'What does a registered investment '
                                 'adviser do?'}], feature='diagnostics', max_tokens=300):
            if first is None:
                first = time.monotonic() - t0
            text += piece
        print('Runtime ai check stream:', json.dumps({
            'ok': True, 'first_piece_seconds': round(first or 0, 1),
            'total_seconds': round(time.monotonic() - t0, 1), 'chars': len(text)}))
    except ai.AIError as e:
        print('Runtime ai check stream:', json.dumps({
            'ok': False, 'seconds': round(time.monotonic() - t0, 1), 'error': str(e)}))


def variants():
    """How the connected OpenAI-compatible model answers the planning prompt
    with less thinking: the same request with each way of asking for it.
    Prints status, time, finish reason and token counts, never the reply."""
    import requests
    if ai.provider() not in ('edenai', 'openai'):
        return
    msgs = [{'role': 'user', 'content': (
        f"{assistant._vocab()}\n\nFILTERS: {assistant._fields_doc()}.\n\nTurn the QUESTION into "
        "a Bellwether search. Reply with one JSON object with keys mode, firm_name, filters, "
        "sort, limit, title; put only the filters the question implies.\n\n"
        f"QUESTION: {QUESTION}")}]
    base = ai._base()
    tries = [('baseline', {}), ('reasoning_effort low', {'reasoning_effort': 'low'}),
             ('reasoning_effort none', {'reasoning_effort': 'none'}),
             ('template no thinking', {'chat_template_kwargs': {'enable_thinking': False}}),
             ('reasoning disabled', {'reasoning': {'enabled': False}})]
    for name, extra in tries:
        headers, body = ai._openai_request(assistant.PLAN_SYSTEM, msgs, ai.model('smart'), 3000,
                                           {'type': 'object'}, base, False)
        body.update(extra)
        t0 = time.monotonic()
        try:
            r = requests.post(base.rstrip('/') + '/chat/completions', json=body, headers=headers,
                              timeout=90)
        except requests.RequestException as e:
            print('Runtime ai check variant:', json.dumps({'name': name, 'error': type(e).__name__}))
            continue
        out = {'name': name, 'status': r.status_code, 'seconds': round(time.monotonic() - t0, 1)}
        try:
            d = r.json()
            ch = (d.get('choices') or [{}])[0]
            msg = ch.get('message') or {}
            u = d.get('usage') or {}
            out.update(finish=ch.get('finish_reason'), content_chars=len(msg.get('content') or ''),
                       reasoning_chars=len(msg.get('reasoning_content') or msg.get('reasoning') or ''),
                       out_tokens=u.get('completion_tokens'),
                       reasoning_tokens=(u.get('completion_tokens_details') or {}).get('reasoning_tokens'))
            if r.status_code >= 400:
                out['error'] = ai._provider_message(r)[:160]
        except ValueError:
            out['unreadable'] = True
        print('Runtime ai check variant:', json.dumps(out))


if __name__ == '__main__':
    main()
    variants()
