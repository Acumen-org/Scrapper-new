"""Ideal customer profile: which people at a firm each product sells to.

config/icp.yml lists, per product, one or two roles as title patterns (the
compliance officer for Glynac, the investment chief for AcuBooth). The firm
page uses them to put those people first and mark their cards, so the person
to call stands out without anyone reading every title.
"""

from __future__ import annotations

import re
from functools import lru_cache

import yaml

from . import config


@lru_cache(maxsize=1)
def rules() -> dict[str, list[dict]]:
    """product key -> [{label, rx, primary}], in the order the file lists them."""
    try:
        raw = yaml.safe_load((config.CONFIG_DIR / "icp.yml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    out: dict[str, list[dict]] = {}
    for key, roles in raw.items():
        items = []
        for i, r in enumerate(roles or []):
            try:
                items.append({"label": r["label"], "rx": re.compile(r["match"], re.I), "primary": i == 0})
            except (KeyError, TypeError, re.error):
                continue
        out[key] = items
    return out


def match(title: str | None, product_keys, primary_only: bool = False) -> list[dict]:
    """The ICP roles a title fills, for the given products, primary roles
    first: [{product, label, primary}]. primary_only keeps each product's
    main buyer only, for pages that show every product at once."""
    t = title or ""
    if not t.strip():
        return []
    hits = []
    for key in product_keys:
        for r in rules().get(key, []):
            if primary_only and not r["primary"]:
                continue
            if r["rx"].search(t):
                hits.append({"product": key, "label": r["label"], "primary": r["primary"]})
                break
    hits.sort(key=lambda h: not h["primary"])
    return hits
