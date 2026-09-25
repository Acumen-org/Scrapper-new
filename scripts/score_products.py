"""Score every firm for every product list and rebuild the product lists.

Replaces the old tier A and tier C ranking. The rules are config/products.yml
plus prospect/products.py; this is only the command that runs them over the
whole universe and prints what came out, so a change in the rules shows up as
a change in these counts the same week.

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
            tiers = {r["tier"]: r["n"] for r in conn.execute(
                "SELECT tier, COUNT(*) n FROM product_score WHERE product=?"
                " AND status='scored' GROUP BY tier", (key,))}
            dq = conn.execute("SELECT COUNT(*) n FROM product_score WHERE product=?"
                              " AND status='disqualified'", (key,)).fetchone()["n"]
            tier_s = "  ".join(f"{t[1]}={tiers.get(str(t[1]), 0):,}" for t in p["tiers"])
            lines.append(f"{p['name']:<12} {counts[key]:>6,} scored   {tier_s}"
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
