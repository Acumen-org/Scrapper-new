"""Bellwether AI: questions in plain English, answered from Bellwether's data.

Two scopes:

  a firm      the question is about one firm (asked on its page, or naming it).
              The model reads that firm's dossier (prospect.dossier) and
              answers from it alone, saying so when the answer is not there.
  everything  the question is about the universe ("which PHH firms in Texas
              hired advisors this year?"). The model turns it into a filter
              spec, Bellwether runs the search itself (prospect.search, every
              value a bound parameter), and the model summarises the rows it
              is shown. The table of firms is always rendered from the query,
              never from the model, so a firm on screen is a firm that matched.

Without an AI provider the same box still works as a firm finder.
"""

from __future__ import annotations

import html
import json

from . import ai, dossier, products, search
from .names import nice_name

SYSTEM = (
    "You are Bellwether AI, the analyst inside Bellwether, Acumen Strategy's "
    "go-to-market intelligence platform on US registered investment advisers. The "
    "team sells three products: PHH (Prairie Hill: an evergreen private net-lease real "
    "estate fund for advisers' HNW clients, a 1031 exchange solution, and joint "
    "ventures with family offices), AcuBooth (a covered call overlay for concentrated "
    "equity positions, for advisers custodying at Schwab) and Glynac (compliance, "
    "portfolio and vendor intelligence for independent RIAs). Answer only from the data "
    "you are given. Lead with the answer in one or two sentences, then the specifics. "
    "Be concise and concrete. When you name a firm, link it as [Firm Name](/firm/CRD). "
    "Never invent a person, an email address, a phone number or a number that is not in "
    "the data; if the data does not hold the answer, say so plainly and say what would. "
    "Scores are out of 100 and count missing data as zero; mention coverage when a score "
    "rests on little data. When you write any part of the answer from general knowledge "
    "rather than from the data, say so. Every firm has a firm type (independent RIA, "
    "custodian, wirehouse, asset manager and so on) with a confidence and reasons; the type "
    "decides which product lists it can be on, so say what kind of firm it is when that "
    "matters, and never pitch a product to a type it is not sold to.")


def system_prompt() -> str:
    """SYSTEM plus a compact digest of the industry knowledge base (what Acumen
    sells to whom, the firm types, the glossary, how Bellwether works), as
    admins keep it in Settings, Industry knowledge. Falls back to SYSTEM alone
    if the knowledge base cannot be read."""
    try:
        from . import knowledge
        kb = knowledge.ai_context()
    except Exception:
        return SYSTEM
    if not kb:
        return SYSTEM
    return (f"{SYSTEM}\n\nINDUSTRY KNOWLEDGE (background kept by Acumen's admins; facts about "
            f"a particular firm come only from the data you are given)\n{kb}")

PLAN_SCHEMA_KEYS = ("mode", "firm_name", "filters", "sort", "limit", "title")


def _plan_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["search", "answer", "firm"]},
            "firm_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "filters": search.filter_schema(),
            "sort": {"type": "string", "enum": list(search.SORTS)},
            "limit": {"type": "integer"},
            "title": {"type": "string"},
        },
        "required": list(PLAN_SCHEMA_KEYS),
        "additionalProperties": False,
    }


def _vocab() -> str:
    products._load_vocab()
    prods = "; ".join(f"{k} = {products.product(k)['name']} ({products.product(k)['audience']})"
                      for k in products.product_keys())
    tags = ", ".join(f"{k} ({v})" for k, v in list(products.TAG_LABELS.items())[:80])
    trig = ", ".join(products.trigger_products().keys())
    return (f"Products: {prods}.\nBrochure tags: {tags}.\nSignal types: {trig}.\n"
            "Amounts are in US dollars (min_aum 500000000 means $500M). min_hnw_share is a "
            "fraction from 0 to 1. owner 'me' means the person asking.")


def _esc(v) -> str:
    return html.escape(str(v)) if v is not None else ""


def _money(v) -> str:
    if v is None:
        return "-"
    v = float(v)
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"


def _table(rows: list[dict], product: str | None) -> str:
    if not rows:
        return ""
    head = "<th>Firm</th><th class='num'>Assets</th><th class='num'>Score</th><th class='num'>Hired 12m</th>"
    body = []
    for r in rows:
        sc = (f"{r['score']:.0f}<span class='muted'> / {r['coverage']:.0f}%</span>"
              if r.get("score") is not None and r.get("coverage") is not None
              else (f"{r['score']:.0f}" if r.get("score") is not None else "-"))
        body.append(
            f"<tr class='go' data-href='/firm/{_esc(r['crd'])}'><td><a href='/firm/{_esc(r['crd'])}'>"
            f"{_esc(nice_name(r['legal_name']))}</a><div class='meta'>"
            f"{_esc(nice_name(r.get('city') or ''))} {_esc(r.get('state') or '')}</div></td>"
            f"<td class='num'>{_money(r.get('raum'))}</td><td class='num'>{sc}</td>"
            f"<td class='num'>{r.get('hires_12m') if r.get('hires_12m') is not None else '-'}</td></tr>")
    return (f"<table class='tight'><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table>")


def _rows_brief(rows: list[dict]) -> str:
    out = []
    for r in rows[:30]:
        out.append(json.dumps({
            "crd": r["crd"], "firm": nice_name(r["legal_name"]), "city": r.get("city"),
            "state": r.get("state"), "aum": r.get("raum"), "advisors": r.get("iar_count"),
            "score": r.get("score"), "coverage": r.get("coverage"),
            "best_list": r.get("best_product"), "headcount": r.get("headcount"),
            "hired_12m": r.get("hires_12m"), "left_12m": r.get("departures_12m"),
            "last_signal": r.get("last_signal"), "owner": r.get("owner"),
            "status": r.get("status"), "firm_type": r.get("firm_type")}, default=str))
    return "\n".join(out)


def _attach_types(c, rows: list[dict]) -> None:
    """Add each row's firm type label, so a written answer can tell an
    independent RIA from a custodian or an asset manager."""
    try:
        from . import firmtype
        types = firmtype.get_many(c, [r["crd"] for r in rows[:30]])
    except Exception:
        c.rollback()
        return
    for r in rows[:30]:
        t = types.get(r["crd"])
        if t:
            r["firm_type"] = f"{t['label']} ({t['confidence']}%)"


def _find_firm(c, name: str) -> dict | None:
    if not name:
        return None
    r = c.execute("""SELECT f.crd, f.legal_name FROM firm_current f
        LEFT JOIN firm_scope s ON s.crd=f.crd
        WHERE f.legal_name ILIKE ? OR f.business_name ILIKE ? OR f.crd = ?
        ORDER BY (s.crd IS NULL), f.raum DESC NULLS LAST LIMIT 1""",
                  (f"%{name}%", f"%{name}%", name.strip())).fetchone()
    return dict(r) if r else None


def ask_firm(c, crd: str, q: str, history: list[dict], who: str) -> dict:
    text = dossier.build(c, crd)
    if not text:
        return {"html": "<p>That firm is not in Bellwether.</p>", "text": ""}
    msgs = [{"role": h["role"], "content": str(h["content"])[:4000]}
            for h in history[-6:] if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content")]
    msgs.append({"role": "user", "content": f"DATA ON THIS FIRM\n{text}\n\nQUESTION\n{q}"})
    out = ai.complete(system_prompt(), msgs, feature="ask", tier="smart", max_tokens=2500,
                      who=who)
    return {"html": ai.md_to_html(out), "text": out}


def ask(c, q: str, scope: str = "global", history: list[dict] | None = None,
        who: str = "", me: str = "") -> dict:
    """Answer one question. Returns {'html': ..., 'text': ...}."""
    history = history or []
    q = (q or "").strip()[:1500]
    if not q:
        return {"html": "<p>Ask anything about the firms in Bellwether.</p>", "text": ""}
    if scope.startswith("firm:"):
        crd = scope[5:]
        if not ai.enabled("ask"):
            if not ai.configured():
                raise ai.AIError("Firm chat needs an AI provider. Ask an admin to connect one in Settings.")
            if ai.budget_left() <= 0:
                raise ai.AIError("Today's AI allowance is used up. It resets at midnight UTC.")
            raise ai.AIError("Firm chat is disabled. Ask an admin to enable AI questions in Settings.")
        return ask_firm(c, crd, q, history, who)
    if not ai.enabled("ask"):
        return _offline(c, q)

    msgs = [{"role": h["role"], "content": str(h["content"])[:2000]}
            for h in history[-6:] if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content")]
    msgs.append({"role": "user", "content": (
        f"{_vocab()}\n\nTurn this question into a Bellwether search. mode 'firm' when it is "
        f"about one named firm (put its name in firm_name); 'answer' when it needs a "
        f"written conclusion drawn from matching firms (comparisons, counts, why, which is "
        f"best); 'search' when a list of firms answers it. Set every filter the question "
        f"implies and null the rest. title is a short description of the result.\n\n"
        f"QUESTION: {q}")})
    plan = ai.complete(system_prompt(), msgs, feature="ask", tier="smart", schema=_plan_schema(),
                       max_tokens=1500, who=who)
    mode = plan.get("mode") or "search"
    if mode == "firm":
        f = _find_firm(c, plan.get("firm_name") or "")
        if f:
            res = ask_firm(c, f["crd"], q, history, who)
            res["html"] = (f"<p class='meta'>About <a href='/firm/{_esc(f['crd'])}'>"
                           f"{_esc(nice_name(f['legal_name']))}</a></p>" + res["html"])
            return res
        mode = "search"
    spec = plan.get("filters") or {}
    rows, total, said = search.run(c, spec, plan.get("sort") or "score",
                                   min(int(plan.get("limit") or 15), 50), me=me)
    product = spec.get("product")
    desc = ", ".join(said) if said else "all firms"
    head = (f"<p><b>{total:,}</b> firm{'s' if total != 1 else ''} {_esc(desc)}"
            f"{'. The top ' + str(len(rows)) + ' are below.' if total > len(rows) else '.'}</p>")
    if mode == "answer" and rows:
        msgs2 = [{"role": "user", "content": (
            f"QUESTION: {q}\n\nBellwether searched for firms {desc} and found {total} in "
            f"total. The top {len(rows)}, one JSON object per line:\n{_rows_brief(rows)}\n\n"
            f"Answer the question from these rows.")}]
        _attach_types(c, rows)
        out = ai.complete(system_prompt(), msgs2, feature="ask", tier="smart", max_tokens=2000,
                          who=who)
        return {"html": ai.md_to_html(out) + head + _table(rows, product),
                "text": out}
    if not rows:
        return {"html": f"<p>No firm matches: {_esc(desc)}. Try loosening a condition.</p>",
                "text": f"No firm matches {desc}."}
    return {"html": head + _table(rows, product),
            "text": f"{total} firms {desc}; top: " + ", ".join(
                nice_name(r["legal_name"]) for r in rows[:5])}


def _offline(c, q: str) -> dict:
    """No model available: behave as a firm finder over the words typed."""
    rows, total, _ = search.run(c, {"name": q}, "aum", 10)
    if ai.configured() and ai.budget_left() <= 0:
        why = "Today's AI allowance is used up (it resets at midnight UTC)"
    elif ai.configured():
        why = "Bellwether AI answers are switched off in Settings"
    else:
        why = ("Bellwether AI is not connected; an admin can connect an AI provider "
               "in Settings")
    note = f"<p class='muted'>{_esc(why)}, so this searched firm names for your words.</p>"
    if not rows:
        return {"html": note + "<p>No firm names match.</p>", "text": ""}
    return {"html": note + _table(rows, None), "text": ""}


def brief(c, crd: str, force: bool = False, who: str = "") -> dict | None:
    """The cached AI brief for a firm, refreshed when its data has changed."""
    text = dossier.build(c, crd)
    if not text:
        return None
    h = dossier.digest(text)
    row = c.execute("SELECT content, model, input_hash, created_at FROM ai_note"
                    " WHERE crd=? AND kind='brief'", (crd,)).fetchone()
    if row and not force and row["input_hash"] == h:
        return dict(row)
    if not ai.enabled("brief"):
        return dict(row) if row else None
    out = ai.complete(
        system_prompt(),
        [{"role": "user", "content": (
            f"DATA ON THIS FIRM\n{text}\n\nWrite a brief for a salesperson about to "
            "contact this firm, in at most 170 words: who they are and how big, which of "
            "our products fits best and why (cite the evidence), what changed recently "
            "(people joining or leaving, signals), who to approach first and the best "
            "way to reach them (only contacts in the data, saying whether verified), and "
            "the biggest unknowns. Use short bullet points with bold labels.")}],
        feature="brief", tier="smart", max_tokens=1200, who=who)
    now = dossier_now()
    c.execute("INSERT INTO ai_note (crd, kind, content, model, input_hash, created_at)"
              " VALUES (?,?,?,?,?,?) ON CONFLICT (crd, kind) DO UPDATE SET"
              " content=excluded.content, model=excluded.model,"
              " input_hash=excluded.input_hash, created_at=excluded.created_at",
              (crd, "brief", out, ai.model("smart"), h, now))
    c.commit()
    return {"content": out, "model": ai.model("smart"), "input_hash": h, "created_at": now}


def dossier_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
