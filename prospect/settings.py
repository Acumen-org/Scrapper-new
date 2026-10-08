"""Settings an admin changes from inside the app, stored in the database.

Bellwether runs on a server nobody wants to SSH into to paste an API key, so
everything that is configuration rather than code lives here: the Microsoft
sign-in registration, the AI provider and its key, how email verification
talks to mail servers, how the crawler behaves. The Settings screen reads and
writes this module; nothing else writes it.

Rules:

  - An environment variable, when set, always wins over the stored value. That
    keeps a deployment able to pin a value (and keeps local development able to
    run without a database row), and the Settings screen says when a value is
    pinned so nobody edits a field that cannot take effect.
  - Secrets are encrypted at rest with a key derived from the app's signing
    secret, and never rendered back into a page: the screen shows only whether
    one is set. If the signing secret changes, stored secrets become
    unreadable, which reads as "not set" rather than as garbage.
  - Reads are cached per process for a few seconds. Writes bump a version row,
    and every process (both web workers and the background worker) notices the
    bump within the cache window, so a change applies everywhere without a
    restart.
"""

from __future__ import annotations

import base64
import hashlib
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_setting (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    secret     INTEGER NOT NULL DEFAULT 0,
    updated_by TEXT,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Spec:
    key: str
    label: str
    group: str
    default: str = ""
    secret: bool = False
    env: str = ""
    help: str = ""
    choices: tuple = ()


# Every setting the app knows. The Settings screen renders from this list, so a
# setting that is not declared here cannot be stored.
SPECS: list[Spec] = [
    # ---- sign-in
    Spec("ms.tenant_id", "Directory (tenant) ID", "signin", env="BELLWETHER_MS_TENANT",
         help="From the app registration's Overview page in Microsoft Entra."),
    Spec("ms.client_id", "Application (client) ID", "signin", env="BELLWETHER_MS_CLIENT_ID",
         help="From the same Overview page."),
    Spec("ms.client_secret", "Client secret value", "signin", secret=True,
         env="BELLWETHER_MS_CLIENT_SECRET",
         help="Certificates & secrets, New client secret. Paste the Value, not the Secret ID."),
    Spec("auth.allowed_domains", "Email domains allowed to sign in", "signin",
         default="acumen-strategy.com", env="BELLWETHER_ALLOWED_DOMAINS",
         help="Comma separated. A Microsoft account outside these domains is refused."),
    Spec("auth.admins", "Always admin", "signin",
         default="rahul.gopan@acumen-strategy.com", env="BELLWETHER_ADMINS",
         help="Comma separated emails or usernames that are admins whatever their stored role."),
    Spec("auth.password_login", "Password sign-in", "signin", default="on",
         choices=("on", "off"),
         help="Turn off once Microsoft sign-in works. It stays on while Microsoft is not configured, so nobody is locked out."),
    Spec("app.public_url", "Public address", "signin", env="BELLWETHER_PUBLIC_URL",
         help="For example https://bellwether.acumen-strategy.com. Used to build the sign-in redirect; leave blank to use the address the browser used."),
    # ---- AI
    Spec("ai.provider", "Provider", "ai", default="none",
         choices=("none", "anthropic", "edenai", "openai"),
         help="Eden AI reaches many model vendors through one key. OpenAI-compatible also covers Azure OpenAI, OpenRouter, Groq and a local Ollama."),
    Spec("ai.api_key", "API key", "ai", secret=True, env="BELLWETHER_AI_KEY"),
    Spec("ai.base_url", "Base URL", "ai",
         help="Only for OpenAI-compatible providers, for example https://api.openai.com/v1 or http://localhost:11434/v1."),
    Spec("ai.model_smart", "Model for answers and briefs", "ai",
         help="Leave blank for the provider default (Claude Opus 5.5). Answers are only as good as "
              "this model: on Eden AI use anthropic/claude-opus-5-5 or anthropic/claude-sonnet-5-5; "
              "small open models often return broken answers and fail contact research."),
    Spec("ai.model_fast", "Model for bulk extraction and cleaning", "ai",
         help="A cheaper, faster model. Leave blank to use the answers model."),
    Spec("ai.daily_limit", "Calls per day, at most", "ai", default="400",
         help="A hard ceiling across every AI feature, so a runaway job cannot run up a bill."),
    Spec("ai.features", "Enabled features", "ai", default="ask,brief,extract,clean,research",
         help="Comma separated: ask, brief, extract, clean, research."),
    # ---- email verification
    Spec("verify.engine", "Verification engine", "verify", default="auto",
         choices=("auto", "reacher", "native", "dns"),
         help="auto uses a Reacher server when one answers, else the built-in SMTP check."),
    Spec("verify.reacher_url", "Reacher server", "verify", env="BELLWETHER_REACHER_URL",
         help="For example http://reacher:8080. The Reacher backend from check-if-email-exists."),
    Spec("verify.reacher_secret", "Reacher secret", "verify", secret=True,
         env="BELLWETHER_REACHER_SECRET",
         help="Only if the Reacher server was started with a header secret."),
    Spec("verify.from_email", "MAIL FROM address", "verify",
         default="verify@acumen-strategy.com",
         help="The sender used in SMTP checks. A real domain you own keeps checks from being refused."),
    Spec("verify.hello_name", "HELO name", "verify", default="acumen-strategy.com"),
    Spec("verify.per_minute", "Mail server conversations per minute, at most", "verify",
         default="30",
         help="Across all mail servers. One conversation can ask about several addresses, "
              "and no server ever has two at once or two within two seconds."),
    # ---- crawling
    Spec("crawl.respect_robots", "Obey robots.txt", "crawl", default="on", choices=("on", "off")),
    Spec("crawl.use_browser", "Render JavaScript pages", "crawl", default="auto",
         choices=("auto", "never"),
         help="auto renders a page in a headless browser only when plain fetching returns an empty shell."),
    Spec("crawl.max_pages", "Pages per firm website, at most", "crawl", default="25"),
    Spec("crawl.recrawl_days", "Re-read a firm website after (days)", "crawl", default="90"),
]
BY_KEY = {s.key: s for s in SPECS}

_CACHE: dict = {"t": 0.0, "values": {}, "loaded": False}
_LOCK = threading.Lock()
TTL_S = 8.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ crypto

def _fernet():
    from cryptography.fernet import Fernet
    from . import auth
    key = hashlib.sha256(b"bellwether-settings-v1" + auth.secret()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def _decrypt(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except Exception:
        return None


# ------------------------------------------------------------------ store

def init(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _load(force: bool = False) -> dict[str, str]:
    with _LOCK:
        if not force and _CACHE["loaded"] and time.monotonic() - _CACHE["t"] < TTL_S:
            return _CACHE["values"]
    from . import db
    values: dict[str, str] = {}
    try:
        c = db.connect()
        try:
            for r in c.execute("SELECT key, value, secret FROM app_setting"):
                v = r["value"]
                if v is None:
                    continue
                if r["secret"]:
                    v = _decrypt(v)
                    if v is None:
                        continue
                values[r["key"]] = v
        finally:
            c.close()
    except Exception:
        # No database yet, or the table is missing on a fresh install: every
        # setting reads as its default, which is the right first-run answer.
        values = dict(_CACHE["values"]) if _CACHE["loaded"] else {}
    with _LOCK:
        _CACHE.update(t=time.monotonic(), values=values, loaded=True)
    return values


def pinned(key: str) -> bool:
    """True when an environment variable fixes this setting."""
    s = BY_KEY.get(key)
    return bool(s and s.env and os.environ.get(s.env, "").strip())


def get(key: str, default: str | None = None) -> str:
    s = BY_KEY.get(key)
    if s and s.env:
        env = os.environ.get(s.env, "").strip()
        if env:
            return env
    v = _load().get(key)
    if v not in (None, ""):
        return v
    if default is not None:
        return default
    return s.default if s else ""


def get_bool(key: str) -> bool:
    return get(key).strip().lower() in ("on", "1", "true", "yes")


def get_int(key: str, fallback: int = 0) -> int:
    try:
        return int(float(get(key)))
    except (TypeError, ValueError):
        return fallback


def get_list(key: str) -> list[str]:
    return [x.strip().lower() for x in get(key).split(",") if x.strip()]


def is_set(key: str) -> bool:
    """Whether a value exists, without revealing it. For secrets on screen."""
    return bool(get(key, ""))


def set(key: str, value: str | None, by: str = "") -> None:  # noqa: A001
    """Store one setting. None or an empty string clears it."""
    spec = BY_KEY.get(key)
    if spec is None:
        raise KeyError(f"unknown setting {key}")
    if spec.choices and value not in (None, "") and value not in spec.choices:
        raise ValueError(f"{key} must be one of {', '.join(spec.choices)}")
    from . import db
    c = db.connect()
    try:
        if value in (None, ""):
            c.execute("DELETE FROM app_setting WHERE key=?", (key,))
        else:
            stored = _encrypt(value) if spec.secret else value
            c.execute("INSERT INTO app_setting (key, value, secret, updated_by, updated_at)"
                      " VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET"
                      " value=excluded.value, secret=excluded.secret,"
                      " updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                      (key, stored, 1 if spec.secret else 0, by or None, _now()))
        c.commit()
    finally:
        c.close()
    _load(force=True)


def updated(key: str) -> tuple[str | None, str | None]:
    """(who, when) for a stored setting, for the audit line under a field."""
    from . import db
    try:
        c = db.connect()
        try:
            r = c.execute("SELECT updated_by, updated_at FROM app_setting WHERE key=?",
                          (key,)).fetchone()
        finally:
            c.close()
    except Exception:
        return None, None
    return (r["updated_by"], r["updated_at"]) if r else (None, None)


def invalidate() -> None:
    with _LOCK:
        _CACHE["t"] = 0.0
