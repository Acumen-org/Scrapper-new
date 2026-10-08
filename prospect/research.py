"""AI contact research: published details for the people every free source missed.

After the filings, the firm's own website, the directories and the email hunt
have each had their go, some people still have no confirmed email, no direct
or mobile line, or no LinkedIn profile. For the best-placed of them, this asks
the configured AI to find what the person or their firm has PUBLISHED: a bio
page with an email on it, a team page with a direct line, a LinkedIn profile.

Trust nothing unchecked. A model can invent an address as fluently as it can
find one, so every claim must name the page it came from, and that page is
fetched again here, by us, and the value has to be on it, next to the person's
name, before anything is stored:

  email     source ai_web, verify_status unverified, then straight into the same
            strict mailbox check as everything else (prospect.verify). It shows
            on a screen only once a mail server confirms it.
  phone     source ai_web, the page in source_ref. Never a number the firm
            already has as its main or office line.
  linkedin  kind linkedin, matched when the page title (or the search result's
            title) names both the person and the firm, otherwise probable.

How the AI looks depends on the provider. Claude, through Anthropic's own API,
searches and reads the web itself (server tools web_search and web_fetch) and
reports what it found as JSON with the page and the exact text for each item.
Other providers (Eden AI, OpenAI-compatible) have no such tools, so the
evidence is gathered here instead (the firm's pages the website crawler cached,
and search results when prospect.websearch is installed) and the model is
asked only to pick the person's details out of it. Weaker models fail at that
now and then; the answer parser forgives the usual slips, a plain rule pass
over the same pages runs regardless, and a run stops early when the model's
answers keep being unusable rather than spending the allowance on them.

A person is researched at most once in 60 days (a failed attempt, once a day),
within ai.daily_limit, keeping a quarter of the day's allowance for people
asking questions in the app.
"""

from __future__ import annotations

import gzip
import html as _html
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import ai, contacts, emailguess, mailcheck

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_research (
    crd           TEXT NOT NULL,
    person_key    TEXT NOT NULL,
    person_name   TEXT,
    researched_at TEXT NOT NULL,
    status        TEXT NOT NULL,      -- ok | nothing | rules_only | error
    provider      TEXT,
    emails        INTEGER NOT NULL DEFAULT 0,
    phones        INTEGER NOT NULL DEFAULT 0,
    linkedin      INTEGER NOT NULL DEFAULT 0,
    rejected      INTEGER NOT NULL DEFAULT 0,   -- claims whose page did not bear them out
    detail        TEXT,
    PRIMARY KEY (crd, person_key)
);
CREATE INDEX IF NOT EXISTS ix_air_at ON ai_research (researched_at);
"""

RESEARCH_DAYS = 60
ERROR_RETRY_HOURS = 24
RESERVE_SHARE = 0.25          # of ai.daily_limit, left for people using the app
RESERVE_MIN = 20
CALLS_PER_PERSON = 5          # research, up to three continuations, one JSON repair
WEAK_STREAK = 3               # unusable answers in a row before the run stops
EVIDENCE_PAGES = 4
EVIDENCE_CHARS = 14000
NEAR_CHARS = 400              # how close to the name a value must sit on its page
DIRECT_LABELS = ("direct", "mobile")

_ITEM = {"type": "object",
         "properties": {"value": {"type": "string"}, "source_url": {"type": "string"},
                        "quote": {"type": "string"}},
         "required": ["value", "source_url", "quote"], "additionalProperties": False}
_PHONE = {"type": "object",
          "properties": {"value": {"type": "string"},
                         "label": {"type": "string",
                                   "enum": ["direct", "mobile", "office", "main", "other"]},
                         "source_url": {"type": "string"}, "quote": {"type": "string"}},
          "required": ["value", "label", "source_url", "quote"], "additionalProperties": False}
RESULT_SCHEMA = {
    "type": "object",
    "properties": {"emails": {"type": "array", "items": _ITEM},
                   "phones": {"type": "array", "items": _PHONE},
                   "linkedin": {"type": "array", "items": _ITEM},
                   "title": {"type": "array", "items": _ITEM}},
    "required": ["emails", "phones", "linkedin", "title"],
    "additionalProperties": False,
}

_RULES = (
    "Report only details a page states outright for this specific person: their own work "
    "email, their direct or mobile phone, their LinkedIn profile URL and their job title. "
    "For every item give the full URL of the page that shows it and quote the exact text on "
    "that page that contains it. Never infer or construct an email address from a naming "
    "pattern. Never report a shared inbox (info@, contact@) or the firm's main switchboard "
    "as the person's. If a page names someone with the same name at a different firm, it "
    "is not this person. If nothing reliable is found, return empty lists.")

WEB_SYSTEM = (
    "You research publicly published professional contact details of people who work at "
    "US registered investment adviser firms, for a business-to-business sales team. Use "
    "web search and web fetch: the firm's own website (team and bio pages), the person's "
    "LinkedIn profile, industry directories and press releases. " + _RULES
    + " Your final message must be one JSON object and nothing else, with four lists: "
    "emails [{value, source_url, quote}], phones [{value, label (direct, mobile, office, "
    "main or other), source_url, quote}], linkedin [{value, source_url, quote}] and title "
    "[{value, source_url, quote}].")

EXTRACT_SYSTEM = (
    "You pick out one person's published contact details from web pages someone else has "
    "already collected. Use only the page excerpts given; never use outside knowledge. "
    + _RULES)

# Words too common in firm names to show that a page names THIS firm.
_FIRM_STOP = {"capital", "advisors", "advisers", "advisory", "management", "financial",
              "wealth", "group", "partners", "investment", "investments", "asset", "assets",
              "company", "services", "planning", "associates", "fund", "funds", "trust",
              "global", "strategies", "strategic", "private", "family", "office", "holdings",
              "securities", "corporation", "corp", "inc", "llc", "llp", "ltd", "the", "and",
              "of", "lp", "co", "pc", "na", "usa", "america", "american", "national"}
_SOCIAL = ("linkedin.", "facebook.", "twitter.", "x.com", "instagram.", "youtube.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def init(conn) -> None:
    """Create the research log. Asks the catalogue first, so it costs nothing
    once the table exists."""
    try:
        r = conn.execute("SELECT to_regclass('ix_air_at') IS NOT NULL AS ok").fetchone()
        if r and r["ok"]:
            return
    except Exception:
        conn.rollback()
    conn.executescript(SCHEMA)
    conn.commit()


# ------------------------------------------------------------------ who to research

@dataclass
class Target:
    crd: str
    key: str
    name: str
    first: str
    last: str
    title: str | None
    firm: str
    city: str
    state: str
    website: str
    domain: str | None
    aliases: set = field(default_factory=set)
    missing: list = field(default_factory=list)       # email | phone | linkedin
    known: dict = field(default_factory=dict)          # kind -> values on file
    # How colleagues are named on a page: their surname, or their first name
    # when they share this person's surname. A value printed nearer to one of
    # them than to this person is theirs.
    colleagues: set = field(default_factory=set)


def _have(conn, crds: list[str]) -> dict[tuple, dict]:
    """(crd, person_key) -> {email, phone, linkedin: bool}, from what screens show."""
    out: dict[tuple, dict] = {}
    for i in range(0, len(crds), 500):
        chunk = crds[i:i + 500]
        for r in conn.execute(
                f"SELECT crd, person_key, kind, label FROM usable_contact_point"
                f" WHERE crd IN ({','.join('?' * len(chunk))}) AND person_key != ''",
                chunk).fetchall():
            h = out.setdefault((r["crd"], r["person_key"]), {})
            if r["kind"] == "phone":
                if r["label"] not in contacts.SHARED_PHONE_LABELS:
                    h["phone"] = True
            else:
                h[r["kind"]] = True
    return out


def _missing(have: dict) -> list[str]:
    return [k for k in ("email", "phone", "linkedin") if not have.get(k)]


def _firm(conn, crd: str) -> dict:
    r = conn.execute("SELECT legal_name, business_name, city, state, website FROM firm_current"
                     " WHERE crd=?", (crd,)).fetchone()
    return dict(r) if r else {}


def _colleagues(people, me_key: str, first: str, last: str) -> set[str]:
    out = set()
    for p in people:
        if p.key == me_key or not p.last:
            continue
        if p.last != last:
            out.add(p.last)
        elif p.first and p.first != first:
            out.add(p.first)
    return out


def _target(conn, crd: str, key: str, name: str, title: str | None, firm: dict,
            missing: list, aliases=(), people=None) -> Target | None:
    parts = emailguess.parse_full(name or "")
    if not parts or not parts[0] or not parts[2]:
        return None
    t = Target(crd=crd, key=key, name=name, first=parts[0], last=parts[2], title=title,
               firm=(firm.get("business_name") or firm.get("legal_name") or "").strip(),
               city=(firm.get("city") or "").title(), state=firm.get("state") or "",
               website=(firm.get("website") or "").strip(), domain=None,
               aliases=set(aliases), missing=missing)
    t.domain = emailguess.domain_for(conn, crd)
    if people is None:
        from . import hunt
        people = hunt.firm_people(conn, crd)
    t.colleagues = _colleagues(people, key, t.first, t.last)
    keys = [key] + list(t.aliases)
    for r in conn.execute(
            f"SELECT kind, value FROM contact_point WHERE crd=? AND person_key IN"
            f" ({','.join('?' * len(keys))})", [crd] + keys).fetchall():
        t.known.setdefault(r["kind"], []).append(r["value"])
    if not t.title:
        r = conn.execute("SELECT title FROM contact_point WHERE crd=? AND person_key=? AND"
                         " title IS NOT NULL LIMIT 1", (crd, key)).fetchone()
        t.title = r["title"] if r else None
    return t


def targets(conn, limit: int, *, crd: str | None = None, force: bool = False) -> list[Target]:
    """The people to research next, best firms and officers first, missing an
    email before missing only a phone or a profile. With crd, that firm's
    people whether or not the email hunt has reached them yet."""
    cut, cut_err = _ago(days=RESEARCH_DAYS), _ago(hours=ERROR_RETRY_HOURS)
    recent = {(r["crd"], r["person_key"]) for r in conn.execute(
        "SELECT crd, person_key FROM ai_research WHERE researched_at >= ?"
        " AND (status != 'error' OR researched_at >= ?)"
        + (" AND crd = ?" if crd else ""),
        (cut, cut_err, crd) if crd else (cut, cut_err)).fetchall()} if not force else set()
    out: list[Target] = []
    if crd:
        from . import hunt
        firm = _firm(conn, crd)
        people = hunt.firm_people(conn, crd)
        have = _have(conn, [crd])
        for p in people:
            if (crd, p.key) in recent:
                continue
            h: dict = {}
            for k in [p.key] + list(p.aliases):
                for kind, v in have.get((crd, k), {}).items():
                    h[kind] = h.get(kind) or v
            miss = _missing(h)
            if miss:
                t = _target(conn, crd, p.key, p.name, p.title, firm, miss, p.aliases,
                            people=people)
                if t:
                    out.append(t)
            if len(out) >= limit:
                break
        return out
    rows = conn.execute(
        "SELECT h.crd, h.person_key, h.person_name, h.state FROM email_hunt h"
        " JOIN firm_scope s ON s.crd = h.crd"
        " WHERE h.state NOT IN ('queued', 'searching') AND COALESCE(h.person_name, '') != ''"
        " AND NOT EXISTS (SELECT 1 FROM ai_research r WHERE r.crd = h.crd"
        "   AND r.person_key = h.person_key AND r.researched_at >= ?"
        "   AND (r.status != 'error' OR r.researched_at >= ?))"
        " ORDER BY s.priority DESC NULLS LAST, h.crd, (h.state = 'found'), h.rank, h.person_key"
        " LIMIT ?", (cut, cut_err, max(limit * 20, 200))).fetchall()
    have = _have(conn, sorted({r["crd"] for r in rows}))
    firms: dict[str, dict] = {}
    staff: dict[str, list] = {}
    for r in rows:
        miss = _missing(have.get((r["crd"], r["person_key"]), {}))
        if not miss:
            continue
        firm = firms.get(r["crd"]) or firms.setdefault(r["crd"], _firm(conn, r["crd"]))
        if r["crd"] not in staff:
            from . import hunt
            staff[r["crd"]] = hunt.firm_people(conn, r["crd"])
        t = _target(conn, r["crd"], r["person_key"], r["person_name"], None, firm, miss,
                    people=staff[r["crd"]])
        if t:
            out.append(t)
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ pages

def _plain(s: str) -> str:
    """Lowercase, accents folded, spacing kept: for finding names and values."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower()


@dataclass
class Page:
    url: str
    html: str
    text: str
    title: str
    via: str                       # cache | fetch | search


def _title_of(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.S | re.I)
    return " ".join(_html.unescape(m.group(1)).split())[:200] if m else ""


def _visible(html: str) -> str:
    try:
        from .harvest import visible_text
        return visible_text(html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html or "")


class Reader:
    """Pages for one run, each fetched once: ours from the crawler's cache when
    it holds them, everything else live, obeying robots.txt as Settings says."""

    def __init__(self) -> None:
        self._pages: dict[str, Page | None] = {}
        self._crawler = None
        self._searcher = None
        self._no_search = False

    def search(self, conn, query: str, n: int = 5) -> list[dict]:
        """Search results [{url, title, snippet}] through prospect.websearch
        (cached, rate limited, never raises). Empty when that module is not
        installed or its searcher is resting after being refused."""
        if self._no_search:
            return []
        if self._searcher is None:
            try:
                from . import websearch
                websearch.init(conn)
                self._searcher = websearch.Searcher(conn)
            except Exception:
                conn.rollback()
                self._no_search = True
                return []
        try:
            res = self._searcher.search(query) or []
        except Exception:
            return []
        out = []
        for r in res[:n]:
            url = r.get("href") or r.get("url") if isinstance(r, dict) else None
            if url:
                out.append({"url": str(url), "title": str(r.get("title") or ""),
                            "snippet": str(r.get("body") or r.get("snippet") or "")})
        return out

    def _crawl(self):
        if self._crawler is None:
            from .crawl import Crawler
            self._crawler = Crawler(use_browser=False)
        return self._crawler

    def cached(self, conn, crd: str) -> list[Page]:
        out = []
        try:
            rows = conn.execute("SELECT url, cache_path FROM web_page WHERE crd=?"
                                " AND cache_path IS NOT NULL", (crd,)).fetchall()
        except Exception:
            conn.rollback()
            return out
        for r in rows:
            page = self._from_cache(r["url"], r["cache_path"], crd)
            if page:
                out.append(page)
        return out

    def _from_cache(self, url: str, path: str, crd: str) -> Page | None:
        from . import config
        for p in (Path(path), config.DATA_DIR / "web_cache" / crd / Path(path).name):
            try:
                raw = gzip.decompress(p.read_bytes()).decode("utf-8", "replace")
            except (OSError, EOFError, ValueError):
                continue
            page = Page(url=url, html=raw, text=_visible(raw), title=_title_of(raw), via="cache")
            self._pages.setdefault(url, page)
            return page
        return None

    def get(self, url: str, conn=None, crd: str | None = None) -> Page | None:
        """The page live, else our own cached copy of it. None when neither."""
        if url in self._pages and (self._pages[url] is None or self._pages[url].via != "cache"):
            return self._pages[url]
        page = None
        host = (urlparse(url).hostname or "").lower()
        if url.startswith(("http://", "https://")) and host and not any(
                s in host for s in _SOCIAL):
            try:
                c = self._crawl()
                if c.allowed(url):
                    pg = c.fetch(url)
                    if pg.ok:
                        page = Page(url=url, html=pg.html, text=pg.text or _visible(pg.html),
                                    title=_title_of(pg.html), via="fetch")
            except Exception:
                page = None
        if page is None and conn is not None and crd:
            r = conn.execute("SELECT cache_path FROM web_page WHERE url=? AND cache_path IS NOT"
                             " NULL", (url,)).fetchone()
            if r:
                page = self._from_cache(url, r["cache_path"], crd)
        self._pages[url] = page
        return page

    def close(self) -> None:
        if self._crawler is not None:
            self._crawler.close()
            self._crawler = None


# ------------------------------------------------------------------ name and value checks

def _name_spots(t: Target, text: str) -> list[int]:
    """Where the person's surname sits on a page, when their first name (or a
    nickname for it) is on the page too. Empty means the page is not about them."""
    pt = _plain(text)
    last = re.escape(_plain(t.last))
    firsts = {t.first} | set(emailguess.NICK_FORMS.get(t.first, ()))
    if not any(re.search(rf"\b{re.escape(f)}\b", pt) for f in firsts if f):
        return []
    return [m.start() for m in re.finditer(rf"\b{last}\b", pt)]


def _near(spots: list[int], at: list[int], within: int = NEAR_CHARS) -> bool:
    return any(abs(a - s) <= within for a in at for s in spots)


def _word_at(pt: str, word: str) -> list[int]:
    return [m.start() for m in re.finditer(rf"\b{re.escape(word)}\b", pt)] if word else []


def _mine(t: Target, pt: str, at: list[int], within: int = NEAR_CHARS) -> bool:
    """A value at these positions of the plain text sits within reach of this
    person's name, and nearer to it than to any colleague's: on a team page
    the number under the next card belongs to the next person."""
    me = _word_at(pt, _plain(t.last))
    others = [o for c in t.colleagues for o in _word_at(pt, c)]
    for a in at:
        d_me = min((abs(a - s) for s in me), default=None)
        if d_me is None or d_me > within:
            continue
        if all(d_me < abs(a - o) for o in others):
            return True
    return False


def _cf_emails(html: str) -> list[str]:
    """Addresses Cloudflare's email protection hides in data-cfemail."""
    out = []
    for hexs in re.findall(r'data-cfemail="([0-9a-fA-F]+)"', html or ""):
        try:
            key = int(hexs[:2], 16)
            out.append("".join(chr(int(hexs[i:i + 2], 16) ^ key)
                               for i in range(2, len(hexs), 2)).lower())
        except ValueError:
            continue
    return out


def _email_spots(page: Page, email: str) -> list[int] | None:
    """Positions of the address in the page's text (deobfuscated), [] when it is
    only in the markup (a mailto link), None when it is nowhere."""
    text = _plain(page.text)
    variants = [text, re.sub(r"\s*[\[(]\s*at\s*[\])]\s*|\s+at\s+", "@",
                             re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*|\s+dot\s+", ".", text))]
    for v in variants:
        at = [m.start() for m in re.finditer(re.escape(email), v)]
        if at:
            return at
    raw = _plain(_html.unescape(page.html or ""))
    if email in raw or email in _cf_emails(page.html):
        return []
    return None


_PHONE_RE = re.compile(r"(?:\+?1[\s.\-]*)?\(?\d{3}\)?[\s.\-]*\d{3}[\s.\-]*\d{4}")


def _phone_spots(page: Page, phone: str) -> list[int] | None:
    want = re.sub(r"\D", "", phone)[:10]
    at = [m.start() for m in _PHONE_RE.finditer(_plain(page.text))
          if (contacts.norm_phone(m.group(0)) or "") and
          re.sub(r"\D", "", contacts.norm_phone(m.group(0)))[:10] == want]
    if at:
        return at
    for m in re.finditer(r'tel:([+\d().\-\s]{7,25})', page.html or "", re.I):
        p = contacts.norm_phone(m.group(1))
        if p and re.sub(r"\D", "", p)[:10] == want:
            return []
    return None


def _names_firm(t: Target, title: str) -> bool:
    """A page or result title that names both the person and the firm."""
    pt = _plain(title)
    if not (re.search(rf"\b{re.escape(_plain(t.last))}\b", pt)
            and any(re.search(rf"\b{re.escape(f)}\b", pt)
                    for f in {t.first} | set(emailguess.NICK_FORMS.get(t.first, ())))):
        return False
    words = [w for w in re.findall(r"[a-z]{3,}", _plain(t.firm)) if w not in _FIRM_STOP]
    host = (urlparse(t.website if "://" in t.website else "http://" + t.website).hostname
            or "").lower() if t.website else ""
    reg = host[4:] if host.startswith("www.") else host
    return any(re.search(rf"\b{re.escape(w)}\b", pt) for w in words) or (
        bool(reg) and reg.split(".")[0] in pt.replace(" ", ""))


# ------------------------------------------------------------------ checking claims

@dataclass
class Verdict:
    kind: str
    value: str
    url: str
    ok: bool
    why: str
    label: str | None = None
    status: str | None = None          # linkedin: matched | probable


def check_claims(t: Target, data: dict, sources: list[dict], reader: Reader, conn=None
                 ) -> list[Verdict]:
    """Hold every claim against its own page. Only what the page bears out,
    next to the person's name, passes."""
    out: list[Verdict] = []
    firm_phones = set()
    if conn is not None:
        firm_phones = {r["value"] for r in conn.execute(
            "SELECT value FROM contact_point WHERE crd=? AND kind='phone'"
            " AND (person_key='' OR label IN ('main','office','toll_free'))",
            (t.crd,)).fetchall()}
    seen_urls = {s["url"]: s for s in sources if s.get("url")}
    done: set = set()

    def once(kind, item) -> bool:
        key = (kind, str((item or {}).get("value") if isinstance(item, dict) else item).lower())
        if key in done:
            return False
        done.add(key)
        return True
    for item in [i for i in (data.get("emails") or []) if once("e", i)][:6]:
        out.append(_check_email(t, item, reader, conn))
    for item in [i for i in (data.get("phones") or []) if once("p", i)][:6]:
        out.append(_check_phone(t, item, reader, conn, firm_phones))
    for item in [i for i in (data.get("linkedin") or []) if once("l", i)][:3]:
        out.append(_check_linkedin(t, item, reader, conn, seen_urls))
    return out


def _src(item) -> tuple[str, str]:
    if not isinstance(item, dict):
        return "", ""
    return str(item.get("value") or "").strip(), str(item.get("source_url") or "").strip()


def _check_email(t: Target, item, reader: Reader, conn) -> Verdict:
    value, url = _src(item)
    email = contacts.norm_email(value)
    v = Verdict("email", email, url, False, "")
    if not mailcheck.valid_syntax(email):
        v.why = "not an email address"
        return v
    dom = email.rsplit("@", 1)[1]
    if contacts.is_role_email(email) or any(b in dom for b in _SOCIAL):
        v.why = "a shared inbox, not the person's"
        return v
    page = reader.get(url, conn, t.crd) if url else None
    if page is None:
        v.why = "its page could not be read"
        return v
    spots = _name_spots(t, page.text)
    if not spots:
        v.why = "its page does not name the person"
        return v
    at = _email_spots(page, email)
    if at is None:
        v.why = "not on its page"
        return v
    local = email.split("@", 1)[0]
    fitted = emailguess._detect(t.first, emailguess.fold(t.last), local) or (
        emailguess.fold(t.last) in local)
    near = bool(at) and _mine(t, _plain(page.text), at)
    if not at and not fitted:
        # Only in a mailto link: measure the distance in the markup instead,
        # with room for the tags between a name and its link.
        raw = _plain(_html.unescape(page.html or ""))
        near = _mine(t, raw, [m.start() for m in re.finditer(re.escape(email), raw)],
                     NEAR_CHARS * 4)
    if not fitted and not near:
        v.why = "on its page, but not beside the person's name"
        return v
    v.ok, v.why = True, "on its page beside the person's name"
    return v


def _check_phone(t: Target, item, reader: Reader, conn, firm_phones: set) -> Verdict:
    value, url = _src(item)
    phone = contacts.norm_phone(value) or ""
    label = (item.get("label") if isinstance(item, dict) else None) or None
    v = Verdict("phone", phone, url, False, "", label=label if label in
                ("direct", "mobile", "office", "main") else None)
    if not phone:
        v.why = "not a US phone number"
        return v
    if phone in firm_phones or (label or "") == "main":
        v.why = "the firm's own line, already on file"
        return v
    page = reader.get(url, conn, t.crd) if url else None
    if page is None:
        v.why = "its page could not be read"
        return v
    spots = _name_spots(t, page.text)
    at = _phone_spots(page, phone) if spots else None
    if not spots:
        v.why = "its page does not name the person"
    elif at is None:
        v.why = "not on its page"
    elif not (at and _mine(t, _plain(page.text), at)):
        v.why = "on its page, but not beside the person's name"
    else:
        v.ok, v.why = True, "on its page beside the person's name"
    return v


def _check_linkedin(t: Target, item, reader: Reader, conn, seen: dict) -> Verdict:
    value, url = _src(item)
    li = contacts.norm_linkedin(value)
    v = Verdict("linkedin", li or value, url, False, "")
    if not li or "/in/" not in li:
        v.why = "not a LinkedIn profile address"
        return v
    slug = li.rsplit("/", 1)[1]
    titles: list[str] = []
    # Evidence one: a search engine returned this very profile.
    for s_url, s in seen.items():
        if contacts.norm_linkedin(s_url) == li:
            titles.append(s.get("title") or "")
    # Evidence two: the cited page (not LinkedIn itself) links to it.
    page = None
    if url and contacts.norm_linkedin(url) != li:
        page = reader.get(url, conn, t.crd)
        if page is not None and f"linkedin.com/in/{slug}" in _plain(page.html):
            if _name_spots(t, page.text):
                titles.append(page.title)
            else:
                page = None
        else:
            page = None
    if not titles and page is None:
        v.why = "no search result or page showed this profile"
        return v
    named = [x for x in titles if _name_spots(t, x)]
    if not named and emailguess.fold(t.last) not in slug.replace("-", ""):
        v.why = "the profile's title and address do not name the person"
        return v
    v.ok = True
    v.status = "matched" if any(_names_firm(t, x) for x in titles) else "probable"
    v.why = f"profile {v.status}"
    return v


# ------------------------------------------------------------------ finding claims

def _rule_claims(t: Target, pages: list[Page]) -> dict:
    """What the pages show without any model: name-shaped addresses, numbers
    printed right after the person's name, LinkedIn links whose address names
    them. Every claim cites its page, and still goes through check_claims."""
    out: dict = {"emails": [], "phones": [], "linkedin": [], "title": []}
    last = emailguess.fold(t.last)
    for page in pages:
        if not _name_spots(t, page.text):
            continue
        found = set(re.findall(r"[a-z0-9._%+'\-]+@[a-z0-9.\-]+\.[a-z]{2,}",
                               _plain(_html.unescape(page.html or "")))) | set(
            _cf_emails(page.html))
        for e in sorted(found):
            local = e.split("@", 1)[0]
            if emailguess._detect(t.first, last, local) or (last and last in local):
                out["emails"].append({"value": e, "source_url": page.url, "quote": e})
        pt = _plain(page.text)
        for m in re.finditer(rf"\b{re.escape(_plain(t.last))}\b", pt):
            window = pt[m.end():m.end() + 160]
            pm = _PHONE_RE.search(window)
            if pm and _mine(t, pt, [m.end() + pm.start()]):
                before = window[:pm.start()]
                label = ("mobile" if re.search(r"mobile|cell", before) else
                         "direct" if "direct" in before else "other")
                out["phones"].append({"value": pm.group(0), "label": label,
                                      "source_url": page.url, "quote": pm.group(0)})
        for slug in set(re.findall(r"linkedin\.com/in/([a-z0-9\-_%]+)", _plain(page.html))):
            if last and last in slug.replace("-", ""):
                out["linkedin"].append({"value": f"https://www.linkedin.com/in/{slug}",
                                        "source_url": page.url, "quote": slug})
    return out


def _excerpts(t: Target, pages: list[Page]) -> str:
    """The parts of each page around the person's name, within EVIDENCE_CHARS."""
    chunks, total = [], 0
    for page in pages:
        text = page.text or ""
        spots = _name_spots(t, text)
        if not spots:
            continue
        spans: list[list[int]] = []
        for s in spots:
            a, b = max(0, s - 700), min(len(text), s + 900)
            if spans and a <= spans[-1][1]:
                spans[-1][1] = b
            else:
                spans.append([a, b])
        body = " ... ".join(" ".join(text[a:b].split()) for a, b in spans)[:3500]
        links = sorted(set(re.findall(r"(?:mailto:|tel:)[^\"'>\s]{4,80}|linkedin\.com/in/[^\"'>\s?#]{2,100}",
                                      page.html or "", re.I)))[:30]
        piece = f"PAGE {page.url}\nTITLE {page.title}\nTEXT {body}\nLINKS {' '.join(links)}\n"
        if total + len(piece) > EVIDENCE_CHARS:
            break
        chunks.append(piece)
        total += len(piece)
    return "\n".join(chunks)


def _evidence(conn, t: Target, reader: Reader) -> list[Page]:
    """Pages about the person: the firm's own pages the crawler cached, most
    mentions first, then search results when a search module is installed."""
    pages = [p for p in reader.cached(conn, t.crd) if _name_spots(t, p.text)]
    pages.sort(key=lambda p: -len(_name_spots(t, p.text)))
    pages = pages[:EVIDENCE_PAGES]
    for r in reader.search(conn, f'"{t.name}" "{t.firm}"', 5):
        if len(pages) >= EVIDENCE_PAGES + 3:
            break
        if any(s in (urlparse(r["url"]).hostname or "") for s in _SOCIAL):
            pages.append(Page(url=r["url"], html=r["url"], text=f"{r['title']} {r['snippet']}",
                              title=r["title"], via="search"))
            continue
        page = reader.get(r["url"], conn, t.crd)
        if page is not None and _name_spots(t, page.text):
            pages.append(page)
    return pages


def _prompt(t: Target) -> str:
    known = "; ".join(f"{k}: {', '.join(v[:3])}" for k, v in t.known.items()) or "nothing"
    site = t.website if not t.website or "://" in t.website else "https://" + t.website
    return (f"Person: {t.name}\nTitle on file: {t.title or 'unknown'}\n"
            f"Firm: {t.firm} (SEC CRD {t.crd}), {t.city} {t.state}\n"
            f"Firm website: {site or 'unknown'}\n"
            f"Already on file for this person: {known}\n"
            f"Still missing: {', '.join(t.missing)}\n"
            f"Find what is missing, then reply with only the JSON object.")


def _fatal(e: ai.AIError) -> bool:
    """An error that will repeat for every person: stop the run."""
    d = (e.detail or str(e)).lower()
    return any(x in d for x in ("http 401", "http 403", "http 404", "allowance",
                                "not available on provider", "no ai provider", "unknown provider"))


def _merge(a: dict, b: dict) -> dict:
    return {k: list((a or {}).get(k) or []) + list((b or {}).get(k) or [])
            for k in ("emails", "phones", "linkedin", "title")}


def research_person(conn, t: Target, reader: Reader) -> dict:
    """Find, check and store one person's details. Returns counts and a
    status; raises AIError only for errors that will repeat for everyone."""
    p = ai.provider()
    res = {"status": "nothing", "emails": 0, "phones": 0, "linkedin": 0, "rejected": 0,
           "weak": False, "detail": ""}
    sources: list[dict] = []
    data: dict = {}
    ai_failed = None
    pages: list[Page] = []
    if p == "anthropic":
        try:
            out = ai.web_research(WEB_SYSTEM, _prompt(t), who="research")
            sources = out["sources"]
            try:
                data = ai._parse_json(out["text"], RESULT_SCHEMA)
            except ai.AIError:
                data = ai.complete(
                    "Turn a research note into JSON. Keep only items the note gives with a "
                    "page URL; invent nothing.",
                    [{"role": "user", "content": out["text"][:12000]}], feature="research",
                    tier="fast", schema=RESULT_SCHEMA, max_tokens=4000, who="research")
        except ai.AIError as e:
            if _fatal(e):
                raise
            ai_failed = e
    else:
        pages = _evidence(conn, t, reader)
        sources = [{"url": pg.url, "title": pg.title, "via": pg.via} for pg in pages]
        excerpt = _excerpts(t, pages)
        if excerpt:
            try:
                data = ai.complete(
                    EXTRACT_SYSTEM,
                    [{"role": "user", "content": _prompt(t) + "\n\nPages:\n" + excerpt}],
                    feature="research", tier="fast", schema=RESULT_SCHEMA, max_tokens=3000,
                    who="research")
            except ai.AIError as e:
                if _fatal(e):
                    raise
                ai_failed = e
    claims = _merge(data, _rule_claims(t, pages))
    verdicts = check_claims(t, claims, sources, reader, conn)
    stored = _store(conn, t, verdicts, claims)
    for k in ("emails", "phones", "linkedin"):
        res[k] = stored[k]
    res["rejected"] = sum(1 for v in verdicts if not v.ok)
    why = Counter(v.why for v in verdicts if not v.ok)
    res["detail"] = "; ".join(f"{n} {w}" for w, n in why.most_common(4))
    if ai_failed is not None:
        res["weak"] = "json" in (ai_failed.detail or "").lower() or "empty" in (
            ai_failed.detail or "").lower()
        res["status"] = "rules_only" if any(stored.values()) else "error"
        res["detail"] = (f"AI: {ai_failed.detail[:160]}; " + res["detail"]).strip("; ")
    elif any(stored.values()):
        res["status"] = "ok"
    return res


def _store(conn, t: Target, verdicts: list[Verdict], claims: dict) -> dict:
    n = {"emails": 0, "phones": 0, "linkedin": 0}
    title = None
    for item in claims.get("title") or []:
        val = str((item or {}).get("value") or "").strip() if isinstance(item, dict) else ""
        if 2 < len(val) <= 80:
            title = val
            break
    new_ids = []
    for v in verdicts:
        if not v.ok:
            continue
        if v.kind == "email":
            contacts.upsert(conn, t.crd, "email", v.value, "ai_web", person_key=t.key,
                            person_name=t.name, title=title or t.title, source_ref=v.url,
                            confidence=55, is_role=False)
            r = conn.execute("SELECT id, verify_status FROM contact_point WHERE crd=?"
                             " AND kind='email' AND value=? AND person_key=?",
                             (t.crd, v.value, t.key)).fetchone()
            if r and r["verify_status"] in ("unverified", "queued"):
                new_ids.append(r["id"])
            n["emails"] += 1
        elif v.kind == "phone":
            n["phones"] += contacts.upsert(conn, t.crd, "phone", v.value, "ai_web",
                                           person_key=t.key, person_name=t.name,
                                           title=title or t.title, label=v.label,
                                           source_ref=v.url, confidence=60) or 0
        elif v.kind == "linkedin":
            n["linkedin"] += contacts.upsert(conn, t.crd, "linkedin", v.value, "ai_web",
                                             person_key=t.key, person_name=t.name,
                                             title=title or t.title, source_ref=v.url,
                                             confidence=60, verify_status=v.status) or 0
    conn.commit()
    if new_ids:
        _verify_found(conn, t, new_ids)
    return n


def _verify_found(conn, t: Target, ids: list[int]) -> None:
    """An address found on a page still needs the mail server's word before any
    screen shows it: the same strict check as everything else, now. Where no
    check can run here, the email hunt asks about it first on its next pass."""
    from . import hunt, verify
    try:
        eng, _auto = verify.resolve_engine()
    except Exception:
        eng = "dns"
    if eng == "dns":
        hunt.queue_person(conn, t.crd, t.key)
        conn.commit()
        return
    verify.verify_contacts(conn, ids)
    for r in conn.execute(f"SELECT value, verify_status FROM contact_point WHERE id IN"
                          f" ({','.join('?' * len(ids))})", ids).fetchall():
        if r["verify_status"] == "valid":
            hunt.init(conn)
            conn.execute("UPDATE email_hunt SET state='found', found=?, detail=?,"
                         " next_try_at=NULL, updated_at=? WHERE crd=? AND person_key=?",
                         (r["value"], "Found by AI research on a published page and confirmed"
                          " by the mail server", _now(), t.crd, t.key))
        elif r["verify_status"] in ("unknown", "risky"):
            hunt.queue_person(conn, t.crd, t.key)
    conn.commit()


def _log(conn, t: Target, res: dict) -> None:
    conn.execute(
        "INSERT INTO ai_research (crd, person_key, person_name, researched_at, status, provider,"
        " emails, phones, linkedin, rejected, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT (crd, person_key) DO UPDATE SET person_name=excluded.person_name,"
        " researched_at=excluded.researched_at, status=excluded.status,"
        " provider=excluded.provider, emails=excluded.emails, phones=excluded.phones,"
        " linkedin=excluded.linkedin, rejected=excluded.rejected, detail=excluded.detail",
        (t.crd, t.key, t.name, _now(), res["status"], ai.provider(), res["emails"],
         res["phones"], res["linkedin"], res["rejected"], (res.get("detail") or "")[:300]))
    conn.commit()


def reserve() -> int:
    from . import settings
    limit = settings.get_int("ai.daily_limit", 400)
    return max(RESERVE_MIN, int(limit * RESERVE_SHARE))


def run(conn, *, limit: int = 8, seconds: float = 1200, crd: str | None = None,
        force: bool = False) -> Counter:
    """One bounded research pass. Returns aggregate counts only."""
    init(conn)
    stats: Counter = Counter()
    if not ai.enabled("research"):
        stats["not_enabled"] = 1
        return stats
    started = time.monotonic()
    people = targets(conn, limit, crd=crd, force=force)
    conn.commit()
    reader = Reader()
    streak = 0
    try:
        for t in people:
            if time.monotonic() - started > seconds:
                stats["stopped_for_time"] = 1
                break
            if ai.budget_left() < reserve() + CALLS_PER_PERSON:
                stats["stopped_for_budget"] = 1
                break
            try:
                res = research_person(conn, t, reader)
            except ai.AIError as e:
                stats["stopped_for_error"] = 1
                stats["error:" + (e.detail or str(e))[:120]] = 1
                break
            except Exception as e:      # one person must never sink the run
                conn.rollback()
                res = {"status": "error", "emails": 0, "phones": 0, "linkedin": 0,
                       "rejected": 0, "detail": f"{type(e).__name__}: {e}"[:200]}
            _log(conn, t, res)
            stats["people"] += 1
            stats[f"status_{res['status']}"] += 1
            for k in ("emails", "phones", "linkedin", "rejected"):
                stats[k] += res.get(k, 0)
            streak = streak + 1 if res.get("weak") else 0
            if streak >= WEAK_STREAK:
                stats["stopped_weak_model"] = 1
                break
    finally:
        reader.close()
    stats["seconds"] = int(time.monotonic() - started)
    return stats


def summary(stats: Counter) -> str:
    if stats.get("not_enabled"):
        return "AI research is off: no provider, the feature is switched off, or today's allowance is used"
    parts = [f"{stats.get('people', 0)} people researched",
             f"{stats.get('emails', 0)} emails found (sent to the mail-server check)",
             f"{stats.get('phones', 0)} phones", f"{stats.get('linkedin', 0)} LinkedIn profiles",
             f"{stats.get('rejected', 0)} claims rejected by their own pages"]
    parts += [f"{k} {v}" for k, v in sorted(stats.items()) if k.startswith("status_")]
    for k in ("stopped_for_time", "stopped_for_budget", "stopped_weak_model"):
        if stats.get(k):
            parts.append(k.replace("_", " "))
    err = next((k[6:] for k in stats if k.startswith("error:")), None)
    if err:
        parts.append(f"stopped: {err}")
    return f"AI research in {stats.get('seconds', 0)}s: " + ", ".join(parts)
