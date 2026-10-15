"""Score every firm for every product list and rebuild the product lists.

Every firm in firm_current is checked against every product, not only the
firms already on some list, so a rule change can bring a firm onto a list as
well as take one off. This is the Scores job behind "Rescore all firms" in
Settings and on each Scoring screen (products.request_rescore queues it), and
it runs after every scoring save and after a reclassification changes any
firm's type.

The rules are config/products.yml, any edits saved on the Scoring screen, and
prospect/products.py; this is only the command that runs them over the whole
universe and prints what came out, so a change in the rules shows up as a
change in these numbers the same day.

Every firm is evaluated, but only the rows whose result changed are written
(usually those of the few firms whose data moved since the last run), and the
run note says how many rows were added, updated and removed. --full deletes
and rewrites both tables the old way; the result is the same.

    python -m scripts.score_products [--full]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, products, runlog  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true",
                    help="delete and rewrite every row instead of only the changed ones")
    args = ap.parse_args()
    cfg = config.load()
    conn = db.connect()
    products.init(conn)

    with runlog.Run(conn, "product_score", "score", products.stamp()) as run:
        wrote: dict = {}
        counts = products.score_all(
            conn, progress=lambda i, n: print(f"  {i:,}/{n:,} firms", flush=True),
            stats=wrote, full=args.full)
        lines = []
        for key in products.product_keys():
            p = products.product(key)
            st = conn.execute(
                "SELECT COUNT(*) FILTER (WHERE score >= 60) hi,"
                " AVG(coverage) cov, MAX(score) top FROM product_score"
                " WHERE product=? AND status='scored'", (key,)).fetchone()
            dq = conn.execute("SELECT COUNT(*) n FROM product_score WHERE product=?"
                              " AND status='disqualified'", (key,)).fetchone()["n"]
            by_type = conn.execute(
                "SELECT COUNT(*) n FROM product_score WHERE product=? AND status='disqualified'"
                " AND reason LIKE ?", (key, "%a kind of firm that never buys%")).fetchone()["n"]
            lowered = conn.execute(
                "SELECT COUNT(*) n FROM product_score WHERE product=? AND status='scored'"
                " AND detail_json LIKE ?", (key, '%"key":"firm_type"%')).fetchone()["n"]
            lines.append(f"{p['name']:<12} {counts[key]:>6,} scored   top={st['top'] or 0:.0f}"
                         f"  60+={st['hi'] or 0:,}  avg coverage={st['cov'] or 0:.0f}%"
                         f"   removed={dq:,} ({by_type:,} by firm type)"
                         f"   lowered as not a usual buyer={lowered:,}")
        scope = conn.execute("SELECT COUNT(*) n FROM firm_scope").fetchone()["n"]
        changed = (f"rows written: {wrote.get('added', 0):,} added, {wrote.get('updated', 0):,}"
                   f" updated, {wrote.get('removed', 0):,} removed,"
                   f" {wrote.get('unchanged', 0):,} unchanged; {wrote.get('scope_changed', 0):,}"
                   f" firm scope rows changed; {wrote.get('reranked', 0)} lists re-ranked"
                   + (" (full rewrite)" if args.full else ""))
        for ln in lines:
            print(ln)
        print(f"{scope:,} firms are on at least one product list")
        print(changed)
        run.rows_out = scope
        run.note("; ".join(lines + [changed]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
