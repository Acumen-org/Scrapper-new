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


if __name__ == '__main__':
    main()
