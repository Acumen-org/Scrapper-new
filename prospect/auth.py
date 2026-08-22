"""Login, sessions, and who is using Bellwether.

Named accounts rather than one shared password, for one concrete reason: the app
already records who owns each firm and who decided each review. Signing in as
yourself means those fields fill themselves and stay honest, instead of three
people sharing a login and nobody knowing who did what.

Design, kept deliberately small:

  - Users live in config/users.yml, which is gitignored. Passwords are stored
    only as PBKDF2-SHA256 hashes, never in the clear, and the file holds no
    plaintext even briefly (scripts.manage_users hashes before writing).
  - Sessions are a signed cookie, not server state, so restarting the server
    does not sign everybody out and there is no session table to prune.
  - The secret that signs cookies comes from BELLWETHER_SECRET, or a file
    generated once on first run. If it changes, all sessions become invalid,
    which is the correct behaviour and not a bug.
  - SameSite=Strict, because the destructive routes are POSTs and that alone
    stops a link on another site from firing one at us.

No new dependencies: hashlib, hmac and secrets are all standard library.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

import yaml

from . import config

USERS_FILE = config.CONFIG_DIR / "users.yml"
SECRET_FILE = config.DATA_DIR / "secret_key"
COOKIE = "bellwether_session"
SESSION_DAYS = 30

# Paths reachable without signing in. Everything else requires a session.
PUBLIC_PATHS = {"/login", "/favicon.ico", "/healthz"}

PBKDF2_ROUNDS = 240_000


# --------------------------------------------------------------- passwords

def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt_hex, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, AttributeError):
        return False
    # Constant time: a timing difference here leaks whether a prefix matched.
    return hmac.compare_digest(dk.hex(), want)


# --------------------------------------------------------------- users file

def load_users() -> dict[str, dict]:
    if not USERS_FILE.exists():
        return {}
    data = yaml.safe_load(USERS_FILE.read_text(encoding="utf-8")) or {}
    return data.get("users") or {}


def save_users(users: dict[str, dict]) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(
        "# Bellwether accounts. Gitignored: this file never enters the repo.\n"
        "# Passwords are PBKDF2-SHA256 hashes. Manage with:\n"
        "#   python -m scripts.manage_users add <username> --name \"Full Name\"\n"
        + yaml.safe_dump({"users": users}, sort_keys=True),
        encoding="utf-8")


def check_login(username: str, password: str) -> dict | None:
    """The user record on success, None on failure.

    Runs the hash even when the username is unknown, so that a wrong username
    and a wrong password take the same time and neither can be enumerated."""
    users = load_users()
    rec = users.get((username or "").strip().lower())
    stored = (rec or {}).get("password_hash") or hash_password("x")
    ok = verify_password(password or "", stored)
    return rec if (ok and rec) else None


# --------------------------------------------------------------- sessions

def secret() -> bytes:
    env = os.environ.get("BELLWETHER_SECRET")
    if env:
        return env.encode("utf-8")
    if SECRET_FILE.exists():
        return SECRET_FILE.read_bytes()
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    SECRET_FILE.write_bytes(key)
    try:                            # best effort; no-op on Windows
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass
    return key


def _sign(payload: bytes) -> str:
    sig = hmac.new(secret(), payload, hashlib.sha256).digest()
    return (base64.urlsafe_b64encode(payload).decode().rstrip("=") + "."
            + base64.urlsafe_b64encode(sig).decode().rstrip("="))


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_session(username: str) -> str:
    payload = json.dumps({"u": username, "exp": int(time.time())
                          + SESSION_DAYS * 86400}).encode()
    return _sign(payload)


def read_session(token: str | None) -> str | None:
    """Username from a valid, unexpired, correctly signed token, else None."""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    try:
        payload = _unb64(body)
        want = hmac.new(secret(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(sig), want):
            return None
        data = json.loads(payload)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    return data.get("u")


def display_name(username: str) -> str:
    rec = load_users().get(username) or {}
    return rec.get("name") or username
