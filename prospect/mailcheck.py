"""Free, local email checking. No third party, no API key, no quota.

What replaced what, and why:

The old path posted every candidate address to an undocumented endpoint on
mailwarm.com with a spoofed browser User-Agent. Tested against a real address
and an obviously fake one at the same firm, it returned the identical verdict
for both ("risky", deliverable false, smtp_check false). It could not tell them
apart, so it produced no signal while depending on someone else's service
without an agreement.

The one field in that response worth having was mx_records, which is a DNS
question anybody can ask for free. So that is what this does:

  1. Syntax. A malformed address is knowably bad.
  2. MX lookup. Whether the domain publishes a mail exchanger at all, which is
     the difference between "this domain can receive mail" and "nothing here
     accepts mail, so the guess is worthless".

What this deliberately does NOT claim: that a specific mailbox exists. Proving
that means opening an SMTP conversation with someone else's mail server, and
most providers either accept every recipient or block probes outright, which is
exactly how a guessed @linkedin.com address once came back "valid". A result
here is about the DOMAIN, and the UI says so.

The DNS query is built by hand over UDP rather than adding dnspython: it is one
question type, forty lines, and no new dependency.
"""

from __future__ import annotations

import random
import re
import socket
import struct

# Deliberately permissive: the goal is catching obvious junk, not adjudicating
# the RFC, which allows addresses no real firm uses.
SYNTAX_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
                       r"(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$")

# Domains a guessed employee address can never live on: social platforms and
# freemail. Guessing against one produced 27 confidently wrong @linkedin.com
# addresses once. Lives here, not in a view module, so scripts can import it
# without dragging in the web app.
BAD_EMAIL_DOMAINS = ("linkedin.", "facebook.", "twitter.", "x.com", "instagram.",
                     "youtube.", "tiktok.", "medium.", "vimeo.", "spotify.",
                     "pinterest.", "yelp.", "gmail.", "yahoo.", "hotmail.",
                     "outlook.", "aol.", "icloud.", "threads.")

RESOLVERS = ("1.1.1.1", "8.8.8.8")
QTYPE_MX = 15
TIMEOUT = 4.0


def valid_syntax(email: str) -> bool:
    email = (email or "").strip()
    if not (5 <= len(email) <= 254) or ".." in email:
        return False
    if SYNTAX_RE.match(email) is None:
        return False
    tld = email.rsplit(".", 1)[-1]
    return len(tld) >= 2 and not tld.isdigit()


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        b = label.encode("idna") if any(ord(c) > 127 for c in label) else label.encode()
        out += bytes([len(b)]) + b
    return out + b"\x00"


def _skip_name(buf: bytes, i: int) -> int:
    """Advance past a DNS name, which may be a pointer or a label sequence."""
    while i < len(buf):
        n = buf[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:      # compression pointer, always two bytes
            return i + 2
        i += 1 + n
    return i


def has_mx(domain: str, timeout: float = TIMEOUT) -> bool | None:
    """True if the domain publishes MX records, False if it answers with none,
    None if no resolver could be reached (unknown, not negative)."""
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain:
        return False
    query = (struct.pack(">HHHHHH", random.randint(0, 0xFFFF), 0x0100, 1, 0, 0, 0)
             + _encode_name(domain) + struct.pack(">HH", QTYPE_MX, 1))
    for resolver in RESOLVERS:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(query, (resolver, 53))
                data, _ = s.recvfrom(4096)
        except OSError:
            continue
        if len(data) < 12:
            continue
        _, flags, qd, an, _, _ = struct.unpack(">HHHHHH", data[:12])
        rcode = flags & 0x000F
        if rcode == 3:            # NXDOMAIN: the domain does not exist
            return False
        if rcode != 0:
            continue
        i = 12
        for _ in range(qd):      # step over the echoed question
            i = _skip_name(data, i) + 4
        for _ in range(an):
            i = _skip_name(data, i)
            if i + 10 > len(data):
                break
            rtype, _, _, rdlen = struct.unpack(">HHIH", data[i:i + 10])
            i += 10 + rdlen
            if rtype == QTYPE_MX:
                return True
        return False              # answered authoritatively, no MX present
    return None                   # every resolver unreachable


QTYPE_TXT = 16


def _read_name(buf: bytes, i: int, depth: int = 0) -> tuple[str, int]:
    """A DNS name at offset i, following compression pointers, and the offset
    just past it in the original position."""
    labels: list[str] = []
    end = None
    while i < len(buf) and depth < 20:
        n = buf[i]
        if n == 0:
            i += 1
            break
        if n & 0xC0 == 0xC0:
            if i + 1 >= len(buf):
                break
            ptr = ((n & 0x3F) << 8) | buf[i + 1]
            if end is None:
                end = i + 2
            i = ptr
            depth += 1
            continue
        labels.append(buf[i + 1:i + 1 + n].decode("ascii", "replace"))
        i += 1 + n
    return ".".join(labels).lower(), (end if end is not None else i)


def records(domain: str, qtype: int, timeout: float = TIMEOUT) -> list[str] | None:
    """MX host names or TXT strings for a domain. [] when the domain answers
    with none, None when no resolver could be reached."""
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain:
        return []
    query = (struct.pack(">HHHHHH", random.randint(0, 0xFFFF), 0x0100, 1, 0, 0, 0)
             + _encode_name(domain) + struct.pack(">HH", qtype, 1))
    for resolver in RESOLVERS:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(query, (resolver, 53))
                data, _ = s.recvfrom(8192)
        except OSError:
            continue
        if len(data) < 12:
            continue
        _, flags, qd, an, _, _ = struct.unpack(">HHHHHH", data[:12])
        rcode = flags & 0x000F
        if rcode == 3:
            return []
        if rcode != 0:
            continue
        i = 12
        for _ in range(qd):
            i = _skip_name(data, i) + 4
        out: list[str] = []
        for _ in range(an):
            i = _skip_name(data, i)
            if i + 10 > len(data):
                break
            rtype, _, _, rdlen = struct.unpack(">HHIH", data[i:i + 10])
            i += 10
            rdata_at = i
            i += rdlen
            if rtype == QTYPE_MX and qtype == QTYPE_MX:
                host, _ = _read_name(data, rdata_at + 2)
                out.append(host)
            elif rtype == QTYPE_TXT and qtype == QTYPE_TXT:
                j, parts = rdata_at, []
                while j < rdata_at + rdlen:
                    ln = data[j]
                    parts.append(data[j + 1:j + 1 + ln].decode("utf-8", "replace"))
                    j += 1 + ln
                out.append("".join(parts))
        return out
    return None


# Hosted mail providers that are known not to be Microsoft 365 or Google.
OTHER_PROVIDERS = ("secureserver.net", "emailsrvr.com", "zoho.", "yahoodns.",
                   "icloud.com", "protonmail.", "intermedia.net", "exch",
                   "mailstore1.secureserver")


def mail_platform(domain: str) -> tuple[str, str]:
    """Which platform a domain's mail runs on, from public DNS alone.

    Returns (platform, evidence) with platform one of:
      m365     Microsoft 365: MX at *.mail.protection.outlook.com, or an SPF
               record that authorises Microsoft behind a filtering gateway
      google   Google Workspace: MX at Google, or SPF authorising only Google
      other    a named hosted provider that is neither
      unknown  a gateway or self-hosted server with no clue to what is behind
               it, or DNS unreachable
      none     the domain publishes no mail server at all
    A free lookup any mail client performs; no account and no third party."""
    mx = records(domain, QTYPE_MX)
    if mx is None:
        return "unknown", "DNS unreachable"
    if not mx:
        return "none", f"{domain} publishes no mail server"
    hosts = " ".join(mx)
    # Microsoft publishes two MX shapes: the long-standing
    # *.mail.protection.outlook.com and the DNSSEC-signed *.mx.microsoft.
    if ("mail.protection.outlook.com" in hosts or ".mx.microsoft" in hosts
            or hosts.endswith("outlook.com")):
        return "m365", f"mail server {mx[0]}"
    if "google.com" in hosts or "googlemail.com" in hosts:
        return "google", f"mail server {mx[0]}"
    txt = records(domain, QTYPE_TXT) or []
    spf = " ".join(t.lower() for t in txt if t.lower().startswith("v=spf1"))
    ms = "spf.protection.outlook.com" in spf
    goog = "_spf.google.com" in spf
    if ms and not goog:
        return "m365", f"mail filtered by {mx[0]}; SPF authorises Microsoft 365"
    if goog and not ms:
        return "google", f"mail filtered by {mx[0]}; SPF authorises Google"
    if any(t.startswith("MS=ms") for t in txt) and not goog:
        return "m365", f"mail filtered by {mx[0]}; domain verified with Microsoft"
    if any(p in hosts for p in OTHER_PROVIDERS):
        return "other", f"mail hosted at {mx[0]}"
    return "unknown", f"mail server {mx[0]}; provider behind it not visible"


def check(email: str) -> tuple[str, str]:
    """(status, human readable reason) for one address.

    Statuses: bad_syntax | no_mail_server | domain_accepts_mail | unknown.
    None of these assert that the individual mailbox exists, because that is
    not knowable without sending mail.
    """
    if not valid_syntax(email):
        return "bad_syntax", "not a well formed address"
    domain = email.rsplit("@", 1)[-1].lower()
    mx = has_mx(domain)
    if mx is None:
        return "unknown", "could not reach a DNS resolver to ask"
    if mx is False:
        return "no_mail_server", f"{domain} publishes no mail server, so no " \
                                 f"address there can receive mail"
    return "domain_accepts_mail", f"{domain} accepts mail; whether this " \
                                  f"particular mailbox exists is still unproven"
