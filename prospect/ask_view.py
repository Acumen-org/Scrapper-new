"""Bellwether AI, full page: ask about any firm, person or market.

The same conversation panel a firm page carries, with the whole universe as
its scope. Questions become a search Bellwether runs itself, and the firms in
the answer are always the rows that matched, so a firm on screen is one that
truly fits what was asked.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from . import ai, users
from .webapp import current_account, esc, page

router = APIRouter()

SUGGESTIONS = [
    "Which PHH Fund I firms in Texas or Florida hired advisors in the last year?",
    "Glynac firms on Microsoft 365 that use Black Diamond, largest first",
    "AcuBooth firms with $500M+ and at least one verified email",
    "Firms on our lists that lost two or more advisors this year",
    "Which unclaimed firms scoring 60+ on any list have a new signal?",
    "Multi-family offices that already run a real estate fund",
]


@router.get("/ask", response_class=HTMLResponse)
def ask_page(q: str = Query("")):
    st = ai.status()
    if st["configured"]:
        sub = f"{max(0, st['limit'] - st['used_today']):,} AI calls remaining today"
    elif users.is_admin(current_account()):
        sub = 'Name search only. <a href="/settings/ai">Connect AI</a>.'
    else:
        sub = "Name search only. Ask an admin to connect AI."
    sugs = "".join(f"<button type='button'>{esc(s)}</button>" for s in SUGGESTIONS)
    body = f"""<div class="pg narrow">
<div class="aipanel aiwide" data-scope="global" data-ask="{esc(q)}">
<div class="aihead"><canvas data-orb="breathing" data-size="64" data-px="72" data-tint="#d9d4ca"
 aria-label="Bellwether AI"></canvas><div><h1>Bellwether AI</h1>
<div class="s">{sub}</div></div></div>
<div class="aisugs">{sugs}</div>
<div class="aimsgs"></div>
<form class="aiform"><input type="text" aria-label="Ask Bellwether" placeholder="Ask about firms, people or product fit" autocomplete="off" autofocus>
<button class="primary" type="submit">Ask</button></form>
</div></div>"""
    return page("Bellwether AI", "ask", body, orbs=True)
