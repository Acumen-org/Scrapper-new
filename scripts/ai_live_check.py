"""A live check of Bellwether AI against the connected model, run inside the
app container by the diagnostics workflow when asked (ai_check). It plans one
question with the model, reads one plain question by rules, and streams one
short answer, then prints the plans' filters and the timings. Two model calls
at most; no firm, person or key is printed.
"""
from __future__ import annotations

import json
import time

from prospect import ai, assistant, quickplan

MODEL_QUESTION = "Which Glynac firms around Dallas look most like good buyers?"
RULES_QUESTION = "Glynac firms in Texas on Microsoft 365 with at least $500M, largest first"


def main():
    if not ai.configured():
        print('Runtime ai check:', json.dumps({'configured': False}))
        return
    t0 = time.monotonic()
    quick = quickplan.plan(RULES_QUESTION)
    print('Runtime ai check rules:', json.dumps({
        'ok': quick is not None, 'ms': round((time.monotonic() - t0) * 1000, 1),
        'filters': (quick or {}).get('filters')}))
    t0 = time.monotonic()
    try:
        plan = assistant._plan(MODEL_QUESTION, [], 'diagnostics')
        print('Runtime ai check plan:', json.dumps({
            'ok': True, 'seconds': round(time.monotonic() - t0, 1), 'mode': plan['mode'],
            'filters': plan['filters'], 'sort': plan['sort']}))
    except ai.AIError as e:
        print('Runtime ai check plan:', json.dumps({
            'ok': False, 'seconds': round(time.monotonic() - t0, 1), 'error': str(e)}))
    t0, thinking, first, text = time.monotonic(), None, None, ''
    try:
        for piece in ai.stream('Answer in one plain sentence.',
                               [{'role': 'user', 'content': 'What does a registered investment '
                                 'adviser do?'}], feature='diagnostics', max_tokens=300):
            if piece is ai.THINKING:
                thinking = time.monotonic() - t0
                continue
            if first is None:
                first = time.monotonic() - t0
            text += piece
        print('Runtime ai check stream:', json.dumps({
            'ok': True, 'thinking_from_seconds': None if thinking is None else round(thinking, 1),
            'first_words_seconds': round(first or 0, 1),
            'total_seconds': round(time.monotonic() - t0, 1), 'chars': len(text)}))
    except ai.AIError as e:
        print('Runtime ai check stream:', json.dumps({
            'ok': False, 'seconds': round(time.monotonic() - t0, 1), 'error': str(e)}))


if __name__ == '__main__':
    main()
