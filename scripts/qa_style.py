"""No em dashes in the application's own source.

A standing rule on this project: no em dashes in any output, comment, or UI
copy. This enforces it over the shipped application, which is where all three
live.

Written in Python rather than as `grep -P` on purpose. On a shell whose locale
is not UTF-8, `grep -P` exits with "supports only unibyte and UTF-8 locales" and
prints nothing, so an `if grep ...` around it reads the error as "no matches
found" and the check silently never fires. A gate that cannot fail is worse than
no gate.

Four files legitimately contain the character as data rather than prose: two
match it in order to strip it out of scraped text, and two search for it as part
of this very rule. They are named here with their reasons instead of being
pattern-matched away, so the exemption cannot quietly widen.

    python -m scripts.qa_style
"""

from __future__ import annotations

import pathlib
import sys

DASHES = {"—": "em dash", "–": "en dash"}

# path -> why the character belongs there
ALLOWED = {
    "scripts/qa_smoke.py":     "asserts pages contain no em dash, so it holds one",
    "scripts/qa_html.py":      "same check over rendered markup",
    "scripts/qa_style.py":     "this file defines the rule",
    "scripts/brochures.py":    "normalises dashes out of PDF text before storing it",
    "scripts/web_enrich.py":   "strips dashes when trimming scraped titles",
}

ROOTS = ("prospect", "scripts", "config")
SUFFIXES = {".py", ".yml", ".yaml"}


def main() -> int:
    bad: list[str] = []
    for root in ROOTS:
        for p in sorted(pathlib.Path(root).rglob("*")):
            if p.suffix not in SUFFIXES or "__pycache__" in p.parts:
                continue
            rel = p.as_posix()
            if rel in ALLOWED:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for ch, name in DASHES.items():
                if ch in text:
                    line = text[:text.index(ch)].count("\n") + 1
                    bad.append(f"{rel}:{line}: {name}")
    if bad:
        print("em dash rule violated:")
        for b in bad:
            print(f"  {b}")
        print("\nUse a comma, a colon, or a sentence break instead.")
        return 1
    checked = sum(1 for r in ROOTS for p in pathlib.Path(r).rglob("*")
                  if p.suffix in SUFFIXES and "__pycache__" not in p.parts)
    print(f"no em dashes in {checked} application files "
          f"({len(ALLOWED)} named exemptions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
