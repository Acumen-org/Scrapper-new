"""The contract fixture, made from the contract itself.

Section 14 of docs/outreach-sandbox-plan.md ("Shared contract with
Bellwether") is Cadence's, copied here word for word. Its JSON examples are
the answers Cadence gives and the requests Bellwether sends; this writes them
into scripts/cadence_contract_fixture.json, which qa_cadence.py reads with
Bellwether's own parser. Run it whenever section 14 is copied over again:

    python -m scripts.contract_fixture

qa_cadence.py checks the fixture still equals the section, so a copy of the
section without a new fixture fails the tests.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs" / "outreach-sandbox-plan.md"
FIXTURE = Path(__file__).with_name("cadence_contract_fixture.json")
HEADING = "## Shared contract with Bellwether"

# The section's JSON examples, in the order they appear, and where each goes
# in the fixture. Ping is the same for both contract versions.
BLOCKS = (("ping", "v1_1.ping"), ("push_response",), ("v1_1.push_response",),
          ("status_response",), ("v1_1.status_response",), ("v1_1.suppress_request",),
          ("v1_1.suppress_response",), ("v1_1.owners_response",))

ABOUT = ("The JSON examples of the shared contract (docs/outreach-sandbox-plan.md, section 14: "
         "14.3 ping, 14.8 push response, 14.12 status response, and contract v1.1's push and status "
         "answers, 14.15 Do not contact and 14.16 owners), made by scripts/contract_fixture.py. "
         "scripts/qa_cadence.py reads them with Bellwether's parser and checks they still match the plan.")
ABOUT_11 = ("Contract v1.1 (the cold-outreach plan, 7.3): what Cadence answers a request that carries "
            "X-Cadence-Contract: 1.1, and what Bellwether sends. Every field only adds to v1; a request "
            "without the header gets the v1 answers above.")


def section(text: str) -> str:
    start = text.index(HEADING)
    end = text.index("\n## ", start + 1)
    return text[start:end + 1]


def blocks(text: str) -> list:
    return [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", section(text), re.S)]


def build(text: str) -> dict:
    found = blocks(text)
    if len(found) != len(BLOCKS):
        raise ValueError(f"section 14 has {len(found)} JSON examples, the fixture expects {len(BLOCKS)}")
    out: dict = {"_about": ABOUT, "v1_1": {"_about": ABOUT_11}}
    for keys, value in zip(BLOCKS, found):
        for key in keys:
            if key.startswith("v1_1."):
                out["v1_1"][key[5:]] = value
            else:
                out[key] = value
    # The v1 examples first, as they always were.
    v11 = out.pop("v1_1")
    out["v1_1"] = v11
    return out


def main() -> int:
    fixture = build(PLAN.read_text(encoding="utf-8"))
    FIXTURE.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {FIXTURE.relative_to(ROOT).as_posix()} from section 14 "
          f"({len(BLOCKS)} examples)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
