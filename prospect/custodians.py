"""Custodian name normalisation against a maintained alias table.

Hand-mapped, not fuzzy. The filed string is never modified: normalisation writes
a separate canonical column so any mapping decision can be reversed by amending
the table and re-running.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from . import config

_SUFFIXES = {
    "INC", "INCORPORATED", "LLC", "LLP", "LP", "LTD", "CO", "CORP", "CORPORATION",
    "COMPANY", "NA", "N A", "THE", "PLC", "SA", "AG", "TRUST", "BANK",
}
# Periods and apostrophes are deleted rather than replaced with a space, so
# "U.S. BANK" reduces to "US BANK" and not "U S BANK". Getting this wrong left
# 751 rows of U.S. Bank unmapped on the first pass.
_DROP = re.compile(r"[.'’]")
_PUNCT = re.compile(r"[^A-Z0-9 ]+")
_WS = re.compile(r"\s+")


def normalise(raw: str | None) -> str:
    """Uppercase, strip punctuation and corporate suffixes, collapse whitespace.

    'Charles Schwab & Co., Inc.' and 'CHARLES SCHWAB & CO INC' both reduce to
    'CHARLES SCHWAB'. Trailing suffixes are stripped repeatedly, but a leading
    token is never removed, so 'BANK OF NEW YORK' keeps its 'BANK'.
    """
    if not raw:
        return ""
    s = _DROP.sub("", raw.upper())
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    parts = s.split(" ")
    while len(parts) > 1 and parts[-1] in _SUFFIXES:
        parts.pop()
    if len(parts) > 1 and parts[0] == "THE":
        parts.pop(0)
    return " ".join(parts)


class CustodianTable:
    def __init__(self, path: Path | None = None):
        path = path or (config.CONFIG_DIR / "custodians.yml")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.version = int(data.get("config_version", 1))
        self.review_threshold = int(data.get("review_threshold_rows", 200))
        self.entities = data["entities"]
        self.migrations = data.get("migrations", []) or []
        self._canon = {e["id"]: e["canonical"] for e in self.entities}
        self._merge = {e["id"]: e["merged_into"] for e in self.entities if e.get("merged_into")}
        # Longest prefix first so 'PERSHING ADVISOR' beats 'PERSHING' where the
        # table intends a more specific entity to win.
        self._prefixes: list[tuple[str, str]] = []
        for e in self.entities:
            for p in e.get("prefixes", []) or []:
                self._prefixes.append((normalise(p) or p.strip().upper(), e["id"]))
            for a in e.get("aliases", []) or []:
                self._prefixes.append((normalise(a), e["id"]))
        self._prefixes.sort(key=lambda kv: -len(kv[0]))

    def canonical_name(self, entity_id: str | None) -> str | None:
        return self._canon.get(entity_id) if entity_id else None

    def match(self, raw: str | None) -> str | None:
        """Return entity id, or None if unmapped."""
        n = normalise(raw)
        if not n:
            return None
        for pref, eid in self._prefixes:
            if n == pref or n.startswith(pref + " "):
                return eid
        return None

    def effective_entity(self, entity_id: str | None, as_of: str | None = None) -> str | None:
        """Apply dated consolidations, e.g. TD Ameritrade becomes Schwab.

        `as_of` is the filing date. Before the merge effective date the original
        entity stands; on or after it, the successor does.
        """
        if entity_id is None:
            return None
        target = self._merge.get(entity_id)
        if not target:
            return entity_id
        ent = next(e for e in self.entities if e["id"] == entity_id)
        eff = ent.get("merged_effective")
        if as_of and eff and as_of < eff:
            return entity_id
        return target

    def is_migration(self, from_id: str | None, to_id: str | None,
                     when: str | None) -> str | None:
        """Return the migration rule id if this transition is a known consolidation.

        A firm moved between the two sides of a custodian consolidation was moved
        by its custodian, not by choice. Those transitions carry no buying signal
        and are suppressed from the trigger and excluded from its base rate.
        """
        if not from_id or not to_id or from_id == to_id:
            return None
        for m in self.migrations:
            if m["from"] == from_id and m["to"] == to_id:
                if when and not (m["window_start"] <= when <= m["window_end"]):
                    continue
                return m["id"]
        return None


def load() -> CustodianTable:
    return CustodianTable()
