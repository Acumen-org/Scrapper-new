"""What a person does at a firm, from their title.

Titles arrive in every shape: Schedule A files "MANAGING MEMBER & CHIEF
COMPLIANCE OFFICER", websites print "Sr. Wealth Advisor, CFP(R)", vCards say
"Principal". The firm page and the people lists sort by role (who decides, who
advises, who runs compliance), so each title is reduced to a clean title and
one role. Titles repeat across thousands of firms, so the mapping is kept per
distinct title (title_role): rules place the large majority, and the AI
clean-up job places the rest when an AI provider is connected, stored as such.
"""

from __future__ import annotations

import re

SCHEMA = """
CREATE TABLE IF NOT EXISTS title_role (
    title_key   TEXT PRIMARY KEY,  -- the raw title, lowercased and squeezed
    title_raw   TEXT,
    title_clean TEXT,
    role        TEXT NOT NULL,     -- leadership | advisor | investment | compliance | operations | client_service | other
    source      TEXT NOT NULL,     -- rules | ai
    updated_at  TEXT NOT NULL
);
"""

ROLE_LABEL = {"leadership": "Leadership", "advisor": "Advisor", "investment": "Investments",
              "compliance": "Compliance", "operations": "Operations",
              "client_service": "Client service", "other": "Other"}
ROLE_ORDER = ["leadership", "investment", "advisor", "compliance", "operations",
              "client_service", "other"]

RULES = [
    ("compliance", re.compile(r"\bCCO\b|COMPLIANCE|REGULATORY", re.I)),
    ("leadership", re.compile(r"\bCEO\b|CHIEF EXECUTIVE|PRESIDENT|FOUNDER|MANAGING (MEMBER|"
                              r"PARTNER|DIRECTOR|PRINCIPAL)|\bOWNER\b|CHAIRM|\bPRINCIPAL\b|"
                              r"\bPARTNER\b|\bCOO\b|CHIEF OPERATING|\bCFO\b|CHIEF FINANCIAL|"
                              r"GENERAL COUNSEL|\bMEMBER\b|EXECUTIVE DIRECTOR|SHAREHOLDER", re.I)),
    ("investment", re.compile(r"\bCIO\b|CHIEF INVESTMENT|PORTFOLIO MANAGER|INVESTMENT "
                              r"(ANALYST|OFFICER|DIRECTOR|STRATEG)|RESEARCH|TRADER|\bCFA\b", re.I)),
    ("advisor", re.compile(r"ADVIS|PLANNER|WEALTH MANAGER|RELATIONSHIP MANAGER|\bCFP\b|"
                           r"FINANCIAL CONSULTANT|REPRESENTATIVE|\bIAR\b|PRIVATE (CLIENT|WEALTH)",
                           re.I)),
    ("operations", re.compile(r"OPERATIONS|\bOPS\b|ADMINISTRAT|TECHNOLOGY|\bIT\b|MARKETING|"
                              r"OFFICE MANAGER|CONTROLLER|ACCOUNT|HUMAN RESOURCES|\bHR\b", re.I)),
    ("client_service", re.compile(r"CLIENT (SERVICE|ASSOCIATE|EXPERIENCE)|SERVICE ASSOCIATE|"
                                  r"PARAPLANNER|ASSISTANT|RECEPTION|COORDINATOR", re.I)),
]

SMALL = {"and", "of", "the", "for", "to", "in", "&"}
KEEP_UPPER = {"CEO", "CFO", "COO", "CIO", "CCO", "CTO", "CFP", "CFA", "CPA", "CHFC", "CLU",
              "AIF", "CPWA", "CIMA", "RICP", "CRPC", "LLC", "IAR", "ESG", "VP", "SVP", "EVP",
              "II", "III", "IV", "HR", "IT"}


def clean_title(raw: str | None) -> str:
    t = " ".join((raw or "").replace("(R)", "").replace("®", "").split()).strip(" ,;-")
    if not t:
        return ""
    words = []
    for i, w in enumerate(re.split(r"(\s+|/|&)", t)):
        core = re.sub(r"[^A-Za-z]", "", w)
        if core.upper() in KEEP_UPPER:
            words.append(w.upper())
        elif w.lower() in SMALL and i:
            words.append(w.lower())
        elif w.isupper() or w.islower():
            words.append(w[:1].upper() + w[1:].lower())
        else:
            words.append(w)
    return "".join(words)[:90]


def classify(raw: str | None) -> str:
    t = raw or ""
    for role, rx in RULES:
        if rx.search(t):
            return role
    return "other"


def rank(role: str | None) -> int:
    return ROLE_ORDER.index(role) if role in ROLE_ORDER else len(ROLE_ORDER)


def init(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def key(raw: str | None) -> str:
    return " ".join((raw or "").lower().split())[:200]


def lookup(conn, titles: list[str]) -> dict[str, tuple[str, str]]:
    """raw title -> (clean title, role), from the stored map where it has an
    answer and the rules where it does not."""
    out: dict[str, tuple[str, str]] = {}
    keys = sorted({key(t) for t in titles if t})
    stored: dict[str, tuple[str, str]] = {}
    if keys:
        try:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                for r in conn.execute(
                        f"SELECT title_key, title_clean, role FROM title_role WHERE title_key IN"
                        f" ({','.join('?' * len(chunk))})", chunk):
                    stored[r["title_key"]] = (r["title_clean"], r["role"])
        except Exception:
            conn.rollback()
    for t in titles:
        if not t:
            continue
        out[t] = stored.get(key(t)) or (clean_title(t), classify(t))
    return out
