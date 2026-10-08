"""Plain list questions turned into a search without asking a model.

Most questions people type into Bellwether AI are filters said in words:
"AcuBooth firms with $500M+ and a verified email", "PHH Fund I firms in Texas
or Florida that hired advisors". A thinking model spends twenty to thirty
seconds planning each of those. This reads them directly, in a millisecond.

It is deliberately strict: every word of the question has to be either part
of a filter it recognised or harmless glue ("firms", "which", "on our
lists"). A word or a number it cannot account for (a city, a firm's name, a
condition it has no filter for) and it steps aside, and the model plans the
question as before. Follow-up questions always go to the model, since they
lean on what was said earlier.
"""

from __future__ import annotations

import re

from . import search

STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC", "washington dc": "DC",
}
CODES = set(STATES.values())

NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
           "eight": 8, "nine": 9, "ten": 10}
_N = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)"

PRODUCTS = [
    (r"\bphh\s*fund(\s*(i|1|one)\b)?|\bprairie hill fund\b", "phh_fund"),
    (r"\bphh\s*1031\b", "phh_1031"),
    (r"\bphh\s*jv\b|\bphh joint ventures?\b", "phh_jv"),
    (r"\bacu\s*booth\b", "acubooth"),
    (r"\bglynac\b", "glynac"),
]
TAGS = [
    (r"\bcovered calls?\b", "covered_calls"),
    (r"\bconcentrated (stock|equity|positions?)\b", "concentrated_equity"),
    (r"\btax[- ]management\b|\btax[- ]loss harvesting\b", "tax_management"),
    (r"\bprivate real estate\b", "private_real_estate"),
    (r"\bprivate markets\b", "private_markets"),
    (r"\bmodel portfolios?\b", "model_portfolios"),
    (r"\binvestment committees?\b", "investment_committee"),
    (r"\bcompliance software\b", "compliance_vendor"),
    (r"\b1031 exchanges?\b", "exchange_1031"),
    (r"\bnet[- ]lease\b", "net_lease_types"),
]
# Software platforms, listed apart: "X, Y or Z" among them means any one will do.
PLATFORMS = [
    (r"\bblack ?diamond\b", "platform_black_diamond"),
    (r"\bsalesforce\b", "platform_salesforce"),
    (r"\bredtail\b", "platform_redtail"),
    (r"\b(microsoft )?dynamics\b", "platform_dynamics"),
    (r"\bpractifi\b", "platform_practifi"),
    (r"\bxlr8\b", "platform_xlr8"),
    (r"\bsalentica\b", "platform_salentica"),
    (r"\borion\b", "platform_orion"),
    (r"\btamarac\b", "platform_tamarac"),
    (r"\baddepar\b", "platform_addepar"),
    (r"\badvyzon\b", "platform_advyzon"),
]
CUSTODIANS = r"(schwab|fidelity|pershing|altruist|lpl|interactive brokers)"

# Words that carry no condition of their own. Anything that could (a number,
# "new", "email", "people", "smallest") is left out on purpose, so a question
# using it goes to the model rather than losing that condition.
GLUE = set("""
a an the and or of for to in on at by from with that which who whose what where
show list lists find get give me my our we us i you please can could would
firm firms company companies ria rias adviser advisers advisor advisors
registered investment wealth manager managers advisory practice practices
are is be been have has had do does did use uses using used run runs
advise advises advising file files filing offer offers mention mentions
than more most less least over under above plus first top largest biggest
how many why best strongest weakest compare explain summarise summarize should
recommend tell about lead leads leading sorted ordered ranked rank all any
""".split())

ANSWER_CUES = (r"\b(why|compare|comparison|how many|explain|summari[sz]e|best|strongest|"
               r"weakest|should|recommend|tell me|which is|what makes)\b")


def _num(s: str) -> int:
    return int(s) if s.isdigit() else NUMBERS[s]


def plan(q: str) -> dict | None:
    """A plan shaped like the model's ({mode, firm_name, filters, sort, limit,
    title}), or None when the question needs the model."""
    text = re.sub(r"\s+", " ", (q or "").strip())
    if not text or len(text) > 300:
        return None
    low = " " + text.lower() + " "
    filters: dict = {}
    spans: list[tuple[int, int]] = []

    def take(rx: str):
        found = [m for m in re.finditer(rx, low)
                 if not any(a <= m.start() < b for a, b in spans)]
        spans.extend(m.span() for m in found)
        return found

    for rx, key in PRODUCTS:
        if take(rx):
            if filters.get("product") not in (None, key):
                return None                      # two products: the model decides
            filters["product"] = key

    states = []
    for name, code in sorted(STATES.items(), key=lambda kv: -len(kv[0])):
        if take(rf"\b{name}\b"):
            states.append(code)
    # Two-letter codes only as capitals in the original, after a word that
    # leads into a place ("in TX", "CA and NV"), so "in", "or", "me" and an
    # opening "OK" are never read as Indiana, Oregon, Maine and Oklahoma.
    for m in re.finditer(r"\b([A-Z]{2})\b", text):
        before = text[:m.start()].rstrip()
        lead = re.search(r"(\b(in|from|and|or|across|to|of)|,)$", before.lower())
        if m.group(1) in CODES and lead:
            states.append(m.group(1))
            spans.append((m.start() + 1, m.end() + 1))
    if states:
        filters["states"] = list(dict.fromkeys(states))

    if take(r"\b(microsoft|office) ?365\b|\bm365\b|\boutlook\b|\bexchange online\b"):
        filters["mail_platform"] = "m365"
    elif take(r"\bgoogle workspace\b|\bgmail\b|\bg ?suite\b"):
        filters["mail_platform"] = "google"

    either = re.search(r"\bor\b", low) is not None
    plats = [key for rx, key in PLATFORMS if take(rx)]
    tags = [key for rx, key in TAGS if take(rx)]
    if len(plats) > 1 and either:
        filters["platforms_any"] = plats
    elif len(plats) == 1 and plats[0] in search.PLATFORM_SIGNAL.values():
        filters["reporting_platform"] = next(k for k, v in search.PLATFORM_SIGNAL.items()
                                             if v == plats[0])
    else:
        tags = plats + tags
    if len(tags) > 1 and either:
        return None                              # "covered calls or options": no such filter
    if tags:
        filters["brochure_tags"] = tags

    money = (r"(at least |over |more than |above |under |below |less than |up to |at most )?"
             r"(\$ ?(\d+(?:\.\d+)?) ?(k|thousand|m|mm|mn|million|b|bn|billion)?|"
             r"(\d+(?:\.\d+)?) ?(m|mm|mn|million|b|bn|billion))\b ?\+?"
             r"( ?(in assets|of assets|aum|or more|and up|plus))?")
    for m in take(money):
        amount, unit = (m.group(3), m.group(4)) if m.group(3) else (m.group(5), m.group(6))
        unit = (unit or "").lower()
        mult = 1e9 if unit.startswith("b") else 1e3 if unit in ("k", "thousand") else (
            1e6 if unit else 1)
        value = float(amount) * mult
        if (m.group(1) or "").strip() in ("under", "below", "less than", "up to", "at most"):
            filters["max_aum"] = value
        else:
            filters["min_aum"] = value

    for m in take(rf"\b(at least|over|more than) {_N} (advisors|advisers|iars)\b"):
        filters["min_advisors"] = _num(m.group(2)) + (1 if m.group(1) != "at least" else 0)
    for m in take(rf"\b(fewer than|less than|under|at most) {_N} (advisors|advisers|iars)\b"):
        filters["max_advisors"] = _num(m.group(2)) - (0 if m.group(1) == "at most" else 1)

    take(r"\b((in|over|during) )?(the )?(last|past|previous) (12|twelve) months\b|"
         r"\b((in|over|during) )?(the )?(last|past) year\b|\bthis year\b")
    hired = take(rf"\b(hired|hiring|added|adding|brought on)( (at least )?{_N}( or more)?)?\b")
    if hired:
        n = next((h.group(4) for h in hired if h.group(4)), None)
        filters["min_hires_12m"] = _num(n) if n else 1
    lost = take(rf"\b(lost|losing)( (at least )?{_N}( or more)?)?\b|\bdepartures?\b|\bpeople left\b")
    if lost:
        n = next((x.group(4) for x in lost if x.group(4)), None)
        filters["min_departures_12m"] = _num(n) if n else 1

    if take(r"\b((at least )?(one |a |an |any ))?verified (email|emails|address|addresses)\b"):
        filters["has_verified_email"] = True
    elif take(r"\b((at least )?(one |a |an |any ))?(named|personal|direct) (email|emails)\b"):
        filters["has_personal_email"] = True
    if take(r"\bsec[- ]registered\b"):
        filters["registration"] = "SEC"
    elif take(r"\bstate[- ]registered\b"):
        filters["registration"] = "STATE"
    if take(r"\bprivate funds?\b"):
        filters["private_funds"] = True
    if take(r"\b13-?f\b"):
        filters["files_13f"] = True
    cust = take(rf"\b((custody(ing)?|custodied|clearing) (at|with|through) |(at|with|on) )?{CUSTODIANS}\b")
    if cust:
        name = cust[0].group(6)
        filters["custodian"] = "LPL" if name == "lpl" else name.title()
    if take(r"\bon (our|any of our|any|the) (product )?lists?\b"):
        filters["on_lists"] = True
    if take(r"\b(my firms|owned by me|mine)\b"):
        filters["owner"] = "me"
    elif take(r"\bunclaimed\b|\bnobody owns\b"):
        filters["owner"] = "unclaimed"
    if take(r"\b(with|had|have) (a |any |recent )?signals?\b|\brecent signals?\b"):
        filters["signal_days"] = 90
    for m in take(r"\bscor(e|es|ing) (of )?(at least |over |above )?(\d+)\b\+?"):
        filters["min_score"] = float(m.group(4))

    limit = 20
    for m in take(r"\btop (\d+)\b"):
        limit = max(1, min(int(m.group(1)), 50))
    sort = "score"
    if take(r"\b(largest|biggest|most assets|by (assets|aum|size))( first)?\b"):
        sort = "aum"
    elif take(r"\b(most advisors|by advisors|by headcount)\b"):
        sort = "advisors"
    elif take(r"\b(hiring the most|most hires|hired the most|fastest growing)\b"):
        sort = "hires"
    mode = "answer" if re.search(ANSWER_CUES, low) else "search"

    if not filters:
        return None
    # Everything outside what was recognised must be glue.
    keep = list(low)
    for a, b in spans:
        for i in range(max(0, a), min(len(keep), b)):
            keep[i] = " "
    rest = re.findall(r"[a-z0-9$%']+", "".join(keep))
    if any(w not in GLUE for w in rest):
        return None
    return {"mode": mode, "firm_name": "", "filters": filters, "sort": sort, "limit": limit,
            "title": text[:80]}
