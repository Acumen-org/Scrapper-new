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
