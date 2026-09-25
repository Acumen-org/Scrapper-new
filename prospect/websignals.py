"""What a firm's own website says about how it works, read deterministically.

Two kinds of evidence the product scores need and no filing carries:

  - the reporting platform behind the client login. Firms link their client
    portal from the homepage, and the link names the vendor: Black Diamond,
    Orion, Tamarac, Addepar or Advyzon. Glynac's portfolio work needs Black
    Diamond today.
  - whether the firm publishes: a blog, insights page or newsletter. Glynac's
    marketing compliance work is worth more where there is outbound content.

Pure functions over HTML, no network: the crawler (scripts/web_enrich) calls
this on every page it fetches, and scripts/web_signals backfills from the pages
already cached.
"""

from __future__ import annotations

import re

HREF_TEXT = re.compile(r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.{0,160}?)</a>',
                       re.I | re.S)
TAGS = re.compile(r"<[^>]+>")

# (signal, pattern matched against link targets and link text)
PLATFORM_PATTERNS = [
    ("platform_black_diamond", re.compile(r"black\s*diamond|blackdiamond|bdportal"
                                          r"|advent\.com", re.I)),
    ("platform_orion", re.compile(r"orionadvisor|orion\s*(connect|client|portal|login)"
                                  r"|orionportal", re.I)),
    ("platform_tamarac", re.compile(r"tamarac", re.I)),
    ("platform_addepar", re.compile(r"addepar", re.I)),
    ("platform_advyzon", re.compile(r"advyzon", re.I)),
    ("compliance_vendor", re.compile(r"hadrius|complysci|riainabox|ria\s+in\s+a\s+box",
                                     re.I)),
]
PUBLISHES = re.compile(r"\b(blog|insights?|newsletters?|articles|commentary"
                       r"|market\s+updates?|perspectives)\b", re.I)


def detect(html: str) -> list[tuple[str, str]]:
    """(signal, evidence) pairs for one page, at most one per signal."""
    found: dict[str, str] = {}
    for href, text in HREF_TEXT.findall(html or ""):
        label = " ".join(TAGS.sub(" ", text).split())[:60]
        hay = f"{href} {label}"
        for sig, pat in PLATFORM_PATTERNS:
            if sig not in found and pat.search(hay):
                found[sig] = f'link "{label or href[:60]}" to {href[:90]}'
        if "publishes" not in found and PUBLISHES.search(label):
            found["publishes"] = f'site links to "{label}"'
    return list(found.items())


def record(conn, crd: str, url: str, html: str, now: str) -> int:
    """Store what one page shows. First evidence per signal wins; a page seen
    again never duplicates a row."""
    n = 0
    for sig, ev in detect(html):
        conn.execute("INSERT INTO web_signal (crd, signal, url, evidence, found_at)"
                     " VALUES (?,?,?,?,?) ON CONFLICT(crd, signal) DO NOTHING",
                     (crd, sig, url, ev, now))
        n += 1
    return n


SCHEMA = """
CREATE TABLE IF NOT EXISTS web_signal (
    crd        TEXT NOT NULL,
    signal     TEXT NOT NULL,      -- platform_* | compliance_vendor | publishes
    url        TEXT NOT NULL,
    evidence   TEXT,
    found_at   TEXT NOT NULL,
    PRIMARY KEY (crd, signal)
);
"""
