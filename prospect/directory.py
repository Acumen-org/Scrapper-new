"""Directories and websites the team adds on the Enrichment screen.

Someone pastes the address of a page that lists advisers: an association's
find-an-advisor results, a conference speaker list, a state society
directory, a partner firm's team page. Bellwether then:

  1. probes it straight away and says, in plain words, whether its generic
     reader can pull people and firms from it, or whether the site needs a
     custom adapter, and why (robots rules forbid it, a login wall, results
     that only appear after a search form, a JavaScript-only page with no
     browser installed, or no repeated listing to read);
  2. crawls it on its schedule: the listing pages, their pagination, and the
     profile pages they link to, politely (robots rules, one request a second
     per site, the Crawler's limits);
  3. reads each page for records: schema.org JSON-LD, vCards, and repeated
     cards that carry a name, a title, an email, a phone, a website or a
     place;
  4. matches every record to a firm (by website domain, then email domain,
     then phone, then firm name within a state) and, where it can, to a
     person on that firm's SEC roster, and files any email or phone into the
     firm's contacts with source 'directory'.

Nothing here is specific to one website. A site whose listing cannot be read
generically is labelled as needing an adapter rather than half-read.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from . import contacts, harvest

SCHEMA = """
CREATE TABLE IF NOT EXISTS directory_source (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    urls           TEXT NOT NULL,          -- one per line
    kind           TEXT NOT NULL DEFAULT 'directory',
    status         TEXT NOT NULL DEFAULT 'active',   -- active | paused
    schedule_days  INTEGER NOT NULL DEFAULT 7,       -- 0 = read once
    respect_robots INTEGER NOT NULL DEFAULT 1,
    max_pages      INTEGER NOT NULL DEFAULT 200,
    probe_json     TEXT,
    needs_adapter  INTEGER NOT NULL DEFAULT 0,
    adapter_note   TEXT,
    last_run_at    TEXT,
    next_run_at    TEXT,
    last_message   TEXT,
    records_found  INTEGER NOT NULL DEFAULT 0,
    firms_matched  INTEGER NOT NULL DEFAULT 0,
    people_found   INTEGER NOT NULL DEFAULT 0,
    emails_found   INTEGER NOT NULL DEFAULT 0,
    phones_found   INTEGER NOT NULL DEFAULT 0,
    created_by     TEXT,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS directory_record (
    id                 INTEGER PRIMARY KEY,
    source_id          INTEGER NOT NULL,
    rkey               TEXT NOT NULL,      -- hash of what identifies the record
    url                TEXT,
    person_name        TEXT,
    title              TEXT,
    firm_name          TEXT,
    website            TEXT,
    email              TEXT,
    phone              TEXT,
    city               TEXT,
    state              TEXT,
    matched_crd        TEXT,
    match_method       TEXT,
    match_score        REAL,
    matched_person_key TEXT,
    raw_json           TEXT,
    found_at           TEXT NOT NULL,
    UNIQUE (source_id, rkey)
);
CREATE INDEX IF NOT EXISTS ix_dirrec_source ON directory_record (source_id);
CREATE INDEX IF NOT EXISTS ix_dirrec_crd ON directory_record (matched_crd);
"""

STATES = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL",
          "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE",
          "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD",
          "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "PR"}
STATE_NAMES = {"alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
               "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
               "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
               "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
               "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
               "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
               "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
               "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
               "new mexico": "NM", "new york": "NY", "north carolina": "NC",
               "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
               "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
               "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
               "vermont": "VT", "virginia": "VA", "washington": "WA",
               "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY"}
CITY_STATE_RE = re.compile(r"\b([A-Z][a-zA-Z.'\- ]{1,28}),\s*([A-Z]{2})\b(?:\s+\d{5})?")
SOCIAL = ("linkedin.", "facebook.", "twitter.", "x.com", "instagram.", "youtube.",
          "tiktok.", "pinterest.", "yelp.", "google.", "apple.com", "maps.", "goo.gl",
          "sec.gov", "finra.org", "adviserinfo", "brokercheck", "investor.gov")
FIRM_WORDS = re.compile(r"\b(llc|l\.l\.c|inc|incorporated|lp|llp|ltd|corp|corporation|co|"
                        r"company|group|partners|advisors?|advisers?|wealth|capital|"
                        r"management|financial|investments?|planning|asset|associates|"
                        r"securities|services|trust|family office)\b", re.I)
SUFFIX_RE = re.compile(r"\b(llc|l\.l\.c\.?|inc\.?|incorporated|lp|l\.p\.|llp|ltd\.?|corp\.?|"
                       r"corporation|co\.?|company|pllc|pc)\b", re.I)
NEXT_RE = re.compile(r"^\s*(next|next page|more results|older|›|»|>|>>)\s*$", re.I)
PROFILE_HINT = re.compile(r"/(advisor|adviser|advisors|advisers|profile|profiles|member|members|"
                          r"people|person|planner|planners|directory|listing|listings|firm|firms|"
                          r"professional|professionals|expert|experts|team|bio)s?/", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ------------------------------------------------------------------ sources

def add_source(conn, name: str, urls: list[str], kind: str, schedule_days: int,
               respect_robots: bool, max_pages: int, created_by: str) -> int:
    cur = conn.execute(
        "INSERT INTO directory_source (name, urls, kind, schedule_days, respect_robots,"
        " max_pages, created_by, created_at) VALUES (?,?,?,?,?,?,?,?) RETURNING id",
        (name, "\n".join(urls), kind, int(schedule_days), 1 if respect_robots else 0,
         int(max_pages), created_by or None, _now()))
    sid = cur.lastrowid
    conn.commit()
    return int(sid)


def list_sources(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM directory_source ORDER BY created_at DESC")]


def get_source(conn, sid: int) -> dict | None:
    r = conn.execute("SELECT * FROM directory_source WHERE id=?", (sid,)).fetchone()
    return dict(r) if r else None


def update_source(conn, sid: int, **fields) -> None:
    allowed = {"name", "urls", "kind", "status", "schedule_days", "respect_robots",
               "max_pages", "probe_json", "needs_adapter", "adapter_note", "last_run_at",
               "next_run_at", "last_message", "records_found", "firms_matched",
               "people_found", "emails_found", "phones_found"}
    sets = [(k, v) for k, v in fields.items() if k in allowed]
    if not sets:
        return
    conn.execute(f"UPDATE directory_source SET {', '.join(k + '=?' for k, _ in sets)} WHERE id=?",
                 (*[v for _, v in sets], sid))
    conn.commit()


def delete_source(conn, sid: int) -> None:
    conn.execute("DELETE FROM directory_record WHERE source_id=?", (sid,))
    conn.execute("DELETE FROM directory_source WHERE id=?", (sid,))
    conn.commit()


def records(conn, sid: int, limit: int = 100, offset: int = 0,
            matched: bool | None = None) -> list[dict]:
    where = "source_id=?"
    if matched is True:
        where += " AND matched_crd IS NOT NULL"
    elif matched is False:
        where += " AND matched_crd IS NULL"
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM directory_record WHERE {where} ORDER BY (matched_crd IS NULL), id"
        f" LIMIT ? OFFSET ?", (sid, limit, offset))]


def due_sources(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM directory_source WHERE status != 'paused'"
        " AND (next_run_at IS NULL OR next_run_at <= ?) ORDER BY next_run_at NULLS FIRST",
        (_now(),))]


# ------------------------------------------------------------------ reading pages

def _host(url: str) -> str:
    return harvest.registrable(urlparse(url).netloc or "")


def _is_external_site(href: str, own: str) -> bool:
    if not href.lower().startswith(("http://", "https://")):
        return False
    h = _host(href)
    if not h or h == own:
        return False
    return not any(s in href.lower() for s in SOCIAL)


def _place(text: str) -> tuple[str | None, str | None]:
    m = CITY_STATE_RE.search(text or "")
    if m and m.group(2) in STATES:
        return m.group(1).strip().title(), m.group(2)
    return None, None


def _jsonld_records(html: str, url: str) -> list[dict]:
    out = []
    for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                            html or "", re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
                continue
            if not isinstance(d, dict):
                continue
            for k in ("@graph", "itemListElement", "employee", "member", "founder", "item"):
                v = d.get(k)
                if isinstance(v, (list, dict)):
                    stack.extend(v if isinstance(v, list) else [v])
            t = d.get("@type")
            types = {t} if isinstance(t, str) else set(t or [])
            if not types & {"Person", "Organization", "LocalBusiness", "FinancialService",
                            "ProfessionalService", "AccountingService", "InsuranceAgency"}:
                continue
            addr = d.get("address") or {}
            if isinstance(addr, list):
                addr = addr[0] if addr else {}
            rec = {"url": d.get("url") if "Person" in types else url,
                   "city": (addr.get("addressLocality") if isinstance(addr, dict) else None),
                   "state": (addr.get("addressRegion") if isinstance(addr, dict) else None),
                   "email": (d.get("email") or "").replace("mailto:", "") or None,
                   "phone": d.get("telephone"), "website": None}
            if "Person" in types:
                if not harvest.looks_like_name(d.get("name") or ""):
                    continue      # page authors such as "admin" are not people
                rec["person_name"] = d.get("name")
                rec["title"] = d.get("jobTitle")
                works = d.get("worksFor") or d.get("affiliation") or {}
                if isinstance(works, list):
                    works = works[0] if works else {}
                rec["firm_name"] = works.get("name") if isinstance(works, dict) else None
                rec["website"] = works.get("url") if isinstance(works, dict) else None
            else:
                rec["firm_name"] = d.get("name")
                rec["website"] = d.get("url") if d.get("url") and _host(d.get("url")) != _host(url) else None
            if rec.get("person_name") or rec.get("firm_name"):
                out.append(rec)
    return out


def _short_name(text: str) -> bool:
    """A heading, not a sentence: few words, no full stop in the middle."""
    t = (text or "").strip()
    return 2 < len(t) <= 70 and len(t.split()) <= 8 and not re.search(r"[.!?;]\s+\S", t)


def _cards(html: str, url: str) -> list[dict]:
    """Repeated blocks that each hold a name and a way to reach or place it.

    A listing is a set of siblings that look alike. For every element holding
    a contact signal (a mailto, a tel, an external website link or a "City,
    ST"), its nearest ancestor whose siblings share its tag and class is the
    card; cards appearing at least three times form the listing."""
    try:
        from lxml import html as lh
        root = lh.fromstring(html)
    except Exception:
        return []
    own = _host(url)
    signals = root.xpath("//a[starts-with(@href,'mailto:') or starts-with(@href,'tel:')]"
                         " | //a[starts-with(@href,'http')]")
    cards = Counter()
    by_key: dict = defaultdict(list)
    for el in signals:
        href = el.get("href") or ""
        if href.startswith("http") and not _is_external_site(href, own):
            continue
        node = el
        for _ in range(8):
            parent = node.getparent()
            if parent is None:
                break
            sib = [s for s in parent if isinstance(s.tag, str) and s.tag == node.tag
                   and (s.get("class") or "") == (node.get("class") or "")]
            if len(sib) >= 3 and node.tag not in ("a", "span", "br"):
                key = (parent, node.tag, node.get("class") or "")
                if node not in by_key[key]:
                    by_key[key].append(node)
                cards[key] += 1
                break
            node = parent
    out = []
    for key, n in cards.most_common(4):
        parent, tag, cls = key
        for card in [s for s in parent if isinstance(s.tag, str) and s.tag == tag
                     and (s.get("class") or "") == cls]:
            text = " ".join(card.text_content().split())
            if len(text) < 6 or len(text) > 700:
                continue
            frag = lh.tostring(card, encoding="unicode")
            heads = [" ".join(h.text_content().split()) for h in
                     card.xpath(".//h1|.//h2|.//h3|.//h4|.//h5|.//strong|.//b"
                                "|.//*[contains(@class,'name')]")]
            heads = [h for h in heads if _short_name(h)]
            if not heads:
                continue
            name = heads[0]
            person, firm, title = None, None, None
            pname, ptitle = harvest.split_name_title(name)
            if pname and harvest.looks_like_name(pname):
                person, title = pname, ptitle
                for h in heads[1:4]:
                    if FIRM_WORDS.search(h) and not harvest.looks_like_name(h):
                        firm = h
                        break
                    if not title:
                        title = harvest.clean_title(h, person)
            elif FIRM_WORDS.search(name) and not harvest.looks_like_name(name):
                firm = name
            else:
                continue
            emails = [e["email"] for e in harvest.extract_emails(frag, "") if e["category"] != "junk"]
            phones = harvest.extract_phones(frag)
            sites = [a.get("href") for a in card.xpath(".//a[starts-with(@href,'http')]")
                     if _is_external_site(a.get("href") or "", own)]
            profile = [urljoin(url, a.get("href")) for a in card.xpath(".//a[@href]")
                       if PROFILE_HINT.search(urljoin(url, a.get("href") or ""))
                       and _host(urljoin(url, a.get("href"))) == own]
            city, state = _place(text)
            if not (emails or phones or sites or city):
                continue            # a name with nothing to reach or place it by
            out.append({"person_name": person, "title": title, "firm_name": firm,
                        "email": emails[0] if emails else None,
                        "phone": phones[0]["phone"] if phones else None,
                        "website": sites[0] if sites else None, "city": city, "state": state,
                        "url": profile[0] if profile else url})
    return out


def _profile_record(html: str, url: str) -> dict | None:
    """One record from a single profile page: its heading and contacts."""
    js = _jsonld_records(html, url)
    if js:
        return js[0]
    try:
        from lxml import html as lh
        root = lh.fromstring(html)
    except Exception:
        return None
    h1 = [" ".join(h.text_content().split()) for h in root.xpath("//h1")]
    if not h1:
        return None
    name, title = harvest.split_name_title(h1[0])
    own = _host(url)
    main = root.xpath("//main") or root.xpath("//article") or [root]
    frag = lh.tostring(main[0], encoding="unicode")
    text = " ".join(main[0].text_content().split())
    emails = [e["email"] for e in harvest.extract_emails(frag, "") if e["category"] != "junk"]
    phones = harvest.extract_phones(frag)
    sites = [a.get("href") for a in main[0].xpath(".//a[starts-with(@href,'http')]")
             if _is_external_site(a.get("href") or "", own)]
    firm = None
    for h in [" ".join(x.text_content().split()) for x in main[0].xpath(".//h2|.//h3|.//strong")][:6]:
        if _short_name(h) and FIRM_WORDS.search(h) and not harvest.looks_like_name(h):
            firm = h
            break
    city, state = _place(text)
    person = name if name and harvest.looks_like_name(name) else None
    if not person and not firm and FIRM_WORDS.search(h1[0]):
        firm = h1[0]
    if not (person or firm):
        return None
    return {"person_name": person, "title": title, "firm_name": firm,
            "email": emails[0] if emails else None,
            "phone": phones[0]["phone"] if phones else None,
            "website": sites[0] if sites else None, "city": city, "state": state, "url": url}


def page_records(html: str, url: str) -> list[dict]:
    if (html or "").lstrip()[:20].upper().startswith("BEGIN:VCARD"):
        v = harvest.parse_vcard(html)
        return [{"person_name": v.get("name"), "title": v.get("title"), "firm_name": v.get("org"),
                 "email": (v.get("emails") or [None])[0],
                 "phone": (v.get("phones") or [None])[0], "url": url}] if v.get("name") else []
    recs = _jsonld_records(html, url)
    recs += _cards(html, url)
    # Team pages: the website reader's people extractor knows them best.
    try:
        for h in harvest.extract_people(html, url, []):
            if h.has_data():
                recs.append({"person_name": h.name, "title": h.title, "email": h.email,
                             "phone": h.phone, "url": h.bio_url or url})
    except Exception:
        pass
    page_site = _host(url)
    for r in recs:
        r.setdefault("page_site", page_site)
    seen, out = set(), []
    for r in recs:
        k = _rkey(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _links(html: str, url: str) -> tuple[list[str], list[str]]:
    """(next listing pages, profile pages) found on a listing page."""
    from .crawl import page_links
    own = _host(url)
    nxt, prof = [], []
    paths = Counter()
    links = page_links(html, url)
    for href, text in links:
        if _host(href) != own:
            continue
        if NEXT_RE.match(text or "") or re.search(r"[?&](page|p|pg|start|offset)=\d+", href):
            nxt.append(href)
        p = urlparse(href).path.rstrip("/")
        if p.count("/") >= 2:
            paths[p.rsplit("/", 1)[0]] += 1
    for m in re.finditer(r'<link[^>]+rel=["\']next["\'][^>]+href=["\']([^"\']+)', html or "", re.I):
        nxt.insert(0, urljoin(url, m.group(1)))
    common = {pre for pre, n in paths.items() if n >= 5}
    for href, _ in links:
        p = urlparse(href).path.rstrip("/")
        if _host(href) == own and p.rsplit("/", 1)[0] in common and href != url:
            if PROFILE_HINT.search(href) or len(common) == 1:
                prof.append(href)
    return list(dict.fromkeys(nxt)), list(dict.fromkeys(prof))


# ------------------------------------------------------------------ matching

class Matcher:
    """Firms by website domain, email domain, phone and name within a state,
    loaded once per run so a thousand records cost a thousand dict lookups."""

    def __init__(self, conn):
        self.by_domain: dict[str, str] = {}
        self.by_phone: dict[str, str] = {}
        self.by_name: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.by_name_any: dict[str, list[str]] = defaultdict(list)
        for r in conn.execute("SELECT crd, legal_name, business_name, website, phone, state"
                              " FROM firm_current WHERE is_era=0"):
            if r["website"]:
                d = _host(r["website"] if "://" in r["website"] else "https://" + r["website"])
                if d and not any(s in d for s in SOCIAL):
                    self.by_domain.setdefault(d, r["crd"])
            if r["phone"]:
                digits = re.sub(r"\D", "", r["phone"])[-10:]
                if len(digits) == 10:
                    self.by_phone.setdefault(digits, r["crd"])
            for nm in {r["legal_name"], r["business_name"]}:
                k = norm_firm(nm)
                if k:
                    self.by_name[(k, (r["state"] or "").upper())].append(r["crd"])
                    self.by_name_any[k].append(r["crd"])
        try:
            for r in conn.execute("SELECT crd, value FROM contact_point WHERE kind='email'"
                                  " AND source IN ('brochure','website','vcard')"):
                d = r["value"].rsplit("@", 1)[-1]
                if d not in self.by_domain and not any(s in d for s in SOCIAL):
                    self.by_domain[d] = r["crd"]
        except Exception:
            conn.rollback()

    def match(self, rec: dict) -> tuple[str | None, str | None, float]:
        if rec.get("website"):
            d = _host(rec["website"])
            if d in self.by_domain:
                return self.by_domain[d], "website", 0.95
        if rec.get("email") and "@" in rec["email"]:
            d = rec["email"].rsplit("@", 1)[-1].lower()
            if d in self.by_domain:
                return self.by_domain[d], "email domain", 0.9
        if rec.get("phone"):
            digits = re.sub(r"\D", "", rec["phone"])[-10:]
            if digits in self.by_phone:
                return self.by_phone[digits], "phone", 0.85
        # A team page sits on the firm's own site: the page is the evidence.
        site = rec.get("page_site")
        if site and site in self.by_domain and not rec.get("firm_name"):
            return self.by_domain[site], "page is on the firm's website", 0.9
        k = norm_firm(rec.get("firm_name"))
        if k:
            st = (rec.get("state") or "").upper()
            st = STATE_NAMES.get(st.lower(), st)
            hits = self.by_name.get((k, st)) if st else None
            if hits and len(set(hits)) == 1:
                return hits[0], "name and state", 0.8
            anyh = self.by_name_any.get(k)
            if anyh and len(set(anyh)) == 1:
                return anyh[0], "name", 0.65
        return None, None, 0.0


def norm_firm(name: str | None) -> str:
    s = (name or "").lower().replace("&", " and ")
    s = SUFFIX_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def _rkey(r: dict) -> str:
    parts = [(r.get(k) or "").strip().lower() for k in
             ("person_name", "firm_name", "email", "phone", "website")]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:24]


def store(conn, sid: int, recs: list[dict], matcher: Matcher) -> dict:
    """Save records, match them, and file their contacts. Returns counts."""
    from . import people
    got = Counter()
    for r in recs:
        crd, method, score = matcher.match(r)
        pkey = None
        if crd and r.get("person_name"):
            try:
                pk = people.match_person(conn, crd, r["person_name"])
            except Exception:
                conn.rollback()
                pk = None
            pkey = f"i:{pk}" if pk else contacts.name_key(r["person_name"])
        cur = conn.execute(
            "INSERT INTO directory_record (source_id, rkey, url, person_name, title, firm_name,"
            " website, email, phone, city, state, matched_crd, match_method, match_score,"
            " matched_person_key, raw_json, found_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (source_id, rkey) DO UPDATE SET matched_crd=excluded.matched_crd,"
            " match_method=excluded.match_method, match_score=excluded.match_score,"
            " matched_person_key=excluded.matched_person_key, found_at=excluded.found_at"
            " RETURNING (xmax = 0) AS fresh",
            (sid, _rkey(r), r.get("url"), r.get("person_name"), r.get("title"), r.get("firm_name"),
             r.get("website"), (r.get("email") or "").lower() or None, r.get("phone"),
             r.get("city"), r.get("state"), crd, method, score, pkey,
             json.dumps(r, default=str)[:4000], _now()))
        if cur.fetchone()["fresh"]:
            got["records"] += 1
        if crd:
            got["matched"] += 1
            ref = f"dir:{sid}"
            if r.get("email"):
                got["emails"] += contacts.upsert(
                    conn, crd, "email", r["email"], "directory", person_key=pkey or "",
                    person_name=r.get("person_name"), title=r.get("title"), source_ref=ref)
            if r.get("phone"):
                got["phones"] += contacts.upsert(
                    conn, crd, "phone", r["phone"], "directory", person_key=pkey or "",
                    person_name=r.get("person_name"), title=r.get("title"), source_ref=ref)
        if r.get("person_name"):
            got["people"] += 1
    conn.commit()
    return got


def refresh_counts(conn, sid: int) -> None:
    r = conn.execute("""SELECT COUNT(*) n, COUNT(DISTINCT matched_crd) firms,
        COUNT(*) FILTER (WHERE person_name IS NOT NULL) ppl,
        COUNT(*) FILTER (WHERE email IS NOT NULL) em,
        COUNT(*) FILTER (WHERE phone IS NOT NULL) ph FROM directory_record
        WHERE source_id=?""", (sid,)).fetchone()
    update_source(conn, sid, records_found=r["n"], firms_matched=r["firms"], people_found=r["ppl"],
                  emails_found=r["em"], phones_found=r["ph"])


# ------------------------------------------------------------------ probe and crawl

def probe(conn, sid: int) -> dict:
    """Read the first address and give a verdict: does the generic reader work?"""
    from .crawl import Crawler, browser_available, looks_like_shell
    s = get_source(conn, sid)
    conn.commit()
    if not s:
        return {}
    url = (s["urls"] or "").splitlines()[0].strip()
    verdict, note = "generic", ""
    sample: list[dict] = []
    pages = 0
    with Crawler(respect_robots=bool(s["respect_robots"]), max_pages=10) as cr:
        if s["respect_robots"] and not cr.allowed(url):
            verdict, note = "blocked", ("The site's robots rules ask crawlers not to read this "
                                        "address. Reading it needs the site's permission or a "
                                        "data feed from them.")
        else:
            page = cr.fetch(url, "other")
            pages += 1
            low = (page.html or "").lower()
            if page.status in (401, 403) or (page.status and page.status >= 400 and not page.html):
                verdict, note = "blocked", (f"The site refused the request (HTTP {page.status}). "
                                            "It may block automated readers or require a login; "
                                            "a custom adapter or an export from the site is needed.")
            elif not page.ok:
                verdict, note = "blocked", f"The address could not be read: {page.error or page.status}."
            elif 'type="password"' in low or "type='password'" in low:
                verdict, note = "needs_adapter", ("The page asks for a login. Bellwether does not "
                                                  "sign in to other sites; a custom adapter with an "
                                                  "agreed account, or an export, is needed.")
            else:
                recs = page_records(page.html, page.final_url or url)
                nxt, prof = _links(page.html, page.final_url or url)
                for purl in prof[:3]:
                    pp = cr.fetch(purl, "other")
                    pages += 1
                    if pp.ok:
                        r = _profile_record(pp.html, pp.final_url or purl)
                        if r:
                            recs.append(r)
                sample = recs[:8]
                if len(recs) >= 3:
                    note = (f"Read {len(recs)} records from the first page"
                            + (f" and its first profiles" if prof else "")
                            + (f"; {len(prof)} profile links and "
                               f"{'pagination' if nxt else 'no further pages'} found." if prof or nxt else "."))
                elif looks_like_shell(page.html, page.text) and not browser_available():
                    verdict, note = "needs_adapter", ("The listing is drawn by JavaScript and no "
                                                      "headless browser is installed here, so it reads "
                                                      "as empty. Install the browser (the server image "
                                                      "has one) or add a custom adapter.")
                elif re.search(r"<form[^>]*>.*?(zip|postal|search|location|city)", low, re.S):
                    verdict, note = "needs_adapter", ("Results only appear after a search form is "
                                                      "submitted (for example by ZIP code). A custom "
                                                      "adapter that runs the searches, or the site's "
                                                      "API or export, is needed.")
                else:
                    verdict, note = "needs_adapter", ("No repeated listing of advisers was found on the "
                                                      f"page ({len(recs)} records). Point it at a "
                                                      "results page, or a custom adapter is needed.")
    result = {"verdict": verdict, "explanation": note, "pages_fetched": pages,
              "records": len(sample), "sample": sample, "url": url, "probed_at": _now()}
    update_source(conn, sid, probe_json=json.dumps(result, default=str)[:20000],
                  needs_adapter=0 if verdict == "generic" else 1,
                  adapter_note=None if verdict == "generic" else note,
                  last_message=note)
    return result


def crawl(conn, sid: int, max_pages: int | None = None, deadline_s: int = 1500) -> dict:
    """Read a source: listings, pagination and profile pages, within its page
    budget and a time limit; store, match and file what is found."""
    import time
    from .crawl import Crawler
    s = get_source(conn, sid)
    conn.commit()
    if not s:
        return {}
    budget = max_pages or s["max_pages"] or 200
    started = time.monotonic()
    matcher = Matcher(conn)
    conn.commit()
    totals = Counter()
    queue = [u.strip() for u in (s["urls"] or "").splitlines() if u.strip()]
    seen: set[str] = set()
    profiles: list[str] = []
    fetched = 0
    with Crawler(respect_robots=bool(s["respect_robots"]), max_pages=budget) as cr:
        while (queue or profiles) and fetched < budget and time.monotonic() - started < deadline_s:
            is_profile = not queue
            url = queue.pop(0) if queue else profiles.pop(0)
            if url in seen:
                continue
            seen.add(url)
            if s["respect_robots"] and not cr.allowed(url):
                continue
            page = cr.fetch(url, "other")
            fetched += 1
            if not page.ok:
                continue
            base = page.final_url or url
            if is_profile:
                r = _profile_record(page.html, base)
                recs = [r] if r else []
            else:
                recs = page_records(page.html, base)
                nxt, prof = _links(page.html, base)
                queue += [u for u in nxt if u not in seen]
                profiles += [u for u in prof if u not in seen]
            if recs:
                got = store(conn, sid, recs, matcher)
                totals.update(got)
    now = _now()
    days = int(s["schedule_days"] or 0)
    nxt_run = ((datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
               if days > 0 else "9999-12-31T00:00:00+00:00")
    msg = (f"read {fetched} pages: {totals['records']} new records, {totals['matched']} matched "
           f"to firms, {totals['emails']} new emails, {totals['phones']} new phones")
    update_source(conn, sid, last_run_at=now, next_run_at=nxt_run, last_message=msg)
    refresh_counts(conn, sid)
    return {"pages": fetched, **totals, "message": msg}
