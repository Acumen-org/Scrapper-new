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
