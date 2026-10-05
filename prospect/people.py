"""Who works at each firm, where they came from, and where they went.

The SEC's weekly individual feed (IA_INDVL) names every registered investment
adviser representative with their current employers, every previous
registration with begin and end dates, ten years of employment history, exams,
professional designations and disclosure flags. Joined to the firms this system
already tracks, that is a roster for every firm and a hiring record: who
joined, who left, from where and to where.

Tables, rebuilt in full by scripts/ingest_people.py from one snapshot:

  person              one row per individual, names in display case.
  person_employment   current employers, previous registrations, and the
                      employment history, one row each.
  people_event        a 'joined' or 'left' per stint at a tracked firm, with
                      the firm the person came from or went to.
  firm_people_stats   headcount and twelve month hiring numbers per firm,
                      computed in SQL after the load.

A stint is the run of registrations a person holds with one firm. Registrations
at the same firm that end and restart within 90 days are one stint, because
that is almost always paperwork (a firm re-registering, a lapsed state renewal)
and not a person leaving and coming back. A current employee's start date in
person_employment is the start of that whole stint.

What the feed cannot show: it lists only people who are registered somewhere
today. Someone who retired or left the industry is not in it at all, so their
departure is invisible here, and headcounts for past years count only the
people still registered. Departures are moves to another registered firm.

The helpers at the bottom are what the firm page and the dashboard call. They
tolerate the tables not existing yet (before the first ingest) by returning
empty results rather than raising, since a missing roster should not take a
firm page down.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, timedelta

from .names import nice_name

TABLES = ("person", "person_employment", "people_event", "firm_people_stats")

# Staging tables carry this suffix while a load is in flight. The ingest loads
# into them, then renames them over the live tables in one short transaction,
# so readers never see a half-built roster. Index and constraint names carry
# it too and are renamed with the tables.
STAGE = "__new"

# {s} is the table suffix: empty for the live tables, STAGE while loading.
# Substituted with str.replace rather than str.format because the comments
# below carry braces of their own.
_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS person{s} (
    indvl_pk        TEXT PRIMARY KEY,          -- CRD number of the individual
    first_name      TEXT,
    middle_name     TEXT,
    last_name       TEXT,
    suffix          TEXT,
    name            TEXT NOT NULL,      -- display "First Middle Last Suffix", proper case
    other_names     TEXT,               -- json list of other names filed (maiden names...)
    exams           TEXT,               -- json list of {code, name, date}
    designations    TEXT,               -- json list of short codes: CFP, CFA, ChFC, PFS, CIC
    disclosures     TEXT,               -- json list of disclosure kinds flagged Y
    has_disclosure  INTEGER NOT NULL DEFAULT 0,
    active_broker   INTEGER NOT NULL DEFAULT 0,  -- actvAGReg: a registered broker rep too
    other_business  TEXT,
    iapd_link       TEXT,
    snapshot_id     INTEGER,            -- null when loaded from a local file
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS person_employment{s} (
    indvl_pk    TEXT NOT NULL,
    org_pk      TEXT NOT NULL DEFAULT '',  -- employer CRD; empty for history not tied to one
    org_name    TEXT,
    kind        TEXT NOT NULL,             -- current | previous | history
    reg_cats    TEXT,                      -- current: registration categories, comma list
    start_date  TEXT NOT NULL DEFAULT '',  -- ISO; history is month precision, yyyy-mm-01
    end_date    TEXT,                      -- ISO, null while ongoing
    city        TEXT,                      -- branch office where the feed gives one
    state       TEXT,
    PRIMARY KEY (indvl_pk, org_pk, kind, start_date)
);

CREATE TABLE IF NOT EXISTS people_event{s} (
    id              INTEGER PRIMARY KEY,
    crd             TEXT NOT NULL,             -- the tracked firm
    indvl_pk        TEXT NOT NULL,
    kind            TEXT NOT NULL,             -- joined | left
    event_date      TEXT NOT NULL,             -- ISO
    other_org_pk    TEXT,                      -- joined: came from; left: went to
    other_org_name  TEXT,
    UNIQUE (crd, indvl_pk, kind, event_date)
);

CREATE TABLE IF NOT EXISTS firm_people_stats{s} (
    crd                  TEXT PRIMARY KEY,
    headcount            INTEGER,
    hires_12m            INTEGER,
    departures_12m       INTEGER,
    hires_prev_12m       INTEGER,
    departures_prev_12m  INTEGER,
    net_12m              INTEGER,
    avg_tenure_years     REAL,
    cfp_count            INTEGER,
    cfa_count            INTEGER,
    broker_dual_count    INTEGER,
    disclosure_count     INTEGER,
    top_sources          TEXT,         -- json [{org_pk, org_name, n}], where 3-year hires came from
    top_destinations     TEXT,         -- json, same shape, where 3-year leavers went
    computed_at          TEXT
);
"""

# The primary key of person_employment leads with indvl_pk, so it already
# serves every lookup by person; a separate index on indvl_pk alone would only
# duplicate it and slow the load.
_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS ix_person_employment_org{s} ON person_employment{s} (org_pk, kind);
CREATE INDEX IF NOT EXISTS ix_people_event_crd{s} ON people_event{s} (crd, event_date);
CREATE INDEX IF NOT EXISTS ix_people_event_date{s} ON people_event{s} (event_date);
"""


def table_ddl(suffix: str = "") -> str:
    return _TABLE_DDL.replace("{s}", suffix)


def index_ddl(suffix: str = "") -> str:
    return _INDEX_DDL.replace("{s}", suffix)


SCHEMA = table_ddl() + index_ddl()


def init(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ------------------------------------------------------------ feed vocabulary

# Designations arrive spelled out. Short codes are what a roster shows.
DESIGNATION_CODES = {
    "Certified Financial Planner": "CFP",
    "Chartered Financial Analyst": "CFA",
    "Chartered Financial Consultant": "ChFC",
    "Personal Financial Specialist": "PFS",
    "Chartered Investment Counselor": "CIC",
}

# DRP attribute -> the word stored in person.disclosures.
DISCLOSURE_KINDS = {
    "hasCustComp": "customer_complaint",
    "hasRegAction": "regulatory",
    "hasCriminal": "criminal",
    "hasTermination": "termination",
    "hasBankrupt": "bankruptcy",
    "hasJudgment": "judgment_lien",
    "hasCivilJudc": "civil",
    "hasBond": "bond",
    "hasInvstgn": "investigation",
}


# ------------------------------------------------------------- display names

_ROMAN = {"II", "III", "IV", "V", "VI", "VII"}
_MC = re.compile(r"\bMc([a-z])")


def proper(part: str | None) -> str:
    """One name part in display case.

    The feed files most names in capitals and some in mixed case as the person
    entered them. Mixed case is left alone (it is how they spell it: DeAngelo,
    McCarthy); all-capitals or all-lowercase is title cased, with McX fixed
    up. Mac is not, because Mack and Macy are names too."""
    s = " ".join((part or "").split())
    if not s:
        return ""
    if s != s.upper() and s != s.lower():
        return s
    if s.upper() in _ROMAN:
        return s.upper()
    out = s.lower().title()
    return _MC.sub(lambda m: "Mc" + m.group(1).upper(), out)


_GENERATIONAL = {"JR": "Jr", "SR": "Sr", "I": "I", "II": "II", "III": "III", "IV": "IV",
                 "V": "V", "VI": "VI", "VII": "VII", "2ND": "2nd", "3RD": "3rd"}
# What people type when the form insists on a middle name they do not have.
_NO_MIDDLE = {"NMN", "NMI", "NONE", "NA", "NOMIDDLENAME"}


def clean_suffix(s: str | None) -> str | None:
    """A generational suffix in display form, or None for anything else.

    The feed's suffix field also carries honorifics (Mr., Ms.), credentials
    (CFA, CPA) and stray text; none of that belongs after a name on a roster."""
    return _GENERATIONAL.get(re.sub(r"[^A-Z0-9]", "", (s or "").upper()))


def name_parts(first: str | None, middle: str | None, last: str | None,
               suffix: str | None = None) -> tuple[str, str, str, str]:
    """(first, middle, last, suffix) in display case, with the feed's clutter
    moved or removed: 'ANDERSON, JR.' as a last name becomes Anderson with
    suffix Jr, 'DEESE, CLTC' becomes Deese, 'Smith III' moves the III to the
    suffix, and NMN-style placeholders for a missing middle name disappear."""
    suf = clean_suffix(suffix)
    last = " ".join((last or "").replace("`", "'").split())
    if "," in last:
        head, *tail = [p.strip() for p in last.split(",")]
        kept = []
        for p in tail:
            g = clean_suffix(p)
            if g:
                suf = suf or g
            elif p and not all(_is_tail_word(t) or t in _HONORIFICS for t in _toks(_fold(p))):
                kept.append(p)
        last = ", ".join([head] + kept)
    toks = last.split()
    tail = re.sub(r"[^A-Z]", "", toks[-1].upper()) if toks else ""
    if len(toks) >= 2 and tail in {"JR", "SR", "II", "III", "IV"}:
        suf = suf or clean_suffix(toks[-1])
        last = " ".join(toks[:-1])
    if re.sub(r"[^A-Z]", "", (middle or "").upper()) in _NO_MIDDLE:
        middle = ""
    first = (first or "").replace("`", "'")
    return proper(first), proper(middle), proper(last), suf or ""


def display_name(first: str | None, middle: str | None, last: str | None,
                 suffix: str | None = None) -> str:
    return " ".join(p for p in name_parts(first, middle, last, suffix) if p)


def schedule_a_display(name: str) -> str:
    """'LAST, FIRST, MIDDLE' as filed on Schedule A, as 'First Middle Last'.
    A suffix filed as a part of its own ('SMITH, JOHN, A, JR') goes last."""
    parts = [p.strip() for p in (name or "").split(",") if p.strip()]
    if len(parts) < 2:
        return proper(name)
    suf, rest = None, []
    for p in parts[1:]:
        g = clean_suffix(p)
        if g:
            suf = suf or g
        else:
            rest.append(p)
    return display_name(rest[0] if rest else "", " ".join(rest[1:]), parts[0], suf)


# ------------------------------------------------------------ name matching

_SUFFIX_WORDS = {"JR", "SR", "II", "III", "IV", "V", "VI", "2ND", "3RD"}
# Letters people put after their name on a website or a filing. Stripped only
# from the end of a name or from a comma part of their own, never from the
# front, because JD and Al are first names too.
_CREDENTIALS = {
    "CFP", "CFA", "CPA", "CHFC", "CLU", "PFS", "CIC", "AIF", "AIFA", "CIMA", "CPWA",
    "CRPC", "RICP", "CAIA", "MBA", "JD", "PHD", "MD", "ESQ", "CRPS", "AAMS", "CDFA",
    "CLTC", "CEPA", "CFS", "CKA", "CMFC", "AWMA", "APMA", "CTFA", "FRM", "EA", "MSFS",
    "CPFA", "CEP", "CSA", "RFC", "LUTCF", "FIC", "AEP", "CWS", "BFA", "CRC", "CAP",
    "CDAA", "CIPM", "CMT", "CFT", "MS", "MA", "BS", "BA", "LLM", "CEBS", "CPCU",
    "RMA", "CRPS", "CWM", "FCHFC", "FLMI", "QPA", "QKA", "CPP", "CIPP", "ECA",
}
_HONORIFICS = {"MR", "MRS", "MS", "MISS", "DR", "PROF", "REV", "HON"}
_PARTICLES = {"DE", "LA", "LE", "DEL", "DELLA", "DA", "DI", "DU", "DOS", "VAN",
              "VON", "DER", "DEN", "TER", "ST", "SAN", "BIN", "AL", "EL"}

# Common forms of the same first name. Two names match when they share a group,
# so Ted matches both Edward and Theodore while Edward and Theodore do not
# match each other.
_NICKNAME_GROUPS = [
    "WILLIAM BILL BILLY WILL WILLIE WILLY LIAM",
    "ROBERT BOB BOBBY ROB ROBBIE BERT",
    "JAMES JIM JIMMY JAMIE",
    "MICHAEL MIKE MIKEY MICK MICKEY",
    "THOMAS TOM TOMMY",
    "DAVID DAVE DAVEY",
    "CHRISTOPHER CHRIS KIT",
    "MATTHEW MATT MATTY",
    "JOSEPH JOE JOEY",
    "DANIEL DAN DANNY",
    "STEPHEN STEVEN STEVE STEVIE",
    "RICHARD RICK RICKY RICH RICHIE DICK",
    "ANDREW ANDY DREW",
    "ANTHONY TONY",
    "KATHERINE KATHARINE CATHERINE CATHARINE KATHRYN KATE KATIE KATHY CATHY KAT",
    "ELIZABETH LIZ LIZZIE BETH BETSY ELIZA LIBBY BETTY",
    "JENNIFER JEN JENN JENNY",
    "SAMUEL SAM SAMMY",
    "BENJAMIN BEN BENNY",
    "NICHOLAS NICOLAS NICK NICKY",
    "GREGORY GREG",
    "JEFFREY JEFFERY GEOFFREY JEFF GEOFF",
    "EDWARD ED EDDIE TED NED",
    "PATRICK PAT",
    "PATRICIA PAT PATTY TRISH",
    "KENNETH KEN KENNY",
    "RONALD RON RONNIE",
    "DONALD DON DONNIE",
    "LAWRENCE LAURENCE LARRY",
    "GERALD GERRY JERRY",
    "CHARLES CHARLIE CHUCK CHAS",
    "THEODORE TED TEDDY",
    "FRANCIS FRANK FRANKIE",
    "ALBERT AL BERT",
    "ALAN ALLAN ALLEN AL",
    "JOHN JACK JOHNNY",
    "TIMOTHY TIM TIMMY",
    "PHILIP PHILLIP PHIL",
    "ALEXANDER ALEX",
    "ALEXANDRA ALEX ALEXA",
    "MARGARET MAGGIE PEGGY PEG MEG",
    "SUSAN SUE SUZY SUZANNE",
    "DEBORAH DEBRA DEB DEBBIE",
    "HENRY HANK HARRY",
    "HAROLD HAL HARRY",
    "LEONARD LEN LENNY",
    "VINCENT VINCE VINNY",
    "ZACHARY ZACH ZACK",
    "JOSHUA JOSH",
    "NATHAN NATHANIEL NATE NAT",
    "CHRISTINE CHRISTINA KRISTINE KRISTINA CHRIS TINA",
    "REBECCA BECKY BECCA",
    "DOUGLAS DOUG",
    "RAYMOND RAY",
    "WALTER WALT",
    "EUGENE GENE",
    "FREDERICK FREDRICK FRED FREDDIE",
    "JONATHAN JON",
]
_NICK: dict[str, set[int]] = {}
for _i, _g in enumerate(_NICKNAME_GROUPS):
    for _n in _g.split():
        _NICK.setdefault(_n, set()).add(_i)


# Typographic quotes from websites, built with chr() so this file stays ASCII.
_RSQUO, _LDQUO, _RDQUO = chr(0x2019), chr(0x201C), chr(0x201D)
_APOSTROPHES = re.compile("[.'`" + _RSQUO + "]")
_QUOTED = re.compile('["(' + _LDQUO + '][^")' + _RDQUO + ']*[")' + _RDQUO + ']')


def _fold(s: str) -> str:
    """Accents off, capitals, apostrophes and periods gone (O'Brien, J.)."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).upper()
    return _APOSTROPHES.sub("", s)


def _toks(s: str) -> list[str]:
    return [t for t in re.split(r"[^A-Z0-9]+", s) if t]


def _is_tail_word(t: str) -> bool:
    return t in _SUFFIX_WORDS or t in _CREDENTIALS


def parse_name(text: str) -> tuple[str, list[str], list[str], str | None] | None:
    """(first, middles, last_tokens, alt_last) from a name in any common form.

    Takes 'First Middle Last', 'LAST, FIRST, MIDDLE' (Schedule A),
    'Last, First M.', and website forms like 'John Smith, CFP' or
    'Robert "Bob" Smith Jr.'. alt_last is the last two tokens run together, for
    'Maria Garcia Lopez' style surnames that a space split would cut in half.
    Returns None when nothing usable is left."""
    s = _fold(text)
    # A quoted or bracketed nickname ('Robert "Bob" Smith') is dropped: the
    # nickname table already pairs the common ones with the filed name, and
    # left in place it would read as a middle name and clash with the real one.
    s = _QUOTED.sub(" ", s)
    parts = [p.strip() for p in s.split(",") if p.strip()]
    parts = [p for p in parts if not all(_is_tail_word(t) for t in _toks(p))]
    if not parts:
        return None
    alt_last = None
    if len(parts) >= 2:
        last = _toks(parts[0])
        while len(last) > 1 and _is_tail_word(last[-1]):
            last.pop()
        rest = _toks(" ".join(parts[1:]))
        while len(rest) > 1 and _is_tail_word(rest[-1]):
            rest.pop()
        while rest and rest[0] in _HONORIFICS:
            rest.pop(0)
        if not last or not rest:
            return None
        first, middles = rest[0], rest[1:]
    else:
        toks = _toks(parts[0])
        while toks and toks[0] in _HONORIFICS:
            toks.pop(0)
        while len(toks) > 2 and _is_tail_word(toks[-1]):
            toks.pop()
        if len(toks) < 2:
            return None
        first, middles, last = toks[0], toks[1:-1], [toks[-1]]
        if middles:
            alt_last = middles[-1] + toks[-1]
    return first, middles, last, alt_last


def _first_score(a: str, b: str) -> int:
    """4 identical, 3 nickname, 2 one is a stem of the other, 1 initial."""
    if not a or not b:
        return 0
    if a == b:
        return 4
    if _NICK.get(a, set()) & _NICK.get(b, set()):
        return 3
    if len(a) == 1 or len(b) == 1:
        return 1 if a[0] == b[0] else 0
    if min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)):
        return 2
    return 0


def _middle_score(qm: list[str], cm: list[str]) -> float | None:
    """Bonus when middles agree, None when they plainly disagree.

    A different middle initial at the same firm under the same name is the
    father and son case, so it rejects rather than guesses."""
    if not qm or not cm:
        return 0.0
    if set(qm) & set(cm):
        return 1.0
    if {m[0] for m in qm} & {m[0] for m in cm}:
        return 0.5
    return None


def _last_score(q_last: list[str], alt_last: str | None, c_last: str) -> tuple[int, bool]:
    """(score, matched_through_alt). 2 exact, 1 a shared surname token."""
    c_toks = _toks(_fold(c_last))
    c_join = "".join(c_toks)
    if not c_join:
        return 0, False
    if c_join == "".join(q_last):
        return 2, False
    if alt_last and c_join == alt_last:
        return 2, True
    shared = {t for t in set(c_toks) & set(q_last) if len(t) >= 3 and t not in _PARTICLES}
    return (1, False) if shared else (0, False)


class _Candidate:
    __slots__ = ("indvl_pk", "variants", "current", "end_date")

    def __init__(self, indvl_pk, variants, current, end_date=None):
        self.indvl_pk = indvl_pk
        self.variants = variants        # [(first, middles, last_name, is_alias)]
        self.current = current
        self.end_date = end_date


def _candidate(row: dict, current: bool) -> _Candidate:
    variants = [(_fold(row.get("first_name") or ""),
                 _toks(_fold(row.get("middle_name") or "")),
                 row.get("last_name") or "", False)]
    for other in _json_list(row.get("other_names")):
        p = parse_name(other)
        if p:
            first, middles, last, _alt = p
            variants.append((first, middles, " ".join(last), True))
    return _Candidate(row["indvl_pk"], variants, current, row.get("end_date"))


def _score(parsed, cand: _Candidate) -> float:
    q_first, q_mid, q_last, alt_last = parsed
    best = 0.0
    for c_first, c_mid, c_last, alias in cand.variants:
        ls, via_alt = _last_score(q_last, alt_last, c_last)
        if not ls:
            continue
        qm = q_mid[:-1] if via_alt else q_mid
        c_first_t = _toks(c_first)[:1]
        cf = c_first_t[0] if c_first_t else ""
        fs = _first_score(q_first, cf)
        check_middle = True
        if fs == 0:
            # People who go by their middle name: 'J. Michael Smith' filed as
            # first J, middle Michael, and listed on a website as Michael Smith.
            if c_mid and _first_score(q_first, c_mid[0]) >= 3:
                fs, check_middle = 2, False
            elif qm and _first_score(qm[0], cf) >= 3:
                fs, check_middle = 2, False
        if not fs or fs + ls < 3:
            continue
        ms = _middle_score(qm, c_mid) if check_middle else 0.0
        if ms is None:
            continue
        s = fs + ls + ms + (0.5 if cand.current else 0.0) - (0.25 if alias else 0.0)
        best = max(best, s)
    return best


def _best(parsed, cands: list[_Candidate]) -> _Candidate | None:
    """The single best candidate, or None when two people tie for it.

    A tie at the same firm is two real people with the same name; picking one
    would attach a title or an email to the wrong person, which is worse than
    leaving it unmatched."""
    scored: dict[str, tuple[float, _Candidate]] = {}
    for c in cands:
        s = _score(parsed, c)
        if s > 0 and s > scored.get(c.indvl_pk, (0.0, None))[0]:
            scored[c.indvl_pk] = (s, c)
    if not scored:
        return None
    ranked = sorted(scored.values(), key=lambda t: -t[0])
    if len(ranked) > 1 and ranked[1][0] >= ranked[0][0]:
        return None
    return ranked[0][1]


def _like_patterns(parsed) -> list[str]:
    _f, _m, last, alt = parsed
    keys = {t for t in last if len(t) >= 2 and t not in _PARTICLES} | {"".join(last)}
    if alt:
        keys.add(alt)
    return [f"%{k}%" for k in keys if k]


_PERSON_COLS = ("p.indvl_pk, p.first_name, p.middle_name, p.last_name, p.suffix,"
                " p.other_names")

# Letters only, so 'O''BRIEN' and 'OBRIEN' and 'Smith-Jones' all compare on
# the same key the Python side folds a name to.
_LAST_KEY_SQL = ("(regexp_replace(upper(p.last_name), '[^A-Z]', '', 'g') LIKE ANY (?)"
                 " OR regexp_replace(upper(COALESCE(p.other_names, '')), '[^A-Z]', '', 'g')"
                 " LIKE ANY (?))")


def _candidates(conn, crd: str, patterns: list[str] | None) -> list[_Candidate]:
    """Current and former people at a firm, narrowed by surname when given.

    The surname filter keeps match_person cheap at the largest firms, where
    the former staff alone run to tens of thousands."""
    where = "e.org_pk = ? AND e.kind IN ('current', 'previous')"
    params: list = [crd]
    if patterns:
        where += " AND " + _LAST_KEY_SQL
        params += [patterns, patterns]
    rows = conn.execute(
        f"SELECT {_PERSON_COLS}, bool_or(e.kind = 'current') AS is_current,"
        "       MAX(e.end_date) AS end_date"
        "  FROM person_employment e JOIN person p ON p.indvl_pk = e.indvl_pk"
        f" WHERE {where}"
        " GROUP BY p.indvl_pk", params).fetchall()
    return [_candidate(r, bool(r["is_current"])) for r in rows]


# ------------------------------------------------------------------ helpers

def _json_list(v) -> list:
    if not v:
        return []
    try:
        out = json.loads(v)
    except (TypeError, ValueError):
        return []
    return out if isinstance(out, list) else []


def _ready(conn, *tables: str) -> bool:
    """True when every named table exists. Checked before reading so a page
    rendered before the first ingest gets empty results, not an error that
    would also roll back whatever else its transaction held."""
    for t in tables:
        r = conn.execute("SELECT to_regclass(?) AS t", (t,)).fetchone()
        if not r or r["t"] is None:
            return False
    return True


def _years_between(start: str, end: date) -> float | None:
    try:
        d = date.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    return round((end - d).days / 365.25, 1)


def _schedule_a(conn, crd: str) -> list[dict]:
    if not _ready(conn, "schedule_a"):
        return []
    rows = conn.execute(
        "SELECT name, title, control_person, as_of FROM schedule_a"
        " WHERE crd = ? AND is_individual = 1 ORDER BY name, title", (crd,)).fetchall()
    return [dict(r) for r in rows]


def _officer_matches(conn, crd: str) -> list[tuple[dict, _Candidate | None]]:
    """Each Schedule A individual at the firm with the person they matched.

    Former staff are candidates too, so an officer who has since left matches
    their own record rather than a namesake still on the roster. Candidates
    are narrowed to the officers' surnames first: at the largest firms the
    full roster runs to 25,000 people, and an officer list to a dozen."""
    officers = _schedule_a(conn, crd)
    if not officers:
        return []
    parsed = [(o, parse_name(o["name"])) for o in officers]
    patterns = sorted({p for _o, pn in parsed if pn for p in _like_patterns(pn)})
    pool = _candidates(conn, crd, patterns) if patterns else []
    return [(o, _best(pn, pool) if pn else None) for o, pn in parsed]


# ------------------------------------------------------------- query helpers

def roster(conn, crd: str) -> list[dict]:
    """Everyone currently registered with the firm, officers first.

    Titles come from Schedule A, matched by name, because the individual feed
    carries no positions. Schedule A comes from the 2024 archive, so a title
    can be stale; the officer list itself is what the firm last filed."""
    if not _ready(conn, "person", "person_employment"):
        return []
    rows = conn.execute(
        "SELECT p.indvl_pk, p.name, p.exams, p.designations, p.disclosures,"
        "       p.has_disclosure, p.active_broker, p.iapd_link,"
        "       e.start_date, e.reg_cats, e.city, e.state,"
        "       pr.org_name AS prior_firm, pr.org_pk AS prior_firm_pk"
        "  FROM person_employment e"
        "  JOIN person p ON p.indvl_pk = e.indvl_pk"
        "  LEFT JOIN LATERAL ("
        "       SELECT x.org_name, x.org_pk FROM person_employment x"
        "        WHERE x.indvl_pk = e.indvl_pk AND x.kind = 'previous'"
        "          AND x.org_pk <> e.org_pk"
        "        ORDER BY COALESCE(x.end_date, '') DESC, x.start_date DESC LIMIT 1"
        "  ) pr ON true"
        " WHERE e.org_pk = ? AND e.kind = 'current'", (crd,)).fetchall()

    today = date.today()
    people: dict[str, dict] = {}
    for r in rows:
        people[r["indvl_pk"]] = {
            "indvl_pk": r["indvl_pk"],
            "name": r["name"],
            "since": r["start_date"] or None,
            "years_at_firm": _years_between(r["start_date"], today),
            "branch_city": nice_name(r["city"]) or None,
            "branch_state": r["state"] or None,
            "reg_cats": r["reg_cats"],
            "exams": [x.get("code") for x in _json_list(r["exams"]) if isinstance(x, dict)],
            "designations": _json_list(r["designations"]),
            "disclosures": _json_list(r["disclosures"]),
            "has_disclosure": bool(r["has_disclosure"]),
            "active_broker": bool(r["active_broker"]),
            "iapd_link": r["iapd_link"],
            "prior_firm": nice_name(r["prior_firm"]) or None,
            "prior_firm_pk": r["prior_firm_pk"],
            "title": None,
            "is_officer": False,
            "control_person": False,
        }

    for officer, hit in _officer_matches(conn, crd):
        if hit is None or hit.indvl_pk not in people:
            continue
        p = people[hit.indvl_pk]
        title = nice_name(officer.get("title")) or None
        if title:
            p["title"] = title if not p["title"] else f"{p['title']}; {title}"
        p["is_officer"] = True
        if (officer.get("control_person") or "").upper() == "Y":
            p["control_person"] = True

    return sorted(people.values(), key=lambda p: (
        not p["is_officer"], p["since"] or "9999", p["name"]))


def officers_unmatched(conn, crd: str) -> list[dict]:
    """Schedule A officers who are not on the current roster.

    Two kinds: people with no registration at the firm at all (owners and
    executives who are not adviser reps, often the CEO or CCO), and people who
    were registered there and have since left, flagged former with the date.
    Either way the roster alone would never show them."""
    if _ready(conn, "person", "person_employment"):
        pairs = _officer_matches(conn, crd)
    else:
        pairs = [(o, None) for o in _schedule_a(conn, crd)]
    # Schedule A files one row per title, so an owner who is also CCO appears
    # twice; they are one person here with both titles.
    out: dict[str, dict] = {}
    for officer, hit in pairs:
        if hit is not None and hit.current:
            continue
        title = nice_name(officer.get("title")) or None
        prev = out.get(officer["name"])
        if prev is not None:
            if title and title not in (prev["title"] or ""):
                prev["title"] = f"{prev['title']}; {title}" if prev["title"] else title
            if (officer.get("control_person") or "").upper() == "Y":
                prev["control_person"] = True
            continue
        out[officer["name"]] = {
            "name": schedule_a_display(officer["name"]),
            "title": title,
            "control_person": (officer.get("control_person") or "").upper() == "Y",
            "as_of": officer.get("as_of"),
            "former": hit is not None,
            "indvl_pk": hit.indvl_pk if hit else None,
            "left_date": hit.end_date if hit else None,
        }
    return list(out.values())


def movements(conn, crd: str, days: int = 730) -> dict:
    """Who joined and who left the firm in the last `days`, newest first."""
    out: dict[str, list] = {"joined": [], "left": []}
    if not _ready(conn, "person", "people_event"):
        return out
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT ev.kind, ev.event_date, ev.indvl_pk, ev.other_org_pk,"
        "       ev.other_org_name, p.name, p.iapd_link"
        "  FROM people_event ev JOIN person p ON p.indvl_pk = ev.indvl_pk"
        " WHERE ev.crd = ? AND ev.event_date >= ?"
        " ORDER BY ev.event_date DESC, p.name", (crd, since)).fetchall()
    for r in rows:
        out.setdefault(r["kind"], []).append({
            "name": r["name"],
            "indvl_pk": r["indvl_pk"],
            "date": r["event_date"],
            "other_org_name": nice_name(r["other_org_name"]) or None,
            "other_org_pk": r["other_org_pk"],
            "iapd_link": r["iapd_link"],
        })
    return out


def stats(conn, crd: str) -> dict | None:
    """The firm's firm_people_stats row, with the two json columns decoded."""
    if not _ready(conn, "firm_people_stats"):
        return None
    r = conn.execute("SELECT * FROM firm_people_stats WHERE crd = ?", (crd,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    for k in ("top_sources", "top_destinations"):
        d[k] = [dict(x, org_name=nice_name(x.get("org_name")) or None)
                for x in _json_list(d.get(k)) if isinstance(x, dict)]
    return d


def headcount_series(conn, crd: str, years: int = 10) -> list[dict]:
    """Year by year: people registered at the firm on December 31 (today for
    the current year), and joins and departures during the year.

    Counts only people still registered somewhere today, since nobody else is
    in the feed. Recent years are close to complete; the further back, the
    more retirements are missing from the count."""
    if not _ready(conn, "person_employment", "people_event"):
        return []
    today = date.today()
    first_year = today.year - max(1, years) + 1
    # Spans ending before the first year-end cannot count in any year, so
    # they are dropped before the join; at the largest firms that is most of
    # the former staff.
    rows = conn.execute(
        "WITH spans AS ("
        "  SELECT indvl_pk, start_date::date AS s,"
        "         COALESCE(NULLIF(end_date, ''), '9999-12-31')::date AS e"
        "    FROM person_employment"
        "   WHERE org_pk = ? AND kind IN ('current', 'previous') AND start_date <> ''"
        "     AND COALESCE(NULLIF(end_date, ''), '9999-12-31') >= ?),"
        " yrs AS ("
        "  SELECT y, LEAST(make_date(y, 12, 31), ?::date) AS d"
        "    FROM generate_series(?::int, ?::int) AS y)"
        " SELECT yrs.y AS year, COUNT(DISTINCT spans.indvl_pk) AS headcount_end"
        "   FROM yrs LEFT JOIN spans ON spans.s <= yrs.d AND spans.e >= yrs.d"
        "  GROUP BY yrs.y ORDER BY yrs.y",
        (crd, f"{first_year}-12-31", today.isoformat(), first_year, today.year)).fetchall()
    ev = conn.execute(
        "SELECT substr(event_date, 1, 4) AS y, kind, COUNT(DISTINCT indvl_pk) AS n"
        "  FROM people_event WHERE crd = ? AND event_date >= ?"
        " GROUP BY 1, 2", (crd, f"{first_year}-01-01")).fetchall()
    counts = {(r["y"], r["kind"]): r["n"] for r in ev}
    return [{"year": r["year"],
             "headcount_end": r["headcount_end"],
             "joined": counts.get((str(r["year"]), "joined"), 0),
             "left": counts.get((str(r["year"]), "left"), 0)} for r in rows]


def recent_moves(conn, days: int = 30, crds: list[str] | None = None,
                 limit: int = 50) -> list[dict]:
    """Joins and departures across firms in the last `days`, newest first,
    for a dashboard feed. `crds` narrows it to a list (a watch list, a
    product list); None means every tracked firm."""
    if not _ready(conn, "person", "people_event"):
        return []
    today = date.today()
    sql = (
        "SELECT ev.kind, ev.event_date, ev.crd, ev.indvl_pk, ev.other_org_pk,"
        "       ev.other_org_name, p.name, p.iapd_link,"
        "       COALESCE(fc.legal_name, emp.org_name) AS legal_name"
        "  FROM people_event ev"
        "  JOIN person p ON p.indvl_pk = ev.indvl_pk"
        "  LEFT JOIN firm_current fc ON fc.crd = ev.crd"
        "  LEFT JOIN LATERAL ("
        "       SELECT org_name FROM person_employment x"
        "        WHERE x.indvl_pk = ev.indvl_pk AND x.org_pk = ev.crd"
        "          AND x.kind IN ('current', 'previous') LIMIT 1) emp ON true"
        " WHERE ev.event_date > ? AND ev.event_date <= ?")
    params: list = [(today - timedelta(days=days)).isoformat(), today.isoformat()]
    if crds is not None:
        if not crds:
            return []
        sql += " AND ev.crd = ANY (?)"
        params.append([str(c) for c in crds])
    sql += " ORDER BY ev.event_date DESC, ev.id DESC LIMIT ?"
    params.append(int(limit))
    return [{
        "kind": r["kind"],
        "date": r["event_date"],
        "crd": r["crd"],
        "legal_name": nice_name(r["legal_name"]) or None,
        "indvl_pk": r["indvl_pk"],
        "name": r["name"],
        "other_org_pk": r["other_org_pk"],
        "other_org_name": nice_name(r["other_org_name"]) or None,
        "iapd_link": r["iapd_link"],
    } for r in conn.execute(sql, params).fetchall()]


def match_person(conn, crd: str, full_name: str) -> str | None:
    """indvl_pk of the current or former employee of `crd` with this name.

    Tolerates case, punctuation, middle names or initials present on one side
    only, suffixes and letters after the name, 'Last, First' order, common
    nicknames, people who go by their middle name, and maiden names filed as
    other names. Returns None when nobody fits or when two people fit equally
    well, rather than guess between namesakes."""
    if not full_name or not _ready(conn, "person", "person_employment"):
        return None
    parsed = parse_name(full_name)
    if parsed is None:
        return None
    hit = _best(parsed, _candidates(conn, str(crd), _like_patterns(parsed)))
    return hit.indvl_pk if hit else None


def roster_names(conn, crd: str) -> list[tuple[str, str]]:
    """(display name, indvl_pk) for everyone currently at the firm."""
    if not _ready(conn, "person", "person_employment"):
        return []
    return [(r["name"], r["indvl_pk"]) for r in conn.execute(
        "SELECT p.name, p.indvl_pk FROM person_employment e"
        "  JOIN person p ON p.indvl_pk = e.indvl_pk"
        " WHERE e.org_pk = ? AND e.kind = 'current' ORDER BY p.name", (crd,)).fetchall()]


def universe_counts(conn) -> dict:
    """Headline numbers for the dashboard."""
    empty = {"people": 0, "firms_with_people": 0, "hires_12m": 0,
             "departures_12m": 0, "updated_at": None}
    if not _ready(conn, "person", "people_event", "firm_people_stats"):
        return empty
    today = date.today()
    lo, hi = (today - timedelta(days=365)).isoformat(), today.isoformat()
    r = conn.execute(
        "SELECT (SELECT COUNT(*) FROM person) AS people,"
        "       (SELECT COUNT(*) FROM firm_people_stats WHERE headcount > 0)"
        "         AS firms_with_people,"
        "       (SELECT COUNT(*) FROM people_event WHERE kind = 'joined'"
        "          AND event_date > ? AND event_date <= ?) AS hires_12m,"
        "       (SELECT COUNT(*) FROM people_event WHERE kind = 'left'"
        "          AND event_date > ? AND event_date <= ?) AS departures_12m,"
        "       (SELECT MAX(updated_at) FROM person) AS updated_at",
        (lo, hi, lo, hi)).fetchone()
    return dict(r) if r else empty
