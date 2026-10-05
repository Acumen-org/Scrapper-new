"""Mailbox-level email checking: would this exact address receive mail?

prospect.mailcheck answers a narrower question, whether a domain can receive
mail at all, and says plainly that it cannot see individual mailboxes. This
goes one step further, the way check-if-email-exists (Reacher) does: it opens a
conversation with the domain's own mail server, says who it is, names a sender,
names the recipient, listens to the answer, and hangs up. No message is ever
sent. The server is only asked whether it would accept one.

Three engines sit behind one interface, check() and check_many():

  reacher  A self-hosted Reacher backend (the reacherhq/backend Docker image).
           The choice when this server's own port 25 is blocked, or when the
           checks should come from a machine with a clean mail reputation.
  native   The same idea written here from the SMTP protocol, not from
           Reacher's code: MX lookup, EHLO, STARTTLS when offered, MAIL FROM,
           RCPT TO the address, then RCPT TO a made-up address at the same
           domain to learn whether the server simply says yes to everything.
  dns      Syntax and mail server lookup only, what mailcheck already did. The
           fallback when neither of the others can run here.

`auto` takes the first that works: Reacher when a URL is set and it answers,
native when outbound port 25 is open, dns otherwise.

What the six answers mean:

  valid           the server accepted this mailbox AND turned away a made-up
                  one, so the acceptance means something
  invalid         the server said this mailbox does not exist or is disabled,
                  or the address is not well formed
  catch_all       the server accepts every address at the domain, real or not,
                  so its yes proves nothing. Common behind filtering gateways
                  (Proofpoint, Mimecast) and at Microsoft 365 tenants that never
                  turned on recipient checking
  risky           accepted, with a warning sign: a full inbox, a throwaway inbox
                  service, or a server that would not answer the made-up test
  unknown         no verdict: greylisted, rate limited, refused our check, or
                  unreachable. Says nothing bad about the address
  no_mail_server  the domain publishes no mail server at all

The @linkedin.com lesson is why an accepted address only counts as valid once a
made-up address at the same domain has been refused: a server that says yes to
everything is not confirming anything. That made-up probe is remembered per
domain for 30 days (table mail_domain) so a firm with forty people costs one
probe, not forty.

Being a good citizen with other people's mail servers is not optional here:
never more than one connection to a mail server at a time, a pause between
connections to the same server, a global per-minute cap from Settings, a
timeout on every socket, QUIT at the end of every conversation, and never DATA.
"""

from __future__ import annotations

import json
import re
import secrets
import smtplib
import socket
import ssl
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import requests

from . import contacts, mailcheck, settings

STATUSES = ("valid", "invalid", "risky", "catch_all", "unknown", "no_mail_server")
ENGINES = ("reacher", "native", "dns")

SMTP_PORT = 25
SMTP_TIMEOUT = 10.0         # every socket operation in a conversation
MAX_MX_TRIED = 3            # later MX hosts are backups; three is plenty
HOST_GAP_S = 2.0            # pause between two connections to one mail server
EGRESS_HOST = "gmail-smtp-in.l.google.com"
EGRESS_TIMEOUT = 6.0
EGRESS_TTL_S = 600          # port 25 does not open or close minute to minute
REACHER_TTL_S = 300
REACHER_TIMEOUT = 60.0      # Reacher may wait out a slow server itself
CATCH_ALL_DAYS = 30
MX_TTL_S = 3600
BATCH = 20                  # verify_contacts commits after this many
WORKERS = 4

# Test hooks only. _MX_OVERRIDE maps a domain to mail server entries ("host" or
# "host:port") used instead of DNS, so a fake SMTP server on localhost can stand
# in for a real one. _DB_CACHE off keeps test domains out of the database.
_MX_OVERRIDE: dict[str, list[str]] = {}
_DB_CACHE = True

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_domain (
    domain     TEXT PRIMARY KEY,
    mx         TEXT,       -- comma separated, most preferred first
    provider   TEXT,       -- google | m365 | proofpoint | mimecast | ... | other
    catch_all  INTEGER,    -- 1 accepts any address, 0 refuses made-up ones, NULL not known
    smtp_ok    INTEGER,    -- 1 a check conversation worked, 0 refused or unreachable
    checked_at TEXT        -- when catch_all was last determined; trusted for 30 days
);
"""

# Consumer mailbox providers. An address here is a person's own inbox, not a
# firm's, which matters for outreach even when it is perfectly deliverable.
FREE_PROVIDERS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "rocketmail.com",
    "outlook.com", "hotmail.com", "live.com", "msn.com", "aol.com", "aim.com",
    "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com", "pm.me",
    "gmx.com", "gmx.net", "mail.com", "zoho.com", "zohomail.com", "yandex.com",
    "fastmail.com", "hey.com", "tutanota.com", "comcast.net", "verizon.net",
    "att.net", "sbcglobal.net", "bellsouth.net", "cox.net", "charter.net",
    "earthlink.net", "optonline.net", "frontier.com", "windstream.net",
}
# Country variants (yahoo.co.uk, hotmail.fr) without listing every one.
FREE_PREFIXES = ("yahoo.", "hotmail.", "outlook.", "live.", "gmx.", "yandex.")

# Throwaway inbox services. Small on purpose: an adviser firm never uses one,
# so this only has to catch the obvious.
DISPOSABLE = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "sharklasers.com",
    "10minutemail.com", "temp-mail.org", "tempmail.com", "tempmail.net",
    "yopmail.com", "trashmail.com", "getnada.com", "dispostable.com",
    "maildrop.cc", "throwawaymail.com", "fakeinbox.com", "mailnesia.com",
    "emailondeck.com", "mohmal.com", "tempr.email", "discard.email",
    "spamgourmet.com", "moakt.com", "burnermail.io", "mintemail.com",
    "getairmail.com", "mytemp.email", "inboxkitten.com", "1secmail.com",
}

# Filtering gateways sit in front of the real mailbox server and very often
# accept every address, which is worth saying in a catch_all reason.
GATEWAY_NAMES = {
    "proofpoint": "Proofpoint", "mimecast": "Mimecast", "barracuda": "Barracuda",
    "cisco": "Cisco Secure Email", "symantec": "Symantec", "spamhero": "SpamHero",
    "appriver": "AppRiver", "sophos": "Sophos", "trendmicro": "Trend Micro",
}

# ---------------------------------------------------------------- reply text

_ENHANCED = re.compile(r"\b([245])\.(\d{1,3})\.(\d{1,3})\b")
_FULL = re.compile(r"mailbox (is )?full|inbox (is )?full|\bfull\b|quota"
                   r"|insufficient (system )?storage|mailbox size limit", re.I)
_DISABLED = re.compile(r"disabled|deactivated|inactive|suspended|not active"
                       r"|account (is |has been )?(closed|locked|terminated)", re.I)
_USER_UNKNOWN = re.compile(
    r"user unknown|unknown user|no such (user|mailbox|recipient|address|account)"
    r"|does ?n[o']t exist|not exist|unknown (recipient|mailbox|address|local)"
    r"|recipient (unknown|not found|invalid)|invalid (recipient|mailbox|address)"
    r"|recipient (address )?rejected|address rejected|mailbox (unavailable|not found)"
    r"|no mailbox|not a valid (mailbox|recipient|address)|unrouteable|user not local"
    r"|bad destination|not our customer|no longer (valid|available)|not found", re.I)
_STRONG_UNKNOWN = re.compile(r"user unknown|unknown user|no such user|does ?n[o']t exist"
                             r"|recipient not found|mailbox not found|no mailbox", re.I)
_POLICY = re.compile(
    r"block|spamhaus|spamcop|barracudacentral|black ?list|deny ?list|reputation"
    r"|\brbl\b|dnsbl|policy|reverse dns|\bptr\b|banned|access denied|relay"
    r"|not permitted|\bspf\b|rate limit|too many|helo|ehlo", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _setting(key: str) -> str:
    """A setting, or '' when Settings cannot be read. A check must still run
    with defaults on a machine where the settings store is unavailable."""
    try:
        return (settings.get(key) or "").strip()
    except Exception:
        spec = settings.BY_KEY.get(key)
        return spec.default if spec else ""


def _explicit(key: str) -> str:
    """Only a value someone actually set, never the built-in default. Used for
    what we pass to Reacher, which has its own defaults that should win over
    ours unless an admin chose otherwise."""
    try:
        return (settings.get(key, "") or "").strip()
    except Exception:
        return ""


def _per_minute() -> int:
    try:
        return settings.get_int("verify.per_minute", 20)
    except Exception:
        return 20


def _reacher_url() -> str:
    return _setting("verify.reacher_url").rstrip("/")


def is_free_provider(domain: str) -> bool:
    d = (domain or "").lower()
    return d in FREE_PROVIDERS or d.startswith(FREE_PREFIXES)


def is_disposable(domain: str) -> bool:
    return (domain or "").lower() in DISPOSABLE


def provider_for(hosts: list[str]) -> str:
    """Who runs a domain's mail, from MX host names alone. Free and instant;
    mailcheck.mail_platform does the fuller SPF-aware version for the scores."""
    h = " ".join(hosts).lower()
    if not h:
        return "unknown"
    for needle, name in (("google.com", "google"), ("googlemail.com", "google"),
                         ("mail.protection.outlook.com", "m365"), (".mx.microsoft", "m365"),
                         ("pphosted.com", "proofpoint"), ("ppe-hosted.com", "proofpoint"),
                         ("mimecast", "mimecast"), ("barracudanetworks.com", "barracuda"),
                         ("iphmx.com", "cisco"), ("messagelabs.com", "symantec"),
                         ("spamh.com", "spamhero"), ("arsmtp.com", "appriver"),
                         ("sophos", "sophos"), ("trendmicro", "trendmicro"),
                         ("secureserver.net", "godaddy"), ("emailsrvr.com", "rackspace"),
                         ("zoho", "zoho"), ("yahoodns", "yahoo"), ("icloud.com", "icloud"),
                         ("protonmail", "proton"), ("intermedia.net", "intermedia")):
        if needle in h:
            return name
    return "other"


# ---------------------------------------------------------------- pacing

class _Pacer:
    """The global per-minute cap. Each caller reserves the next free slot and
    sleeps until it, so threads never queue on a lock while they wait."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self, per_minute: int) -> None:
        if per_minute <= 0:
            return
        gap = 60.0 / per_minute
        with self._lock:
            now = time.monotonic()
            at = max(now, self._next)
            self._next = at + gap
        if at > now:
            time.sleep(at - now)


class _HostGate:
    """One connection per mail server at a time, with a pause between them.
    Many firms share one server (Google, Microsoft, Proofpoint), so this is
    keyed on the server, not the firm's domain."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hosts: dict[str, list] = {}

    @contextmanager
    def hold(self, host: str):
        with self._lock:
            entry = self._hosts.setdefault(host.lower(), [threading.Lock(), 0.0])
        entry[0].acquire()
        try:
            wait = entry[1] + HOST_GAP_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            yield
        finally:
            entry[1] = time.monotonic()
            entry[0].release()


_PACER = _Pacer()
_GATE = _HostGate()


# ---------------------------------------------------------------- engine choice

_PROBES: dict[str, tuple] = {}
_PROBE_LOCK = threading.Lock()


def smtp_egress_ok(refresh: bool = False) -> tuple[bool, str]:
    """Whether this machine can open outbound connections on port 25, which
    residential and many cloud networks block. Asks Google's mail server for
    its greeting; cached 10 minutes."""
    with _PROBE_LOCK:
        hit = _PROBES.get("egress")
    if not refresh and hit and time.monotonic() - hit[2] < EGRESS_TTL_S:
        return hit[0], hit[1]
    ok, detail = _probe_egress()
    with _PROBE_LOCK:
        _PROBES["egress"] = (ok, detail, time.monotonic())
    return ok, detail


def _probe_egress() -> tuple[bool, str]:
    try:
        infos = socket.getaddrinfo(EGRESS_HOST, 25, socket.AF_INET, socket.SOCK_STREAM)
    except OSError as e:
        return False, f"could not look up {EGRESS_HOST} ({e})"
    ip = infos[0][4][0]
    banner = ""
    try:
        # One address, one timeout: create_connection with a host name would
        # try every address in turn and could take several timeouts to fail.
        with socket.create_connection((ip, 25), timeout=EGRESS_TIMEOUT) as s:
            s.settimeout(EGRESS_TIMEOUT)
            buf = b""
            while b"\n" not in buf and len(buf) < 1024:
                chunk = s.recv(512)
                if not chunk:
                    break
                buf += chunk
            banner = buf.decode("ascii", "replace").strip()
            try:
                s.sendall(b"QUIT\r\n")
                s.recv(512)
            except OSError:
                pass
    except TimeoutError:
        return False, "outbound port 25 timed out, so the network blocks direct mail checks"
    except ConnectionRefusedError:
        return False, "outbound port 25 was refused"
    except OSError as e:
        return False, f"outbound port 25 failed ({e})"
    if not banner.startswith("220"):
        return False, f"port 25 connected but no mail server greeting came back ({banner[:60]!r})"
    if "google" not in banner.lower():
        # Some networks redirect port 25 to their own relay, which accepts
        # everything and would make every address look deliverable.
        return False, (f"port 25 answered, but not as Google ({banner[:60]!r}), so "
                       f"something on this network intercepts mail traffic")
    return True, f"outbound port 25 open ({EGRESS_HOST} answered)"


def _reacher_headers() -> dict:
    h = {"Accept": "application/json"}
    secret = _setting("verify.reacher_secret")
    if secret:
        h["x-reacher-secret"] = secret
    return h


def _reacher_health(refresh: bool = False) -> tuple[bool | None, str]:
    url = _reacher_url()
    if not url:
        return None, "no Reacher server is configured"
    with _PROBE_LOCK:
        hit = _PROBES.get("reacher")
    if (not refresh and hit and hit[3] == url
            and time.monotonic() - hit[2] < REACHER_TTL_S):
        return hit[0], hit[1]
    ok, detail = _probe_reacher(url)
    with _PROBE_LOCK:
        _PROBES["reacher"] = (ok, detail, time.monotonic(), url)
    return ok, detail


def _probe_reacher(url: str) -> tuple[bool, str]:
    try:
        r = requests.get(f"{url}/version", headers=_reacher_headers(), timeout=5)
    except requests.RequestException as e:
        return False, f"no answer from {url} ({type(e).__name__})"
    if r.status_code == 200:
        version = r.text.strip().strip('"')[:40]
        return True, f"Reacher {version} answering at {url}"
    if r.status_code in (401, 403):
        return False, f"{url} refused our Reacher secret"
    if r.status_code == 404:
        # Older backends have no version page but do answer HTTP; a check will
        # show soon enough whether it is really Reacher.
        return True, f"{url} answering (no version page, an older Reacher build)"
    return False, f"{url} answered HTTP {r.status_code}"


def _mark_reacher_down(detail: str) -> None:
    with _PROBE_LOCK:
        _PROBES["reacher"] = (False, detail, time.monotonic(), _reacher_url())


def _configured() -> str:
    v = (_setting("verify.engine") or "auto").lower()
    return v if v in ENGINES or v == "auto" else "auto"


def _auto_pick() -> str:
    if _reacher_url() and _reacher_health()[0]:
        return "reacher"
    if smtp_egress_ok()[0]:
        return "native"
    return "dns"


def _resolve(engine: str | None) -> tuple[str, bool]:
    """(engine to run, chosen automatically?). An engine named explicitly is
    honoured even when it cannot work, so an admin who picked Reacher to keep
    probes off this server's address never gets port 25 traffic instead."""
    e = (engine or _configured()).strip().lower()
    if e == "auto":
        return _auto_pick(), True
    if e not in ENGINES:
        raise ValueError(f"engine must be auto or one of {', '.join(ENGINES)}")
    return e, False


def engine_status(refresh: bool = False) -> dict:
    """What the Settings screen shows: the configured engine, the one that will
    actually run, and why."""
    url = _reacher_url()
    r_ok, r_detail = _reacher_health(refresh)
    p_ok, p_detail = smtp_egress_ok(refresh)
    configured = _configured()
    if configured == "auto":
        resolved = "reacher" if (url and r_ok) else ("native" if p_ok else "dns")
    else:
        resolved = configured
    return {"configured": configured, "resolved": resolved, "reacher_url": url or None,
            "reacher_ok": r_ok, "reacher_detail": r_detail,
            "port25_ok": p_ok, "port25_detail": p_detail}


# ---------------------------------------------------------------- domain memory

_DOMAINS: dict[str, dict] = {}
_DIRTY: set[str] = set()
_DOM_LOCK = threading.Lock()
_MX_CACHE: dict[str, tuple] = {}
_TABLE_READY = False


def init(conn) -> None:
    global _TABLE_READY
    conn.executescript(SCHEMA)
    conn.commit()
    _TABLE_READY = True


def _cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=CATCH_ALL_DAYS)
            ).isoformat(timespec="seconds")


def _cached_catch_all(domain: str) -> bool | None:
    with _DOM_LOCK:
        rec = _DOMAINS.get(domain)
    if not rec or rec.get("catch_all") is None or (rec.get("checked_at") or "") < _cutoff():
        return None
    return bool(rec["catch_all"])


def _remember(domain: str, hosts: list[str], provider: str,
              smtp_ok: bool | None, catch_all: bool | None) -> None:
    """Note what a conversation taught us about a domain. checked_at moves only
    when catch_all is freshly determined, so a value read from the cache can
    never extend its own life past 30 days."""
    with _DOM_LOCK:
        rec = _DOMAINS.get(domain) or {"catch_all": None, "smtp_ok": None,
                                       "checked_at": _now()}
        rec["mx"] = list(hosts)
        rec["provider"] = provider
        if smtp_ok is not None:
            rec["smtp_ok"] = smtp_ok
        if catch_all is not None:
            rec["catch_all"] = catch_all
            rec["checked_at"] = _now()
        _DOMAINS[domain] = rec
        _DIRTY.add(domain)


def _load_domains(conn, domains) -> None:
    with _DOM_LOCK:
        want = sorted({d for d in domains if d and d not in _DOMAINS})
    cutoff = _cutoff()
    for i in range(0, len(want), 500):
        chunk = want[i:i + 500]
        rows = conn.execute(
            f"SELECT domain, mx, provider, catch_all, smtp_ok, checked_at FROM mail_domain"
            f" WHERE domain IN ({','.join('?' * len(chunk))}) AND checked_at >= ?",
            (*chunk, cutoff)).fetchall()
        with _DOM_LOCK:
            for r in rows:
                _DOMAINS.setdefault(r["domain"], {
                    "mx": [h for h in (r["mx"] or "").split(",") if h],
                    "provider": r["provider"],
                    "catch_all": None if r["catch_all"] is None else bool(r["catch_all"]),
                    "smtp_ok": None if r["smtp_ok"] is None else bool(r["smtp_ok"]),
                    "checked_at": r["checked_at"]})


def _save_domains(conn) -> None:
    """Write what changed. The caller commits."""
    def flag(v):
        return None if v is None else (1 if v else 0)
    with _DOM_LOCK:
        dirty = sorted(_DIRTY)
        _DIRTY.clear()
        rows = [(d, ",".join(_DOMAINS[d].get("mx") or []), _DOMAINS[d].get("provider"),
                 flag(_DOMAINS[d].get("catch_all")), flag(_DOMAINS[d].get("smtp_ok")),
                 _DOMAINS[d].get("checked_at")) for d in dirty if d in _DOMAINS]
    if not rows:
        return
    try:
        conn.executemany(
            "INSERT INTO mail_domain (domain, mx, provider, catch_all, smtp_ok, checked_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET mx=excluded.mx,"
            " provider=excluded.provider, catch_all=excluded.catch_all,"
            " smtp_ok=excluded.smtp_ok, checked_at=excluded.checked_at", rows)
    except Exception:
        with _DOM_LOCK:          # keep them for the next save
            _DIRTY.update(dirty)
        raise


def _own_conn_cache(action: str, domains=()) -> None:
    """Load or save the domain memory on a short connection of its own, for
    check() and check_many(), which are called without one. Best effort: a
    check must still work when the database is unavailable."""
    global _TABLE_READY
    if not _DB_CACHE:
        return
    try:
        from . import db
        conn = db.connect()
    except Exception:
        return
    try:
        if not _TABLE_READY:
            init(conn)
        if action == "load":
            _load_domains(conn, domains)
        else:
            _save_domains(conn)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


# ---------------------------------------------------------------- lookups

def _mail_hosts(domain: str) -> tuple[list[str], str]:
    """(hosts, state), state one of ok | none | null | dns. Hosts are MX names,
    most preferred first, or the domain itself when it has no MX but does have
    an address, which RFC 5321 section 5.1 says senders must then try."""
    if domain in _MX_OVERRIDE:
        return list(_MX_OVERRIDE[domain]), "ok"
    hit = _MX_CACHE.get(domain)
    if hit and time.monotonic() - hit[2] < MX_TTL_S:
        return list(hit[0]), hit[1]
    mx = mailcheck.mx_records(domain)
    if mx is None:
        return [], "dns"          # never cached: a resolver outage is not an answer
    if mx:
        hosts = [h for _, h in mx if h]
        out = (hosts, "ok") if hosts else ([], "null")
    else:
        a = mailcheck.a_records(domain)
        if a is None:
            return [], "dns"
        if not a:
            a = mailcheck.a_records(domain, ipv6=True)
            if a is None:
                return [], "dns"
        out = ([domain], "ok") if a else ([], "none")
    _MX_CACHE[domain] = (out[0], out[1], time.monotonic())
    return list(out[0]), out[1]


def _split_host(entry: str) -> tuple[str, int]:
    host, sep, port = entry.rpartition(":")
    if sep and port.isdigit() and host:
        return host, int(port)
    return entry, SMTP_PORT


def _address_for(host: str, port: int) -> str:
    """IPv4 first. Most mail servers publish both, and this machine's IPv6 is
    the likelier one to be broken, which would cost a full timeout per check."""
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    infos.sort(key=lambda i: 0 if i[0] == socket.AF_INET else 1)
    return infos[0][4][0]


def _tls_context() -> ssl.SSLContext:
    # Certificates are not checked: nothing secret crosses this connection, and
    # mail server certificates rarely match the MX name. TLS is only here
    # because some servers will not hear MAIL FROM without it.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _text(msg) -> str:
    if isinstance(msg, bytes):
        msg = msg.decode("utf-8", "replace")
    return " ".join(str(msg or "").split())[:300]


# ---------------------------------------------------------------- native engine

def _converse(host: str, port: int, hello: str, sender: str, target: str,
              domain: str, need_probe: bool, tls: bool = True) -> dict:
    """One SMTP conversation. Never sends DATA; always ends with QUIT."""
    out = {"connected": False, "stage": "connect", "code": None, "message": None,
           "target": None, "probe": None, "error": None, "tls": False,
           "tls_failed": False}
    try:
        ip = _address_for(host, port)
    except OSError as e:
        out["error"] = f"could not resolve {host} ({e})"
        return out
    s = smtplib.SMTP(local_hostname=hello or "localhost", timeout=SMTP_TIMEOUT)
    try:
        try:
            code, msg = s.connect(ip, port)
        except (OSError, smtplib.SMTPException) as e:
            out["error"] = _text(str(e)) or type(e).__name__
            return out
        out["connected"] = True
        if code != 220:
            out.update(stage="banner", code=code, message=_text(msg))
            return out
        code, msg = s.ehlo()
        if not 200 <= code < 300:
            code, msg = s.helo()
            if not 200 <= code < 300:
                out.update(stage="helo", code=code, message=_text(msg))
                return out
        if tls and s.has_extn("starttls"):
            s._host = host       # so the TLS hello names the server, not its IP
            try:
                code, msg = s.starttls(context=_tls_context())
            except (ssl.SSLError, OSError, smtplib.SMTPException) as e:
                out.update(tls_failed=True, error=f"STARTTLS failed ({_text(str(e))})")
                return out
            if code == 220:
                out["tls"] = True
                s.ehlo()         # after STARTTLS the earlier greeting no longer counts
        code, msg = s.mail(sender)
        if code not in (250, 251):
            out.update(stage="mail", code=code, message=_text(msg))
            return out
        out["stage"] = "rcpt"
        code, msg = s.rcpt(target)
        out["target"] = (code, _text(msg))
        out.update(code=code, message=_text(msg))
        if code in (250, 251) and need_probe:
            probe = f"bw{secrets.token_hex(8)}@{domain}"
            try:
                pcode, pmsg = s.rcpt(probe)
                out["probe"] = (pcode, _text(pmsg))
            except (OSError, smtplib.SMTPException):
                out["probe"] = None   # hung up on the probe: catch-all not known
        return out
    except (OSError, smtplib.SMTPException) as e:
        out["error"] = _text(str(e)) or type(e).__name__
        return out
    finally:
        try:
            s.quit()
        except Exception:
            s.close()


def _classify(code: int, text: str) -> str:
    """What one SMTP reply says about the mailbox: ok | invalid | disabled |
    full | temp | policy | other."""
    if code in (250, 251):
        return "ok"
    m = _ENHANCED.search(text or "")
    enh = m.group(0) if m else ""
    full = bool(_FULL.search(text or "")) or enh in ("4.2.2", "5.2.2")
    if 400 <= code < 500:
        # Greylisting, rate limits and "try again" in general. Never a verdict,
        # except a full mailbox, which is a fact about the mailbox.
        return "full" if full else "temp"
    if 500 <= code < 600:
        t = text or ""
        if full or code == 552:
            return "full"
        if enh == "5.2.1" or _DISABLED.search(t):
            return "disabled"
        if enh.startswith("5.1."):
            return "invalid"
        # Microsoft 365 with directory based edge blocking refuses an unknown
        # recipient as "5.4.1 Recipient address rejected: Access denied", which
        # reads like a policy refusal but is its "no such mailbox".
        if enh == "5.4.1" and "recipient address rejected" in t.lower():
            return "invalid"
        strong = bool(_STRONG_UNKNOWN.search(t))
        # Policy before the looser "user unknown" phrasing: "550 reverse DNS not
        # found" is about us, and must not mark someone's mailbox as bouncing.
        if enh.startswith("5.7.") and not strong:
            return "policy"
        if strong:
            return "invalid"
        if _POLICY.search(t):
            return "policy"
        if _USER_UNKNOWN.search(t):
            return "invalid"
    return "other"


def _probe_says(probe) -> bool | None:
    """Catch-all from the made-up address: accepted means the server takes
    anything, a permanent refusal means it checks, anything else is unknown."""
    if not probe:
        return None
    code = probe[0]
    if code in (250, 251):
        return True
    if 500 <= code < 600:
        return False
    return None


def _done(res: dict, status: str, reason: str) -> dict:
    res["status"] = status
    res["reason"] = reason
    return res


def _accepted_reason(domain: str, role: bool) -> str:
    if role:
        return (f"The mail server for {domain} confirmed this mailbox exists, though it is "
                f"a shared inbox rather than a person.")
    return f"The mail server for {domain} confirmed this mailbox exists."


def _catch_all_reason(domain: str, provider: str) -> str:
    via = GATEWAY_NAMES.get(provider)
    if via:
        return (f"{domain} accepts mail for any address (its mail runs through {via}), "
                f"so this mailbox cannot be confirmed.")
    return f"{domain} accepts mail for any address, so this mailbox cannot be confirmed."


def _native(res: dict, email: str, domain: str, hosts: list[str]) -> dict:
    provider = provider_for(hosts)
    hello = _setting("verify.hello_name") or "localhost"
    sender = _setting("verify.from_email")
    out: dict = {}
    known = None
    for entry in hosts[:MAX_MX_TRIED]:
        host, port = _split_host(entry)
        with _GATE.hold(host):
            _PACER.wait(_per_minute())
            # Read after taking the gate: another thread may have just probed
            # this domain while we waited for the same server.
            known = _cached_catch_all(domain)
            out = _converse(host, port, hello, sender, email, domain, known is None)
            if out["tls_failed"]:
                # The handshake broke the connection. Some old servers offer
                # STARTTLS and cannot finish it; they still talk in plain text.
                time.sleep(HOST_GAP_S)
                out = _converse(host, port, hello, sender, email, domain,
                                known is None, tls=False)
        if out["connected"]:
            break
    smtp = res["smtp"]
    if not out.get("connected"):
        smtp["can_connect"] = False
        smtp["message"] = out.get("error")
        _remember(domain, hosts, provider, False, None)
        return _done(res, "unknown", f"Could not connect to the mail server for {domain}, "
                                     f"so this address is unchecked.")
    smtp["can_connect"] = True
    smtp["code"] = out.get("code")
    smtp["message"] = out.get("message") or out.get("error")
    if out["stage"] in ("banner", "helo", "mail"):
        _remember(domain, hosts, provider, False, None)
        what = {"banner": "turned our connection away before any check ran",
                "helo": "refused our greeting",
                "mail": "refused our test sender"}[out["stage"]]
        return _done(res, "unknown", f"The mail server for {domain} {what}, which says "
                                     f"nothing about this mailbox.")
    if out["target"] is None:
        return _done(res, "unknown", f"The mail server for {domain} hung up during the "
                                     f"check, so this address is unchecked.")
    code, text = out["target"]
    kind = _classify(code, text)
    role = res["misc"]["role"]
    if kind == "ok":
        smtp["deliverable"] = True
        smtp["disabled"] = False
        smtp["full_inbox"] = False
        catch = known if known is not None else _probe_says(out["probe"])
        smtp["catch_all"] = catch
        _remember(domain, hosts, provider, True, catch if known is None else None)
        if catch is True:
            return _done(res, "catch_all", _catch_all_reason(domain, provider))
        if catch is False:
            return _done(res, "valid", _accepted_reason(domain, role))
        return _done(res, "risky", f"The mail server for {domain} accepted this address but "
                                   f"would not answer a follow-up test, so it may accept "
                                   f"every address.")
    if kind in ("invalid", "disabled"):
        smtp["deliverable"] = False
        smtp["disabled"] = kind == "disabled"
        # A refused address proves the domain is not accept-all.
        _remember(domain, hosts, provider, True, False if known is None else None)
        if kind == "disabled":
            return _done(res, "invalid", f"The mail server for {domain} says this mailbox "
                                         f"has been disabled.")
        return _done(res, "invalid", f"The mail server for {domain} says this mailbox does "
                                     f"not exist.")
    if kind == "full":
        smtp["deliverable"] = False
        smtp["full_inbox"] = True
        _remember(domain, hosts, provider, True, None)
        return _done(res, "risky", "The mailbox exists but is full, so mail to it bounces "
                                   "for now.")
    if kind == "temp":
        _remember(domain, hosts, provider, None, None)
        return _done(res, "unknown", f"The mail server for {domain} asked us to try again "
                                     f"later (greylisting or rate limiting), so this address "
                                     f"is unchecked.")
    if kind == "policy":
        _remember(domain, hosts, provider, False, None)
        return _done(res, "unknown", f"The mail server for {domain} refused to answer our "
                                     f"check, which says nothing about this mailbox.")
    return _done(res, "unknown", f"The mail server for {domain} gave a reply we could not "
                                 f"interpret, so this address is unchecked.")


# ---------------------------------------------------------------- reacher engine

class _ReacherDown(Exception):
    """The Reacher server itself is unusable, as opposed to one check failing."""


def _core_error(part) -> str | None:
    """Reacher reports a failed stage as {"type": ..., "message": ...} in place
    of the stage's details."""
    if isinstance(part, dict) and "message" in part and "type" in part and len(part) <= 3:
        return _text(f"{part.get('type')}: {part.get('message')}")
    return None


def _reacher(res: dict, email: str, domain: str, hosts: list[str]) -> dict:
    url = _reacher_url()
    if not url:
        raise _ReacherDown("no Reacher server is configured")
    body = {"to_email": email}
    # Reacher's own configuration should usually decide these; ours is sent only
    # when an admin has set one deliberately in Settings.
    for key, field in (("verify.from_email", "from_email"), ("verify.hello_name", "hello_name")):
        v = _explicit(key)
        if v:
            body[field] = v
    # Reacher opens the SMTP connection, but it is still a connection to this
    # domain's server, so the same per-server rule applies.
    with _GATE.hold(_split_host(hosts[0])[0]):
        _PACER.wait(_per_minute())
        try:
            r = requests.post(f"{url}/v0/check_email", json=body,
                              headers=_reacher_headers(), timeout=(5, REACHER_TIMEOUT))
        except requests.ConnectionError as e:
            raise _ReacherDown(f"no answer from {url} ({type(e).__name__})") from e
        except requests.Timeout:
            res["smtp"]["message"] = f"Reacher gave no answer within {REACHER_TIMEOUT:g}s"
            return _done(res, "unknown", "The Reacher server took too long to answer, so "
                                         "this address is unchecked.")
        except requests.RequestException as e:
            raise _ReacherDown(f"request to {url} failed ({type(e).__name__})") from e
    if r.status_code in (401, 403):
        raise _ReacherDown(f"{url} refused our Reacher secret")
    try:
        data = r.json()
    except ValueError:
        data = None
    if r.status_code >= 400 or not isinstance(data, dict) or "error" in data:
        msg = data.get("error") if isinstance(data, dict) else r.text
        res["smtp"]["message"] = _text(f"HTTP {r.status_code}: {msg}")
        return _done(res, "unknown", "The Reacher server could not check this address, so "
                                     "it is unchecked.")
    return _from_reacher(res, data, domain, hosts)


def _from_reacher(res: dict, data: dict, domain: str, hosts: list[str]) -> dict:
    syntax = data.get("syntax") if isinstance(data.get("syntax"), dict) else {}
    mx = data.get("mx") if isinstance(data.get("mx"), dict) else {}
    smtp = data.get("smtp") if isinstance(data.get("smtp"), dict) else {}
    misc = data.get("misc") if isinstance(data.get("misc"), dict) else {}
    provider = provider_for(hosts)
    out = res["smtp"]
    smtp_err = _core_error(smtp)
    if smtp_err or not smtp:
        out["message"] = smtp_err
    else:
        out.update(can_connect=smtp.get("can_connect_smtp"),
                   deliverable=smtp.get("is_deliverable"),
                   catch_all=smtp.get("is_catch_all"),
                   full_inbox=smtp.get("has_full_inbox"),
                   disabled=smtp.get("is_disabled"))
        if out["can_connect"] is not None:
            _remember(domain, hosts, provider, bool(out["can_connect"]),
                      out["catch_all"] if out["can_connect"] else None)
    if misc and not _core_error(misc):
        m = res["misc"]
        m["role"] = bool(misc.get("is_role_account")) or m["role"]
        m["disposable"] = bool(misc.get("is_disposable")) or m["disposable"]
        m["free_provider"] = bool(misc.get("is_b2c")) or m["free_provider"]
    reach = str(data.get("is_reachable") or "unknown").lower()
    role = res["misc"]["role"]

    if syntax.get("is_valid_syntax") is False:
        return _done(res, "invalid", "This is not a well formed email address.")
    if not _core_error(mx) and mx.get("accepts_mail") is False:
        # Our own lookup found a mail server (or we would not have asked), so
        # this is a disagreement between resolvers, not a finding.
        return _done(res, "unknown", f"Reacher found no mail server for {domain} where our "
                                     f"lookup did, so this address is unchecked.")
    if reach == "safe":
        return _done(res, "valid", _accepted_reason(domain, role))
    if reach == "invalid":
        if out["can_connect"] is False:
            # Reacher calls an unreachable server invalid; not reaching a server
            # says nothing about the mailbox.
            return _done(res, "unknown", f"Could not connect to the mail server for {domain}, "
                                         f"so this address is unchecked.")
        if out["disabled"]:
            return _done(res, "invalid", f"The mail server for {domain} says this mailbox has "
                                         f"been disabled.")
        return _done(res, "invalid", f"The mail server for {domain} says this mailbox does "
                                     f"not exist.")
    if reach == "risky":
        if out["catch_all"]:
            return _done(res, "catch_all", _catch_all_reason(domain, provider))
        if out["full_inbox"]:
            return _done(res, "risky", "The mailbox exists but is full, so mail to it bounces "
                                       "for now.")
        if res["misc"]["disposable"]:
            return _done(res, "risky", "This is a throwaway inbox service, not a business "
                                       "address.")
        # Reacher marks every shared inbox risky before it looks at whether the
        # server accepted it. contact_point already flags role inboxes, so the
        # verdict here is the server's, the same one the native engine gives.
        if role and out["can_connect"] and out["deliverable"] and not out["disabled"]:
            return _done(res, "valid", _accepted_reason(domain, role))
        if role and out["can_connect"] and out["deliverable"] is False:
            return _done(res, "invalid", f"The mail server for {domain} says this mailbox "
                                         f"does not exist.")
        return _done(res, "risky", f"The mail server for {domain} accepted this address with "
                                   f"warning signs, so send to it with care.")
    return _done(res, "unknown", f"The mail server for {domain} would not give a clear "
                                 f"answer, so this address is unchecked.")


# ---------------------------------------------------------------- one check

def _blank(email: str, engine: str) -> dict:
    return {"email": email, "status": "unknown", "reason": "", "engine": engine, "mx": [],
            "smtp": {"can_connect": None, "deliverable": None, "catch_all": None,
                     "full_inbox": None, "disabled": None, "code": None, "message": None},
            "misc": {"role": False, "disposable": False, "free_provider": False},
            "checked_at": _now()}


def _dns_verdict(res: dict, domain: str) -> dict:
    res["engine"] = "dns"
    return _done(res, "unknown", f"{domain} accepts mail, but this check cannot see whether "
                                 f"this particular mailbox exists.")


def _check_one(email: str, engine: str, auto: bool) -> dict:
    email = (email or "").strip()
    res = _blank(email, engine)
    try:
        if not mailcheck.valid_syntax(email):
            return _done(res, "invalid", "This is not a well formed email address.")
        domain = email.rsplit("@", 1)[1].lower().rstrip(".")
        res["misc"] = {"role": contacts.is_role_email(email),
                       "disposable": is_disposable(domain),
                       "free_provider": is_free_provider(domain)}
        # DNS is ours for every engine, so "no mail server" means one thing
        # whichever engine runs, and Reacher is never asked about a dead domain.
        hosts, state = _mail_hosts(domain)
        if state == "dns":
            return _done(res, "unknown", f"Could not reach a DNS server to look up {domain}, "
                                         f"so nothing was checked.")
        if state == "null":
            return _done(res, "no_mail_server", f"{domain} declares that it accepts no mail, "
                                                f"so nothing sent there can arrive.")
        if state == "none":
            return _done(res, "no_mail_server", f"{domain} has no mail server, so nothing "
                                                f"sent there can arrive.")
        res["mx"] = [_split_host(h)[0] for h in hosts]
        if res["misc"]["disposable"]:
            return _done(res, "risky", "This is a throwaway inbox service, not a business "
                                       "address.")
        if engine == "reacher" and auto and _reacher_health()[0] is False:
            engine = "native" if smtp_egress_ok()[0] else "dns"   # went down mid-run
            res["engine"] = engine
        if engine == "dns":
            return _dns_verdict(res, domain)
        if engine == "reacher":
            try:
                return _reacher(res, email, domain, hosts)
            except _ReacherDown as e:
                if not auto:
                    res["smtp"]["message"] = str(e)
                    return _done(res, "unknown", "The Reacher server could not be reached, so "
                                                 "this address is unchecked.")
                _mark_reacher_down(str(e))
                engine = "native" if smtp_egress_ok()[0] else "dns"
                res["engine"] = engine
                if engine == "dns":
                    return _dns_verdict(res, domain)
        return _native(res, email, domain, hosts)
    except Exception as e:     # one odd address must never sink a batch
        res["smtp"]["message"] = _text(f"{type(e).__name__}: {e}")
        return _done(res, "unknown", "The check failed unexpectedly, so this address is "
                                     "unchecked.")


def _run(emails: list[str], engine: str, auto: bool, max_workers: int) -> list[dict]:
    if len(emails) <= 1 or max_workers <= 1:
        return [_check_one(e, engine, auto) for e in emails]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(emails))) as ex:
        return list(ex.map(lambda e: _check_one(e, engine, auto), emails))


def _domain_of(email: str) -> str:
    return (email or "").strip().rsplit("@", 1)[-1].lower().rstrip(".")


# ---------------------------------------------------------------- public

def check(email: str, engine: str | None = None) -> dict:
    """Check one address. engine: auto | reacher | native | dns, default from
    Settings. Returns email, status, reason, engine, mx, smtp, misc, checked_at."""
    return check_many([email], engine, max_workers=1)[0]


def check_many(emails: list[str], engine: str | None = None,
               max_workers: int = 4) -> list[dict]:
    """Check several addresses, results in input order. Concurrency is capped
    by the per-server gate and the per-minute pacer, not only by max_workers."""
    emails = list(emails or [])
    if not emails:
        return []
    eng, auto = _resolve(engine)
    if eng != "dns":
        _own_conn_cache("load", [_domain_of(e) for e in emails])
    results = _run(emails, eng, auto, max_workers)
    if eng != "dns":
        _own_conn_cache("save")
    return results


def detail_json(result: dict) -> str:
    return json.dumps(result, separators=(",", ":"), ensure_ascii=False, default=str)


def verify_contacts(conn, ids: list[int], engine: str | None = None) -> dict:
    """Check contact_point email rows and store each verdict on its row.

    Commits after every batch of BATCH addresses and before any network
    traffic, so a long run never sits on an open transaction while it waits on
    someone else's mail server. Returns counts by status, plus `checked` and
    `engine` (the engines that actually ran, joined with + if more than one)."""
    counts: dict = {s: 0 for s in STATUSES}
    ids = [int(i) for i in ids or []]
    init(conn)
    by_id: dict[int, str] = {}
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        for r in conn.execute(
                f"SELECT id, value FROM contact_point WHERE kind='email'"
                f" AND id IN ({','.join('?' * len(chunk))})", chunk).fetchall():
            by_id[int(r["id"])] = r["value"]
    conn.commit()                # end the read before the first network call
    rows = [(i, by_id[i]) for i in dict.fromkeys(ids) if i in by_id]
    if not rows:
        counts.update(checked=0, engine=_configured())
        return counts
    eng, auto = _resolve(engine)
    if eng != "dns" and _DB_CACHE:
        try:
            _load_domains(conn, [_domain_of(v) for _, v in rows])
            conn.commit()
        except Exception:
            conn.rollback()
    ran: Counter = Counter()
    for b in range(0, len(rows), BATCH):
        batch = rows[b:b + BATCH]
        results = _run([v for _, v in batch], eng, auto, WORKERS)
        for (cid, _), res in zip(batch, results):
            contacts.set_verification(conn, cid, res["status"], detail_json(res))
            counts[res["status"]] = counts.get(res["status"], 0) + 1
            ran[res["engine"]] += 1
        if eng != "dns" and _DB_CACHE:
            try:
                _save_domains(conn)
            except Exception:
                # A failed statement has rolled the batch back; store the
                # verdicts again without the cache rather than lose them.
                for (cid, _), res in zip(batch, results):
                    contacts.set_verification(conn, cid, res["status"], detail_json(res))
        conn.commit()
    counts["checked"] = len(rows)
    counts["engine"] = "+".join(e for e, _ in ran.most_common()) or eng
    return counts
