"""Free web search for public contact details: LinkedIn profiles found the way
anyone finds them, through a search engine, and addresses a firm's people
published somewhere on the open web.

Why a search engine and not LinkedIn itself: the scrapers that log in to
LinkedIn (linkedin_scraper, linkedin-api) need an account, break LinkedIn's
terms and get that account banned. A public profile URL is in every search
engine's index already. This module asks those engines through `ddgs`
(github.com/deedy5/ddgs, MIT), a metasearch client that needs no API key and
costs nothing, and only ever stores what a result actually shows.

Behaviour that keeps it polite and keeps a job alive:
  - One query at a time per process, at most one every INTERVAL_S seconds
    (default 3, BELLWETHER_SEARCH_INTERVAL), and the spacing also honours the
    last query any process cached, so a manual run next to the job does not
    double the rate.
  - Every answer is cached in web_search_cache and reused for 30 days (an
    empty answer for 7), so a re-run or a second person at the same firm
    costs nothing.
  - A rate-limit answer backs off (30s, 60s, 120s) and then trips the
    searcher for the rest of the run; so do repeated network failures, and a
    run of empty answers that a known-good probe query confirms is a block.
    A tripped searcher answers None, which callers record as "not searched",
    never as "nothing found". Nothing here raises into a job.

Matching is deliberately strict, because a wrong profile sends a salesperson
to a stranger:
  - find_linkedin keeps a result only when the name in the result's title is
    this person's (surname plus given name or a nickname of it) and the firm
    shows in the title or snippet. Both strong: 'matched'. One of them loose:
    'probable'. Anything else is dropped.
  - find_published_emails keeps only addresses at the firm's exact domain,
    shown whole in a result, and not from the email-format sites that print
    made-up examples (first.last@...) next to real domains.

ddgs's Bing parser runs result titles together (each title carries every
later one), so titles are repaired before anything is matched against them.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from . import contacts, harvest

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_search_cache (
    query      TEXT PRIMARY KEY,       -- whitespace-normalised query text
    results    TEXT NOT NULL,          -- JSON list of {title, href, body}, repaired
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_wsc_fetched ON web_search_cache (fetched_at);
CREATE TABLE IF NOT EXISTS contact_search_state (
    crd         TEXT NOT NULL,
    person_key  TEXT NOT NULL,         -- '' for the firm-level searches
    searched_at TEXT NOT NULL,
    found       INTEGER NOT NULL DEFAULT 0,   -- profiles or addresses stored
    status      TEXT,                  -- found | none | empty (engines gave nothing at all)
    queries     INTEGER,               -- searches it took (cached ones included)
    PRIMARY KEY (crd, person_key)
);
CREATE INDEX IF NOT EXISTS ix_css_searched ON contact_search_state (searched_at);
-- Shared between processes: when the engines last told us to stop.
CREATE TABLE IF NOT EXISTS web_search_meta (
    k   TEXT PRIMARY KEY,
    v   TEXT,
    at  TEXT
);
"""

INTERVAL_S = float(os.environ.get("BELLWETHER_SEARCH_INTERVAL") or 3.0)
# Engines tried in turn until one answers (ddgs moves to the next when one
# comes back empty). Both read Bing's index, which honours
# site:linkedin.com/in. In testing Google answered 403, Brave 429, Mojeek 403
# and Startpage a captcha to every query, and ddgs ships its own Bing engine
# disabled, so they are left out; DuckDuckGo itself starts answering 202
# (a challenge) after a few quoted queries, which is why Yahoo is listed too.
BACKENDS = os.environ.get("BELLWETHER_SEARCH_BACKENDS") or "yahoo,duckduckgo"
TIMEOUT_S = 12
MAX_RESULTS = 10
CACHE_DAYS = 30
EMPTY_CACHE_DAYS = 2           # an empty answer is as often a hiccup as a fact
BACKOFF_S = (30, 60, 120)
EMPTY_STREAK_PROBE = 6        # empty answers in a row before checking for a block
FAILS_TO_TRIP = 4             # network errors in a row before giving up for the run
PROBE_QUERY = "linkedin financial advisor"
COOLDOWN_MIN = 30             # minutes every process stays off the engines after a block

_LOCK = threading.Lock()
_LAST = [0.0]                 # monotonic time of this process's last live query
_DASHES = "-" + chr(0x2013) + chr(0x2014)


def init(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def _parse(ts: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat(ts or "")
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def norm_query(q: str) -> str:
    return " ".join((q or "").split())


# ------------------------------------------------------------------ results

def _fix_text(s) -> str:
    return " ".join(str(s or "").replace(chr(0xFFFD), "").split())


_RUN_ON = re.compile(r"(?<=\.\.\.)(?=[A-Z0-9])|(?<=LinkedIn)(?=[A-Za-z0-9\"])")


def _cut_run_on(title: str) -> str:
    """'Jane Doe - Advisor at Acme ...John Roe - CFO' -> the first title:
    a truncated title ends in '...' and the next one starts right after it."""
    return _RUN_ON.split(title, 1)[0].strip()


def _own_part(v: str, others: list[str], min_len: int) -> str:
    """v up to where another result's text starts inside it."""
    cut = len(v)
    for o in others:
        if not o or o == v:
            continue
        head = o[:40]
        if len(head) < min_len:
            continue
        i = v.find(head, 1)
        if 0 < i < cut:
            cut = i
    return v[:cut].strip()


def repair(results: list[dict]) -> list[dict]:
    """Undo the run-together results ddgs's parsers produce. A result's title
    and snippet arrive as its own text followed by other results' text (the
    Yahoo and Bing pages nest each result inside the previous one), so a
    profile can appear to mention a firm that only the next result, the
    firm's own team page, names. Each field is cut where any other result's
    text begins, and a title also where a truncated title ('...') runs
    straight into the next. Safe to run twice."""
    rows = [{"title": _fix_text(r.get("title")), "href": (r.get("href") or "").strip(),
             "body": _fix_text(r.get("body"))} for r in results or [] if r.get("href")]
    for field, min_len in (("title", 12), ("body", 20)):
        vals = [r[field] for r in rows]
        for r in rows:
            r[field] = _own_part(r[field], vals, min_len)
    for r in rows:
        r["title"] = _cut_run_on(r["title"])
    return rows


# ------------------------------------------------------------------ searcher

class Searcher:
    """Cached, rate-limited search. One per job run."""

    def __init__(self, conn, interval: float | None = None, backends: str | None = None):
        self.conn = conn
        self.interval = INTERVAL_S if interval is None else interval
        self.backends = backends or BACKENDS
        self.live = 0
        self.cached = 0
        self.errors = 0
        self.empty_streak = 0
        self.fail_streak = 0
        self.tripped: str | None = None
        self._synced = False
        try:
            r = conn.execute("SELECT v FROM web_search_meta WHERE k='cooldown_until'").fetchone()
            conn.commit()
            until = _parse(r["v"]) if r else None
            if until and until > _now():
                self.tripped = (f"search engines refused recent queries; resting until"
                                f" {_iso(until)}")
        except Exception:
            conn.rollback()

    def _trip(self, reason: str) -> None:
        """Stop live searching for this run, and for every process for
        COOLDOWN_MIN minutes: hammering an engine that has started refusing
        only gets the address blocked for longer."""
        self.tripped = reason
        until = _iso(_now() + timedelta(minutes=COOLDOWN_MIN))
        try:
            self.conn.execute(
                "INSERT INTO web_search_meta (k, v, at) VALUES ('cooldown_until', ?, ?)"
                " ON CONFLICT (k) DO UPDATE SET v=excluded.v, at=excluded.at",
                (until, _iso(_now())))
            self.conn.commit()
        except Exception:
            self.conn.rollback()

    # -- cache
    def _cache_get(self, q: str) -> list[dict] | None:
        try:
            r = self.conn.execute("SELECT results, fetched_at FROM web_search_cache"
                                  " WHERE query=?", (q,)).fetchone()
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            return None
        if not r:
            return None
        when = _parse(r["fetched_at"])
        try:
            res = json.loads(r["results"])
        except ValueError:
            return None
        days = CACHE_DAYS if res else EMPTY_CACHE_DAYS
        if when is None or _now() - when > timedelta(days=days):
            return None
        return repair(res)

    def _cache_put(self, q: str, res: list[dict]) -> None:
        try:
            self.conn.execute(
                "INSERT INTO web_search_cache (query, results, fetched_at) VALUES (?,?,?)"
                " ON CONFLICT (query) DO UPDATE SET results=excluded.results,"
                " fetched_at=excluded.fetched_at", (q, json.dumps(res), _iso(_now())))
            self.conn.commit()
        except Exception:
            self.conn.rollback()

    # -- pacing
    def _wait(self) -> None:
        if not self._synced:
            # Another process (the job, or a manual run) may have searched a
            # moment ago; its last cached query is when.
            self._synced = True
            try:
                r = self.conn.execute("SELECT MAX(fetched_at) t FROM web_search_cache").fetchone()
                self.conn.commit()
                last = _parse(r["t"]) if r else None
                if last:
                    ago = (_now() - last).total_seconds()
                    if 0 <= ago < self.interval:
                        time.sleep(self.interval - ago)
            except Exception:
                self.conn.rollback()
        with _LOCK:
            gap = time.monotonic() - _LAST[0]
            if gap < self.interval:
                time.sleep(self.interval - gap)
            _LAST[0] = time.monotonic()

    def _ask(self, q: str) -> list[dict]:
        """One live query. Raises on failure; [] for an empty answer."""
        from ddgs import DDGS
        from ddgs.exceptions import DDGSException
        self._wait()
        self.live += 1
        try:
            raw = DDGS(timeout=TIMEOUT_S).text(q, region="us-en", safesearch="off",
                                               max_results=MAX_RESULTS, backend=self.backends)
        except DDGSException as e:
            if "no results" in str(e).lower():
                return []
            raise
        return repair(raw or [])

    def _rate_limited(self, e: Exception) -> bool:
        s = f"{type(e).__name__} {e}".lower()
        return "ratelimit" in s or "429" in s or "403" in s or "too many" in s

    def search(self, query: str, *, cache_only: bool = False) -> list[dict] | None:
        """Results for one query, or None when it could not be asked (rate
        limited, offline, tripped). Never raises."""
        q = norm_query(query)
        if not q:
            return []
        hit = self._cache_get(q)
        if hit is not None:
            self.cached += 1
            return hit
        if cache_only or self.tripped:
            return None
        attempt = 0
        while True:
            try:
                res = self._ask(q)
                break
            except Exception as e:  # network, parser, engine: never into the job
                self.errors += 1
                if self._rate_limited(e) and attempt < len(BACKOFF_S):
                    time.sleep(BACKOFF_S[attempt])
                    attempt += 1
                    continue
                self.fail_streak += 1
                if self._rate_limited(e):
                    self._trip(f"rate limited: {str(e)[:120]}")
                elif self.fail_streak >= FAILS_TO_TRIP:
                    self._trip(f"{self.fail_streak} failures in a row: {str(e)[:120]}")
                return None
        self.fail_streak = 0
        if res:
            self.empty_streak = 0
        else:
            self.empty_streak += 1
            if self.empty_streak >= EMPTY_STREAK_PROBE:
                # A long run of nothing is either a run of obscure people or
                # an engine quietly blocking us. A query that always has
                # answers tells which; a block must not be cached as "none".
                self.empty_streak = 0
                try:
                    probe = self._ask(PROBE_QUERY)
                except Exception:
                    probe = []
                if not probe:
                    self._trip("search engines returned nothing, even for a probe query")
                    return None
        self._cache_put(q, res)
        return res


# ------------------------------------------------------------------ firms

def firm_info(conn, crd: str) -> dict:
    r = conn.execute("SELECT business_name, legal_name, website FROM firm_current WHERE crd=?",
                     (crd,)).fetchone()
    conn.commit()
    if not r:
        return {"crd": crd, "names": [], "short": [], "domain": ""}
    names = [n for n in (r["business_name"], r["legal_name"]) if n]
    short = []
    for n in names:
        s = harvest.firm_short_name(n)
        if s and s.lower() not in [x.lower() for x in short]:
            short.append(s)
    host = urlparse(r["website"] if "://" in (r["website"] or "") else
                    "https://" + (r["website"] or "")).hostname or ""
    return {"crd": crd, "names": names, "short": short,
            "domain": harvest.registrable(host.lower()) if host else ""}


def _flat(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower().replace("'", "")).split())


# Brand words hundreds of unrelated firms share. A firm whose only
# distinctive word is one of these ("Summit Financial Services") is not
# identified by that word alone: Summit Wealth is somebody else.
COMMON_BRAND_WORDS = set("""
summit pinnacle horizon horizons heritage legacy keystone cornerstone bridge harbor
harbour anchor beacon compass navigator stewardship north south east west central
first united premier prime alpha apex peak crest evergreen oak oaks pine cedar river
lake mountain ocean coastal bay valley liberty freedom patriot eagle falcon lion
sterling silver gold golden diamond capstone landmark milestone pathway pathways
journey insight vision clarity balance harmony integrity trinity covenant genesis
synergy fusion nexus vertex zenith meridian atlas titan phoenix sage true trusted
signature elite select choice optimal foundation community citizens peoples regional
world park main street square circle point bluestone blue green red black white
mission guardian shield fortress pillar pillars cardinal north star highland
highlands summitt crossroads gateway frontier pioneer heritage ridge stone rock
""".split())


def _core(name: str) -> str:
    """The part of a firm name that identifies it, as a phrase: legal suffix
    and generic leading or trailing words off. 'Baltimore-Washington
    Financial Advisors' gives 'baltimore washington'."""
    toks = _flat(harvest.firm_short_name(name)).split()
    while toks and toks[-1] in harvest.FIRM_STOPWORDS:
        toks.pop()
    while toks and toks[0] in harvest.FIRM_STOPWORDS:
        toks.pop(0)
    return " ".join(toks)


_SEGMENT = re.compile(r"\s[" + _DASHES + "|" + chr(0xB7) + r"]\s")   # hyphen first: a literal
_PLACE = re.compile(r"\bArea\b|United States|Metropolitan|^\s*Location:|, [A-Z]{2}\s*$"
                    r"|^\s*Greater [A-Z]")


def _strip_places(text: str) -> str:
    """Drop the short location fragments LinkedIn puts in titles and snippets
    ('Washington DC-Baltimore Area', 'Location: Greater Philadelphia'). A firm
    named after a place would otherwise be 'found' in where a stranger lives."""
    return " ".join(seg for seg in _SEGMENT.split(text or "")
                    if not (len(seg) < 70 and _PLACE.search(seg)))


# Words that follow a firm's name and not a person's: "Joel Isaacson & Co",
# "Roffman Miller Associates".
_FIRMISH = ("co company inc llc ltd lp llp pc wealth capital financial associates advisors"
            " advisers advisory group partners management investment investments asset"
            " assets planning securities private trust holdings consulting").split()


def firm_evidence(text: str, firm: dict, person_name: str | None = None) -> str | None:
    """'strong' when the firm's name is in the text: its short name, or the
    core of it as a phrase (two words or more, or one long and uncommon
    word); 'weak' when only some of its distinctive words or its domain
    are; else None. Location fragments are ignored.

    A firm named after a person (Joel Isaacson & Co) is not found in a
    namesake's own name: when the firm's name shares a word with
    `person_name`, it counts only followed by a firm word ('& Co',
    'Associates')."""
    t = f" {_flat(_strip_places(text))} "
    mine = set(_flat(person_name or "").split()) - set(harvest.FIRM_STOPWORDS)

    def found(phrase: str) -> bool:
        if f" {phrase} " not in t:
            return False
        if mine & set(phrase.split()):
            return re.search(r" " + re.escape(phrase) + r" (?:" + "|".join(_FIRMISH) + r") ",
                             t) is not None
        return True

    for s in firm.get("short", []):
        fs = _flat(s)
        if len(fs) >= 6 and found(fs):
            return "strong"
    best = None
    for n in firm.get("names", []):
        core = _core(n)
        if core and found(core) and (
                " " in core or (len(core) >= 5 and core not in COMMON_BRAND_WORDS)):
            return "strong"
        words = [w for w in harvest.firm_words(n) if w not in mine]
        if any(len(w) >= 3 and f" {w} " in t for w in words):
            best = "weak"
    label = (firm.get("domain") or "").split(".")[0]
    if (not best and len(label) >= 5 and label in t.replace(" ", "")
            and not any(len(m) >= 3 and m in label for m in mine)):
        best = "weak"
    return best


# LinkedIn's boilerplate repeats the person's name, never the firm's.
_BOILERPLATE = re.compile(r"View [^.]{0,120}?profile on LinkedIn[^.]{0,80}\.?", re.I)


def _firm_text(result: dict, extra: str = "") -> str:
    """What may vouch for the firm in one result: the title without its
    first segment (the person's name), the snippet without LinkedIn's
    boilerplate, and any of their posts in the same answer."""
    parts = re.split(r"\s[" + _DASHES + r"|]\s", result.get("title", "") or "", maxsplit=1)
    rest = parts[1] if len(parts) > 1 else ""
    body = _BOILERPLATE.sub(" ", result.get("body", "") or "")
    return f"{rest} {body} {extra}"


# ------------------------------------------------------------------ LinkedIn

def _title_name(title: str) -> str:
    """The person's name from a LinkedIn result title: 'Jane Doe, CFP - Wealth
    Advisor - Acme | LinkedIn' gives 'Jane Doe'."""
    t = (title or "").split("|")[0]
    t = re.split(r"\s[" + _DASHES + r"]\s", t)[0]
    t = t.split(",")[0].split("(")[0]
    return " ".join(t.split())


def _posts_text(results: list[dict]) -> dict[str, str]:
    """Text of LinkedIn posts in the results, by the slug of the profile that
    posted them (linkedin.com/posts/<slug>_<topic>-activity-...). A post of
    Bill Berlin's that names his firm is evidence for his profile, whose own
    snippet often names nobody."""
    out: dict[str, str] = {}
    for r in results or []:
        m = re.match(r"https?://[\w.]*linkedin\.com/posts/([^/_?#]+)_", r.get("href", ""), re.I)
        if m:
            k = m.group(1).lower()
            out[k] = f"{out.get(k, '')} {r.get('title', '')} {r.get('body', '')}"
    return out


def judge_profile(result: dict, person: dict, firm: dict,
                  posts: dict[str, str] | None = None) -> str | None:
    """'matched', 'probable' or None for one search result about one person."""
    url = contacts.norm_linkedin(result.get("href"))
    if not url or "/in/" not in url:
        return None
    shown = _title_name(result.get("title", ""))
    names = [person["name"]] + list(person.get("other_names") or [])
    fits = [harvest.name_fit(shown, n) for n in names]
    name = "strong" if "strong" in fits else "weak" if "weak" in fits else None
    if not name:
        return None
    if harvest.slug_names_other(url, person["name"]):
        return None
    slug = url.rsplit("/", 1)[-1]
    extra = (posts or {}).get(slug, "")
    firm_ev = firm_evidence(_firm_text(result, extra), firm, person["name"])
    if not firm_ev:
        return None
    if name == "strong" and firm_ev == "strong":
        return "matched"
    if name == "weak" and firm_ev == "weak":
        return None
    return "probable"


def person_queries(person: dict, firm: dict) -> list[str]:
    first = (person.get("first") or "").strip()
    if len(first.strip(".")) <= 1 and person.get("middle"):
        first = person["middle"].split()[0]      # 'S Josh Yeyni' goes by Josh
    last = (person.get("last") or "").strip()
    if not first or not last or not firm.get("short"):
        return []
    biz = firm["short"][0]
    words = " ".join(dict.fromkeys(w for n in firm.get("names", [])
                                   for w in harvest.firm_words(n))) or biz
    # Measured on real people: the firm's words unquoted find more profiles
    # than its full name quoted (LinkedIn pages rarely spell the legal name),
    # and a plain query without site: is the one DuckDuckGo still answers.
    out = [f'"{first} {last}" {words} site:linkedin.com/in',
           f'{first} {last} {biz} linkedin',
           # Bob for Robert, Kym for Kimberly: the surname and the firm find
           # the profile whatever given name it uses.
           f'"{last}" "{biz}" site:linkedin.com/in']
    if len(firm["short"]) > 1:
        out.append(f'"{first} {last}" "{firm["short"][1]}" site:linkedin.com/in')
    return out


def firm_query(firm: dict) -> str | None:
    if not firm.get("short"):
        return None
    return f'"{firm["short"][0]}" site:linkedin.com/in'


def best_profile(results: list[dict], person: dict, firm: dict,
                 taken: set[str] = frozenset()) -> tuple[dict, str] | None:
    best = None
    posts = _posts_text(results)
    for r in results or []:
        verdict = judge_profile(r, person, firm, posts)
        if not verdict:
            continue
        if contacts.norm_linkedin(r["href"]) in taken:
            continue
        if verdict == "matched":
            return r, verdict
        best = best or (r, verdict)
    return best


def _taken(conn, crd: str, person_key: str) -> set[str]:
    """Profiles already given to somebody else at the firm."""
    rows = conn.execute("SELECT value FROM contact_point WHERE crd=? AND kind='linkedin'"
                        " AND person_key NOT IN ('', ?)", (crd, person_key)).fetchall()
    conn.commit()
    return {r["value"] for r in rows}


def find_linkedin(conn, person: dict, *, searcher: Searcher | None = None,
                  firm: dict | None = None, extra_results: list[dict] | None = None,
                  max_queries: int = 2) -> dict:
    """Look for one roster person's public LinkedIn profile and store it.

    person: crd, person_key, name, first, middle, last, other_names (a list),
    title. Results already in hand (the firm-wide query) are judged first,
    then up to `max_queries` searches. Returns {status: found | none | empty |
    unsearched, url, verdict, title, queries}. 'empty' means every search came
    back with no results at all, which these engines also do when they are
    shedding load; 'unsearched' means they could not be asked. Neither is the
    same as nothing found."""
    s = searcher or Searcher(conn)
    firm = firm or firm_info(conn, person["crd"])
    taken = _taken(conn, person["crd"], person["person_key"])
    seen = list(extra_results or [])
    hit = best_profile(seen, person, firm, taken)
    queries, asked, answered = 0, 0, 0
    if not hit or hit[1] != "matched":
        for q in person_queries(person, firm)[:max_queries]:
            res = s.search(q)
            queries += 1
            if res is None:
                continue
            asked += 1
            answered += 1 if res else 0
            # Judged over every answer so far: a post in this answer can
            # vouch for a profile the last one returned.
            seen += res
            got = best_profile(seen, person, firm, taken)
            if got and (not hit or got[1] == "matched"):
                hit = got
            if hit and hit[1] == "matched":
                break
    if not hit:
        if asked and not answered:
            status = "empty"
        else:
            status = "none" if asked or extra_results else "unsearched"
        return {"status": status, "queries": queries}
    r, verdict = hit
    url = contacts.norm_linkedin(r["href"])
    if verdict == "matched":
        # A strong match supersedes a weaker guess this search made earlier.
        conn.execute("DELETE FROM contact_point WHERE crd=? AND person_key=? AND kind='linkedin'"
                     " AND sources='web_search' AND verify_status='probable' AND value != ?",
                     (person["crd"], person["person_key"], url))
    contacts.upsert(conn, person["crd"], "linkedin", url, "web_search",
                    person_key=person["person_key"], person_name=person["name"],
                    title=person.get("title"), source_ref=r["title"] or url,
                    confidence=75 if verdict == "matched" else 55, verify_status=verdict)
    conn.commit()
    return {"status": "found", "url": url, "verdict": verdict, "title": r["title"],
            "queries": queries}


# ------------------------------------------------------------------ emails

# Sites that print an "email format" for a company next to made-up examples
# (first.last@firm.com, j.doe@firm.com) or addresses they guessed themselves.
# Neither is an address anybody published.
EMAIL_FORMAT_HOSTS = (
    "rocketreach.co", "zoominfo.com", "signalhire.com", "contactout.com", "leadiq.com",
    "apollo.io", "lusha.com", "hunter.io", "email-format.com", "emailformat.com",
    "datanyze.com", "clearbit.com", "adapt.io", "snov.io", "getemail.io",
    "voilanorbert.com", "anymailfinder.com", "skrapp.io", "salesintel.io", "uplead.com",
    "seamless.ai", "kendoemailapp.com", "emailcrawlr.com", "findthatlead.com",
    "lead411.com", "prospeo.io", "aeroleads.com", "cognism.com", "kaspr.io",
    "contactsout.com", "rocketreach.com", "zoominfo.co", "theorg.com", "craft.co",
    "growjo.com", "leadgenius.com", "salesql.com", "swordfish.ai", "fullenrich.com",
)
_PLACEHOLDER_LOCALS = {
    "first", "last", "firstname", "lastname", "first.last", "firstlast", "flast",
    "firstl", "f.last", "first_last", "first-last", "lastf", "last.first", "jdoe",
    "johndoe", "john.doe", "jane.doe", "janedoe", "j.doe", "john.smith", "jsmith",
    "jane.smith", "name", "yourname", "your.name", "username", "user", "email", "example",
    "someone", "somebody", "test", "abc", "xyz", "fname", "lname", "fname.lname",
}
_EXAMPLE_CUE = re.compile(r"(format|e\.g\.|for example|example|pattern|such as)\W*$",
                          re.I)
_EMAIL_RE = re.compile(r"(?<![\w.+-])([a-z0-9][a-z0-9._%+-]{0,63})@([a-z0-9.-]+\.[a-z]{2,})",
                       re.I)


def email_queries(domain: str) -> list[str]:
    return [f'"@{domain}"', f'"@{domain}" email']


def _format_site(href: str) -> bool:
    host = (urlparse(href or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in EMAIL_FORMAT_HOSTS)


def emails_in(result: dict, domain: str) -> list[str]:
    """Addresses at exactly `domain` printed in one result's title or snippet."""
    if _format_site(result.get("href", "")):
        return []
    out = []
    for field in ("title", "body"):
        text = result.get(field) or ""
        for m in _EMAIL_RE.finditer(text):
            local, dom = m.group(1).lower().strip("."), m.group(2).lower().strip(".")
            if dom != domain:
                continue
            if local in _PLACEHOLDER_LOCALS or "*" in text[max(0, m.start() - 3):m.start()]:
                continue
            if _EXAMPLE_CUE.search(text[max(0, m.start() - 40):m.start()]):
                continue
            email = f"{local}@{dom}"
            ok, _cat, _why = harvest.classify(email, domain)
            if ok and email not in out:
                out.append(email)
    return out


def roster_names(conn, crd: str) -> list[tuple[str, str]]:
    """(person_key, display name) for everyone on the firm's current roster."""
    rows = conn.execute("SELECT e.indvl_pk, p.name FROM person_employment e"
                        " JOIN person p ON p.indvl_pk=e.indvl_pk"
                        " WHERE e.org_pk=? AND e.kind='current'", (crd,)).fetchall()
    conn.commit()
    return [(f"i:{r['indvl_pk']}", r["name"]) for r in rows if r["name"]]


def find_published_emails(conn, crd: str, domain: str, *, searcher: Searcher | None = None,
                          people: list[tuple[str, str]] | None = None) -> dict:
    """Search for addresses published at the firm's domain and store each one,
    on the roster person it belongs to when exactly one person's name fits it
    (jdoe@ for Jane Doe), else at firm level. Stored unverified; the
    verification job checks them. Returns {status, emails, queries}."""
    domain = (domain or "").lower().strip(".")
    if not domain or "." not in domain:
        return {"status": "skipped", "emails": [], "queries": 0}
    s = searcher or Searcher(conn)
    people = people if people is not None else roster_names(conn, crd)
    # A domain several firms share (a parent's, a broker-dealer's) carries
    # other firms' people too: there only an address that fits someone on
    # this firm's roster is kept.
    shared = conn.execute("SELECT COUNT(*) n FROM firm_current WHERE LOWER(website) LIKE ?",
                          (f"%{domain}%",)).fetchone()["n"] > 1
    conn.commit()
    found: dict[str, dict] = {}
    queries, asked = 0, 0
    for q in email_queries(domain):
        res = s.search(q)
        queries += 1
        if res is None:
            continue
        asked += 1
        for r in res:
            for e in emails_in(r, domain):
                found.setdefault(e, r)
    stored = []
    for email, r in found.items():
        existing = conn.execute(
            "SELECT person_key, person_name FROM contact_point WHERE crd=? AND kind='email'"
            " AND value=? AND person_key != '' LIMIT 1", (crd, email)).fetchone()
        conn.commit()
        key, name = "", None
        if existing:
            key, name = existing["person_key"], existing["person_name"]
        elif not contacts.is_role_email(email):
            fits = {k: n for k, n in people if harvest.email_fits(email, n)}
            text = f"{r.get('title', '')} {r.get('body', '')}"
            if len(fits) > 1:
                # Two people fit jsmith@; the one the snippet names is it.
                named = {k: n for k, n in fits.items()
                         if (rx := harvest.name_regex(n)) is not None and rx.search(text)}
                fits = named
            if len(fits) == 1:
                key, name = next(iter(fits.items()))
        if shared and not key:
            continue
        ref = f"{r.get('title') or 'Search result'} ({r.get('href')})"
        contacts.upsert(conn, crd, "email", email, "web_search", person_key=key,
                        person_name=name, source_ref=ref[:500], confidence=60,
                        verify_status="unverified")
        stored.append(email)
    conn.commit()
    status = "found" if stored else ("none" if asked else "unsearched")
    return {"status": status, "emails": stored, "queries": queries}
