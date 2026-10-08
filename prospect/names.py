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


# Legal suffixes and place abbreviations read as words: Inc., Ltd., St. Louis.
_PROPER = {"INC": "Inc", "INC.": "Inc.", "LTD": "Ltd", "LTD.": "Ltd.", "CORP": "Corp",
           "CORP.": "Corp.", "CO": "Co", "CO.": "Co.", "ST": "St", "ST.": "St.", "MT": "Mt",
           "MT.": "Mt.", "FT": "Ft", "FT.": "Ft.", "MC": "Mc", "MAC": "Mac"}
# Joining words stay lower case inside a name: Management and Consulting.
_SMALL = {"AND", "OF", "THE", "FOR", "IN", "AT", "ON", "TO", "BY", "WITH", "AN", "A", "DE", "DU"}
# Short all-capital words that are words, not initialisms. Anything else of two
# or three letters (UBS, BMO, AWM, IEQ, AJ) is kept in capitals.
_WORDS = set("""
ONE TWO SIX TEN NEW OLD OAK BAY SUN SKY SEA RED BIG TOP KEY ACE ARC AIM ALL ART ASH BOW BOX
CAP DAY END FAR FIR FOX GAP GEM HUB INN IVY JAY JOY LAW LEE MAX OWL PAR PEN PRO ROW RAY SET
TAX TEA TIE VAN WAY WIN ZEN AGE AIR ARK BEE BAR BUD CUP DEN DOW ELK ELM EYE FIG FIN FLY GOLD
HAT HEN HOP ICE JET LAB LAP LOG MAP MOM NET NUT OAR ORE PAD PIE PIN POD POT RAM RIM ROD RUN
SKI SOL SPA SUM TIN TOE TOY TRI USE WEB WOK YEW ZIP AVE BLVD RD DR HWY
LEE KIM ANN ROY TOM JIM JOE BOB DAN SAM BEN AMY EVA IAN JON KEN LOU MAE NED TED TIM VAL ZOE
ABE ALI AVA DEB DON GUS HAL JAN KAY LEO MEG PAT RON SUE WES CAL ELI EVE GIL IDA LIZ MEL NAT
ROB SAL SID VIC COX LIM LOW RYE ADA ORO PAZ RIO LOS LAS SAN EL LA LE DEL VON VAN DI DA
""".split())
# Four-letter initialisms that contain vowels and so would otherwise read as words.
_ACRONYMS4 = {"PGIM", "HSBC", "TIAA", "BBVA", "CIBC", "AXA", "AEGON", "ICMA", "NAPFA", "FINRA"}


def nice_name(name) -> str:
    """Firm names arrive from the SEC in capitals. Title case reads faster,
    but LLC, LP and initialisms keep their capitals, legal suffixes read as
    words (Inc., Ltd.), and joining words stay lower case inside a name. Text
    already in mixed case is left exactly as filed."""
    if not name:
        return ""
    s = str(name)
    if s != s.upper():
        return s
    # "CONSULTING,LLC" reads better with the space the filer left out.
    s = re.sub(r",(?=[A-Z])", ", ", s)
    first = [True]

    def word(m):
        w = m.group(0)
        lead, first[0] = first[0], False
        bare = w.rstrip(".,")
        if w in _PROPER or bare in _PROPER:
            out = _PROPER.get(w) or _PROPER[bare] + w[len(bare):]
            return out
        if re.fullmatch(r"(?:[A-Z]\.){2,}[A-Z]?", w) or w in _KEEP_UPPER or bare in _KEEP_UPPER:
            return w
        if not lead and bare in _SMALL:
            return w.lower()
        if bare.isalpha() and (2 <= len(bare) <= 3 and bare not in _WORDS and bare not in _SMALL
                               or bare in _ACRONYMS4
                               or (len(bare) <= 5 and not re.search(r"[AEIOUY]", bare))):
            return w
        # O'NEIL -> O'Neil; MCDONALD stays McDonald via the Mc prefix.
        out = w[:1] + w[1:].lower()
        out = re.sub(r"'([a-z])", lambda q: "'" + q.group(1).upper(), out)
        if re.match(r"Mc[a-z]", out):
            out = "Mc" + out[2:3].upper() + out[3:]
        return out
    return re.sub(r"[A-Z][A-Z.']*", word, s)
