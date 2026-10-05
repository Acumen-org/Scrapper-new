"""Sign in with Microsoft: the main way into Bellwether.

The team already signs in to Microsoft 365 every morning, so Bellwether uses
that identity instead of its own passwords. This is the OpenID Connect
authorization code flow with PKCE, run by MSAL (Microsoft's own library), which
also checks the state and nonce on the way back.

What has to exist in Microsoft Entra, once, by an admin:
  an app registration (single tenant), a web redirect URI equal to
  <public address>/auth/microsoft/callback, and a client secret. Settings,
  Sign-in shows the exact redirect URI to paste and takes the three values.

Who gets in: an account in the configured tenant whose email domain is on the
allowed list. First sign-in creates the account as a user; anyone on the
admins list is an admin from the first visit.

The half-finished flow (state, nonce, PKCE verifier) rides in a short-lived,
encrypted cookie rather than in server memory, because two web workers serve
the app and the callback may land on the other one. It is SameSite=Lax, since
the callback is a top-level navigation coming back from Microsoft.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading

from . import settings

FLOW_COOKIE = "bellwether_ms_flow"
FLOW_TTL_S = 600
CALLBACK_PATH = "/auth/microsoft/callback"
GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

_APP: dict = {}
_LOCK = threading.Lock()


class SignInError(Exception):
    """A sign-in that must not succeed, with a reason fit to show the person."""


def configured() -> bool:
    return all(settings.get(k) for k in ("ms.tenant_id", "ms.client_id",
                                          "ms.client_secret"))


def _app():
    import msal
    tenant = settings.get("ms.tenant_id").strip()
    cid = settings.get("ms.client_id").strip()
    secret = settings.get("ms.client_secret").strip()
    key = hashlib.sha256(f"{tenant}|{cid}|{secret}".encode()).hexdigest()
    with _LOCK:
        app = _APP.get(key)
        if app is None:
            _APP.clear()
            app = msal.ConfidentialClientApplication(
                cid, authority=f"https://login.microsoftonline.com/{tenant}",
                client_credential=secret)
            _APP[key] = app
    return app


def redirect_uri(base: str) -> str:
    return base.rstrip("/") + CALLBACK_PATH


def start(base: str, nxt: str) -> tuple[str, str]:
    """(URL to send the browser to, value for the flow cookie).

    The code comes back in the query string. MSAL suggests form_post instead,
    but a cross-site POST would not carry the SameSite=Lax flow cookie, and the
    code is useless to anyone else: it is single use and bound by PKCE to a
    verifier that only exists inside our encrypted cookie."""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        flow = _app().initiate_auth_code_flow(
            ["User.Read"], redirect_uri=redirect_uri(base), prompt="select_account")
    if "auth_uri" not in flow:
        raise SignInError(flow.get("error_description") or "Microsoft sign-in is "
                          "not configured correctly.")
    payload = json.dumps({"flow": flow, "next": nxt}).encode("utf-8")
    return flow["auth_uri"], settings._fernet().encrypt(payload).decode("ascii")


def finish(cookie_value: str | None, params: dict) -> tuple[dict, str]:
    """Complete the flow. Returns (claims, next path) or raises SignInError."""
    if not cookie_value:
        raise SignInError("The sign-in took too long or started in another browser. "
                          "Please try again.")
    try:
        raw = settings._fernet().decrypt(cookie_value.encode("ascii"), ttl=FLOW_TTL_S)
        saved = json.loads(raw)
    except Exception:
        raise SignInError("The sign-in expired. Please try again.") from None
    if params.get("error"):
        desc = params.get("error_description") or params["error"]
        raise SignInError(f"Microsoft declined the sign-in: {desc.split(chr(13))[0][:200]}")
    try:
        result = _app().acquire_token_by_auth_code_flow(saved["flow"], params)
    except ValueError:
        # MSAL raises ValueError when state or nonce do not match.
        raise SignInError("The sign-in response did not match the request. "
                          "Please try again.") from None
    if "error" in result:
        raise SignInError(result.get("error_description", result["error"]).split("\r")[0][:300])
    claims = result.get("id_token_claims") or {}
    check(claims)
    return claims, saved.get("next") or "/"


def email_of(claims: dict) -> str:
    for k in ("email", "preferred_username", "upn"):
        v = (claims.get(k) or "").strip().lower()
        if "@" in v:
            return v
    return ""


def check(claims: dict) -> None:
    """Refuse anyone outside the tenant or the allowed email domains."""
    tenant = settings.get("ms.tenant_id").strip().lower()
    if GUID_RE.match(tenant) and (claims.get("tid") or "").lower() != tenant:
        raise SignInError("That Microsoft account belongs to a different organisation.")
    email = email_of(claims)
    if not email:
        raise SignInError("Microsoft did not share an email address for this account.")
    allowed = settings.get_list("auth.allowed_domains")
    if allowed and email.rsplit("@", 1)[-1] not in allowed:
        raise SignInError(f"{email} is not in an organisation that can use Bellwether.")
