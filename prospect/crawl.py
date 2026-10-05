"""Fetch a firm's own website, politely, and decide which of its pages to read.

The people at a firm are on a handful of pages: the team page, one bio page
per person, sometimes a vCard per person, the contact page. The old crawler
read the homepage plus five links that looked promising and missed most of
those. This one reads up to `crawl.max_pages` pages per site (25 by default),
in the order most likely to pay off:

  1. the homepage
  2. team-like pages (team, people, leadership, advisors, professionals, bios,
     staff, who-we-are) and vCard links
  3. about and contact pages
  4. individual bio pages: links found on a team page that are deeper than it,
     carry a person's name, or say "bio" / "read more"
  5. a few other pages on the site

It also reads sitemap.xml (and Sitemap lines in robots.txt) for team-like URLs
that no menu links to, and when nothing on the homepage links to a team,
about or contact page it tries the usual paths (/team, /about-us, /contact).

Fetching goes through Scrapling's FetcherSession, which speaks TLS like a real
Chrome browser. Many adviser sites sit behind Cloudflare or a WAF that refuses
the Python `requests` fingerprint outright, which is why the old crawler came
back "unreachable" on so many of them. What it deliberately does not do: send
a fake Google referer (Scrapling's default `stealthy_headers`), retry hard, or
fetch in parallel. One site at a time, about one request per second per host,
robots.txt obeyed when `crawl.respect_robots` is on, 20 second timeouts, and at
most about 2 MB read from any page.

Some sites are empty JavaScript shells until a browser runs them. When
`crawl.use_browser` is 'auto', a page that looks like a shell (almost no
visible text, a root div, a "please enable JavaScript" notice) is rendered in
headless Chromium through Scrapling's DynamicSession, only if the Playwright
browser is actually installed (checked once per process), and only one browser
runs at a time. If the browser is missing the static page is used as it is;
nothing crashes.

If Scrapling is not importable the crawler falls back to `requests` with a
normal browser User-Agent, so the job degrades instead of failing.
"""

from __future__ import annotations

import heapq
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urldefrag, urljoin, urlparse, urlunparse

from . import harvest

try:
    from scrapling.fetchers import FetcherSession as _FetcherSession
    # Scrapling logs every fetch at INFO and every failure at ERROR. Failures
    # here are expected (dead links, 404 probes) and are recorded on the Page,
    # so its logger is kept quiet and the job's output stays one line a firm.
    logging.getLogger("scrapling").setLevel(logging.CRITICAL)
except Exception:  # pragma: no cover - Scrapling missing or broken
    _FetcherSession = None

# Used only on the requests fallback; Scrapling sends a real Chrome header set.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
# The product token robots.txt rules are checked against. A site with no group
# for it falls back to its '*' rules, which is what applies to us.
ROBOTS_AGENT = "Bellwether"

TIMEOUT_S = 20
MAX_BYTES = 2_000_000
DELAY_S = 1.0
MAX_CRAWL_DELAY_S = 10.0
OTHER_PAGES_MAX = 5          # generic pages read after the useful ones
# Wall-clock budget for one site. The job runs in slices of six firms under a
# 40 minute timeout (prospect/jobs.py); a site whose every page takes the full
# 20 seconds must not eat the slice.
SITE_SECONDS_MAX = 300
SITEMAP_FETCHES_MAX = 3
PROBE_PATHS = {
    "team": ("/team", "/our-team", "/people", "/leadership", "/advisors",
             "/our-people", "/who-we-are"),
    "about": ("/about", "/about-us"),
    "contact": ("/contact", "/contact-us"),
}

# Lower runs first.
PRIORITY = {"home": 0, "team": 10, "vcard": 12, "about": 20, "contact": 22,
            "bio": 30, "other": 80}

TEAM_RE = re.compile(
    r"\b(?:our[\s_-]*)?(?:team|people|leadership|advis[oe]rs|professionals|bios?|staff"
    r"|who[\s_-]*we[\s_-]*are|meet[\s_-]*(?:the[\s_-]*)?team|principals|our[\s_-]*firm"
    r"|management[\s_-]*team|partners|associates|employees|members|personnel"
    r"|meet[\s_-]*us|directory)\b", re.I)
# Matched at the start of a short slug or link text only (see classify_link).
ABOUT_RE = re.compile(r"(?:about(?:\s+us|\s+the\s+firm)?|our\s+story|our\s+history|history"
                      r"|who\s+we\s+are|our\s+company|company|firm\s+overview)\b", re.I)
CONTACT_RE = re.compile(r"\bcontact(?:[\s_-]*us)?\b|\blocations?\b|\boffices?\b", re.I)
BIO_TEXT_RE = re.compile(r"\b(?:bio|biography|profile|read\s+more|learn\s+more|view|meet"
                         r"|more\s+about|full\s+bio)\b", re.I)

# Pages that never carry contact details worth a request.
SKIP_PATH_RE = re.compile(
    r"/(?:blog|news|insights?|articles?|posts?|category|categories|tag|tags|author"
    r"|events?|podcasts?|videos?|webinars?|calculators?|login|log-in|signin|sign-in"
    r"|portal|client-?login|privacy|privacy-policy|terms|terms-of-use|disclosures?"
    r"|legal|form-?adv|adv|crs|cart|checkout|feed|rss|wp-json|wp-admin|wp-content"
    r"|wp-includes|xmlrpc\.php|cdn-cgi|search|comments|trackback|amp|print"
    r"|newsletters?|market-?updates?|commentary|press|media|resources|faqs?"
    r"|resource-?cent(?:er|re)|learning-?cent(?:er|re)|library|education-?cent(?:er|re)"
    r"|knowledge-?cent(?:er|re)|video-?library|white-?papers?)(?:/|$)",
    re.I)
BINARY_RE = re.compile(
    r"\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|7z|gz|tar|csv|png|jpe?g|gif|svg|webp|avif"
    r"|ico|bmp|tiff?|mp[34]|m4a|wav|ogg|webm|mov|avi|mkv|css|js|json|xml|rss|woff2?"
    r"|ttf|eot|otf|exe|dmg|apk|iso|txt|ics)$", re.I)
SOCIAL_HOSTS = ("linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
                "youtube.com", "tiktok.com", "threads.net", "vimeo.com")
# A "website" that is really a profile or link page: read the one page only.
NOT_A_SITE_HOSTS = SOCIAL_HOSTS + ("linktr.ee", "yelp.com", "google.com", "goo.gl",
                                   "bit.ly", "about.me", "brokercheck.finra.org",
                                   "adviserinfo.sec.gov")
TEXT_TYPES = ("html", "xml", "text/plain", "vcard", "text/directory", "text/x-vcard")
LOGIN_RE = re.compile(r"sign-?in|log-?in|logon|/auth|^auth\.|^login\.|portal|/account", re.I)
# curl (6) could not resolve, (7) could not connect, (28) timed out, (35) TLS
# handshake failed; and the requests equivalents. The server is not there.
CONNECT_FAIL_RE = re.compile(r"curl: \((?:6|7|28|35)\)|ConnectionError|ConnectTimeout"
                             r"|NameResolution|timed out", re.I)


@dataclass
class Page:
    url: str
    final_url: str = ""
    status: int = 0
    html: str = ""
    text: str = ""
    content_type: str = ""
    via: str = "static"          # static | browser
    error: str | None = None
    kind: str = "other"          # why it was fetched: home team vcard about contact bio other
    social: list = field(default_factory=list)   # off-site social profile links on it

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400 and bool(self.html) and not self.error

    @property
    def is_vcard(self) -> bool:
        return ("vcard" in self.content_type or "directory" in self.content_type
                or self.url.lower().endswith(".vcf")
                or self.html.lstrip()[:11].upper() == "BEGIN:VCARD")


# ---------------------------------------------------------------- browser

_BROWSER_OK: bool | None = None
_BROWSER_CHECK_LOCK = threading.Lock()
# One headless browser per process at a time. A crawler holds this while its
# browser session is open and releases it on close.
_BROWSER_SLOT = threading.Semaphore(1)


def browser_available() -> bool:
    """Whether Playwright's Chromium is installed, asked once per process.

    Launching a browser that is not installed raises deep inside Playwright
    after a long wait; asking for the executable path and checking the file
    is cheap and cannot crash."""
    global _BROWSER_OK
    with _BROWSER_CHECK_LOCK:
        if _BROWSER_OK is None:
            ok = False
            try:
                from scrapling.fetchers import DynamicSession  # noqa: F401
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    ok = os.path.exists(pw.chromium.executable_path)
            except Exception:
                ok = False
            _BROWSER_OK = ok
        return _BROWSER_OK


def looks_like_shell(html: str, text: str) -> bool:
    """A page that only becomes content once JavaScript runs: almost no visible
    text, plus the usual signs of a client-rendered app."""
    visible = len((text or "").strip())
    if visible >= 700:
        return False
    low = (html or "")[:300_000].lower()
    signs = 0
    if re.search(r"""<div[^>]+id\s*=\s*["'](?:root|app|__next|__nuxt|svelte|main-app|q-app)["']""", low):
        signs += 1
    if re.search(r"<noscript[^>]*>.{0,300}?(?:enable|turn on|requires?|need)\s.{0,20}javascript",
                 low, re.S):
        signs += 1
    if low.count("<script") >= 8:
        signs += 1
    return (visible < 250 and signs >= 1) or signs >= 2


# ---------------------------------------------------------------- urls

def _norm_url(u: str) -> str:
    u = urldefrag(u.strip())[0]
    p = urlparse(u)
    host = (p.hostname or "").lower()
    if p.port and p.port not in (80, 443):
        host = f"{host}:{p.port}"
    path = p.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1:
        path = path.rstrip("/")
    # "/assets/v-cards/Peaden, George.vcf": spaces must be encoded for curl.
    path = quote(path, safe="/%:@!$&'()*+,;=-._~")
    query = "&".join(q for q in p.query.split("&")
                     if q and not q.lower().startswith(("utm_", "fbclid", "gclid", "hsctatracking")))
    return urlunparse(((p.scheme or "https").lower(), host, path, "", query, ""))


def _key(u: str) -> str:
    """Identity of a page for de-duplication: scheme and www. ignored."""
    p = urlparse(_norm_url(u))
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return f"{host}{p.path}{'?' + p.query if p.query else ''}".lower()


def _person_slug(seg: str) -> bool:
    """'jane-doe' or 'jane-a-doe' or 'jane_doe': a URL segment that names a person."""
    seg = seg.lower().split(".")[0]
    bits = [b for b in re.split(r"[-_]", seg) if b]
    if not 2 <= len(bits) <= 4:
        return False
    if not all(b.isalpha() and len(b) <= 15 for b in bits):
        return False
    if any(b in harvest.NON_NAME_WORDS or b in harvest.CREDENTIALS for b in bits if len(b) > 1):
        return False
    return sum(len(b) > 1 for b in bits) >= 2


def _strip_firm(words: list[str], firm_label: str) -> list[str]:
    """Drop the firm's own name from a URL slug or link text. Advisers put the
    firm name in every slug ("acme-wealth-advisors-contact-us") and a name
    with Advisors or Partners in it would otherwise make every page look like
    a team page. Only a run of two or more words that all occur in the domain
    is removed, so a plain "/advisors" still counts."""
    if not firm_label or len(words) < 2:
        return words
    inside = [len(w) >= 3 and w.lower() in firm_label for w in words]
    out, i = [], 0
    while i < len(words):
        j = i
        while j < len(words) and inside[j]:
            j += 1
        if j - i >= 2:
            i = j
            continue
        out.append(words[i])
        i += 1
    return out


def classify_link(url: str, text: str = "", from_kind: str = "other",
                  from_url: str = "", firm_label: str = "") -> str | None:
    """The kind a link is worth fetching as, or None to skip it.

    `firm_label` is the firm's domain without its suffix (acmewealthadvisors),
    used to ignore the firm's own name in slugs and link text."""
    p = urlparse(url)
    path = p.path or "/"
    low = path.lower()
    if low.endswith(".vcf") or re.search(r"/(?:v-?card|vcf|download-?v-?card)/?$", low) \
            or re.search(r"(?:^|&)(?:format|download|type)=v-?card", p.query, re.I):
        return "vcard"
    if BINARY_RE.search(low):
        return None
    if re.search(r"(?:^|&)(?:redirect(?:_?url|_?to)?|return_?url|url|goto|out|next|target)=",
                 p.query, re.I):
        return None           # an outbound-link interstitial, not a page of the site
    segs = [s for s in low.split("/") if s]
    last = segs[-1] if segs else ""
    # Only the last segment and a short link text say what a page is. A blog
    # slug such as 9-facts-about-retirement or how-financial-professionals-
    # can-help mentions "about" and "professionals" without being either.
    last_words = _strip_firm([w for w in re.split(r"[-_.]", last) if w], firm_label)
    text_words = _strip_firm((text or "").split(), firm_label)
    slug = " ".join(last_words) if 0 < len(last_words) <= 4 else ""
    label = " ".join(text_words) if 0 < len(text_words) <= 4 else ""
    if SKIP_PATH_RE.search(low + "/") or re.search(r"/(?:19|20)\d\d/\d\d?/", low):
        if not (slug and TEAM_RE.search(slug) and len(last_words) <= 3):
            return None
    # /team/jane-doe, /our-people/jane-doe: a bio wherever it is linked from
    if len(segs) >= 2 and TEAM_RE.search(segs[-2].replace("-", " ")) and _person_slug(last):
        return "bio"
    if from_kind in ("team", "bio", "about"):
        fp = urlparse(from_url).path.rstrip("/").lower()
        deeper = fp and fp != "/" and low.startswith(fp + "/") and len(last_words) <= 4
        if deeper or harvest.looks_like_name(text) or (
                BIO_TEXT_RE.search(text or "") and _person_slug(last)) or (
                _person_slug(last) and from_kind == "team"):
            return "bio"
    for hay in (slug, label):
        if hay and TEAM_RE.search(hay):
            return "team"
    for hay in (slug, label):
        if hay and ABOUT_RE.match(hay):
            return "about"
    for hay in (slug, label):
        if hay and CONTACT_RE.search(hay):
            return "contact"
    if from_kind == "home" and harvest.looks_like_name(text):
        return "bio"
    return "other"


_HREF_RE = re.compile(r"""<a\b([^>]*?)href\s*=\s*["']([^"'#][^"']*)["']([^>]*)>(.{0,300}?)</a>""",
                      re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def page_links(html: str, base: str) -> list[tuple[str, str]]:
    """(absolute url, anchor text) for every link on the page."""
    out = []
    for _pre, href, _post, inner in _HREF_RE.findall(html or ""):
        href = href.strip()
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "data:", "sms:")):
            continue
        if re.search(r"\{\{|\}\}|%7b%7b|\$\{|<%", href, re.I):
            continue          # an unrendered template placeholder, always a 404
        text = harvest.tidy(_TAG_RE.sub(" ", inner))
        if not text:
            m = re.search(r"""(?:title|aria-label)\s*=\s*["']([^"']+)""", _pre + _post, re.I)
            text = harvest.tidy(m.group(1)) if m else ""
        try:
            out.append((urljoin(base, href), text[:120]))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------- crawler

class Crawler:
    """One polite crawler, reused across firms so the TLS session is too."""

    def __init__(self, *, timeout: int = TIMEOUT_S, max_bytes: int = MAX_BYTES,
                 delay: float = DELAY_S, respect_robots: bool | None = None,
                 use_browser: bool | None = None, max_pages: int | None = None):
        from . import settings
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.delay = delay
        self.respect_robots = (settings.get_bool("crawl.respect_robots")
                               if respect_robots is None else respect_robots)
        self.use_browser = ((settings.get("crawl.use_browser") or "auto").strip().lower() == "auto"
                            if use_browser is None else use_browser)
        self.max_pages = max_pages or settings.get_int("crawl.max_pages", 25) or 25
        self._session_mgr = None   # Scrapling's FetcherSession (a factory)
        self._session = None       # the live session it hands out
        self._req = None
        self._browser = None
        self._holds_slot = False
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, object] = {}
        self._crawl_delay: dict[str, float] = {}
        self._sitemaps: dict[str, list[str]] = {}
        self._dead: set[str] = set()   # scheme://host that failed to connect
        self.social: set[str] = set()
        self.requests = 0
        self.browser_pages = 0
        self.root_error: str | None = None

    # -- lifecycle
    def __enter__(self) -> "Crawler":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self._close_browser()
        if self._session_mgr is not None:
            try:
                self._session_mgr.__exit__(None, None, None)
            except Exception:
                pass
            self._session_mgr = self._session = None
        if self._req is not None:
            try:
                self._req.close()
            except Exception:
                pass
            self._req = None

    def _close_browser(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._holds_slot:
            self._holds_slot = False
            _BROWSER_SLOT.release()

    # -- politeness
    def _wait(self, host: str) -> None:
        gap = max(self.delay, self._crawl_delay.get(host, 0.0))
        last = self._last_hit.get(host)
        if last is not None:
            left = last + gap - time.monotonic()
            if left > 0:
                time.sleep(left)
        self._last_hit[host] = time.monotonic()

    def _robots_for(self, url: str):
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if host in self._robots:
            return self._robots[host]
        self._robots[host] = None
        robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
        page = self._static(robots_url, count=True)
        rp = None
        if page.status == 200 and page.html and "<html" not in page.html[:500].lower():
            body = page.html
            try:
                from protego import Protego
                rp = ("protego", Protego.parse(body))
            except Exception:
                from urllib.robotparser import RobotFileParser
                r = RobotFileParser()
                r.parse(body.splitlines())
                rp = ("stdlib", r)
            maps = re.findall(r"(?im)^\s*sitemap\s*:\s*(\S+)", body)
            self._sitemaps[host] = maps
            delay = None
            try:
                delay = (rp[1].crawl_delay(ROBOTS_AGENT) if rp[0] == "protego"
                         else rp[1].crawl_delay(ROBOTS_AGENT))
            except Exception:
                delay = None
            if delay:
                self._crawl_delay[host] = min(float(delay), MAX_CRAWL_DELAY_S)
        self._robots[host] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = self._robots_for(url)
        if rp is None:
            return True
        try:
            if rp[0] == "protego":
                return rp[1].can_fetch(url, ROBOTS_AGENT)
            return rp[1].can_fetch(ROBOTS_AGENT, url)
        except Exception:
            return True

    # -- fetching
    def _static(self, url: str, count: bool = True) -> Page:
        host = (urlparse(url).hostname or "").lower()
        self._wait(host)
        if count:
            self.requests += 1
        page = Page(url=url, final_url=url)
        try:
            if _FetcherSession is not None:
                if self._session is None:
                    self._session_mgr = _FetcherSession(impersonate="chrome",
                                                        stealthy_headers=False,
                                                        timeout=self.timeout, retries=1)
                    self._session = self._session_mgr.__enter__()
                r = self._session.get(url, timeout=self.timeout, retries=1,
                                      stealthy_headers=False)
                page.status = int(r.status or 0)
                page.final_url = str(r.url or url)
                headers = {str(k).lower(): str(v) for k, v in (r.headers or {}).items()}
                page.content_type = headers.get("content-type", "").lower()
                body = r.body if isinstance(r.body, (bytes, bytearray)) else str(r.body or "").encode()
                enc = getattr(r, "encoding", None) or "utf-8"
            else:
                import requests
                if self._req is None:
                    self._req = requests.Session()
                    self._req.headers.update({
                        "User-Agent": BROWSER_UA,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9"})
                with self._req.get(url, timeout=self.timeout, stream=True,
                                   allow_redirects=True) as r:
                    page.status = r.status_code
                    page.final_url = r.url
                    page.content_type = r.headers.get("content-type", "").lower()
                    chunks, total = [], 0
                    for chunk in r.iter_content(65536):
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= self.max_bytes:
                            break
                    body = b"".join(chunks)
                    enc = r.encoding or "utf-8"
        except Exception as e:
            page.error = f"{type(e).__name__}: {str(e)[:180]}"
            if CONNECT_FAIL_RE.search(page.error):
                p = urlparse(url)
                self._dead.add(f"{p.scheme}://{(p.hostname or '').lower()}")
            return page
        ctype = page.content_type
        if ctype and not any(t in ctype for t in TEXT_TYPES) and not url.lower().endswith(".vcf"):
            page.error = f"not text ({ctype.split(';')[0]})"
            return page
        try:
            page.html = bytes(body[:self.max_bytes]).decode(enc, errors="replace")
        except LookupError:
            page.html = bytes(body[:self.max_bytes]).decode("utf-8", errors="replace")
        return page

    def _render(self, url: str) -> Page | None:
        """The page as a browser sees it, or None when no browser can be had."""
        if not browser_available():
            return None
        if self._browser is None:
            if not _BROWSER_SLOT.acquire(blocking=False):
                return None     # another crawler in this process has the browser
            self._holds_slot = True
            try:
                from scrapling.fetchers import DynamicSession
                self._browser = DynamicSession(headless=True, network_idle=True,
                                               google_search=False, disable_resources=True,
                                               timeout=30_000, retries=1, block_ads=True)
                self._browser.start()
            except Exception:
                self._browser = None
                self._close_browser()
                return None
        host = (urlparse(url).hostname or "").lower()
        self._wait(host)
        self.requests += 1
        page = Page(url=url, final_url=url, via="browser")
        try:
            r = self._browser.fetch(url)
            page.status = int(r.status or 0)
            page.final_url = str(r.url or url)
            page.content_type = "text/html"
            body = r.body if isinstance(r.body, (bytes, bytearray)) else str(r.body or "").encode()
            page.html = bytes(body[:self.max_bytes]).decode("utf-8", errors="replace")
        except Exception as e:
            page.error = f"{type(e).__name__}: {str(e)[:180]}"
        self.browser_pages += 1
        return page

    def fetch(self, url: str, kind: str = "other", *, prefer_browser: bool = False) -> Page:
        """One page, rendered in a browser only when it is an empty shell."""
        page = None
        if prefer_browser and self.use_browser and kind != "vcard":
            page = self._render(url)
            if page is not None and page.ok:
                page.text = harvest.visible_text(page.html)
        if page is None or not page.ok:
            page = self._static(url)
            if page.ok and not page.is_vcard:
                page.text = harvest.visible_text(page.html)
                if (self.use_browser and "html" in (page.content_type or "text/html")
                        and looks_like_shell(page.html, page.text)):
                    rendered = self._render(url)
                    if rendered is not None and rendered.ok:
                        rendered.text = harvest.visible_text(rendered.html)
                        if len(rendered.text) > len(page.text):
                            page = rendered
            elif page.ok:
                page.text = page.html
        page.kind = kind
        return page

    # -- site crawl
    def _start(self, root_url: str) -> tuple[Page | None, list[str]]:
        raw = (root_url or "").strip().strip("\"'")
        if not raw:
            return None, []
        if not re.match(r"(?i)^https?://", raw):
            raw = "https://" + raw.lstrip("/")
        p = urlparse(raw)
        host = (p.hostname or "").lower()
        path = p.path or "/"
        tries = []
        for scheme in ("https", "http") if p.scheme.lower() == "https" else ("http", "https"):
            tries.append(f"{scheme}://{host}{path}")
        alt = host[4:] if host.startswith("www.") else "www." + host
        tries += [f"https://{alt}{path}", f"http://{alt}{path}"]
        last = None

        def dead(u: str) -> bool:
            q = urlparse(u)
            return f"{q.scheme}://{(q.hostname or '').lower()}" in self._dead

        for u in tries:
            # A scheme and host that already failed to connect (on robots.txt
            # or a previous try) is not tried again: a dead site would
            # otherwise cost two 20 second timeouts per variant.
            if dead(u):
                continue
            if self.respect_robots and not self.allowed(u):
                last = Page(url=u, final_url=u, error="robots.txt disallows")
                continue
            if dead(u):
                last = last or Page(url=u, final_url=u, error="host not answering")
                continue
            page = self.fetch(u, "home")
            if page.ok and page.status < 400:
                return page, tries
            last = page
            if page.status and page.status < 500 and not page.error:
                # The server answered (a 404 or 403): another scheme will not help.
                break
        return last, tries

    def crawl_site(self, root_url: str, max_pages: int | None = None,
                   on_page=None) -> list[Page]:
        """Read one firm's site. Returns the pages fetched (failed ones
        included, with `error` or `status` saying why), homepage first.

        `on_page(page)` is called after each page is fetched, so the caller
        can store and commit page by page; no fetch happens while it runs.
        Off-site social profile links seen on the way land in `self.social`."""
        max_pages = max_pages or self.max_pages
        self.social = set()
        self._dead = set()
        self.requests = 0
        self.browser_pages = 0
        self.root_error = None
        budget = max_pages + 15          # robots, sitemaps, probes and failures
        started = time.monotonic()
        pages: list[Page] = []
        try:
            home, _ = self._start(root_url)
            if home is None:
                self.root_error = "no website"
                return []
            if not home.ok:
                self.root_error = home.error or f"HTTP {home.status}"
                pages.append(home)
                if on_page:
                    on_page(home)
                return pages
            pages.append(home)
            if on_page:
                on_page(home)
            final = urlparse(home.final_url)
            orig = urlparse(home.url)
            domains = {harvest.registrable(final.hostname or ""),
                       harvest.registrable(orig.hostname or "")}
            domains.discard("")
            if any((final.hostname or "").endswith(h) for h in NOT_A_SITE_HOSTS):
                return pages
            if (harvest.registrable(final.hostname or "") != harvest.registrable(orig.hostname or "")
                    and LOGIN_RE.search(f"{final.hostname}{final.path}")):
                # The firm's address forwards to a client portal on a vendor's
                # domain (eMoney, a custodian). Crawling on would read the
                # vendor's own team page as the firm's.
                self.root_error = f"website forwards to a client login at {final.hostname}"
                return pages
            shell_site = home.via == "browser"
            # A site that asks for a long Crawl-delay gets it, and fewer pages
            # in exchange, so one firm stays at a couple of minutes.
            delay = self._crawl_delay.get((final.hostname or "").lower(), 0.0)
            if delay > 2:
                max_pages = min(max_pages, max(8, int(150 / delay)))
                budget = max_pages + 8

            firm_label = harvest.registrable(final.hostname or "").split(".")[0]
            seen: set[str] = {_key(home.url), _key(home.final_url)}   # fetched or tried
            queued: dict[str, float] = {}                             # key -> best priority
            queue: list = []
            counter = [0]
            kinds_linked: set[str] = set()
            others = [0]

            def push(url: str, kind: str, bump: float = 0.0) -> None:
                # A link first seen in a menu as "other" and later on the team
                # page as a bio is upgraded; the stale heap entry is skipped.
                k = _key(url)
                prio = PRIORITY[kind] + bump
                if k in seen or queued.get(k, 1e9) <= prio:
                    return
                queued[k] = prio
                counter[0] += 1
                heapq.heappush(queue, (prio, counter[0], url, kind))

            def harvest_links(page: Page) -> None:
                for url, text in page_links(page.html, page.final_url or page.url):
                    pu = urlparse(url)
                    if pu.scheme not in ("http", "https"):
                        continue
                    host = (pu.hostname or "").lower()
                    if any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS):
                        if pu.path.strip("/"):
                            clean = _norm_url(url)
                            self.social.add(clean)
                            page.social.append(clean)
                        continue
                    if harvest.registrable(host) not in domains:
                        # Never leave the firm's own site, with one exception:
                        # a vCard the firm's page links to. Site builders such
                        # as Twenty Over Ten keep them on their own CDN.
                        if not pu.path.lower().endswith(".vcf"):
                            continue
                    kind = classify_link(url, text, page.kind, page.final_url or page.url,
                                         firm_label)
                    if kind is None:
                        continue
                    kinds_linked.add(kind)
                    depth = len([s for s in pu.path.split("/") if s])
                    push(_norm_url(url), kind, bump=min(depth, 5) * 0.1)

            harvest_links(home)

            # Sitemaps: team and bio URLs that no menu links to.
            sm_urls = []
            if self.respect_robots:
                self._robots_for(home.final_url)
            sm_urls = list(self._sitemaps.get((final.hostname or "").lower(), []))
            base = f"{final.scheme}://{final.netloc}"
            if not sm_urls:
                sm_urls = [base + "/sitemap.xml", base + "/sitemap_index.xml"]
            sm_fetched = 0
            sm_queue = list(dict.fromkeys(sm_urls))
            while sm_queue and sm_fetched < SITEMAP_FETCHES_MAX and self.requests < budget:
                sm = sm_queue.pop(0)
                if harvest.registrable(urlparse(sm).hostname or "") not in domains:
                    continue
                if not self.allowed(sm):
                    continue
                sp = self._static(sm)
                sm_fetched += 1
                if not sp.ok or "<loc" not in sp.html.lower():
                    continue
                locs = [harvest.tidy(x) for x in re.findall(r"<loc>\s*(.*?)\s*</loc>", sp.html, re.I | re.S)]
                if "<sitemapindex" in sp.html.lower():
                    children = sorted(locs, key=lambda u: (not re.search(
                        r"page|team|people|staff|bio|member|advis|profile|person", u, re.I), u))
                    sm_queue = children[:SITEMAP_FETCHES_MAX] + sm_queue
                    continue
                for u in locs[:2000]:
                    pu = urlparse(u)
                    if harvest.registrable(pu.hostname or "") not in domains:
                        continue
                    kind = classify_link(u, "", "sitemap", "", firm_label)
                    if kind in ("team", "bio", "vcard"):
                        push(_norm_url(u), kind, bump=0.5)
                    elif kind in ("about", "contact"):
                        push(_norm_url(u), kind, bump=0.5)
                    if kind == "team":
                        kinds_linked.add("team")

            # Common paths, tried only for the kinds of page nothing linked to.
            probes: list[tuple[str, str]] = []
            for kind, paths in PROBE_PATHS.items():
                if kind not in kinds_linked:
                    probes += [(base + p, kind) for p in paths]
            probe_found: set[str] = set()
            home_text = (home.text or "")[:2000]

            n_ok = 1
            strikes = 0
            while n_ok < max_pages and self.requests < budget and (queue or probes):
                if time.monotonic() - started > SITE_SECONDS_MAX:
                    self.root_error = self.root_error or "time budget for the site used up"
                    break
                if queue and (not probes or queue[0][0] <= PRIORITY[probes[0][1]] + 5):
                    prio, _, url, kind = heapq.heappop(queue)
                    k = _key(url)
                    if k in seen or prio > queued.get(k, 1e9):
                        continue      # already read, or superseded by a better entry
                    seen.add(k)
                    probe = False
                else:
                    url, kind = probes.pop(0)
                    if kind in probe_found or _key(url) in seen or _key(url) in queued:
                        continue
                    seen.add(_key(url))
                    probe = True
                if kind == "other":
                    if others[0] >= OTHER_PAGES_MAX:
                        continue
                    others[0] += 1
                if not self.allowed(url):
                    continue
                page = self.fetch(url, kind, prefer_browser=shell_site)
                # A server that starts timing out or answering 429/5xx is
                # telling us to go away; three in a row ends the visit.
                failed = ((page.error is not None and "not text" not in page.error)
                          or page.status == 429 or page.status >= 500)
                if failed:
                    strikes += 1
                    if strikes >= 3:
                        self.root_error = self.root_error or "site stopped answering"
                        if not probe:
                            pages.append(page)
                            if on_page:
                                on_page(page)
                        break
                else:
                    strikes = 0
                fk = _key(page.final_url) if page.final_url else None
                if probe:
                    soft404 = (not page.ok or fk == _key(home.final_url)
                               or (page.text or "")[:2000] == home_text
                               or re.search(r"\b(?:page not found|404)\b",
                                            (page.text or "")[:400], re.I))
                    if soft404:
                        continue      # a missing probe is noise, not a page
                    probe_found.add(kind)
                if page.ok and fk and fk in {_key(p.final_url) for p in pages if p.ok}:
                    continue          # a redirect onto a page already read
                if fk:
                    seen.add(fk)
                pages.append(page)
                if on_page:
                    on_page(page)
                if not page.ok:
                    continue
                n_ok += 1
                if page.via == "browser":
                    shell_site = True
                if not page.is_vcard:
                    harvest_links(page)
            return pages
        finally:
            self._close_browser()


def crawl_site(root_url: str, max_pages: int | None = None, on_page=None) -> list[Page]:
    """Convenience wrapper: one site with a fresh crawler."""
    with Crawler() as c:
        return c.crawl_site(root_url, max_pages=max_pages, on_page=on_page)
