"""Bellwether AI, full page: ask about any firm, person or market.

The same conversation panel a firm page carries, with the whole universe as
its scope. Questions become a search Bellwether runs itself, and the firms in
the answer are always the rows that matched, so a firm on screen is one that
truly fits what was asked.

The page is a single centred composer, the way people now expect a model to
look: a greeting and four starting points while it is empty, then the
conversation with the composer docked at the bottom.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from . import ai, users
from .webapp import ICONS, current_account, esc, page

router = APIRouter()

# Starting points that show what the platform can do in one question each.
CARDS = [
    ("Find buyers", "Which PHH Fund I firms in Texas or Florida hired advisors in the last year?"),
    ("Glynac fit", "Glynac firms on Microsoft 365 that use Black Diamond, Salesforce or Redtail, largest first"),
    ("Who to call", "AcuBooth firms with $500M+ and at least one verified email"),
    ("What changed", "Firms on our lists that lost two or more advisors this year"),
]


def _provider_label() -> str:
    names = {"anthropic": "Anthropic", "edenai": "Eden AI", "openai": "OpenAI-compatible"}
    return names.get(ai.provider(), ai.provider())


@router.get("/ask", response_class=HTMLResponse)
def ask_page(q: str = Query("")):
    st = ai.status()
    admin = users.is_admin(current_account())
    if st["configured"]:
        left = max(0, st["limit"] - st["used_today"])
        status = f"{left:,} questions left today"
        if admin:
            status += f' &middot; Model {esc(ai.model("smart"))} &middot; <a href="/settings/ai">Change</a>'
        model = (f'<canvas data-orb="breathing" data-size="20" data-px="18" data-tint="#bdbdbd" '
                 f'aria-hidden="true"></canvas>Bellwether AI &middot; {esc(_provider_label())}')
    else:
        status = ('AI is not connected, so questions run as a name search. '
                  + ('<a href="/settings/ai">Connect a provider</a>' if admin else "Ask an admin to connect one."))
        model = "Name search"
    cards = "".join(f'<button type="button" data-question="{esc(question)}"><b>{esc(title)}</b>'
                    f'<span>{esc(question)}</span></button>' for title, question in CARDS)
    body = f"""<div class="ai-stage aipanel" data-scope="global" data-ask="{esc(q)}">
<header class="ai-top"><span>Bellwether AI</span><a href="/ask" class="btn ghost sm">{ICONS["plus"]}New chat</a></header>
<div class="ai-center">
<div class="ai-welcome"><canvas data-orb="breathing" data-size="64" data-px="72" aria-hidden="true"></canvas>
<h1>What should we find today?</h1>
<p>Ask about any firm, person, list or market. Answers come from Bellwether's own data.</p></div>
<div class="aimsgs" role="log" aria-live="polite"></div>
<div class="dock">
<form class="aiform composer"><label class="sr-only" for="global-question">Ask Bellwether</label>
<textarea id="global-question" placeholder="Ask anything about firms, people or opportunities" rows="1"
 maxlength="1500" required autofocus></textarea>
<div class="cfoot"><span class="model">{model}</span>
<button class="send" type="submit" aria-label="Send">{ICONS["send"]}</button></div></form>
<div class="ai-cards aisugs">{cards}</div>
<p class="ai-status">{status}</p>
</div></div></div>"""
    return page("Bellwether AI", "ask", body, orbs=True)
