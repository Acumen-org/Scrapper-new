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

A failed call explains itself. The screen gets a sentence; ai_call.error (and
the Settings connection test) gets what an engineer needs: the provider, the
model, the HTTP status and the provider's own message, or, for an empty
answer, why it was empty (the stop reason and what the reply held instead).
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
    "research": "Searches the web for published emails, phones and LinkedIn profiles of "
                "people every free source missed; each one is checked on its page first",
}

DEFAULT_MODELS = {
    "anthropic": "claude-opus-5-5",
    "edenai": "anthropic/claude-opus-5-5",
    "openai": "",
}
EDEN_BASE = "https://api.edenai.run/v3"


class AIError(Exception):
    """A model call that failed, with a message fit for the screen. `detail`
    is the technical account (status, provider message, model) that goes to
    ai_call.error and the connection test."""

    def __init__(self, message: str, detail: str | None = None):
        super().__init__(message)
        self.detail = detail or message
        self.bad_json: str | None = None   # the answer that failed to parse, for a retry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init(conn) -> None:
    """ai_call and ai_note, and the research log with them: the app calls this
    at startup, so the screens can read what research found before it runs."""
    conn.executescript(SCHEMA)
    conn.commit()
    try:
        from . import research
        research.init(conn)
    except Exception:
        conn.rollback()


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


def _anthropic_options(mdl: str, effort: str, schema: dict | None) -> dict:
    """The optional request parameters this model accepts."""
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
    return extra


def _call_anthropic(system: str, messages: list[dict], mdl: str, max_tokens: int,
                    schema: dict | None, effort: str,
                    timeout: float = 120.0) -> tuple[str, int, int]:
    extra = _anthropic_options(mdl, effort, schema)
    client = _anthropic_client()
    if timeout != 120.0:
        client = client.with_options(timeout=timeout)
    resp = _anthropic_create(client, mdl, model=mdl, max_tokens=max_tokens,
                             system=system, messages=messages, **extra)
    text = "".join(b.text for b in resp.content if b.type == "text")
    _check_anthropic_reply(resp, text, mdl, max_tokens)
    u = resp.usage
    return text, int(u.input_tokens or 0), int(u.output_tokens or 0)


def _anthropic_detail(e, mdl: str) -> str:
    """HTTP status, Anthropic's own error type and message, model, request id."""
    status = getattr(e, "status_code", None)
    body = getattr(e, "body", None)
    msg = ""
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        msg = f"{err.get('type', '')}: {err.get('message', '')}".strip(": ")
    msg = msg or str(getattr(e, "message", "") or e)
    req = getattr(e, "request_id", None)
    return (f"anthropic HTTP {status}: {msg[:200]} (model {mdl}"
            + (f", request {req}" if req else "") + ")")


def _anthropic_create(client, mdl: str, **kw):
    """One Messages API request, every failure turned into an AIError that
    says what happened."""
    import anthropic
    try:
        return client.beta.messages.create(**kw)
    except anthropic.APIError as e:
        raise _anthropic_error(e, mdl) from None


def _anthropic_error(e, mdl: str) -> AIError:
    """The AIError for an Anthropic SDK exception, plain words on the screen
    and the status, type and request id in the detail."""
    import anthropic
    if isinstance(e, anthropic.AuthenticationError):
        return AIError("The Anthropic API key was rejected. Check it in Settings, AI.",
                       _anthropic_detail(e, mdl))
    if isinstance(e, anthropic.PermissionDeniedError):
        return AIError("That Anthropic key cannot use this model.", _anthropic_detail(e, mdl))
    if isinstance(e, anthropic.NotFoundError):
        return AIError(f"Anthropic does not recognise the model {mdl}.", _anthropic_detail(e, mdl))
    if isinstance(e, anthropic.RateLimitError):
        return AIError("Anthropic is rate limiting this key. Try again in a minute.",
                       _anthropic_detail(e, mdl))
    if isinstance(e, anthropic.BadRequestError):
        return AIError(f"Anthropic refused the request: {e.message[:200]}",
                       _anthropic_detail(e, mdl))
    if isinstance(e, anthropic.APIStatusError):
        return AIError(f"Anthropic returned an error ({e.status_code}).", _anthropic_detail(e, mdl))
    if isinstance(e, anthropic.APITimeoutError):
        return AIError("Anthropic took too long to answer.", f"anthropic timeout (model {mdl})")
    cause = e.__cause__ or e
    return AIError("Could not reach Anthropic.",
                   f"anthropic connection error: {type(cause).__name__}: "
                   f"{str(cause)[:160]} (model {mdl})")


def _check_anthropic_reply(resp, text: str, mdl: str, max_tokens: int) -> None:
    """A refusal or an empty answer, with the reason it was empty."""
    if resp.stop_reason == "refusal":
        cat = getattr(getattr(resp, "stop_details", None), "category", None)
        raise AIError("The model declined to answer that.",
                      f"anthropic refusal (category {cat}, model {resp.model})")
    if text.strip():
        return
    kinds = ",".join(sorted({b.type for b in resp.content})) or "nothing"
    out = int(getattr(resp.usage, "output_tokens", 0) or 0)
    detail = (f"anthropic empty answer: stop_reason {resp.stop_reason}, reply held {kinds},"
              f" {out} output tokens, max_tokens {max_tokens}, model {resp.model or mdl}")
    if resp.stop_reason == "max_tokens":
        raise AIError("The model used its whole allowance thinking and wrote no answer. "
                      "It needs a larger max_tokens.", detail)
    raise AIError("The AI provider returned an empty answer. An admin can review the "
                  "model in Settings, AI.", detail)


def _strict_json_ok(base: str) -> bool:
    """Whether to ask for strict JSON schema output. Only OpenAI's own API
    keeps it dependable. Through a gateway such as Eden AI, on an open model,
    constrained decoding of a large schema can stall: the model starts the
    object, then writes blank space until max_tokens runs out (Gemma did this
    on every planning call). Everywhere else the schema goes in the prompt and
    the answer is parsed leniently, which is also faster."""
    return provider() == "openai" and "api.openai.com" in (base or "")


# Thinking models behind OpenAI-compatible gateways (Gemma 4 on Eden AI) count
# their reasoning against max_tokens: about 1,500 tokens before a two-line
# plan, so a 500 token limit ended mid-JSON or with no answer at all. Callers
# say how long the answer may be; this much more is allowed for the thinking.
# A model that does not think never uses it, and tokens are billed as used.
THINK_ROOM = 3000

# Yielded by stream() once, when a model starts thinking before it writes,
# so the screen can say so instead of sitting silent.
THINKING = object()


def _openai_request(system: str, messages: list[dict], mdl: str, max_tokens: int,
                    schema: dict | None, base: str, schema_hint: bool) -> tuple[dict, dict]:
    """Headers and body for an OpenAI-compatible chat completion."""
    key = settings.get("ai.api_key")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if schema is not None and not _strict_json_ok(base):
        system += "\n\nReply with one JSON object only: no prose, no code fences, nothing after it."
        if schema_hint:
            system += " It must match this JSON schema: " + json.dumps(schema)
    msgs = [{"role": "system", "content": system}] + messages
    body: dict = {"model": mdl, "messages": msgs, "max_tokens": max_tokens + THINK_ROOM}
    if schema is not None and _strict_json_ok(base):
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "result", "schema": schema, "strict": True}}
    return headers, body


def _http_error(r, mdl: str, base: str) -> AIError:
    where = f"(model {mdl}, base {base})"
    if r.status_code in (401, 403):
        return AIError("The AI provider rejected the key. Check it in Settings, AI.",
                       f"{provider()} HTTP {r.status_code}: {_provider_message(r)} {where}")
    if r.status_code == 402:
        return AIError("The AI provider account is out of credit. An admin can top it up.",
                       f"{provider()} HTTP 402: {_provider_message(r)} {where}")
    if r.status_code == 429:
        return AIError("The AI provider is rate limiting this account. Try again in a minute.",
                       f"{provider()} HTTP 429: {_provider_message(r)} {where}")
    return AIError(f"The AI provider returned an error ({r.status_code}).",
                   f"{provider()} HTTP {r.status_code}: {_provider_message(r)} {where}")


def _call_openai_compatible(system: str, messages: list[dict], mdl: str, max_tokens: int,
                            schema: dict | None, base: str, timeout: float = 120.0,
                            schema_hint: bool = True) -> tuple[str, int, int]:
    import requests
    headers, body = _openai_request(system, messages, mdl, max_tokens, schema, base, schema_hint)
    try:
        r = requests.post(base.rstrip("/") + "/chat/completions", json=body,
                          headers=headers, timeout=timeout)
        if r.status_code == 400 and "max_tokens" in (r.text or "").lower():
            # A model with a small output cap: ask for the answer length alone.
            body["max_tokens"] = max_tokens
            r = requests.post(base.rstrip("/") + "/chat/completions", json=body,
                              headers=headers, timeout=timeout)
        if r.status_code == 400 and "response_format" in body:
            # Not every endpoint honours json_schema; ask in words instead.
            headers, body = _openai_request(system, messages, mdl, max_tokens, schema, "", True)
            r = requests.post(base.rstrip("/") + "/chat/completions", json=body,
                              headers=headers, timeout=timeout)
    except requests.Timeout:
        raise AIError("The AI provider took too long to answer.",
                      f"{provider()} timeout after {timeout:.0f}s (model {mdl}, base {base})") from None
    except requests.RequestException as e:
        raise AIError("Could not reach the AI provider.",
                      f"{provider()} connection error: {type(e).__name__}: {str(e)[:160]}"
                      f" (model {mdl}, base {base})") from None
    where = f"(model {mdl}, base {base})"
    if r.status_code >= 400:
        raise _http_error(r, mdl, base)
    try:
        d = r.json()
        choice = d["choices"][0]
        msg = choice.get("message") or {}
        text = msg.get("content") or ""
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise AIError("The AI provider sent back something unreadable.",
                      f"{provider()} unreadable reply: {r.text[:200]} {where}") from None
    if isinstance(text, list):      # some gateways return content parts
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    if not str(text).strip():
        held = [k for k in ("reasoning_content", "reasoning", "tool_calls", "refusal")
                if msg.get(k)]
        u = d.get("usage") or {}
        detail = (f"{provider()} empty answer: finish_reason {choice.get('finish_reason')},"
                  f" reply held {','.join(held) or 'nothing'},"
                  f" {u.get('completion_tokens')} output tokens, max_tokens {max_tokens} {where}")
        if choice.get("finish_reason") == "length":
            raise AIError("The model used its whole allowance and wrote no answer. It needs "
                          "a larger max_tokens.", detail)
        raise AIError("The AI provider returned an empty answer. An admin can review the "
                      "model in Settings, AI.", detail)
    u = d.get("usage") or {}
    return text, int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def _provider_message(r) -> str:
    """The error message an OpenAI-compatible provider put in its reply."""
    try:
        d = r.json()
        err = d.get("error") if isinstance(d, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err)[:200]
        if err:
            return str(err)[:200]
        return str(d.get("detail") or d.get("message") or d)[:200]
    except (ValueError, AttributeError):
        return (r.text or "")[:200]


_THINK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def strip_thinking(text: str) -> str:
    """A reply without the <think> blocks some open models write first."""
    return _THINK.sub("", text or "")


def _json_spans(t: str) -> list[str]:
    """Every balanced {...} or [...] in the text, outermost first, skipping
    braces inside strings."""
    out: list[str] = []
    i = 0
    while i < len(t):
        if t[i] not in "{[":
            i += 1
            continue
        depth, j, in_str, esc = 0, i, False, False
        while j < len(t):
            ch = t[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    out.append(t[i:j + 1])
                    break
            j += 1
        i = j + 1 if depth == 0 and j < len(t) else i + 1
    return sorted(out, key=len, reverse=True)


def _loads_lenient(t: str):
    """json.loads, then the usual small slips a weaker model makes: smart
    quotes, trailing commas, Python's True/False/None and single quotes."""
    try:
        return json.loads(t)
    except ValueError:
        pass
    u = (t.replace(chr(0x201C), '"').replace(chr(0x201D), '"')
          .replace(chr(0x2018), "'").replace(chr(0x2019), "'"))
    u = _TRAILING_COMMA.sub(r"\1", u)
    try:
        return json.loads(u)
    except ValueError:
        pass
    import ast
    try:
        v = ast.literal_eval(u)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        raise ValueError("not JSON") from None
    if isinstance(v, (dict, list)):
        return v
    raise ValueError("not JSON")


def _parse_json(text: str, schema: dict | None = None):
    """The JSON object in a model's answer, however it was wrapped: thinking
    tags, code fences, prose around it, small syntax slips. With a schema, a
    bare list is wrapped in the schema's one array field and missing required
    fields get empty values, so a weak model's near miss still parses."""
    t = _THINK.sub("", text or "").strip()
    tries = [t] + _FENCE.findall(t) + _json_spans(t)
    for cand in tries:
        cand = cand.strip()
        if not cand:
            continue
        try:
            v = _loads_lenient(cand)
        except ValueError:
            continue
        v = _coerce(v, schema)
        if isinstance(v, dict):
            return v
    raise AIError("The model's answer was not valid JSON.",
                  f"{provider()} answer was not JSON: {t[:160]!r}")


def _coerce(v, schema: dict | None):
    if not schema or schema.get("type") != "object":
        return v
    props = schema.get("properties") or {}
    if isinstance(v, list):
        arrays = [k for k, p in props.items() if p.get("type") == "array"]
        if len(arrays) == 1:
            v = {arrays[0]: v}
        elif not v:
            v = {}          # "nothing found" said as an empty list
    if not isinstance(v, dict):
        return v
    lower = {str(k).lower(): k for k in v}
    for k, p in props.items():
        if k not in v and k.lower() in lower:
            v[k] = v.pop(lower[k.lower()])
        if k not in v:
            v[k] = {"array": [], "string": "", "boolean": False,
                    "object": {}}.get(p.get("type"), None)
    return v


def complete(system: str, messages: list[dict], *, feature: str, tier: str = "smart",
             schema: dict | None = None, max_tokens: int = 4000,
             who: str | None = None, timeout: float = 120.0,
             schema_hint: bool = True, effort: str | None = None) -> str | dict:
    """One model call. Returns text, or a dict when a JSON schema is given.
    schema_hint=False when the prompt already spells out the JSON wanted, so
    a provider without strict output is not also sent the whole schema.
    effort overrides how hard Claude thinks (low for quick structured steps)."""
    if not configured():
        raise AIError("No AI provider is connected. An admin can add one in Settings, AI.")
    if budget_left() <= 0:
        raise AIError("Today's AI allowance is used up. It resets at midnight UTC.")
    mdl = model(tier)
    opts = {"timeout": timeout, "schema_hint": schema_hint, "effort": effort}
    try:
        return _complete_once(system, messages, mdl, feature, tier, schema, max_tokens, who, **opts)
    except AIError as e:
        # Weaker models wrap JSON in prose or slip on its syntax now and then.
        # One more try, shown its own answer and told plainly what is wanted,
        # usually lands; a second miss is reported as it is.
        if schema is None or not getattr(e, "bad_json", None) or budget_left() <= 0:
            raise
        again = list(messages) + [
            {"role": "assistant", "content": e.bad_json[:6000]},
            {"role": "user", "content": "That was not one valid JSON object. Reply again "
             "with only the JSON object, starting with { and ending with }, no other text "
             "and no code fences, matching this JSON schema: " + json.dumps(schema)}]
        return _complete_once(system, again, mdl, feature, tier, schema, max_tokens, who, **opts)


def _base() -> str:
    """The chat completions base URL for the OpenAI-compatible providers."""
    if provider() == "edenai":
        return EDEN_BASE
    return settings.get("ai.base_url") or "https://api.openai.com/v1"


def _complete_once(system: str, messages: list[dict], mdl: str, feature: str, tier: str,
                   schema: dict | None, max_tokens: int, who: str | None,
                   timeout: float = 120.0, schema_hint: bool = True, effort: str | None = None):
    p = provider()
    t0 = time.monotonic()
    text = ""
    try:
        if p == "anthropic":
            text, tin, tout = _call_anthropic(system, messages, mdl, max_tokens, schema,
                                              effort or ("low" if tier == "fast" else "medium"),
                                              timeout)
        elif p in ("edenai", "openai"):
            text, tin, tout = _call_openai_compatible(system, messages, mdl, max_tokens, schema,
                                                      _base(), timeout, schema_hint)
        else:
            raise AIError("Unknown AI provider.", f"unknown provider {p!r}")
        if not str(text or "").strip():
            # The provider calls explain an empty answer themselves; this is
            # the backstop for any path that does not.
            raise AIError("The AI provider returned an empty answer. An admin can review "
                          "the model in Settings, AI.", f"{p} empty answer (model {mdl})")
        out = _parse_json(text, schema) if schema is not None else text.strip()
    except AIError as e:
        _log(feature, mdl, False, None, None, int((time.monotonic() - t0) * 1000), who,
             e.detail)
        if schema is not None and text.strip():
            e.bad_json = text
        raise
    _log(feature, mdl, True, tin, tout, int((time.monotonic() - t0) * 1000), who, None)
    return out


# ------------------------------------------------------------------ streaming

def stream(system: str, messages: list[dict], *, feature: str, tier: str = "smart",
           max_tokens: int = 2000, who: str | None = None, timeout: float = 90.0):
    """One model call that yields the answer as it is written, so the screen
    shows words within a second or two instead of after the whole reply. The
    call is logged once when it ends. A provider that cannot stream gets one
    ordinary call, yielded whole."""
    if not configured():
        raise AIError("No AI provider is connected. An admin can add one in Settings, AI.")
    if budget_left() <= 0:
        raise AIError("Today's AI allowance is used up. It resets at midnight UTC.")
    mdl = model(tier)
    p = provider()
    t0 = time.monotonic()
    usage = {"in": None, "out": None}
    try:
        if p == "anthropic":
            gen = _stream_anthropic(system, messages, mdl, max_tokens,
                                    "low" if tier == "fast" else "medium", timeout, usage)
        elif p in ("edenai", "openai"):
            gen = _stream_openai_compatible(system, messages, mdl, max_tokens, _base(), timeout, usage)
        else:
            raise AIError("Unknown AI provider.", f"unknown provider {p!r}")
        said = False
        for piece in gen:
            if piece is THINKING:
                yield piece
            elif piece:
                said = True
                yield piece
        if not said:
            raise AIError("The AI provider returned an empty answer. An admin can review "
                          "the model in Settings, AI.", f"{p} empty answer (model {mdl})")
    except AIError as e:
        _log(feature, mdl, False, None, None, int((time.monotonic() - t0) * 1000), who, e.detail)
        raise
    _log(feature, mdl, True, usage["in"], usage["out"], int((time.monotonic() - t0) * 1000),
         who, None)


def _stream_anthropic(system, messages, mdl, max_tokens, effort, timeout, usage):
    import anthropic
    extra = _anthropic_options(mdl, effort, None)
    client = _anthropic_client().with_options(timeout=timeout)
    text = []
    try:
        with client.beta.messages.stream(model=mdl, max_tokens=max_tokens, system=system,
                                         messages=messages, **extra) as s:
            for piece in s.text_stream:
                text.append(piece)
                yield piece
            resp = s.get_final_message()
    except anthropic.APIError as e:
        raise _anthropic_error(e, mdl) from None
    _check_anthropic_reply(resp, "".join(text), mdl, max_tokens)
    usage["in"] = int(resp.usage.input_tokens or 0)
    usage["out"] = int(resp.usage.output_tokens or 0)


def _stream_openai_compatible(system, messages, mdl, max_tokens, base, timeout, usage):
    """Server-sent events from /chat/completions with stream on. A gateway
    that refuses to stream (HTTP 400) gets an ordinary request instead."""
    import requests
    headers, body = _openai_request(system, messages, mdl, max_tokens, None, base, False)
    body["stream"] = True
    try:
        r = requests.post(base.rstrip("/") + "/chat/completions", json=body, headers=headers,
                          timeout=timeout, stream=True)
    except requests.Timeout:
        raise AIError("The AI provider took too long to answer.",
                      f"{provider()} timeout after {timeout:.0f}s (model {mdl}, base {base})") from None
    except requests.RequestException as e:
        raise AIError("Could not reach the AI provider.",
                      f"{provider()} connection error: {type(e).__name__}: {str(e)[:160]}"
                      f" (model {mdl}, base {base})") from None
    if r.status_code == 400:
        r.close()
        text, tin, tout = _call_openai_compatible(system, messages, mdl, max_tokens, None, base,
                                                  timeout)
        usage["in"], usage["out"] = tin, tout
        yield text
        return
    if r.status_code >= 400:
        raise _http_error(r, mdl, base)
    finish, said, thinking = None, False, False
    try:
        # chunk_size=None hands over each piece as it arrives; the default
        # waits for 512 bytes, which held a short answer back until it ended.
        for line in r.iter_lines(chunk_size=None, decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                d = json.loads(data)
            except ValueError:
                continue
            if d.get("error"):
                err = d["error"]
                msg = err.get("message") if isinstance(err, dict) else err
                raise AIError("The AI provider stopped with an error.",
                              f"{provider()} stream error: {str(msg)[:200]} (model {mdl}, base {base})")
            if d.get("usage"):
                usage["in"] = d["usage"].get("prompt_tokens")
                usage["out"] = d["usage"].get("completion_tokens")
            for ch in d.get("choices") or []:
                delta = ch.get("delta") or {}
                if not thinking and not said and (delta.get("reasoning_content")
                                                   or delta.get("reasoning")):
                    thinking = True
                    yield THINKING
                piece = delta.get("content")
                if isinstance(piece, list):
                    piece = "".join(x.get("text", "") for x in piece if isinstance(x, dict))
                if piece:
                    said = True
                    yield piece
                finish = ch.get("finish_reason") or finish
    except requests.RequestException as e:
        raise AIError("The connection to the AI provider dropped mid-answer.",
                      f"{provider()} stream broke: {type(e).__name__}: {str(e)[:160]}"
                      f" (model {mdl}, base {base})") from None
    finally:
        r.close()
    if not said:
        raise AIError("The AI provider returned an empty answer. An admin can review the "
                      "model in Settings, AI.",
                      f"{provider()} empty answer: finish_reason {finish}, streamed nothing,"
                      f" max_tokens {max_tokens} (model {mdl}, base {base})")


# ------------------------------------------------------------------ web research

WEB_SEARCH_TOOL = "web_search_20260209"
WEB_FETCH_TOOL = "web_fetch_20260209"
MAX_CONTINUATIONS = 3       # pause_turn resumptions per research question
RESEARCH_TIMEOUT_S = 300.0  # a searching, reading turn takes longer than a chat reply


def web_research(system: str, prompt: str, *, feature: str = "research",
                 max_searches: int = 4, max_fetches: int = 5, max_tokens: int = 16000,
                 who: str | None = None) -> dict:
    """Let Claude search and read the web to answer, through Anthropic's
    server tools (web_search and web_fetch, the versions with dynamic
    filtering). Returns {"text": the final answer, "sources": [{url, title,
    via}]}, where sources are the pages the tools really returned, which a
    caller can hold the answer's citations against.

    Anthropic runs the tool loop itself. When it stops a long turn with
    pause_turn, the conversation so far is sent back and it carries on, at
    most MAX_CONTINUATIONS times. Every request is one ai_call row, so the
    daily limit counts each of them. Other providers have no such tools; their
    callers gather the evidence themselves and use complete()."""
    if provider() != "anthropic":
        raise AIError("Web research needs the Anthropic provider.",
                      f"web research is not available on provider {provider()}")
    if not configured():
        raise AIError("No AI provider is connected. An admin can add one in Settings, AI.")
    mdl = model("smart")
    # The dynamic-filtering versions need a 4.6-generation model or newer; an
    # older model chosen in Settings gets the basic ones instead of a 400.
    dynamic = mdl.startswith(("claude-opus-5", "claude-sonnet-5", "claude-fable-5",
                              "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
                              "claude-sonnet-4-6"))
    tools = [{"type": WEB_SEARCH_TOOL if dynamic else "web_search_20250305",
              "name": "web_search", "max_uses": max_searches},
             {"type": WEB_FETCH_TOOL if dynamic else "web_fetch_20250910",
              "name": "web_fetch", "max_uses": max_fetches, "max_content_tokens": 20000}]
    extra = _anthropic_options(mdl, "medium", None)
    client = _anthropic_client().with_options(timeout=RESEARCH_TIMEOUT_S)
    messages: list = [{"role": "user", "content": prompt}]
    sources: list[dict] = []
    for _turn in range(MAX_CONTINUATIONS + 1):
        if budget_left() <= 0:
            raise AIError("Today's AI allowance is used up. It resets at midnight UTC.")
        t0 = time.monotonic()
        try:
            resp = _anthropic_create(client, mdl, model=mdl, max_tokens=max_tokens,
                                     system=system, messages=messages, tools=tools, **extra)
            sources += _web_sources(resp.content)
            if resp.stop_reason != "pause_turn":
                text = _final_text(resp.content)
                _check_anthropic_reply(resp, text, mdl, max_tokens)
        except AIError as e:
            _log(feature, mdl, False, None, None, int((time.monotonic() - t0) * 1000), who,
                 e.detail)
            raise
        u = resp.usage
        _log(feature, mdl, True, int(u.input_tokens or 0), int(u.output_tokens or 0),
             int((time.monotonic() - t0) * 1000), who, None)
        if resp.stop_reason == "pause_turn":
            # Send the paused turn back as it stands; the API sees the trailing
            # server tool call and resumes it. No extra "continue" message.
            messages = [{"role": "user", "content": prompt},
                        {"role": "assistant", "content": resp.content}]
            continue
        return {"text": text, "sources": sources}
    detail = f"anthropic research still paused after {MAX_CONTINUATIONS} continuations (model {mdl})"
    _log(feature, mdl, False, None, None, 0, who, detail)
    raise AIError("The research took too many steps and was stopped.", detail)


def _web_sources(content) -> list[dict]:
    """Pages the server tools actually returned in one response."""
    out: list[dict] = []
    for b in content or []:
        kind = getattr(b, "type", "")
        if kind == "web_search_tool_result":
            items = getattr(b, "content", None)
            if isinstance(items, list):      # an error result is an object, not a list
                for r in items:
                    if getattr(r, "type", "") == "web_search_result" and getattr(r, "url", None):
                        out.append({"url": r.url, "title": getattr(r, "title", "") or "",
                                    "via": "search"})
        elif kind == "web_fetch_tool_result":
            c = getattr(b, "content", None)
            if getattr(c, "type", "") == "web_fetch_result" and getattr(c, "url", None):
                doc = getattr(c, "content", None)
                out.append({"url": c.url, "title": getattr(doc, "title", "") or "",
                            "via": "fetch"})
    return out


def _final_text(content) -> str:
    """The answer: text written after the last tool call or result, since
    text before it is the model narrating what it is about to look up."""
    blocks = list(content or [])
    last = -1
    for i, b in enumerate(blocks):
        kind = getattr(b, "type", "")
        if kind == "server_tool_use" or kind.endswith("tool_result"):
            last = i
    tail = "".join(getattr(b, "text", "") for b in blocks[last + 1:]
                   if getattr(b, "type", "") == "text")
    if tail.strip():
        return tail
    return "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text")


def test_connection() -> tuple[bool, str]:
    """(ok, what happened). On failure the message carries the provider, model,
    HTTP status and the provider's own words, so a failed check says why.
    The allowance is generous because current Claude models always think
    before answering, and a small max_tokens can be spent before any text."""
    try:
        out = complete("You are a connectivity check. Reply with the single word OK.",
                       [{"role": "user", "content": "Say OK."}], feature="test",
                       tier="fast", max_tokens=2048)
    except AIError as e:
        return False, f"{e} [{e.detail}]" if e.detail != str(e) else str(e)
    return True, f"Connected to {provider()} using {model('fast')}. It replied: {str(out)[:40]}"


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
        m = re.match(r"^([-*\u2022]|\d+[.)])\s+(.*)$", s)
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
