"""One best-guess email per person, from the pattern the firm actually uses.

Guessing three patterns per person and showing all three (as the firm page did)
is noise: for a mass email you want the single most likely address, not a menu
where two of three bounce. This picks one, in priority order:

  1. the firm's OWN pattern, learned from a real person-to-email pair we hold
     for that firm (if jsmith@ is real for Jane Smith, the CEO follows suit);
  2. otherwise the most common pattern across every firm where we DID observe
     one, which our data puts at "first" then "flast".

The domain is the firm's own mail domain (from a filed/scraped email, else its
website), never a social or freemail host. Shared by the firm-page button and
the bulk inference job so both behave identically.
"""

from __future__ import annotations

import re
from collections import Counter

from .mailcheck import BAD_EMAIL_DOMAINS

PATTERNS = {
    "first.last": lambda f, l: f"{f}.{l}",
    "flast":      lambda f, l: f"{f[0]}{l}",
    "first":      lambda f, l: f,
    "firstlast":  lambda f, l: f"{f}{l}",
    "first_l":    lambda f, l: f"{f}{l[0]}",
    "f.last":     lambda f, l: f"{f[0]}.{l}",
    "last":       lambda f, l: l,
    "lastfirst":  lambda f, l: f"{l}{f}",
    "last.first": lambda f, l: f"{l}.{f}",
}


def name_parts(full: str) -> tuple[str, str] | None:
    parts = [x for x in re.sub(r"[^a-z ]", "", (full or "").lower()).split() if x]
    if len(parts) < 2:
        return None
    return parts[0], parts[-1]


def pretty(filed: str) -> str:
    """Schedule A files 'LAST, FIRST, MIDDLE'; people read the other order."""
    parts = [p.strip() for p in (filed or "").split(",") if p.strip()]
    return (" ".join(parts[1:] + parts[:1]) if len(parts) >= 2 else filed).title()


def _detect(first: str, last: str, local: str) -> str | None:
    for pat, fn in PATTERNS.items():
        if fn(first, last) == local:
            return pat
    return None


def observed(conn) -> tuple[dict[str, tuple[str, str]], str]:
    """Return (firm_pattern_by_crd, global_fallback_pattern).

    firm_pattern maps crd -> (domain, pattern) wherever we hold a real
    person-to-email pair that fits a known pattern.
    """
    firm_pat: dict[str, tuple[str, str]] = {}
    pop: Counter = Counter()
    rows = list(conn.execute(
        "SELECT crd, person, email FROM web_contact"
        " WHERE person IS NOT NULL AND email IS NOT NULL"))
    rows += [(r["crd"], r["name"], r["value"]) for r in conn.execute(
        """SELECT f.crd, s.name, f.value FROM firm_contact_info f
           JOIN schedule_a s ON s.crd=f.crd AND s.is_individual=1
           WHERE f.kind='email'""")]
    for crd, person, email in rows:
        np = name_parts(pretty(person) if "," in (person or "") else person)
        if not np:
            continue
        local, _, dom = (email or "").lower().partition("@")
        if not dom or any(b in dom for b in BAD_EMAIL_DOMAINS):
            continue
        pat = _detect(np[0], np[1], local)
        if pat:
            firm_pat.setdefault(crd, (dom, pat))
            pop[pat] += 1
    return firm_pat, (pop.most_common(1)[0][0] if pop else "flast")


def domain_for(conn, crd: str) -> str | None:
    """The firm's own mail domain: a filed email's domain first, else its
    website. Never a social or freemail host."""
    r = conn.execute("""SELECT value FROM firm_contact_info
        WHERE crd=? AND kind='email' ORDER BY id LIMIT 1""", (crd,)).fetchone()
    if r:
        dom = r["value"].rsplit("@", 1)[-1].lower()
        if not any(b in dom for b in BAD_EMAIL_DOMAINS):
            return dom
    r = conn.execute("SELECT website FROM firm_current WHERE crd=?", (crd,)).fetchone()
    if r and r["website"]:
        m = re.search(r"(?:https?://)?(?:www\.)?([a-z0-9.-]+\.[a-z]{2,})",
                      r["website"].lower())
        if m and not any(b in m.group(1) for b in BAD_EMAIL_DOMAINS):
            return m.group(1)
    return None


def best_email(full_name: str, crd: str, firm_pat: dict, fallback: str,
               domain: str | None) -> tuple[str, str] | None:
    """(email, source_label) for one person, or None if unguessable.

    source_label records how the guess was made, so the UI and the export can
    say whether it rests on the firm's own pattern or the common fallback.
    """
    np = name_parts(full_name)
    if not np:
        return None
    if crd in firm_pat:
        dom, pat = firm_pat[crd]
        label = f"pattern seen at firm ({pat})"
    else:
        dom, pat, label = domain, fallback, f"best-guess pattern ({fallback})"
    if not dom:
        return None
    return f"{PATTERNS[pat](np[0], np[1])}@{dom}", label
