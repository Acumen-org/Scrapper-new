"""Read each firm's own website for the people who work there: names, titles,
personal emails and direct phone numbers.

No filing carries an adviser's email or direct line; the firm's own team page,
bio pages and vCards are where they live. The first version of this job read
the homepage and five promising links with `requests` and regexes, and mostly
came back with info@ and compliance@. This one:

  - crawls up to `crawl.max_pages` pages per site (prospect.crawl): team pages
    first, then bio pages and vCards, then contact and about pages, with the
    sitemap read for team URLs no menu links to. Fetching goes through
    Scrapling with a real browser's TLS fingerprint, which gets past the WAFs
    that refused the old fetcher; JavaScript-only sites are rendered in a
    headless browser when one is installed.
  - extracts people from team-card layouts, schema.org data, vCards, roster
    names we already hold and name-shaped email addresses (prospect.harvest),
    with titles cleaned so a bio sentence never lands in the title column.
  - writes every contact through prospect.contacts.upsert into contact_point
    (source 'website', or 'vcard' for vCard data), keyed to the person's IAPD
    record when prospect.people can match the name, and still mirrors people
    into web_contact for the screens that read it.
  - learns the firm's email pattern from the addresses it found (jdoe@ for
    Jane Doe, twice, means flast) and writes a pattern guess, clearly labelled
    source 'pattern', for each known person still without an address.
  - keeps the LinkedIn links a site shows: a profile in a person's team card
    or on their bio page becomes that person's LinkedIn, and the company page
    (usually in the footer) the firm's. Both are stored as kind 'linkedin',
    verify_status 'matched', since the firm's own site put them side by side.
  - optionally asks the AI extractor about a team page the rules could not
    parse, when the AI module is configured for it.

Every page fetched is cached to disk, and pages that yielded nothing have
their bytes dropped after the firm is done (the web_page row stays). Firms are
read once, then again after `crawl.recrawl_days`; a re-read replaces what the
site said before, since a person no longer on the team page has usually left.

Commits happen after every page and nothing is ever written while a page is
being fetched: see enrich_one for why.

Several sites are read at once with --parallel N: the firms due are split
into N shards by CRD, and each shard is read by its own process (its own
crawler, browser and database connection), so no two ever read the same
firm and one stuck site holds up only its own shard.

    python -m scripts.web_enrich [--limit N] [--parallel N] [--crd CRD] [--url URL]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import (config, contacts, db, emailguess, harvest, runlog,  # noqa: E402
                      settings, websignals)
from prospect.crawl import Crawler, Page  # noqa: E402

CACHE_DIR = config.DATA_DIR / "web_cache"

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_page (
    url        TEXT PRIMARY KEY,
    crd        TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    status     INTEGER,
    cache_path TEXT
);
CREATE INDEX IF NOT EXISTS ix_webpage_crd ON web_page (crd);
CREATE TABLE IF NOT EXISTS web_contact (
    id         INTEGER PRIMARY KEY,
    crd        TEXT NOT NULL,
    person     TEXT,               -- NULL = firm-level detail
    title      TEXT,               -- title text found next to the name
    email      TEXT,
    phone      TEXT,
    source_url TEXT NOT NULL,
    found_at   TEXT NOT NULL,
    UNIQUE (crd, person, email, phone)
);
CREATE INDEX IF NOT EXISTS ix_webcontact_crd ON web_contact (crd);
CREATE TABLE IF NOT EXISTS web_enrich_state (
    crd        TEXT PRIMARY KEY,
    scanned_at TEXT NOT NULL,
    pages      INTEGER NOT NULL,
    people     INTEGER NOT NULL,
    emails     INTEGER NOT NULL,
    status     TEXT NOT NULL       -- ok | unreachable | no_website | error
);
"""

# Columns added after the table first shipped. Added through db.add_column,
# which asks the catalogue first: ALTER TABLE ... ADD COLUMN IF NOT EXISTS
# takes the table's exclusive lock even when the column is already there, and
# run on every slice it would queue behind readers and then block every page
# that reads this table.
STATE_COLUMNS = (
    ("personal_emails", "INTEGER"),   # distinct personal (non-role) mailboxes found
    ("phones", "INTEGER"),            # direct or mobile lines tied to a person
    ("guesses", "INTEGER"),           # pattern guesses written
    ("method", "TEXT"),               # static | browser | mixed
    ("site", "TEXT"),                 # the address the site settled on
)

AI_PAGES_MAX = 2          # AI extraction calls per firm, at most
GUESSES_MAX = 60          # pattern guesses per firm, at most
FIRM_PHONES_PER_PAGE = 3  # firm-level numbers kept from any one page


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ optional modules

def _people_module(conn):
    """prospect.people when it is importable and its roster is loaded."""
    try:
        from prospect import people
    except Exception:
        return None
    try:
        r = conn.execute("SELECT to_regclass('person') AS t").fetchone()
        if not r or r["t"] is None:
            return None
        if not conn.execute("SELECT 1 FROM person LIMIT 1").fetchone():
            return None
    except Exception:
        conn.rollback()
        return None
    return people


def _ai_module():
    try:
        from prospect import ai
        return ai if ai.enabled("extract") else None
    except Exception:
        return None


# ------------------------------------------------------------------ roster

@dataclass
class Roster:
    names: list[str] = field(default_factory=list)      # display names, for matching
    pk: dict[str, str] = field(default_factory=dict)    # name_key -> 'i:<indvl_pk>'
    titles: dict[str, str] = field(default_factory=dict)
    officers: list[str] = field(default_factory=list)   # Schedule A people, filed order


def load_roster(conn, crd: str, people) -> Roster:
    """Everyone we already know at the firm: the individual feed's roster when
    prospect.people has loaded it, Schedule A officers and owners, and the
    legacy contact table."""
    ro = Roster()
    seen: set[str] = set()

    def add(name: str, pk: str | None = None, title: str | None = None,
            officer: bool = False) -> None:
        name = " ".join((name or "").split())
        key = contacts.name_key(name)
        if not key:
            return
        if pk and key not in ro.pk:
            ro.pk[key] = f"i:{pk}"
        if title and key not in ro.titles:
            ro.titles[key] = title
        if officer and name not in ro.officers:
            ro.officers.append(name)
        if key not in seen:
            seen.add(key)
            ro.names.append(name)

    if people is not None:
        try:
            for name, pk in people.roster_names(conn, crd):
                add(name, pk)
        except Exception:
            conn.rollback()
    for r in conn.execute("SELECT name, title FROM schedule_a WHERE crd=? AND is_individual=1",
                          (crd,)).fetchall():
        title = harvest.clean_title(r["title"]) if r["title"] else None
        add(emailguess.pretty(r["name"]), title=title, officer=True)
    for r in conn.execute("SELECT name FROM contact WHERE crd=?", (crd,)).fetchall():
        add(" ".join((r["name"] or "").split()).title())
    return ro


# ------------------------------------------------------------------ storage helpers

def cache_page(conn, crd: str, url: str, status: int, html: str, now: str) -> None:
    path = None
    if html:
        h = hashlib.sha256(url.encode()).hexdigest()[:20]
        p = CACHE_DIR / crd
        p.mkdir(parents=True, exist_ok=True)
        fp = p / f"{h}.html.gz"
        fp.write_bytes(gzip.compress(html.encode("utf-8", "replace")))
        path = str(fp)
    conn.execute("INSERT OR REPLACE INTO web_page VALUES (?,?,?,?,?)",
                 (url, crd, now, status, path))


def web_contact_row(conn, crd: str, person: str | None, title: str | None,
                    email: str | None, phone: str | None, url: str, now: str) -> None:
    """Mirror one finding into web_contact for the screens that still read it.
    A row seen again is refreshed rather than duplicated (Postgres UNIQUE does
    not dedupe NULLs, so the check is explicit), which is also what lets the
    end of a crawl drop rows the site no longer shows."""
    row = conn.execute(
        "SELECT id FROM web_contact WHERE crd=? AND COALESCE(person,'')=?"
        " AND COALESCE(email,'')=? AND COALESCE(phone,'')=?",
        (crd, person or "", email or "", phone or "")).fetchone()
    if row:
        conn.execute("UPDATE web_contact SET found_at=?, source_url=?,"
                     " title=COALESCE(?, title) WHERE id=?", (now, url, title, row["id"]))
    else:
        conn.execute("INSERT INTO web_contact (crd, person, title, email, phone, source_url,"
                     " found_at) VALUES (?,?,?,?,?,?,?)",
                     (crd, person, title, email, phone, url, now))


def drop_barren_cache(conn, crd: str) -> None:
    """Delete cached HTML for this firm's pages that yielded no contact.

    The web_page row stays; only the bytes go. Most pages fetched produce
    nothing, and keeping their HTML would grow to gigabytes of files that
    answer no question anybody asks."""
    rows = conn.execute("""
        SELECT url, cache_path FROM web_page
        WHERE crd=? AND cache_path IS NOT NULL
          AND url NOT IN (SELECT source_url FROM web_contact WHERE crd=?)
          AND url NOT IN (SELECT source_ref FROM contact_point
                          WHERE crd=? AND source_ref IS NOT NULL)""",
                        (crd, crd, crd)).fetchall()
    for r in rows:
        try:
            Path(r["cache_path"]).unlink()
        except OSError:
            pass
        conn.execute("UPDATE web_page SET cache_path=NULL WHERE url=?", (r["url"],))
    if rows:
        conn.commit()


def pin(conn, crd: str, kind: str, value: str, source: str, **kw) -> None:
    """Record a detail against a person and drop any firm-level copy the
    website gave earlier. contacts.upsert removes that copy only when it
    inserts a new person row; on a re-crawl the person row already exists, so
    the copy written by the contact page this time would otherwise stay."""
    contacts.upsert(conn, crd, kind, value, source, **kw)
    v = contacts.norm_email(value) if kind == "email" else contacts.norm_phone(value)
    if v and kw.get("person_key"):
        conn.execute("DELETE FROM contact_point WHERE crd=? AND kind=? AND value=?"
                     " AND person_key='' AND source IN ('website','vcard','ai')",
                     (crd, kind, v))


# ------------------------------------------------------------------ one firm

@dataclass
class FirmRun:
    crd: str
    now: str
    site_domain: str = ""
    main_phone: str | None = None
    roster: Roster = field(default_factory=Roster)
    people_mod: object = None
    pages_ok: int = 0
    browser_pages: int = 0
    phone_pages: Counter = field(default_factory=Counter)   # phone -> pages it appears on
    sitewide: set = field(default_factory=set)              # numbers in headers and footers
    person_phone: dict = field(default_factory=dict)        # phone -> set of person keys
    persons: dict = field(default_factory=dict)             # person_key -> display name
    person_email: dict = field(default_factory=dict)        # person_key -> email
    person_title: dict = field(default_factory=dict)
    person_dial: dict = field(default_factory=dict)         # person_key -> their own number
    person_src: dict = field(default_factory=dict)          # person_key -> page that showed them
    known_of: dict = field(default_factory=dict)            # person_key -> roster name
    firm_names: list = field(default_factory=list)          # business and legal name
    company_li: Counter = field(default_factory=Counter)    # company page -> pages linking it
    company_chrome: set = field(default_factory=set)        # company pages in headers and footers
    company_src: dict = field(default_factory=dict)         # company page -> first page showing it
    person_li: dict = field(default_factory=dict)           # person_key -> profile URL
    li_keys: dict = field(default_factory=dict)             # profile URL -> person keys given it
    emails: set = field(default_factory=set)
    personal: set = field(default_factory=set)
    direct: set = field(default_factory=set)
    ai_pages: list = field(default_factory=list)

    def key_for(self, conn, name: str, known: str | None) -> str:
        """'i:<indvl_pk>' when the person can be tied to an IAPD record, else
        the name key contacts.py uses. Reads only; called before any write on
        a page so a failed lookup cannot roll back pending work."""
        if self.people_mod is not None:
            for n in (known, name):
                if not n:
                    continue
                try:
                    pk = self.people_mod.match_person(conn, self.crd, n)
                except Exception:
                    conn.rollback()
                    pk = None
                if pk:
                    pk = str(pk)
                    return pk if pk.startswith("i:") else f"i:{pk}"
        if known:
            k = self.roster.pk.get(contacts.name_key(known))
            if k:
                return k
        return contacts.name_key(known or name)


def _shared(run: FirmRun, phone: str, key: str = "") -> bool:
    """The firm's own line rather than one person's: its filed main number, a
    number in the site's header or footer, or one already printed beside
    somebody else. Appearing on two pages is not enough, since a direct line
    is usually on both the team card and the bio page. An extension is always
    somebody's own."""
    if " x" in phone:
        return False
    return (phone == run.main_phone or phone in run.sitewide
            or bool(run.person_phone.get(phone, set()) - {key}))


def _phone_label(run: FirmRun, phone: str, label: str | None, key: str = "") -> str | None:
    """A number printed beside one person, and not shared, is their line,
    whatever word sits next to it: a vCard files everyone's direct dial as
    WORK, and team cards say "Office" just as often. A shared one is the
    office's. Mobile, main and toll-free keep their labels."""
    if label in (None, "direct", "office") and " x" in phone:
        return "direct"
    shared = _shared(run, phone, key)
    if label in (None, "direct", "office"):
        return "office" if shared else "direct"
    return label


def store_page(conn, run: FirmRun, page: Page) -> None:
    """Extract and store one fetched page. Runs between fetches; commits."""
    url = page.final_url or page.url
    cache_page(conn, run.crd, url, page.status, page.html if page.ok else "", run.now)
    if not page.ok:
        conn.commit()
        return
    run.pages_ok += 1
    if page.via == "browser":
        run.browser_pages += 1
    vcard = page.is_vcard
    source = "vcard" if vcard else "website"
    if not vcard:
        websignals.record(conn, run.crd, url, page.html, run.now)
    conn.commit()

    known = run.roster.names
    hits = harvest.extract_people(page.html, url, known, site_domain=run.site_domain)
    emails = [] if vcard else harvest.extract_emails(page.html, run.site_domain)
    phones = [] if vcard else harvest.extract_phones(page.html)
    for p in phones:
        run.phone_pages[p["phone"]] += 1
    if not vcard:
        run.sitewide |= harvest.chrome_phones(page.html)

    # Every read first: the person keys need lookups, and a failed statement
    # would roll back anything written before it.
    keyed = [(h, run.key_for(conn, h.name, h.known)) for h in hits]
    li_links = {} if vcard else harvest.linkedin_links(page.html, url)
    for u in li_links.get("company", []):
        run.company_li[u] += 1
        run.company_src.setdefault(u, url)
    run.company_chrome.update(li_links.get("company_chrome", []))
    # A profile in the header or footer of a one-adviser site is usually the
    # founder's. It is kept only when its slug spells exactly one roster name.
    chrome_li = []
    for u in li_links.get("chrome_people", []):
        fits = {contacts.name_key(n): n for n in run.roster.names
                if harvest.linkedin_slug_fit(u, n) == "strong"}
        if len(fits) == 1:
            name = next(iter(fits.values()))
            chrome_li.append((u, name, run.key_for(conn, name, name)))
    # Values already pinned on a person, by another source or earlier in this
    # crawl. A firm-level copy of those would show one mailbox twice. Rows a
    # previous crawl of this site left are not counted: they are about to be
    # replaced by what the site says today.
    on_person = {(r["kind"], r["value"]) for r in conn.execute(
        "SELECT DISTINCT kind, value FROM contact_point WHERE crd=? AND person_key != ''"
        " AND (source NOT IN ('website','vcard') OR updated_at >= ?)",
        (run.crd, run.now)).fetchall()}
    conn.commit()

    claimed_emails: set[str] = set()
    claimed_phones: set[str] = set()
    for h, key in keyed:
        if not key:
            continue
        run.persons.setdefault(key, h.name)
        if h.known:
            run.known_of.setdefault(key, h.known)
        if h.title:
            run.person_title.setdefault(key, h.title)
        # The team card may carry the title and the bio page the address.
        title = h.title or run.person_title.get(key)
        run.person_src.setdefault(key, url)
        if h.email:
            ok, cat, _ = harvest.classify(h.email, run.site_domain)
            if ok and cat == "personal":
                pin(conn, run.crd, "email", h.email, source, person_key=key,
                    person_name=h.name, title=title, source_ref=url)
                claimed_emails.add(h.email)
                if key not in run.person_email:
                    run.person_email[key] = h.email
                    run.person_src[key] = url   # the page that printed the address
                run.emails.add(h.email)
                run.personal.add(h.email)
        all_phones = ([{"phone": h.phone, "label": h.phone_label}] if h.phone else []) + list(h.other_phones)
        best_phone = None
        for ph in all_phones:
            p = contacts.norm_phone(ph["phone"])
            if not p:
                continue
            shared = _shared(run, p, key)
            run.person_phone.setdefault(p, set()).add(key)
            if shared:
                # Left for the firm-level pass below. Pinning it on a person
                # would also make contacts.upsert drop the firm-level copy,
                # including the main number filed on Form ADV.
                continue
            label = _phone_label(run, p, ph.get("label"), key)
            pin(conn, run.crd, "phone", p, source, person_key=key,
                person_name=h.name, title=title, label=label, source_ref=url)
            claimed_phones.add(p)
            if label in ("direct", "mobile"):
                run.direct.add(p)
                run.person_dial.setdefault(key, p)
            best_phone = best_phone or p
        if h.linkedin and run.person_li.get(key, h.linkedin) == h.linkedin:
            contacts.upsert(conn, run.crd, "linkedin", h.linkedin, source, person_key=key,
                            person_name=h.name, title=title, source_ref=url,
                            verify_status="matched")
            run.person_li[key] = h.linkedin
            run.li_keys.setdefault(h.linkedin, {})[key] = h.name
        if title and not h.email and not all_phones:
            # Nothing to put in contact_point, but the title can still label
            # rows other sources hold for the same person.
            conn.execute("UPDATE contact_point SET title=? WHERE crd=? AND person_key=?"
                         " AND title IS NULL", (title, run.crd, key))
        if h.has_data():
            web_contact_row(conn, run.crd, h.name, title, h.email, best_phone, url, run.now)

    for u, name, key in chrome_li:
        if key and key not in run.person_li:
            contacts.upsert(conn, run.crd, "linkedin", u, source, person_key=key,
                            person_name=name, title=run.roster.titles.get(contacts.name_key(name)),
                            source_ref=url, verify_status="matched")
            run.person_li[key] = u
            run.li_keys.setdefault(u, {})[key] = name

    # Firm-level details nobody on this page claimed.
    for e in emails:
        if e["email"] in claimed_emails or ("email", e["email"]) in on_person:
            run.emails.add(e["email"])
            if e["category"] == "personal":
                run.personal.add(e["email"])
            continue          # already tied to a person; a firm copy would duplicate it
        contacts.upsert(conn, run.crd, "email", e["email"], source, source_ref=url,
                        confidence=e["confidence"], is_role=e["category"] == "role")
        run.emails.add(e["email"])
        if e["category"] == "personal":
            run.personal.add(e["email"])
        web_contact_row(conn, run.crd, None, None, e["email"], None, url, run.now)
    kept = 0
    for p in phones:
        if (p["phone"] in claimed_phones or ("phone", p["phone"]) in on_person
                or kept >= FIRM_PHONES_PER_PAGE):
            continue
        label = p["label"] or ("main" if p["phone"] == run.main_phone else None)
        contacts.upsert(conn, run.crd, "phone", p["phone"], source, label=label, source_ref=url)
        web_contact_row(conn, run.crd, None, None, None, p["phone"], url, run.now)
        kept += 1
    conn.commit()

    # A team page whose names came without titles or emails is what the AI
    # extractor is for; remember it and ask after the crawl, not during it.
    # Kept as candidates ranked by how many names lack both; the best few are
    # sent once the crawl is done.
    bare = sum(1 for h in hits if not h.title and not h.email)
    if page.kind in ("team", "about", "bio") and bare:
        run.ai_pages.append((bare, page.text or harvest.visible_text(page.html), url))
        run.ai_pages.sort(key=lambda x: -x[0])
        del run.ai_pages[AI_PAGES_MAX * 3:]   # bounded memory on large sites


def ask_ai(conn, run: FirmRun) -> int:
    """Store what the AI extractor reads off the pages the rules struggled
    with. Silent when the module is missing or switched off."""
    ai = _ai_module()
    if ai is None or not run.ai_pages:
        return 0
    n = 0
    for _bare, text, url in run.ai_pages[:AI_PAGES_MAX]:
        source_phones = {row['phone'] for row in harvest.extract_phones(text or '')}
        try:
            found = ai.extract_people(text, url, run.roster.names)
        except Exception:
            continue
        rows = []
        for d in found or []:
            name = harvest.normalise_name(d.get("name"))
            if not name:
                continue
            known = harvest.match_known(name, run.roster.names)
            rows.append((name, known, harvest.clean_title(d.get("title"), name),
                         (d.get("email") or "").strip().lower(), d.get("phone") or ""))
        keyed = [(r, run.key_for(conn, r[0], r[1])) for r in rows]
        conn.commit()
        for (name, known, title, email, phone), key in keyed:
            if not key:
                continue
            run.persons.setdefault(key, name)
            if email:
                ok, cat, _ = harvest.classify(email, run.site_domain)
                if ok and cat == "personal" and email in (text or "").lower():
                    pin(conn, run.crd, "email", email, "ai", person_key=key,
                        person_name=name, title=title, source_ref=url)
                    run.person_email.setdefault(key, email)
                    n += 1
            p = contacts.norm_phone(phone)
            if p and p in source_phones and not _shared(run, p, key):
                pin(conn, run.crd, "phone", p, "ai", person_key=key, person_name=name,
                    title=title, label=_phone_label(run, p, None, key), source_ref=url)
                n += 1
            if title:
                run.person_title.setdefault(key, title)
                conn.execute("UPDATE contact_point SET title=? WHERE crd=? AND person_key=?"
                             " AND title IS NULL", (title, run.crd, key))
        conn.commit()
    return n


def attribute_leftovers(conn, run: FirmRun) -> int:
    """Personal addresses no page tied to anyone, matched by name pattern
    against everyone the whole site showed plus the roster. The contact page
    lists nova.cruce@ while Nova Cruce is named only on her bio page, so this
    can only be decided once the crawl is over."""
    rows = conn.execute(
        "SELECT value, source, source_ref FROM contact_point WHERE crd=? AND kind='email'"
        " AND person_key='' AND is_role=0 AND source IN ('website','vcard')"
        " AND updated_at >= ?", (run.crd, run.now)).fetchall()
    names: list[tuple[str, str | None]] = [(n, run.known_of.get(k)) for k, n in run.persons.items()]
    names += [(n, n) for n in run.roster.names
              if contacts.name_key(n) not in {contacts.name_key(k or "") for _, k in names}]
    found = []
    for r in rows:
        email = r["value"]
        fits = [(n, k) for n, k in names
                if harvest.email_fits(email, n) or (k and harvest.email_fits(email, k))]
        keys = {contacts.name_key(k or n) for n, k in fits}
        if len(keys) == 1:
            n, k = fits[0]
            found.append((r, n, k))
    keyed = [(r, n, k, run.key_for(conn, n, k)) for r, n, k in found]
    conn.commit()
    done = 0
    for r, n, k, key in keyed:
        if not key:
            continue
        title = run.person_title.get(key)
        pin(conn, run.crd, "email", r["value"], r["source"], person_key=key,
            person_name=n, title=title, source_ref=r["source_ref"])
        run.persons.setdefault(key, n)
        if k:
            run.known_of.setdefault(key, k)
        run.person_email.setdefault(key, r["value"])
        conn.execute("DELETE FROM web_contact WHERE crd=? AND person IS NULL AND email=?",
                     (run.crd, r["value"]))
        web_contact_row(conn, run.crd, n, title, r["value"], None,
                        r["source_ref"] or "", run.now)
        done += 1
    conn.commit()
    return done


def learn_and_guess(conn, run: FirmRun) -> int:
    """Learn the firm's address pattern from the people the site attributed an
    address to, then guess one address for each known person still without
    one. Only patterns seen on the firm's own mail domain count, and every
    guess is stored as source 'pattern' so nobody mistakes it for a published
    address."""
    votes: Counter = Counter()
    for key, email in run.person_email.items():
        local, _, dom = email.lower().partition("@")
        if not dom or any(b in dom for b in emailguess.BAD_EMAIL_DOMAINS):
            continue
        for nm in (run.persons.get(key), run.known_of.get(key)):
            np = emailguess.name_parts(nm or "")
            pat = emailguess._detect(np[0], np[1], local) if np else None
            if pat:
                votes[(dom, pat)] += 1
                break
    if not votes:
        return 0
    (domain, pat), seen = votes.most_common(1)[0]
    if harvest.registrable(domain) != harvest.registrable(run.site_domain) and seen < 2:
        return 0              # one off-site address is not the firm's pattern
    conf = 70 if seen >= 2 else 60

    # Who to guess for: people the site showed, then filed officers, then the
    # rest of the roster. Site names first because they are current staff,
    # and the name they go by is the one their address uses.
    targets: list[tuple[str, str, str | None]] = []
    seen_keys: set[str] = set()
    for key, name in run.persons.items():
        targets.append((key, name, run.person_title.get(key)))
        seen_keys.add(key)
    pending = list(run.roster.officers) + [n for n in run.roster.names
                                           if n not in run.roster.officers]
    keyed = []
    for name in pending:
        nk = contacts.name_key(name)
        if not nk or any(contacts.name_key(run.known_of.get(k, "")) == nk for k in seen_keys):
            continue
        keyed.append((run.key_for(conn, name, name), name, run.roster.titles.get(nk)))
    conn.commit()
    for key, name, title in keyed:
        if key and key not in seen_keys:
            targets.append((key, name, title))
            seen_keys.add(key)

    have = {r["person_key"] for r in conn.execute(
        "SELECT DISTINCT person_key FROM contact_point WHERE crd=? AND kind='email'"
        " AND source != 'pattern' AND is_role=0 AND person_key != ''", (run.crd,)).fetchall()}
    taken = {r["value"] for r in conn.execute(
        "SELECT value FROM contact_point WHERE crd=? AND kind='email' AND source != 'pattern'",
        (run.crd,)).fetchall()}
    # "Kym Davis" on the site with kym@ is the "Kimberly Kaye Davis" Schedule A
    # files, though no nickname table says so. Someone the site showed with an
    # address, same surname and first initial, is taken to be the same person:
    # a guess for the filed name would be a second, wrong address for them.
    shown = set()
    for key, email in run.person_email.items():
        np = emailguess.name_parts(run.persons.get(key) or "")
        if np:
            shown.add((np[0][:1], np[1]))
    guesses: dict[str, list[tuple[str, str, str | None]]] = {}
    for key, name, title in targets:
        if key in have:
            continue
        np = emailguess.name_parts(name)
        if not np:
            continue
        if key not in run.persons and (np[0][:1], np[1]) in shown:
            continue
        guess = f"{emailguess.PATTERNS[pat](np[0], np[1])}@{domain}"
        if guess in taken:
            continue
        guesses.setdefault(guess, []).append((key, name, title))
    n = 0
    for guess, who in guesses.items():
        if len({k for k, _, _ in who}) != 1:
            continue          # two people would share it (two Johns, 'first'): skip both
        key, name, title = who[0]
        contacts.upsert(conn, run.crd, "email", guess, "pattern", person_key=key,
                        person_name=name, title=title, confidence=conf,
                        source_ref=f"pattern:{pat}")
        n += 1
        if n >= GUESSES_MAX:
            break
    conn.commit()
    return n


def settle_shared_linkedin(conn, run: FirmRun) -> None:
    """A profile this crawl gave to two or more people is nobody's own: some
    team pages repeat the founder's link on every card. It stays only with
    the one person its slug spells, if any."""
    for url, keys in run.li_keys.items():
        if len(keys) < 2:
            continue
        owners = [k for k, n in keys.items() if harvest.linkedin_slug_fit(url, n) == "strong"]
        for key in keys:
            if len(owners) == 1 and key == owners[0]:
                continue
            conn.execute("DELETE FROM contact_point WHERE crd=? AND kind='linkedin' AND value=?"
                         " AND person_key=? AND sources='website' AND found_at >= ?",
                         (run.crd, url, key, run.now))
            if run.person_li.get(key) == url:
                del run.person_li[key]
    conn.commit()


def store_firm_linkedin(conn, run: FirmRun) -> int:
    """The firm's own LinkedIn company page, chosen once the whole site has
    been read. One company page on the site is the firm's. Several (its own,
    a parent's, a custodian's) are narrowed to those whose slug carries the
    firm's name or domain, then to the one in the site-wide header or footer;
    a choice that stays ambiguous stores nothing."""
    pages = list(run.company_li)
    if not pages:
        return 0
    if len(pages) > 1:
        named = [u for u in pages if harvest.company_slug_fits(u, run.firm_names,
                                                               run.site_domain)]
        if not named:
            named = [u for u in pages if u in run.company_chrome]
        pages = named
    if not pages or len(pages) > 2:
        return 0
    for u in pages:
        contacts.upsert(conn, run.crd, "linkedin", u, "website",
                        source_ref=run.company_src.get(u), verify_status="matched")
    conn.commit()
    return len(pages)


def settle_shared_phones(conn, run: FirmRun) -> None:
    """A number that turned out to be printed beside two or more people, or in
    a footer seen only on a later page, is the firm's line, not anybody's
    direct dial. Pages are stored one at a time, so this moves such numbers
    back to firm level once the whole site has been read."""
    for phone, keys in run.person_phone.items():
        if " x" in phone:
            continue
        if len(keys) < 2 and phone not in run.sitewide and phone != run.main_phone:
            continue
        conn.execute("DELETE FROM contact_point WHERE crd=? AND kind='phone' AND value=?"
                     " AND person_key != '' AND source IN ('website','vcard')"
                     " AND found_at >= ?", (run.crd, phone, run.now))
        contacts.upsert(conn, run.crd, "phone", phone, "website", label="office")
        run.direct.discard(phone)
    conn.commit()


def finish_people(conn, run: FirmRun) -> None:
    """One web_contact row per person, carrying the best of every page: the
    team card's title, the bio page's address, the vCard's direct line. Rows
    were written page by page so a crash loses nothing; this replaces them
    with the consolidated view. Titles also reach the person's contact_point
    rows that were written before the title was seen."""
    for key, title in run.person_title.items():
        conn.execute("UPDATE contact_point SET title=? WHERE crd=? AND person_key=?"
                     " AND title IS NULL", (title, run.crd, key))
    conn.execute("DELETE FROM web_contact WHERE crd=? AND person IS NOT NULL", (run.crd,))
    for key, name in run.persons.items():
        title = run.person_title.get(key)
        email = run.person_email.get(key)
        phone = run.person_dial.get(key)
        if title or email or phone:
            web_contact_row(conn, run.crd, name, title, email, phone,
                            run.person_src.get(key, ""), run.now)
    conn.commit()


def prune_stale(conn, run: FirmRun) -> None:
    """The site as read today is the truth for what came from the site. Rows a
    previous crawl wrote and this one did not find again are removed: a person
    gone from the team page has usually left, and the first crawler's rows
    carried garbled titles under name-only keys that now duplicate people
    matched to their IAPD record. Only rows the website alone vouched for go;
    anything another source also reported, or a mail server confirmed, stays."""
    conn.execute("DELETE FROM web_contact WHERE crd=? AND found_at < ?", (run.crd, run.now))
    conn.execute(
        "DELETE FROM contact_point WHERE crd=? AND source IN ('website','vcard')"
        " AND sources IN ('website','vcard','website,vcard','vcard,website')"
        " AND updated_at < ? AND verify_status != 'valid'", (run.crd, run.now))
    # This job's own earlier pattern guesses (source_ref 'pattern:<name>') are
    # re-derived below from today's site; the ones not re-derived go, unless a
    # check has since said something about them. Other jobs' guesses are left.
    conn.execute(
        "DELETE FROM contact_point WHERE crd=? AND source='pattern' AND sources='pattern'"
        " AND source_ref LIKE 'pattern:%' AND updated_at < ?"
        " AND verify_status IN ('unverified','queued')", (run.crd, run.now))
    conn.commit()


def enrich_one(conn, crawler: Crawler, crd: str, website: str, people) -> dict:
    """Read one firm's site and store what it says.

    Commits after every page and never writes while a page is being fetched.
    On SQLite an open write transaction held across a fetch blocked every
    reader for the length of the crawl; on Postgres it would hold row locks
    and a connection slot just as long. So the crawler calls back between
    fetches (store_page), each callback commits before returning, and every
    database read a page needs happens before that page's writes."""
    run = FirmRun(crd=crd, now=_now(), people_mod=people)
    r = conn.execute("SELECT phone, business_name, legal_name FROM firm_current WHERE crd=?",
                     (crd,)).fetchone()
    run.main_phone = contacts.norm_phone(r["phone"]) if r and r["phone"] else None
    run.firm_names = [n for n in ((r["business_name"], r["legal_name"]) if r else ()) if n]
    run.roster = load_roster(conn, crd, people)
    host = urlparse(website if "://" in website else "https://" + website).hostname or ""
    run.site_domain = harvest.registrable(host)
    conn.commit()

    def on_page(page: Page) -> None:
        if page.kind == "home" and page.ok:
            final = urlparse(page.final_url).hostname or ""
            run.site_domain = harvest.registrable(final) or run.site_domain
        store_page(conn, run, page)

    pages = crawler.crawl_site(website, on_page=on_page)
    conn.commit()
    err = crawler.root_error or ""
    if err == "no website" or err.startswith("website forwards to a client login"):
        status = "no_website"
    else:
        status = "ok" if run.pages_ok else "unreachable"
    guesses = 0
    if status == "ok":
        ask_ai(conn, run)
        attribute_leftovers(conn, run)
        settle_shared_phones(conn, run)
        settle_shared_linkedin(conn, run)
        store_firm_linkedin(conn, run)
        if not crawler.root_error:
            # Only a complete read may retire old rows; a site that stopped
            # answering halfway has not said those people are gone.
            prune_stale(conn, run)
        guesses = learn_and_guess(conn, run)
        finish_people(conn, run)
    drop_barren_cache(conn, crd)
    method = ("browser" if run.browser_pages and run.browser_pages >= run.pages_ok
              else "mixed" if run.browser_pages else "static")
    return {"status": status, "pages": run.pages_ok, "people": len(run.persons),
            "emails": len(run.emails), "personal": len(run.personal),
            "phones": len(run.direct), "guesses": guesses, "method": method,
            "linkedin": len(run.person_li),
            "site": pages[0].final_url if pages else website,
            "error": crawler.root_error}


# ------------------------------------------------------------------ schedule

def todo(conn, limit: int, shard: tuple[int, int] | None = None) -> list[dict]:
    """All firm websites, including explicit refresh requests and contact gaps.
    With shard (k, n), only the firms whose CRD falls in shard k of n."""
    days = settings.get_int("crawl.recrawl_days", 90) or 90
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    retry = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
    return conn.execute(f"""
        SELECT f.crd, f.website FROM firm_current f
        LEFT JOIN firm_scope s ON s.crd=f.crd
        LEFT JOIN web_enrich_state w ON w.crd=f.crd
        LEFT JOIN firm_refresh_request r ON r.crd=f.crd
        WHERE f.website IS NOT NULL AND f.website != '' AND
          (w.crd IS NULL OR w.scanned_at < ? OR r.requested_at > w.scanned_at OR
           (w.scanned_at < ? AND EXISTS (SELECT 1 FROM person_employment pe
            WHERE pe.org_pk=f.crd AND pe.kind='current' AND (
             NOT EXISTS (SELECT 1 FROM usable_contact_point cp WHERE cp.crd=f.crd
               AND cp.person_key='i:'||pe.indvl_pk AND cp.kind='email') OR
             NOT EXISTS (SELECT 1 FROM usable_contact_point cp WHERE cp.crd=f.crd
               AND cp.person_key='i:'||pe.indvl_pk AND cp.kind='phone')))))
          {"AND MOD(ABS(hashtext(f.crd)), ?) = ?" if shard else ""}
        ORDER BY COALESCE(w.scanned_at, '1970-01-01'), s.priority DESC NULLS LAST
        LIMIT ?""", (cutoff, retry, *((shard[1], shard[0]) if shard else ()), limit)).fetchall()


def save_state(conn, crd: str, res: dict) -> None:
    conn.execute(
        "INSERT INTO web_enrich_state (crd, scanned_at, pages, people, emails, status,"
        " personal_emails, phones, guesses, method, site) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT (crd) DO UPDATE SET scanned_at=excluded.scanned_at,"
        " pages=excluded.pages, people=excluded.people, emails=excluded.emails,"
        " status=excluded.status, personal_emails=excluded.personal_emails,"
        " phones=excluded.phones, guesses=excluded.guesses, method=excluded.method,"
        " site=excluded.site",
        (crd, _now(), res["pages"], res["people"], res["emails"], res["status"],
         res["personal"], res["phones"], res["guesses"], res["method"], res["site"]))
    conn.commit()


def run_shards(args) -> int:
    """Read --parallel shards at once, one process each, and wait for all.
    Each shard prints its own firm lines; the last line here sums them up."""
    import subprocess
    n = args.parallel
    procs = [subprocess.Popen([sys.executable, "-m", "scripts.web_enrich", "--limit", str(args.limit),
                               "--shard", f"{k}/{n}"], cwd=str(config.ROOT),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              encoding="utf-8", errors="replace")
             for k in range(n)]
    lines, failed = [], 0
    for pr in procs:
        out, _ = pr.communicate()
        lines += [ln for ln in (out or "").splitlines() if ln.strip()]
        failed += pr.returncode != 0
    for ln in lines:
        if ln.startswith("  "):
            print(ln, flush=True)
    firms = sum(1 for ln in lines if ln.startswith("  ") and ":" in ln)
    print(f"{n} shards read {firms} firm websites at once"
          + (f"; {failed} shard{'s' if failed != 1 else ''} failed" if failed else ""))
    return 1 if failed == n else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--crd", help="read this one firm now, whatever the schedule says")
    ap.add_argument("--url", help="with --crd: read this address instead of the filed one")
    ap.add_argument("--parallel", type=int, default=1, help="read this many shards at once")
    ap.add_argument("--shard", help="k/n: read only shard k of n (set by --parallel)")
    args = ap.parse_args()
    if args.parallel > 1 and not args.crd and not args.shard:
        return run_shards(args)
    shard = None
    if args.shard:
        k, n = (int(x) for x in args.shard.split("/"))
        shard = (k, n)

    cfg = config.load()
    conn = db.connect()
    conn.executescript(SCHEMA)
    conn.executescript(websignals.SCHEMA)
    for col, decl in STATE_COLUMNS:
        db.add_column(conn, "web_enrich_state", col, decl)
    conn.commit()
    contacts.init(conn)
    from prospect import jobs
    jobs.init(conn)

    with runlog.Run(conn, "web_enrich", "scrape", cfg.stamp) as run:
        if args.crd:
            r = conn.execute("SELECT crd, website FROM firm_current WHERE crd=?",
                             (args.crd,)).fetchone()
            website = args.url or (r["website"] if r else None)
            rows = [{"crd": args.crd, "website": website}]
        else:
            rows = todo(conn, args.limit, shard)
        conn.commit()
        if not rows:
            print("nothing left to enrich")
            run.skip("all websites read recently")
            return 0
        people = _people_module(conn)
        conn.commit()
        tot = Counter()
        with Crawler() as crawler:
            for r in rows:
                crd, website = r["crd"], (r["website"] or "").strip()
                t0 = time.monotonic()
                if not website:
                    res = {"status": "no_website", "pages": 0, "people": 0, "emails": 0,
                           "personal": 0, "phones": 0, "guesses": 0, "method": None,
                           "site": None, "error": None}
                else:
                    try:
                        res = enrich_one(conn, crawler, crd, website, people)
                    except Exception as e:  # one bad site must not end the slice
                        conn.rollback()
                        res = {"status": "error", "pages": 0, "people": 0, "emails": 0,
                               "personal": 0, "phones": 0, "guesses": 0, "method": None,
                               "site": website, "error": f"{type(e).__name__}: {e}"[:200]}
                save_state(conn, crd, res)
                for k in ("pages", "people", "personal", "phones", "guesses", "linkedin"):
                    tot[k] += res.get(k, 0)
                why = f" ({res['error']})" if res["status"] != "ok" and res.get("error") else ""
                via = f", {res['method']}" if res["method"] and res["method"] != "static" else ""
                print(f"  {crd}: {res['status']}{why}, {res['pages']} pages{via},"
                      f" {res['people']} people, {res['personal']} personal emails,"
                      f" {res['phones']} direct phones, {res['guesses']} guesses,"
                      f" {res.get('linkedin', 0)} LinkedIn profiles"
                      f" [{time.monotonic() - t0:.0f}s]", flush=True)
        jobs.request_run(conn, 'email_verify')
        jobs.request_run(conn, 'rescore')
        run.rows_out = len(rows)
        run.note(f"{len(rows)} firms, {tot['pages']} pages, {tot['people']} people,"
                 f" {tot['personal']} personal emails, {tot['phones']} direct phones,"
                 f" {tot['guesses']} pattern guesses, {tot['linkedin']} LinkedIn profiles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
