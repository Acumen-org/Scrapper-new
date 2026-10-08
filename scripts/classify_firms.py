"""Classify every firm by type: custodian, wirehouse, asset manager, private
fund manager, independent RIA and the rest (prospect/firmtype.py).

One run, in order:

  1  refresh the Form ADV answers the rules read (scripts.ingest_adv_profile)
     when a newer weekly feed has arrived;
  2  classify every firm in firm_current with the rules and the known
     entities in Industry knowledge, keeping every type a person set by hand;
  3  optionally ask Bellwether AI about firms the rules left unsure
     (--ai-limit, within the daily AI limit, feature 'clean');
  4  when any firm's type changed, queue the Scores job, because a type can
     put a firm on a product list or take it off.

Prints the count per type and the run time.

    python -m scripts.classify_firms [--ai-limit 40] [--crd 104559] [--no-rescore]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, firmtype, knowledge, runlog  # noqa: E402
from scripts import ingest_adv_profile  # noqa: E402


def explain(conn, crd: str) -> int:
    """Print the rules' answer for one firm, for checking a rule change."""
    d = firmtype.load_inputs(conn, [crd]).get(crd)
    if d is None:
        print(f"no firm {crd}")
        return 1
    v = firmtype.classify(d, knowledge.matcher(conn))
    print(f"{crd} {d['legal_name']}: {firmtype.label(v.category)} ({v.confidence}%)")
    for e in v.evidence:
        print(f"  - {e}")
    if v.signals:
        print("  signals: " + ", ".join(firmtype.SIGNALS.get(s, s) for s in v.signals))
    stored = firmtype.get(conn, crd)
    if stored and stored["source"] != "rules":
        print(f"  stored: {stored['label']} ({stored['source_label']})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ai-limit", type=int, default=0,
                    help="ask Bellwether AI about at most this many unsure firms")
    ap.add_argument("--crd", help="explain one firm and store nothing")
    ap.add_argument("--no-rescore", action="store_true",
                    help="do not queue the Scores job even if types changed")
    args = ap.parse_args()

    cfg = config.load()
    conn = db.connect()
    firmtype.init(conn)
    knowledge.init(conn)
    if args.crd:
        return explain(conn, args.crd.strip())

    t0 = time.monotonic()
    with runlog.Run(conn, "firm_class", "classify", cfg.stamp) as run:
        if not ingest_adv_profile.is_current(conn):
            n = ingest_adv_profile.build(conn)
            print(f"Form ADV answers refreshed for {n:,} firms")
        res = firmtype.classify_all(
            conn, progress=lambda i, n: print(f"  {i:,}/{n:,} firms", flush=True))
        ai_n = firmtype.ai_pass(conn, args.ai_limit) if args.ai_limit else 0
        took = time.monotonic() - t0
        by = res["by_category"]
        width = max(len(c["label"]) for c in firmtype.CATEGORIES.values())
        for c in firmtype.categories(conn):
            print(f"  {c['label']:<{width}} {by.get(c['key'], 0):>7,}")
        note = (f"{res['firms']:,} firms classified in {took:.1f}s; {res['changed']:,} changed "
                f"type, {res['written']:,} rows written, {ai_n} settled by AI")
        print(note)
        run.rows_out = res["firms"]
        run.note(note)
        if (res["changed"] or ai_n) and not args.no_rescore:
            from prospect import jobs
            jobs.init(conn)
            jobs.request_run(conn, "rescore")
            print("Scores queued, since firm types changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
