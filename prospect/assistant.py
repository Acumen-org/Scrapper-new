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

Speed. A question costs at most two model calls, and the screen does not wait
for either to finish: the plan is a few dozen tokens (only the filters the
question sets, not every field), the firms that matched are shown the moment
the search returns, and the written answer streams in as it is produced
(ask_stream). Plans, answers and firm replies are remembered for a while, so
asking the same thing again, or clicking a starting card someone else already
clicked, comes back at once.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import time

from . import ai, dossier, products, quickplan, search
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

PLAN_SYSTEM = (
    "You turn questions about US registered investment advisers into a search in "
    "Bellwether, Acumen Strategy's sales intelligence platform. Reply with one JSON "
    "object and nothing else.")
MODES = ("search", "answer", "firm")


def _plan_schema() -> dict:
    """The planner's JSON. Only mode is required: a plan names just the filters
    the question sets, which keeps the answer to a few dozen tokens instead of
    every field spelled out as null."""
    filters = dict(search.filter_schema(), required=[])
    return {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": list(MODES)},
            "firm_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "filters": filters,
            "sort": {"type": "string", "enum": list(search.SORTS)},
            "limit": {"type": "integer"},
            "title": {"type": "string"},
        },
        "required": ["mode"],
        "additionalProperties": False,
    }


def _fields_doc() -> str:
    """Every filter the search takes, one short line, for the planning prompt."""
    out = []
    for k, v in search.filter_schema()["properties"].items():
        t = next((x for x in v.get("anyOf", [v]) if x.get("type") != "null"), v)
        if t.get("enum"):
            kind = "one of " + "|".join(str(e) for e in t["enum"])
        elif t.get("type") == "array":
            kind = f"list of {t.get('items', {}).get('type', 'string')}s"
        else:
            kind = t.get("type", "string")
        out.append(f"{k} ({kind})")
    return "; ".join(out)


class _Recent:
    """A small time-limited memory, shared by every request in this process."""

    def __init__(self, seconds: int, size: int = 400):
        self.seconds, self.size, self.d, self.lock = seconds, size, {}, threading.Lock()

    def get(self, key):
        with self.lock:
            hit = self.d.get(key)
            if hit and time.monotonic() - hit[0] < self.seconds:
                return hit[1]
            self.d.pop(key, None)
            return None

    def put(self, key, value) -> None:
        with self.lock:
            if len(self.d) >= self.size:
                for k in sorted(self.d, key=lambda k: self.d[k][0])[: self.size // 4]:
                    self.d.pop(k, None)
            self.d[key] = (time.monotonic(), value)


# A plan depends only on the words asked, so it keeps for hours; the search
# itself always runs fresh. Answers depend on the data they were given and are
# keyed on a digest of it.
_PLANS = _Recent(6 * 3600)
_ANSWERS = _Recent(30 * 60)


def _norm(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").strip().lower())


def _digest(*parts) -> str:
    return hashlib.sha1("\x1f".join(str(x) for x in parts).encode("utf-8")).hexdigest()


def _model_key() -> str:
    return f"{ai.provider()}:{ai.model('smart')}"


def _history(history: list, limit: int) -> list[dict]:
    return [{"role": h["role"], "content": str(h["content"])[:limit]}
            for h in (history or [])[-6:]
            if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content")]


def _plan(q: str, history: list, who: str) -> dict:
    """The search a question asks for: mode, filters, sort, limit, title."""
    key = (_norm(q), _model_key()) if not history else None
    if key and (hit := _PLANS.get(key)) is not None:
        return dict(hit)
    msgs = _history(history, 2000)
    msgs.append({"role": "user", "content": (
        f"{_vocab()}\n\nFILTERS: {_fields_doc()}.\nSORTS: {'|'.join(search.SORTS)}.\n\n"
        "Turn the QUESTION into a Bellwether search. Reply with one JSON object with these "
        "keys: mode, firm_name, filters, sort, limit, title. mode is 'firm' when the "
        "question is about one named firm (put its name in firm_name), 'answer' when it "
        "needs a written conclusion drawn from matching firms (comparisons, counts, why, "
        "which is best), and 'search' when a list of firms answers it. In filters put only "
        "the filters the question implies and leave every other one out. limit is 1 to 50. "
        "title is a few words describing the result. Example: "
        '{"mode":"search","firm_name":null,"filters":{"product":"glynac","states":["TX"]},'
        '"sort":"score","limit":15,"title":"Glynac firms in Texas"}\n\n'
        f"QUESTION: {q}")})
    long_think = ai.provider() == "anthropic"     # Claude may think before it answers
    raw = ai.complete(PLAN_SYSTEM, msgs, feature="ask", tier="smart", schema=_plan_schema(),
                      max_tokens=1500 if long_think else 500, who=who, timeout=45,
                      schema_hint=False, effort="low")
    plan = _clean_plan(raw)
    if key:
        _PLANS.put(key, plan)
    return dict(plan)


def _clean_plan(raw) -> dict:
    """A plan with only known filters and sane values. A weaker model that
    puts filters at the top level, or says null for everything, still works."""
    raw = raw if isinstance(raw, dict) else {}
    props = search.filter_schema()["properties"]
    filters = raw.get("filters") if isinstance(raw.get("filters"), dict) else {}
    filters = dict(filters, **{k: v for k, v in raw.items() if k in props and k not in filters})
    filters = {k: v for k, v in filters.items() if k in props and v not in (None, "", [], {})}
    try:
        limit = max(1, min(int(raw.get("limit") or 15), 50))
    except (TypeError, ValueError):
        limit = 15
    return {"mode": raw.get("mode") if raw.get("mode") in MODES else "search",
            "firm_name": raw.get("firm_name") if isinstance(raw.get("firm_name"), str) else "",
            "filters": filters,
            "sort": raw.get("sort") if raw.get("sort") in search.SORTS else "score",
            "limit": limit, "title": str(raw.get("title") or "")}


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


def _write(msgs: list[dict], *, live: bool, who: str, max_tokens: int):
    """The model's written answer: piece by piece when live, else in one call."""
    if live:
        yield from ai.stream(system_prompt(), msgs, feature="ask", max_tokens=max_tokens,
                             who=who, timeout=90)
    else:
        yield ai.complete(system_prompt(), msgs, feature="ask", tier="smart",
                          max_tokens=max_tokens, who=who)


def _answer_events(msgs: list[dict], live: bool, who: str, max_tokens: int, parts: list):
    """Events for the written answer, its text collected in parts. A model
    that thinks first is said to be thinking, so the wait is not silent."""
    for piece in _write(msgs, live=live, who=who, max_tokens=max_tokens):
        if piece is ai.THINKING:
            yield {"t": "status", "text": "Thinking it through"}
            continue
        parts.append(piece)
        yield {"t": "delta", "text": piece}


def _firm_events(c, crd: str, q: str, history: list, who: str, live: bool, lead: str = ""):
    text = dossier.build(c, crd)
    if not text:
        yield {"t": "done", "html": "<p>That firm is not in Bellwether.</p>", "text": ""}
        return
    key = (crd, _norm(q), _digest(text), _model_key()) if not history else None
    if key and (hit := _ANSWERS.get(key)) is not None:
        yield {"t": "done", "html": lead + ai.md_to_html(hit), "text": hit}
        return
    msgs = _history(history, 4000)
    msgs.append({"role": "user", "content": f"DATA ON THIS FIRM\n{text}\n\nQUESTION\n{q}"})
    yield {"t": "status", "text": "Writing the answer"}
    parts: list = []
    yield from _answer_events(msgs, live, who, 2500, parts)
    out = ai.strip_thinking("".join(parts)).strip()
    if key:
        _ANSWERS.put(key, out)
    yield {"t": "done", "html": lead + ai.md_to_html(out), "text": out}


def ask_firm(c, crd: str, q: str, history: list[dict], who: str) -> dict:
    return _final(_firm_events(c, crd, q, history, who, live=False))


def _final(events) -> dict:
    out = {"html": "", "text": ""}
    for ev in events:
        if ev["t"] == "done":
            out = {"html": ev["html"], "text": ev.get("text", "")}
    return out


def ask(c, q: str, scope: str = "global", history: list[dict] | None = None,
        who: str = "", me: str = "") -> dict:
    """Answer one question. Returns {'html': ..., 'text': ...}."""
    return _final(ask_stream(c, q, scope, history, who, me, live=False))


def ask_stream(c, q: str, scope: str = "global", history: list[dict] | None = None,
               who: str = "", me: str = "", live: bool = True):
    """Answer one question as a series of events for the chat panel:
      {"t": "status", "text"}    what Bellwether is doing now
      {"t": "table", "html"}     the firms that matched, before any writing
      {"t": "delta", "text"}     the next piece of the written answer
      {"t": "done", "html", "text"}  the finished answer
    An AIError is raised for a failure the person should read."""
    history = history or []
    q = (q or "").strip()[:1500]
    if not q:
        yield {"t": "done", "html": "<p>Ask anything about the firms in Bellwether.</p>", "text": ""}
        return
    if scope.startswith("firm:"):
        crd = scope[5:]
        if not ai.enabled("ask"):
            if not ai.configured():
                raise ai.AIError("Firm chat needs an AI provider. Ask an admin to connect one in Settings.")
            if ai.budget_left() <= 0:
                raise ai.AIError("Today's AI allowance is used up. It resets at midnight UTC.")
            raise ai.AIError("Firm chat is disabled. Ask an admin to enable AI questions in Settings.")
        yield {"t": "status", "text": "Reading the firm's record"}
        yield from _firm_events(c, crd, q, history, who, live)
        return
    if not ai.enabled("ask"):
        yield dict(_offline(c, q), t="done")
        return

    # A plain list question is read by rules in a millisecond; anything else,
    # and every follow-up, is planned by the model.
    quick = None if history else quickplan.plan(q)
    if quick:
        plan = _clean_plan(quick)
    else:
        yield {"t": "status", "text": "Reading your question"}
        plan = _plan(q, history, who)
    mode = plan["mode"]
    if mode == "firm":
        f = _find_firm(c, plan["firm_name"])
        if f:
            name = nice_name(f["legal_name"])
            yield {"t": "status", "text": f"Reading {name}'s record"}
            lead = (f"<p class='meta'>About <a href='/firm/{_esc(f['crd'])}'>"
                    f"{_esc(name)}</a></p>")
            yield from _firm_events(c, f["crd"], q, history, who, live, lead)
            return
        mode = "search"
    yield {"t": "status", "text": "Searching the firms"}
    spec = plan["filters"]
    rows, total, said = search.run(c, spec, plan["sort"], plan["limit"], me=me)
    product = spec.get("product")
    desc = ", ".join(said) if said else "all firms"
    head = (f"<p><b>{total:,}</b> firm{'s' if total != 1 else ''} {_esc(desc)}"
            f"{'. The top ' + str(len(rows)) + ' are below.' if total > len(rows) else '.'}</p>")
    if not rows:
        yield {"t": "done", "html": f"<p>No firm matches: {_esc(desc)}. Try loosening a condition.</p>",
               "text": f"No firm matches {desc}."}
        return
    table = head + _table(rows, product)
    if mode != "answer":
        yield {"t": "done", "html": table,
               "text": f"{total} firms {desc}; top: " + ", ".join(
                   nice_name(r["legal_name"]) for r in rows[:5])}
        return
    # The rows are on screen while the model reads them and writes.
    yield {"t": "table", "html": table}
    _attach_types(c, rows)
    brief_rows = _rows_brief(rows)
    key = (_norm(q), _digest(total, brief_rows), _model_key()) if not history else None
    if key and (hit := _ANSWERS.get(key)) is not None:
        yield {"t": "done", "html": ai.md_to_html(hit) + table, "text": hit}
        return
    yield {"t": "status", "text": "Writing the answer"}
    msgs = [{"role": "user", "content": (
        f"QUESTION: {q}\n\nBellwether searched for firms {desc} and found {total} in "
        f"total. The top {len(rows)}, one JSON object per line:\n{brief_rows}\n\n"
        f"Answer the question from these rows.")}]
    parts: list = []
    yield from _answer_events(msgs, live, who, 2000, parts)
    out = ai.strip_thinking("".join(parts)).strip()
    if key:
        _ANSWERS.put(key, out)
    yield {"t": "done", "html": ai.md_to_html(out) + table, "text": out}


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
