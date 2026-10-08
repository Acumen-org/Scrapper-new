"""Find contacts for one firm now: every discovery step, in order.

The background jobs work through all firms on their own schedules. This is
the button on a firm page for when someone wants that firm done now: read its
website, search the web for its people's profiles and published addresses,
work out and verify each person's email against the mail server, ask the AI
researcher for whoever is still missing, then refresh the People index so the
results show everywhere. Steps whose script is not installed are skipped.

    python -m scripts.find_contacts --crd 123456 [--url https://their-site.com]
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STEPS = [
    ("scripts.web_enrich", ["--crd", "{crd}"], 900),
    ("scripts.search_contacts", ["--crd", "{crd}"], 600),
    ("scripts.hunt_emails", ["--crd", "{crd}"], 900),
    ("scripts.verify_emails", ["--crd", "{crd}"], 600),
    ("scripts.ai_research", ["--crd", "{crd}"], 600),
    ("scripts.build_people_index", [], 600),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--crd", required=True)
    ap.add_argument("--url", default="")
    args = ap.parse_args()
    if not args.crd.isdigit():
        print("not a CRD")
        return 2
    for module, argv, timeout in STEPS:
        if importlib.util.find_spec(module) is None:
            continue
        cmd = [sys.executable, "-m", module] + [a.format(crd=args.crd) for a in argv]
        if module == "scripts.web_enrich" and args.url:
            cmd += ["--url", args.url]
        print(f"-- {module}", flush=True)
        try:
            subprocess.run(cmd, cwd=str(ROOT), timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            print(f"   {module} ran past {timeout}s; moving on", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
