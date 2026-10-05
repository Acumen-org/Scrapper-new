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
    prompts = [('Find firms', SUGGESTIONS[0]), ('Compare product fit', SUGGESTIONS[2]), ('Track changes', SUGGESTIONS[3])]
    sugs = "".join(f'<button type="button" data-question="{esc(question)}">{label}</button>' for label, question in prompts)
    body = f"""<div class="ai-workspace">
<header class="ai-page-head"><span>Bellwether AI</span><a href="/ask" class="btn ghost">New conversation</a></header>
<div class="aipanel aiwide" data-scope="global" data-ask="{esc(q)}">
<div class="ai-welcome"><img src="/static/mark.svg" width="48" height="48" alt="">
<h1>What would you like to know?</h1></div>
<div class="aimsgs" role="log" aria-live="polite"></div>
<form class="aiform ai-composer"><label class="sr-only" for="global-question">Ask Bellwether</label>
<textarea id="global-question" aria-label="Ask Bellwether" placeholder="Ask about firms, people, or opportunities..." rows="2" maxlength="1500" required autofocus></textarea>
<div class="ai-compose-foot"><span><canvas data-orb="breathing" data-size="20" data-px="20" data-tint="#c9c9c9" aria-label="AI status"></canvas> Bellwether AI</span><button class="primary" type="submit">Ask</button></div></form>
<div class="aisugs">{sugs}</div><p class="ai-availability">{sub}</p>
</div></div>"""
    return page("Bellwether AI", "ask", body, orbs=True)
