"""Candidate addresses for a person, in the order most likely to be right.

A guessed address is never shown to anyone. The guesses made here go to the
email hunter (prospect.hunt), which asks the firm's own mail server about each
one in turn and keeps only an address the server confirms. So the job of this
module is the ORDER of the guesses, because every guess costs a question to
someone else's mail server:

  1. the firm's OWN pattern, learned from a real person-to-address pair we hold
     for that firm (if jsmith@ is real for Jane Smith, the CEO follows suit),
     including addresses the hunter itself has confirmed;
  2. then every other common pattern, most common across all firms first,
     where we DID observe one;
  3. then the forms people actually go by: Bill for William, a middle name
     for someone filed as J. Robert Smith, the second half of a double surname.

The domain is the firm's own mail domain (from a filed/scraped email, else its
website), never a social or freemail host.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict

from .mailcheck import BAD_EMAIL_DOMAINS

# Order matters twice: next_candidate() walks it, and _detect() returns the
# first pattern that fits an address. The first nine are the original set;
# later ones were added for the hunter and must stay after them.
PATTERNS = {
    "first.last": lambda f, l: f"{f}.{l}",
    "flast":      lambda f, l: f"{f[0]}{l}",
    "first":      lambda f, l: f,
    "firstlast":  lambda f, l: f"{f}{l}",
    "first_l":    lambda f, l: f"{f}{l[0]}",       # johns (named before first_last existed)
    "f.last":     lambda f, l: f"{f[0]}.{l}",
    "last":       lambda f, l: l,
    "lastfirst":  lambda f, l: f"{l}{f}",
    "last.first": lambda f, l: f"{l}.{f}",
    "first_last": lambda f, l: f"{f}_{l}",
    "lastf":      lambda f, l: f"{l}{f[0]}",
    "first-last": lambda f, l: f"{f}-{l}",
}

# Used when no firm in the data shows a pattern yet. Small advisory firms
# favour first.last and flast; bare first names are common at very small ones.
DEFAULT_ORDER = ("first.last", "flast", "first", "firstlast", "first_l", "f.last",
                 "first_last", "lastf", "last.first", "last", "lastfirst", "first-last")

# Patterns that spell out the first name or its initial. Only these are worth
# trying with a nickname, since "last" or "lastfirst" with Bill is a long shot.
FIRST_PATTERNS = {"first.last", "flast", "first", "firstlast", "first_l", "f.last",
                  "first_last", "first-last"}

# Patterns that carry the surname, so a hit names one person, not anyone
# sharing a first name.
WITH_LAST = {"first.last", "flast", "firstlast", "f.last", "first_last", "first-last",
             "lastf", "last.first", "lastfirst"}

# Formal first name -> the short forms people put in their address. One way
# only: a William may be bill@, but a filed "Bill" is not guessed as william@.
NICK_FORMS = {
    "william": ("bill", "will"), "robert": ("bob", "rob"), "james": ("jim", "jamie"),
    "michael": ("mike",), "thomas": ("tom",), "david": ("dave",),
    "christopher": ("chris",), "matthew": ("matt",), "joseph": ("joe",),
    "daniel": ("dan",), "stephen": ("steve",), "steven": ("steve",),
    "richard": ("rick", "rich"), "andrew": ("andy", "drew"), "anthony": ("tony",),
    "katherine": ("kate", "kathy"), "catherine": ("cathy", "kate"),
    "kathryn": ("kathy", "kate"), "elizabeth": ("liz", "beth"),
    "jennifer": ("jen", "jenn"), "samuel": ("sam",), "benjamin": ("ben",),
    "nicholas": ("nick",), "gregory": ("greg",), "jeffrey": ("jeff",),
    "geoffrey": ("geoff",), "edward": ("ed", "ted"), "patrick": ("pat",),
    "patricia": ("pat", "trish"), "kenneth": ("ken",), "ronald": ("ron",),
    "donald": ("don",), "lawrence": ("larry",), "gerald": ("jerry",),
    "charles": ("chuck", "charlie"), "theodore": ("ted",), "francis": ("frank",),
    "albert": ("al",), "timothy": ("tim",), "philip": ("phil",), "phillip": ("phil",),
    "alexander": ("alex",), "alexandra": ("alex",), "margaret": ("maggie", "peggy"),
    "susan": ("sue",), "suzanne": ("sue",), "deborah": ("deb",), "debra": ("deb",),
    "leonard": ("len",), "vincent": ("vince",), "zachary": ("zach",),
    "joshua": ("josh",), "nathan": ("nate",), "nathaniel": ("nate",),
    "christine": ("chris",), "christina": ("chris", "tina"), "rebecca": ("becky",),
    "douglas": ("doug",), "raymond": ("ray",), "walter": ("walt",), "eugene": ("gene",),
    "frederick": ("fred",), "jonathan": ("jon",), "johnathan": ("jon",),
    "harold": ("hal",), "henry": ("hank",), "abigail": ("abby",), "cynthia": ("cindy",),
    "jacqueline": ("jackie",), "kimberly": ("kim",), "pamela": ("pam",),
    "victoria": ("vicky", "tori"), "barbara": ("barb",), "jessica": ("jess",),
    "stephanie": ("steph",), "bradley": ("brad",), "mitchell": ("mitch",),
    "russell": ("russ",), "randall": ("randy",), "terrence": ("terry",),
    "terence": ("terry",), "kristopher": ("kris",), "maximilian": ("max",),
    "peter": ("pete",), "stanley": ("stan",), "valerie": ("val",),
    "frederic": ("fred",), "jerome": ("jerry",), "dennis": ("denny",),
}

MAX_CANDIDATES = 18      # per person: a dozen patterns plus the likeliest variants
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "2nd", "3rd"}
# Surname particles: part of the whole surname (vanderberg), never a surname
# on their own, so never offered as the half a firm might have kept.
_PARTICLES = {"van", "von", "der", "den", "de", "la", "le", "del", "della", "da", "di",
              "du", "dos", "ter", "st", "san", "bin", "al", "el"}


def fold(s: str | None) -> str:
    """Lowercase ASCII letters only: accents folded (an accented e becomes e), apostrophes,
    hyphens and spaces dropped (obrien, smithjones). This is what an address
    can hold."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z]", "", s)


def next_candidate(full_name: str, domain: str, preferred: str, tried: set[str]) -> tuple[str, str] | None:
    """One new internal candidate; rejected addresses are never recycled."""
    parts = name_parts(full_name)
    if not parts or not domain:
        return None
    for pattern in dict.fromkeys([preferred] + list(PATTERNS)):
        if pattern not in PATTERNS:
            continue
        address = f'{PATTERNS[pattern](*parts)}@{domain}'.lower()
        if address not in tried:
            return address, pattern
    return None


def name_parts(full: str) -> tuple[str, str] | None:
    """(first, last) from 'First Middle Last', accents folded. Generational
    suffixes are dropped so John Smith Jr is smith, not jr."""
    s = unicodedata.normalize("NFKD", full or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    parts = [x for x in re.sub(r"[^a-z ]", "", s).split() if x]
    while len(parts) > 2 and parts[-1] in _SUFFIXES:
        parts.pop()
    if len(parts) < 2:
        return None
    return parts[0], parts[-1]


def pretty(filed: str) -> str:
    """Schedule A files 'LAST, FIRST, MIDDLE'; people read the other order."""
    from .names import person_name
    return person_name(filed)


def _detect(first: str, last: str, local: str) -> str | None:
    for pat, fn in PATTERNS.items():
        if fn(first, last) == local:
            return pat
    return None


def _observe(conn) -> tuple[dict[str, tuple[str, str]], Counter]:
    """Every person-to-address pair we hold, reduced to (firm patterns, global
    counts). A firm's pattern is the one most of its pairs agree on."""
    votes: dict[str, Counter] = defaultdict(Counter)
    pop: Counter = Counter()
    rows = []
    # Published person-to-address pairs from every real source, plus guesses a
    # mail server has confirmed. Unconfirmed guesses are excluded: learning a
    # pattern from our own guesses would be circular.
    try:
        rows += [(r["crd"], r["person_name"], r["value"]) for r in conn.execute(
            "SELECT crd, person_name, value FROM contact_point WHERE kind='email'"
            " AND person_key != '' AND person_name IS NOT NULL AND is_role=0"
            " AND (source != 'pattern' OR verify_status = 'valid')"
            " AND verify_status NOT IN ('invalid','no_mail_server')")]
    except Exception:
        conn.rollback()
    # Rows are dicts under Postgres, so they are unpacked by name: tuple
    # unpacking a dict row yields its column names, not its values.
    try:
        rows += [(r["crd"], r["person"], r["email"]) for r in conn.execute(
            "SELECT crd, person, email FROM web_contact"
            " WHERE person IS NOT NULL AND email IS NOT NULL")]
    except Exception:
        conn.rollback()
    try:
        rows += [(r["crd"], r["name"], r["value"]) for r in conn.execute(
            """SELECT f.crd, s.name, f.value FROM firm_contact_info f
               JOIN schedule_a s ON s.crd=f.crd AND s.is_individual=1
               WHERE f.kind='email'""")]
    except Exception:
        conn.rollback()
    for crd, person, email in rows:
        np = name_parts(pretty(person) if "," in (person or "") else person)
        if not np:
            continue
        local, _, dom = (email or "").lower().partition("@")
        if not dom or any(b in dom for b in BAD_EMAIL_DOMAINS):
            continue
        pat = _detect(np[0], np[1], local)
        if pat:
            votes[crd][(dom, pat)] += 1
            pop[pat] += 1
    firm_pat = {crd: v.most_common(1)[0][0] for crd, v in votes.items()}
    return firm_pat, pop


def observed(conn) -> tuple[dict[str, tuple[str, str]], str]:
    """Return (firm_pattern_by_crd, global_fallback_pattern).

    firm_pattern maps crd -> (domain, pattern) wherever we hold a real
    person-to-email pair that fits a known pattern.
    """
    firm_pat, pop = _observe(conn)
    return firm_pat, (pop.most_common(1)[0][0] if pop else "flast")


def observed_ranked(conn) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """(firm_pattern_by_crd, every pattern most common first). Patterns never
    observed follow in DEFAULT_ORDER, so the list always covers them all."""
    firm_pat, pop = _observe(conn)
    return firm_pat, ranking(pop)


def ranking(pop: Counter | dict | None) -> list[str]:
    seen = [p for p, _ in Counter(pop or {}).most_common() if p in PATTERNS]
    return list(dict.fromkeys(seen + list(DEFAULT_ORDER) + list(PATTERNS)))


def split_name(first: str | None, middle: str | None, last: str | None
               ) -> tuple[str, str, str, list[str]]:
    """(first, middle, last, other surnames) as address parts.

    'J Robert Smith' goes by Robert, so an initial-only first name gives way to
    the middle name. A double surname is kept whole (smithjones) with each half
    offered as a variant, since firms pick either."""
    f = fold(first)
    mids = [fold(m) for m in re.split(r"[\s.]+", middle or "") if fold(m)]
    raw_last = [p for p in re.split(r"[\s\-]+", unicodedata.normalize("NFKD", last or "")) if fold(p)]
    raw_last = [p for p in raw_last if fold(p) not in _SUFFIXES] or raw_last
    l = "".join(fold(p) for p in raw_last)
    others = [fold(p) for p in raw_last if fold(p) not in _PARTICLES] if len(raw_last) > 1 else []
    if len(f) == 1 and mids and len(mids[0]) > 1:
        f, mids = mids[0], [f] + mids[1:]
    m = mids[0] if mids else ""
    return f, m, l, [o for o in others if len(o) > 1 and o != l]


def parse_full(full: str) -> tuple[str, str, str, list[str]] | None:
    """split_name() for a name in any written form: 'First M. Last, CFP',
    'LAST, FIRST, MIDDLE' as Schedule A files it, 'Robert "Bob" Smith Jr'."""
    from .people import parse_name
    p = parse_name(full or "")
    if not p:
        return None
    first, middles, lasts = p[0], list(p[1]), list(p[2])
    # 'Maria De La Cruz' parses with De and La as middle names; they belong
    # to the surname.
    while middles and fold(middles[-1]) in _PARTICLES:
        lasts.insert(0, middles.pop())
    return split_name(first, " ".join(middles), " ".join(lasts))


def candidates(first: str, last: str, domain: str, *, middle: str = "",
               other_lasts: list[str] | tuple = (), preferred: str | None = None,
               order: list[str] | None = None, limit: int = MAX_CANDIDATES
               ) -> list[tuple[str, str, str]]:
    """Every address worth asking about for one person, most likely first, as
    (address, pattern, variant). variant is '' for the filed name, else what
    was substituted: 'nickname bill', 'middle name robert', 'surname jones'.

    Parts must already be folded (split_name). Fewer than two letters in the
    first name or none in the last gives nothing: an initial alone is not a
    first name, and 'first' with an initial would be a one-letter mailbox."""
    domain = (domain or "").strip().lower()
    if not domain or len(first or "") < 2 or not last:
        return []
    pats = list(dict.fromkeys(([preferred] if preferred in PATTERNS else [])
                              + [p for p in (order or ranking(None)) if p in PATTERNS]))
    out: dict[str, tuple[str, str, str]] = {}

    def add(f: str, l: str, pat: str, variant: str) -> None:
        local = PATTERNS[pat](f, l)
        addr = f"{local}@{domain}"
        if 1 < len(local) <= 64 and addr not in out:
            out[addr] = (addr, pat, variant)

    for pat in pats:
        add(first, last, pat, "")
    if middle:
        # The middle initial: jdgardner@, john.d.gardner@.
        for local, pat in ((f"{first[0]}{middle[0]}{last}", "fmlast"),
                           (f"{first}.{middle[0]}.{last}", "first.m.last")):
            addr = f"{local}@{domain}"
            if len(local) <= 64 and addr not in out:
                out[addr] = (addr, pat, "")
    # Variants only on the likeliest few patterns: each one costs a question to
    # the firm's mail server, and the long tail almost never answers yes.
    lead = [p for p in pats if p in FIRST_PATTERNS][:3]
    for nick in NICK_FORMS.get(first, ()):
        for pat in lead:
            add(nick, last, pat, f"nickname {nick}")
    if middle and len(middle) > 1 and middle != first:
        # Someone who goes by their middle name, but never a bare middle@: a
        # David with no surname in the address could be any David there.
        for pat in [p for p in lead if p in WITH_LAST][:2]:
            add(middle, last, pat, f"middle name {middle}")
    for other in other_lasts:
        for pat in pats[:3]:
            add(first, other, pat, f"surname {other}")
    return list(out.values())[:limit]


def domain_for(conn, crd: str) -> str | None:
    """The firm's own mail domain: the domain its published addresses use
    first, else its website. Never a social or freemail host."""
    try:
        rows = conn.execute("""SELECT value FROM contact_point
            WHERE crd=? AND kind='email' AND source != 'pattern'
              AND verify_status NOT IN ('invalid','no_mail_server')
            ORDER BY confidence DESC, id LIMIT 20""", (crd,)).fetchall()
    except Exception:
        conn.rollback()
        rows = []
    rows += conn.execute("""SELECT value FROM firm_contact_info
        WHERE crd=? AND kind='email' ORDER BY id LIMIT 1""", (crd,)).fetchall()
    for r in rows:
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
