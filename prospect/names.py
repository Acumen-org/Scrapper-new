"""Readable names. SEC filings carry firm names, cities and titles in capitals;
title case reads faster, but initialisms and legal suffixes keep theirs.
Kept dependency-free so scoring scripts can use it without the web app."""

from __future__ import annotations

import re

_KEEP_UPPER = {"LLC", "L.L.C.", "LP", "L.P.", "LLP", "L.L.P.", "INC", "INC.", "PC", "P.C.",
               "USA", "US", "U.S.", "NA", "N.A.", "II", "III", "IV", "RIA", "CPA", "CFA",
               "CFP", "ETF", "NY", "LTD", "LTD.", "PLLC", "SA", "AG", "UK", "PA", "P.A.",
               "CEO", "CIO", "CFO", "COO", "CCO", "CTO", "CMO", "VP", "SVP", "EVP", "MD",
               "LLC,", "IRA", "ESG", "RE"}

_TITLES = {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "professor", "rev", "sir", "dame"}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "cfa", "cfp", "cpa", "esq"}


def first_name(full_name: str | None, given_name: str | None = None) -> str:
    """Use the filed given name, or parse a display/SEC surname-first name.

    Titles and credentials are never returned as a person's first name.
    Initials stay initials; a full given name cannot be inferred from them.
    """
    def first(value):
        parts = str(value or "").strip().split()
        while parts and parts[0].casefold().replace('.', '').strip(',®') in _TITLES:
            parts.pop(0)
        if not parts or parts[0].casefold().replace('.', '').strip(',®') in _SUFFIXES:
            return ""
        return nice_name(parts[0].strip(","))

    given = first(given_name)
    if given:
        return given
    name = str(full_name or "").strip()
    if "," in name:
        left, right = name.split(",", 1)
        # "Smith, Jane Ann" versus "Jane Smith, CFP".
        name = right if first(right) else left
    return first(name)


def person_name(filed: str | None) -> str:
    """Display SEC surname-first records without mangling already mixed-case names."""
    parts = [p.strip() for p in str(filed or '').split(',') if p.strip()]
    if len(parts) > 1 and parts[1].casefold().replace('.', '').strip('®') not in _SUFFIXES:
        parts = parts[1:] + parts[:1]
    return nice_name(' '.join(parts))


def nice_name(name) -> str:
    """Firm names arrive from the SEC in capitals. Title case reads faster,
    but LLC, LP and initialisms keep their capitals. Text already in mixed
    case is left exactly as filed."""
    if not name:
        return ""
    s = str(name)
    if s != s.upper():
        return s

    def word(m):
        w = m.group(0)
        if (w in _KEEP_UPPER or w.rstrip(".") in _KEEP_UPPER
                or (len(w) <= 3 and not re.search(r"[AEIOU]", w))):
            return w
        return w[:1] + w[1:].lower()
    return re.sub(r"[A-Z][A-Z.']*", word, s)
