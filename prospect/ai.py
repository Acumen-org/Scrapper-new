"""Bellwether AI: one door to whichever model the team connects.

Bellwether ran for months with no model anywhere, on purpose: every number had
to trace to a filing. That rule still holds for numbers. What a model adds is
reading and writing: turning a question into a search, a firm's whole record
into a short brief, a team page into a list of people, a messy title into a
role. Everything a model produces is labelled as AI in the interface, cites
what it read, and never overwrites a filed fact.

Providers, chosen in Settings, AI:
  anthropic   Claude through Anthropic's own SDK. Default model claude-opus-5-5.
  edenai      Eden AI's OpenAI-compatible gateway (https://api.edenai.run/v3),
              one key for many vendors; models are named vendor/model.
  openai      any OpenAI-compatible endpoint: OpenAI, Azure OpenAI, OpenRouter,
              Groq, or a local Ollama, at the base URL given.

Cost control is not optional: every call is counted in ai_call, and once the
day's count reaches ai.daily_limit every feature reports itself unavailable
until midnight UTC, rather than quietly running up a bill.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

from . import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_call (
    id         INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,
    feature    TEXT NOT NULL,
    provider   TEXT NOT NULL,
    model      TEXT,
    ok         INTEGER NOT NULL,
    in_tokens  INTEGER,
    out_tokens INTEGER,
    ms         INTEGER,
    who        TEXT,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS ix_aicall_at ON ai_call (at);
CREATE TABLE IF NOT EXISTS ai_note (
    crd        TEXT NOT NULL,
    kind       TEXT NOT NULL,            -- brief
    content    TEXT NOT NULL,
    model      TEXT,
    input_hash TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (crd, kind)
);
"""

FEATURES = {
    "ask": "Bellwether AI answers questions about firms and the whole universe",
    "brief": "A short written brief on each firm page",
    "extract": "Reads team pages and directories for people the rules missed",
    "clean": "Tidies names and titles and sorts people into roles",
}

DEFAULT_MODELS = {
    "anthropic": "claude-opus-5-5",
    "edenai": "anthropic/claude-opus-5-5",
    "openai": "",
}
EDEN_BASE = "https://api.edenai.run/v3"


class AIError(Exception):
    """A model call that failed, with a message fit for the screen."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def provider() -> str:
    return settings.get("ai.provider") or "none"


def configured() -> bool:
    p = provider()
    if p == "none":
        return False
    if p == "openai":
        # A local Ollama needs no key; anything else does.
        return bool(settings.get("ai.base_url")) and bool(model("smart"))
    return bool(settings.get("ai.api_key"))


def model(tier: str = "smart") -> str:
    p = provider()
    smart = settings.get("ai.model_smart") or DEFAULT_MODELS.get(p, "")
    if tier == "fast":
        return settings.get("ai.model_fast") or smart
    return smart


def calls_today() -> int:
    from . import db
    day = datetime.now(timezone.utc).date().isoformat()
    try:
        c = db.connect()
        try:
            r = c.execute("SELECT COUNT(*) n FROM ai_call WHERE at >= ?", (day,)).fetchone()
            return int(r["n"] or 0)
        finally:
            c.close()
    except Exception:
        return 0


def budget_left() -> int:
    return max(0, settings.get_int("ai.daily_limit", 400) - calls_today())


def enabled(feature: str) -> bool:
    """Whether a feature may call a model right now."""
    if not configured():
        return False
    if feature not in settings.get_list("ai.features"):
        return False
    return budget_left() > 0


def status() -> dict:
    """For Settings and the AI panels: what is connected and what is left."""
    return {"provider": provider(), "configured": configured(), "model": model("smart"),
            "model_fast": model("fast"), "features": settings.get_list("ai.features"),
            "limit": settings.get_int("ai.daily_limit", 400), "used_today": calls_today()}


def _log(feature: str, mdl: str, ok: bool, tin: int | None, tout: int | None,
         ms: int, who: str | None, error: str | None) -> None:
    from . import db
    try:
        c = db.connect()
        try:
            c.execute("INSERT INTO ai_call (at, feature, provider, model, ok, in_tokens,"
                      " out_tokens, ms, who, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (_now(), feature, provider(), mdl, 1 if ok else 0, tin, tout, ms,
                       who, (error or "")[:300] or None))
            c.commit()
        finally:
            c.close()
    except Exception:
        pass


# ------------------------------------------------------------------ calling

_CLIENTS: dict = {}


def _anthropic_client():
    import anthropic
    key = settings.get("ai.api_key")
    c = _CLIENTS.get(("anthropic", key))
    if c is None:
        _CLIENTS.clear()
        c = anthropic.Anthropic(api_key=key, timeout=120.0, max_retries=2)
        _CLIENTS[("anthropic", key)] = c
    return c


def _call_anthropic(system: str, messages: list[dict], mdl: str, max_tokens: int,
                    schema: dict | None, effort: str) -> tuple[str, int, int]:
    import anthropic
    # Not every Claude model takes every parameter: effort exists from the 4.6
    # generation on, and server-side fallback only on the newest models. An
    # admin can pick any model in Settings, so send only what it accepts.
    newest = mdl.startswith(("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5"))
    takes_effort = newest or mdl.startswith(("claude-opus-4-6", "claude-opus-4-7",
                                             "claude-opus-4-8", "claude-sonnet-4-6",
                                             "claude-sonnet-5"))
    oc: dict = {"effort": effort} if takes_effort else {}
    if schema is not None:
        oc["format"] = {"type": "json_schema", "schema": schema}
    extra: dict = {}
    if oc:
        extra["output_config"] = oc
    if newest:
        # Server-side fallback: if a safety classifier declines the request,
        # the API retries it on the model Anthropic recommends for that
        # category instead of returning an empty answer.
        extra.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    try:
        resp = _anthropic_client().beta.messages.create(
            model=mdl, max_tokens=max_tokens, system=system, messages=messages, **extra)
    except anthropic.AuthenticationError:
        raise AIError("The Anthropic API key was rejected. Check it in Settings, AI.") from None
    except anthropic.PermissionDeniedError:
        raise AIError("That Anthropic key cannot use this model.") from None
    except anthropic.NotFoundError:
        raise AIError(f"Anthropic does not recognise the model {mdl}.") from None
    except anthropic.RateLimitError:
        raise AIError("Anthropic is rate limiting this key. Try again in a minute.") from None
    except anthropic.BadRequestError as e:
        raise AIError(f"Anthropic refused the request: {e.message[:200]}") from None
    except anthropic.APIStatusError as e:
        raise AIError(f"Anthropic returned an error ({e.status_code}).") from None
    except anthropic.APIConnectionError:
        raise AIError("Could not reach Anthropic.") from None
    if resp.stop_reason == "refusal":
        raise AIError("The model declined to answer that.")
    text = "".join(b.text for b in resp.content if b.type == "text")
    u = resp.usage
    return text, int(u.input_tokens or 0), int(u.output_tokens or 0)


def _call_openai_compatible(system: str, messages: list[dict], mdl: str, max_tokens: int,
                            schema: dict | None, base: str) -> tuple[str, int, int]:
    import requests
    key = settings.get("ai.api_key")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    msgs = [{"role": "system", "content": system}] + messages
    body: dict = {"model": mdl, "messages": msgs, "max_tokens": max_tokens}
    if schema is not None:
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "result", "schema": schema, "strict": True}}
    try:
        r = requests.post(base.rstrip("/") + "/chat/completions", json=body,
                          headers=headers, timeout=120)
        if r.status_code == 400 and schema is not None:
            # Not every gateway honours json_schema; ask in words instead.
            body.pop("response_format", None)
            body["messages"][0]["content"] += ("\n\nReply with one JSON object only, no prose, "
                                               "matching this JSON schema: " + json.dumps(schema))
            r = requests.post(base.rstrip("/") + "/chat/completions", json=body,
                              headers=headers, timeout=120)
    except requests.RequestException:
        raise AIError("Could not reach the AI provider.") from None
    if r.status_code in (401, 403):
        raise AIError("The AI provider rejected the key. Check it in Settings, AI.")
    if r.status_code >= 400:
        raise AIError(f"The AI provider returned an error ({r.status_code}): {r.text[:160]}")
    try:
        d = r.json()
        text = d["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        raise AIError("The AI provider sent back something unreadable.") from None
    u = d.get("usage") or {}
    return text, int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def _parse_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    raise AIError("The model's answer was not valid JSON.")


def complete(system: str, messages: list[dict], *, feature: str, tier: str = "smart",
             schema: dict | None = None, max_tokens: int = 4000,
             who: str | None = None) -> str | dict:
    """One model call. Returns text, or a dict when a JSON schema is given."""
    if not configured():
        raise AIError("No AI provider is connected. An admin can add one in Settings, AI.")
    if budget_left() <= 0:
        raise AIError("Today's AI allowance is used up. It resets at midnight UTC.")
    p = provider()
    mdl = model(tier)
    t0 = time.monotonic()
    try:
        if p == "anthropic":
            text, tin, tout = _call_anthropic(system, messages, mdl, max_tokens, schema,
                                              "low" if tier == "fast" else "medium")
        elif p == "edenai":
            text, tin, tout = _call_openai_compatible(system, messages, mdl, max_tokens,
                                                      schema, EDEN_BASE)
        elif p == "openai":
            text, tin, tout = _call_openai_compatible(
                system, messages, mdl, max_tokens, schema,
                settings.get("ai.base_url") or "https://api.openai.com/v1")
        else:
            raise AIError("Unknown AI provider.")
        if not text.strip():
            raise AIError('The AI provider returned an empty answer. Try again with a shorter question; an admin can review the model in Settings, AI.')
        out = _parse_json(text) if schema is not None else text.strip()
    except AIError as e:
        _log(feature, mdl, False, None, None, int((time.monotonic() - t0) * 1000), who, str(e))
        raise
    _log(feature, mdl, True, tin, tout, int((time.monotonic() - t0) * 1000), who, None)
    return out


def test_connection() -> tuple[bool, str]:
    try:
        out = complete("You are a connectivity check. Reply with the single word OK.",
                       [{"role": "user", "content": "Say OK."}], feature="test",
                       max_tokens=200)
    except AIError as e:
        return False, str(e)
    return True, f"Connected to {provider()} using {model('smart')}. It replied: {str(out)[:40]}"


# ------------------------------------------------------------------ rendering

def md_to_html(text: str) -> str:
    """The small subset of Markdown a model writes, rendered safely: every
    character is escaped first, then bold, lists, links to firm pages and
    paragraphs are put back. No raw HTML from a model ever reaches a page."""
    import html as _h
    out, para, items = [], [], []

    def inline(s: str) -> str:
        s = _h.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<i>\1</i>", s)
        s = re.sub(r"\[([^\]]{1,120})\]\((/firm/\d{1,12}[^)\s]*)\)", r'<a href="\2">\1</a>', s)
        s = re.sub(r"`([^`]{1,80})`", r"<code>\1</code>", s)
        return s

    def flush():
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{inline(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            flush()
            continue
        m = re.match(r"^([-*•]|\d+[.)])\s+(.*)$", s)
        if m:
            if para:
                out.append("<p>" + inline(" ".join(para)) + "</p>")
                para.clear()
            items.append(m.group(2))
            continue
        h = re.match(r"^#{1,4}\s+(.*)$", s)
        if h:
            flush()
            out.append(f"<p><b>{inline(h.group(1))}</b></p>")
            continue
        if items:
            flush()
        para.append(s)
    flush()
    return "".join(out)


# ------------------------------------------------------------------ features

PEOPLE_SCHEMA = {
    "type": "object",
    "properties": {"people": {"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "title": {"type": "string"},
                       "email": {"type": "string"}, "phone": {"type": "string"}},
        "required": ["name", "title", "email", "phone"],
        "additionalProperties": False}}},
    "required": ["people"],
    "additionalProperties": False,
}


def extract_people(text: str, url: str, known_names: list[str]) -> list[dict]:
    """People on a page the rules could not parse. Only what the page states:
    empty strings for anything not printed there, never a guess."""
    if not enabled("extract"):
        return []
    page = (text or "")[:14000]
    known = ", ".join(known_names[:40])
    out = complete(
        "You extract staff listings from investment advisory firm web pages. Return "
        "only people who work at the firm and are named on the page, with their job "
        "title, email and direct phone exactly as printed. Use an empty string for any "
        "field the page does not show. Never invent or infer an email or phone.",
        [{"role": "user", "content": f"Page: {url}\nPeople we already know work there: "
          f"{known or 'none'}\n\nPage text:\n{page}"}],
        feature="extract", tier="fast", schema=PEOPLE_SCHEMA, max_tokens=3000)
    people = []
    for p in (out or {}).get("people", []):
        name = " ".join((p.get("name") or "").split())
        if 2 <= len(name.split()) <= 5:
            people.append({"name": name, "title": (p.get("title") or "").strip()[:80],
                           "email": (p.get("email") or "").strip().lower(),
                           "phone": (p.get("phone") or "").strip()})
    return people


ROLE_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "title": {"type": "string"},
                       "role": {"type": "string", "enum": [
                           "leadership", "advisor", "investment", "compliance",
                           "operations", "client_service", "other"]}},
        "required": ["id", "title", "role"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}


def classify_titles(items: list[tuple[str, str]]) -> dict[str, tuple[str, str]]:
    """id -> (clean title, role) for titles the rules could not place."""
    if not items or not enabled("clean"):
        return {}
    listing = "\n".join(f"{i}: {t}" for i, t in items[:80])
    out = complete(
        "You tidy job titles at investment advisory firms. For each line give a clean "
        "title in normal capitalisation (expand obvious abbreviations, drop "
        "registration codes such as CRD numbers) and the role it belongs to.",
        [{"role": "user", "content": listing}], feature="clean", tier="fast",
        schema=ROLE_SCHEMA, max_tokens=4000)
    return {x["id"]: (x["title"], x["role"]) for x in (out or {}).get("items", [])}
