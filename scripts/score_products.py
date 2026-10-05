"""Score every firm for every product list and rebuild the product lists.

The rules are config/products.yml, any edits saved on the Scoring screen, and
prospect/products.py; this is only the command that runs them over the whole
universe and prints what came out, so a change in the rules shows up as a
change in these numbers the same day.

    python -m scripts.score_products
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, products, runlog  # noqa: E402


def main() -> int:
    cfg = config.load()
    conn = db.connect()
    products.init(conn)

    with runlog.Run(conn, "product_score", "score", products.stamp()) as run:
        counts = products.score_all(
            conn, progress=lambda i, n: print(f"  {i:,}/{n:,} firms", flush=True))
        lines = []
        for key in products.product_keys():
            p = products.product(key)
            st = conn.execute(
                "SELECT COUNT(*) FILTER (WHERE score >= 60) hi,"
                " AVG(coverage) cov, MAX(score) top FROM product_score"
                " WHERE product=? AND status='scored'", (key,)).fetchone()
            dq = conn.execute("SELECT COUNT(*) n FROM product_score WHERE product=?"
                              " AND status='disqualified'", (key,)).fetchone()["n"]
            lines.append(f"{p['name']:<12} {counts[key]:>6,} scored   top={st['top'] or 0:.0f}"
                         f"  60+={st['hi'] or 0:,}  avg coverage={st['cov'] or 0:.0f}%"
                         f"   disqualified={dq:,}")
        scope = conn.execute("SELECT COUNT(*) n FROM firm_scope").fetchone()["n"]
        for ln in lines:
            print(ln)
        print(f"{scope:,} firms are on at least one product list")
        run.rows_out = scope
        run.note("; ".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
