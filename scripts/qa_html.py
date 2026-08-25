"""Markup and style quality gate: the bugs a status-code check cannot see.

qa_smoke proves every route returns 200 with the right words in it. That says
nothing about whether the page is well formed, which is where formatting and
spacing bugs actually live. This parses each rendered page and fails on:

  duplicate attributes   <a style="x" style="y"> silently drops the second, so
                         the element loses styling nobody notices was missing
  unbalanced tags        a stray </div> or missing </td> reflows a whole page
  nested forms           invalid HTML; the inner form never submits
  template leakage       a literal { } left from an f-string, or the text
                         "None" where a value should have been
  undefined CSS vars     var(--typo) resolves to nothing, so the rule vanishes
  duplicate ids          breaks label targeting and any getElementById
  bad colspan            an empty-state cell that does not span the real table
  orphan separators      a leading or trailing "middot" from a joined list

    python -m scripts.qa_html [--base http://127.0.0.1:8787]
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILURES: list[str] = []
WARNINGS: list[str] = []

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}
# Elements the HTML parser is allowed to leave open: browsers close them
# implicitly and every real page relies on that.
OPTIONAL_CLOSE = {"p", "li", "tr", "td", "th", "thead", "tbody", "option",
                  "dt", "dd", "html", "head", "body"}

PAGES = [
    "/", "/?product=PHH", "/firms", "/firms?preset=phh_a", "/firms?preset=acu",
    "/firms?preset=phh_x", "/firms?preset=comp", "/firms?view=contacts",
    "/firms?view=contacts&preset=phh_a", "/firms?q=WEALTH", "/lists",
    "/health", "/review", "/review?kind=match_13f", "/guide", "/quit",
]

# Filled in at run time: the firm detail page needs a real CRD.
def detail_pages() -> list[str]:
    import sqlite3
    from prospect import config
    c = sqlite3.connect(config.DB_PATH)
    try:
        r = c.execute("SELECT crd FROM tier_a_rank ORDER BY rank LIMIT 1").fetchone()
        return [f"/firm/{r[0]}"] if r else []
    finally:
        c.close()


def fail(page: str, msg: str) -> None:
    FAILURES.append(f"{page}: {msg}")
    print(f"  FAIL  {page}: {msg}")


def warn(page: str, msg: str) -> None:
    WARNINGS.append(f"{page}: {msg}")
    print(f"  warn  {page}: {msg}")


class Checker(HTMLParser):
    def __init__(self, page: str):
        super().__init__(convert_charrefs=True)
        self.page = page
        self.stack: list[tuple[str, int]] = []
        self.ids: Counter = Counter()
        self.form_depth = 0
        self.dupe_attrs: list[str] = []
        self.nested_forms = 0
        self.unbalanced: list[str] = []
        self.in_skip = 0          # inside <script>/<style>
        self.text_parts: list[str] = []
        self.table_cols: list[int] = []   # header count per table
        self.colspans: list[tuple[int, int]] = []  # (colspan, header count)
        self._th_count = 0
        self._in_thead_row = False

    def handle_starttag(self, tag, attrs):
        names = [a for a, _ in attrs]
        for name, n in Counter(names).items():
            if n > 1:
                self.dupe_attrs.append(f"<{tag}> has {n} '{name}' attributes")
        d = dict(attrs)
        if d.get("id"):
            self.ids[d["id"]] += 1
        if tag == "form":
            self.form_depth += 1
            if self.form_depth > 1:
                self.nested_forms += 1
        if tag in ("script", "style"):
            self.in_skip += 1
        if tag == "table":
            self._th_count = 0
        if tag == "th":
            self._th_count += 1
        if tag == "td" and d.get("colspan"):
            try:
                self.colspans.append((int(d["colspan"]), self._th_count))
            except ValueError:
                self.dupe_attrs.append(f"<td colspan='{d['colspan']}'> not a number")
        if tag not in VOID:
            self.stack.append((tag, self.getpos()[0]))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.in_skip = max(0, self.in_skip - 1)
        if tag == "form":
            self.form_depth = max(0, self.form_depth - 1)
        if tag in VOID:
            return
        # find the matching open tag, tolerating implicitly-closed elements
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for orphan, line in self.stack[i + 1:]:
                    if orphan not in OPTIONAL_CLOSE:
                        self.unbalanced.append(
                            f"<{orphan}> (line {line}) never closed before </{tag}>")
                del self.stack[i:]
                return
        self.unbalanced.append(f"stray </{tag}>")

    def handle_data(self, data):
        if not self.in_skip:
            self.text_parts.append(data)

    def text(self) -> str:
        return "".join(self.text_parts)


def css_var_check(page: str, html: str) -> None:
    """Every var(--x) must have a --x definition somewhere in the page CSS."""
    styles = " ".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
    inline = " ".join(re.findall(r'style="([^"]*)"', html))
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", styles))
    used = set(re.findall(r"var\((--[a-z0-9-]+)", styles + " " + inline))
    missing = sorted(used - defined)
    if missing:
        fail(page, f"CSS variables used but never defined: {missing}")


def css_class_check(page: str, html: str) -> None:
    """A class used in the markup with no rule anywhere is a styling bug.

    This is the check that actually finds formatting problems: an element given
    a class that was renamed, typo'd, or whose rule lives in a stylesheet the
    page does not include renders unstyled, and nothing else notices."""
    styles = " ".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
    defined = set(re.findall(r"\.([A-Za-z][\w-]*)", styles))
    used: set[str] = set()
    for attr in re.findall(r'class="([^"]*)"', html):
        used.update(a for a in attr.split() if a)
    # Classes deliberately carrying no style: they exist for JS hooks or as
    # data markers, and PAGE_CSS styles some only via element+class selectors.
    hooks = {"krow", "on", "done"}
    missing = sorted(c for c in used - defined - hooks if not c.startswith("js-"))
    if missing:
        warn(page, f"class used with no CSS rule: {missing}")


def check_page(base: str, path: str, opener) -> None:
    try:
        r = opener.open(base + path, timeout=30)
        html = r.read().decode("utf-8", "replace")
    except Exception as exc:
        fail(path, f"could not fetch: {type(exc).__name__} {exc}")
        return

    ck = Checker(path)
    try:
        ck.feed(html)
    except Exception as exc:
        fail(path, f"parser blew up: {type(exc).__name__} {exc}")
        return

    for msg in dict.fromkeys(ck.dupe_attrs):
        fail(path, msg)
    if ck.nested_forms:
        fail(path, f"{ck.nested_forms} nested <form> element(s); the inner one "
                   f"cannot submit")
    for msg in dict.fromkeys(ck.unbalanced)  :
        fail(path, f"unbalanced markup: {msg}")
    left_open = [t for t, _ in ck.stack if t not in OPTIONAL_CLOSE]
    if left_open:
        fail(path, f"tags left open at end of document: {left_open}")
    dupe_ids = [i for i, n in ck.ids.items() if n > 1]
    if dupe_ids:
        fail(path, f"duplicate id attributes: {dupe_ids}")

    for span, cols in ck.colspans:
        if cols and span != cols:
            warn(path, f"colspan={span} but the table has {cols} headers")

    css_var_check(path, html)

    body = ck.text()
    # Template leakage: an f-string brace that never got substituted.
    leaked = re.findall(r"\{[a-zA-Z_][\w\.\[\]'\"]*\}", body)
    if leaked:
        fail(path, f"unsubstituted template braces in visible text: {leaked[:5]}")
    # A Python value that leaked into the page. Checked per text node and per
    # attribute rather than across the whole body: "None of it proves..." is
    # ordinary prose, whereas a cell whose entire content is "None" is a bug.
    for chunk in ck.text_parts:
        if chunk.strip() in ("None", "none", "[]", "{}", "()"):
            fail(path, f"a text node is exactly {chunk.strip()!r}, a leaked value")
            break
    for m in re.finditer(r'(?:class|value|href|title)="([^"]*)"', html):
        v = m.group(1)
        if re.search(r"(?<![A-Za-z])None(?![A-Za-z])", v) or "sqlite3" in v:
            fail(path, f"leaked value in an attribute: {m.group(0)[:70]!r}")
            break
    for pat, what in ((r"sqlite3\.Row", "sqlite3.Row repr"),
                      (r"&lt;sqlite3", "sqlite3 object repr"),
                      (r"Traceback \(most recent", "a Python traceback")):
        if re.search(pat, body):
            fail(path, f"literal {what} in visible text")
    # Separator hygiene, judged on the RENDERED inline flow rather than source
    # lines: a newline in the markup collapses to a space, so a dot at the end
    # of a source line is usually fine. What is never fine is two separators
    # with nothing between them, which means a value came back empty.
    flow = re.sub(r"\s+", " ", body)
    for bad in ("· ·", "·, ", " ·,", "( ·", "· )"):
        if bad in flow:
            warn(path, f"separator with nothing beside it: {bad!r}")
            break

    css_class_check(path, html)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8787")
    args = ap.parse_args()

    from scripts.qa_smoke import _OPENER, sign_in
    created = sign_in(args.base)

    print("[html] markup, CSS and template checks")
    try:
        for p in PAGES + detail_pages():
            check_page(args.base, p, _OPENER)
    finally:
        if created:
            from scripts.qa_smoke import remove_qa_account
            remove_qa_account()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"HTML QA: {len(FAILURES)} FAILURE(S), {len(WARNINGS)} warning(s)")
        return 1
    print(f"HTML QA: PASS ({len(WARNINGS)} warning(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
