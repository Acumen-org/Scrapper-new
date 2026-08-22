"""Read each firm's own website for contact details: people, titles, emails,
phone numbers.

No filing carries an individual adviser's email or direct line; the firm's own
team page is where that lives. This job fetches the homepage plus the few pages
that look like team/about/contact pages, and extracts:

  - firm-level emails and phones (mailto:, tel:, and visible text)
  - person-level matches: names we already know from Schedule A and the
    individual feed, found on the page, with any email/phone/title text that
    sits next to them

Everything deterministic: regexes and known-name matching, no model calls, no
third-party enrichment APIs, nothing paid. Every page fetched is cached to disk
and never refetched (the cache is the crawl's memory), requests carry the real
User-Agent from config, and a slice touches a handful of firms so Pause in the
UI takes effect quickly.

    python -m scripts.web_enrich [--limit N]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, runlog  # noqa: E402

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
    status     TEXT NOT NULL       -- ok | unreachable | no_website
);
"""

# Links whose href or text suggests the page lists people or contact details.
PAGE_HINTS = re.compile(
    r"team|people|advisor|adviser|staff|leadership|about|our-firm|ourfirm"
    r"|professionals|bios?|contact|who-we-are", re.I)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\(\d{3}\)\s?|\d{3}[-.\s])\d{3}[-.\s]\d{4}")
TAG_RE = re.compile(r"<[^>]+>")
HREF_RE = re.compile(r'href\s*=\s*["\']([^"\'#]+)["\'][^>]*>(.{0,120}?)</a>',
                     re.I | re.S)

# Words that make the text after a name plausible as a job title.
TITLE_WORDS = re.compile(
    r"president|chief|ceo|cio|cco|cfo|coo|founder|principal|partner|owner"
    r"|managing|director|advisor|adviser|officer|portfolio|planner|wealth"
    r"|analyst|associate|vice|chairman|cfp|cfa|cpa", re.I)

SKIP_EMAIL = ("sec.gov", "finra.org", "example.", "sentry.", "wixpress",
              "@2x", ".png", ".jpg", ".webp", "user@domain", "email@",
              "name@", "yourname", "@email.", "@example", "@test.")


def norm_phone(raw: str) -> str:
    d = re.sub(r"\D", "", raw)
    return f"({d[:3]}) {d[3:6]}-{d[6:10]}" if len(d) == 10 else raw.strip()


def fetch(url: str, ua: str) -> tuple[int, str]:
    import requests
    r = requests.get(url, headers={"User-Agent": ua}, timeout=15,
                     allow_redirects=True)
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype:
        return r.status_code, ""
    return r.status_code, r.text[:800_000]


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


def candidate_links(base_url: str, html: str) -> list[str]:
    from urllib.parse import urljoin, urlparse
    seen, out = set(), []
    host = urlparse(base_url).netloc.lower().removeprefix("www.")
    for href, text in HREF_RE.findall(html):
        if not PAGE_HINTS.search(href) and not PAGE_HINTS.search(TAG_RE.sub("", text)):
            continue
        full = urljoin(base_url, href.strip())
        pu = urlparse(full)
        if pu.scheme not in ("http", "https"):
            continue
        if pu.netloc.lower().removeprefix("www.") != host:
            continue  # never leave the firm's own site
        full = full.split("?")[0].rstrip("/")
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out[:5]


def known_names(conn, crd: str) -> list[str]:
    names = []
    for r in conn.execute("SELECT name FROM schedule_a WHERE crd=?"
                          " AND is_individual=1", (crd,)):
        parts = [p.strip() for p in r["name"].split(",") if p.strip()]
        if len(parts) >= 2:
            names.append(f"{parts[1]} {parts[0]}".title())
    for r in conn.execute("SELECT name FROM contact WHERE crd=?", (crd,)):
        n = " ".join(r["name"].split()).title()
        # drop middle names for matching: pages rarely print them
        bits = n.split()
        if len(bits) >= 2:
            names.append(f"{bits[0]} {bits[-1]}")
    seen, out = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def extract(conn, crd: str, url: str, html: str, names: list[str], now: str) -> tuple[int, int]:
    """Pull details from one page. Returns (person_matches, emails_found)."""
    text = TAG_RE.sub(" ", html)
    text = re.sub(r"\s+", " ", text)

    emails = []
    for m in EMAIL_RE.findall(html):  # html catches mailto: too
        low = m.lower().strip(".")
        if any(s in low for s in SKIP_EMAIL) or low in emails:
            continue
        emails.append(low)
    phones = []
    for m in PHONE_RE.findall(text):
        p = norm_phone(m)
        if p not in phones:
            phones.append(p)

    people = 0
    lowtext = text.lower()
    for name in names:
        i = lowtext.find(name.lower())
        if i < 0:
            continue
        window = text[i:i + 260]
        # Title: the stretch right after the name, accepted only when it reads
        # like a title and not like the start of a biography sentence. Pages
        # often repeat the name ("Jane Doe Founding Partner Jane Doe joined..."),
        # so name fragments are stripped before judging.
        after = window[len(name):len(name) + 80]
        after = re.sub(re.escape(name), " ", after, flags=re.I)
        for part in name.split():
            after = re.sub(rf"\b{re.escape(part)}\b", " ", after, flags=re.I)
        after = re.split(r"(?i)\b(?:joined|manages|leads|brings|has|is|was|works"
                         r"|began|spent|holds|earned|received|meet|read|learn"
                         r"|view|back|contact|schedule)\b", after)[0]
        cand = re.split(r"[.|•]|  ", after.strip(" ,|-–"))[0]
        cand = " ".join(cand.split())[:48].strip(" ,|-–")
        # Pages often follow the title with a nickname ("Chief Investment
        # Officer Jeff ..."); a trailing word that looks like the person's
        # first name, or an honorific, is that bleed, not the title.
        words = cand.split()
        first3 = name.split()[0][:3].lower()
        while words and (words[-1].lower().startswith(first3)
                         or words[-1].rstrip(".").lower() in ("dr", "mr", "ms", "mrs")):
            words.pop()
        cand = " ".join(words)
        title = (cand if cand and TITLE_WORDS.search(cand[:40])
                 and not re.search(r"\d", cand) else None)
        # email: prefer one whose local part shares the name
        first, last = name.split()[0].lower(), name.split()[-1].lower()
        best_email = None
        for e in emails:
            local = e.split("@")[0]
            if last in local or (first[0] + last) in local or first in local:
                best_email = e
                break
        wemail = EMAIL_RE.findall(window)
        if not best_email and wemail:
            best_email = wemail[0].lower()
        wphone = PHONE_RE.findall(window)
        phone = norm_phone(wphone[0]) if wphone else None
        if title or best_email or phone:
            dup = conn.execute(
                "SELECT 1 FROM web_contact WHERE crd=? AND person=?"
                " AND COALESCE(email,'')=? AND COALESCE(phone,'')=?",
                (crd, name, best_email or "", phone or "")).fetchone()
            if not dup:
                conn.execute("""INSERT INTO web_contact
                    (crd, person, title, email, phone, source_url, found_at)
                    VALUES (?,?,?,?,?,?,?)""",
                             (crd, name, title, best_email, phone, url, now))
            people += 1

    # Firm-level rows for anything not attached to a person. SQLite UNIQUE
    # treats NULLs as distinct, so the constraint cannot dedupe these rows;
    # an explicit existence check has to.
    def have(email, phone) -> bool:
        return conn.execute(
            "SELECT 1 FROM web_contact WHERE crd=? AND person IS NULL"
            " AND COALESCE(email,'')=? AND COALESCE(phone,'')=?",
            (crd, email or "", phone or "")).fetchone() is not None

    attached = {r["email"] for r in conn.execute(
        "SELECT email FROM web_contact WHERE crd=? AND email IS NOT NULL", (crd,))}
    for e in emails[:4]:
        if e not in attached and not have(e, None):
            conn.execute("""INSERT INTO web_contact
                (crd, person, title, email, phone, source_url, found_at)
                VALUES (?,NULL,NULL,?,NULL,?,?)""", (crd, e, url, now))
    for p in phones[:2]:
        if not have(None, p):
            conn.execute("""INSERT INTO web_contact
                (crd, person, title, email, phone, source_url, found_at)
                VALUES (?,NULL,NULL,NULL,?,?,?)""", (crd, p, url, now))
    return people, len(emails)


def todo(conn, limit: int):
    # Scored lists first, then in-band by size; only firms with a real website.
    return conn.execute(f"""
        SELECT f.crd, f.website FROM firm_current f
        LEFT JOIN tier_a_rank ta ON ta.crd = f.crd
        LEFT JOIN (SELECT crd, MIN(rank) rk FROM tier_c_score GROUP BY crd) tc
               ON tc.crd = f.crd
        WHERE f.is_era = 0 AND f.raum >= 25e6 AND f.raum < 500e6
          AND f.website IS NOT NULL AND f.website != ''
          AND f.crd NOT IN (SELECT crd FROM web_enrich_state)
        ORDER BY (ta.crd IS NULL), ta.rank, (tc.rk IS NULL), tc.rk, f.raum DESC
        LIMIT {int(limit)}""").fetchall()


def enrich_one(conn, crd: str, website: str, ua: str, now: str) -> tuple[str, int, int, int]:
    from urllib.parse import urlparse
    url = website if website.lower().startswith("http") else "https://" + website
    pages = people = emails = 0
    try:
        status, html = fetch(url, ua)
    except Exception:
        return "unreachable", 0, 0, 0
    cache_page(conn, crd, url, status, html, now)
    if status >= 400 or not html:
        return "unreachable", 1, 0, 0
    pages = 1
    names = known_names(conn, crd)
    p, e = extract(conn, crd, url, html, names, now)
    people += p
    emails += e
    for link in candidate_links(url, html):
        # Cache is the crawl's memory: a held page is read from disk and still
        # extracted, never refetched. Skipping extraction along with the fetch
        # was a bug that silently ignored every team page on a re-run.
        cached = conn.execute("SELECT cache_path, status FROM web_page WHERE url=?",
                              (link,)).fetchone()
        if cached:
            if cached["cache_path"] and (cached["status"] or 500) < 400:
                try:
                    h2 = gzip.decompress(
                        Path(cached["cache_path"]).read_bytes()).decode(
                        "utf-8", "replace")
                except OSError:
                    continue
                pages += 1
                p2, e2 = extract(conn, crd, link, h2, names, now)
                people += p2
                emails += e2
            continue
        time.sleep(1.0)  # polite: one page a second within a site
        try:
            st2, h2 = fetch(link, ua)
        except Exception:
            continue
        cache_page(conn, crd, link, st2, h2, now)
        pages += 1
        if st2 < 400 and h2:
            p2, e2 = extract(conn, crd, link, h2, names, now)
            people += p2
            emails += e2
    return "ok", pages, people, emails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()

    cfg = config.load()
    ua = cfg.http["user_agent"]
    conn = db.connect()
    conn.executescript(SCHEMA)

    with runlog.Run(conn, "web_enrich", "scrape", cfg.stamp) as run:
        rows = todo(conn, args.limit)
        if not rows:
            print("nothing left to enrich")
            run.skip("all websites visited")
            return 0
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tot_people = tot_emails = 0
        for r in rows:
            status, pages, people, emails = enrich_one(conn, r["crd"], r["website"],
                                                       ua, now)
            conn.execute("INSERT OR REPLACE INTO web_enrich_state VALUES (?,?,?,?,?,?)",
                         (r["crd"], now, pages, people, emails, status))
            conn.commit()
            tot_people += people
            tot_emails += emails
            print(f"  {r['crd']}: {status}, {pages} pages, {people} people matched, "
                  f"{emails} emails")
            time.sleep(0.5)
        run.rows_out = len(rows)
        run.note(f"{len(rows)} firms, {tot_people} people, {tot_emails} emails")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
