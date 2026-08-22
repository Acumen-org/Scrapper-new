"""Named people for every scoreable firm, from the SEC individual adviser feed.

A scored firm with no named human cannot be dialed. The IA_INDVL feed carries
every registered representative with their current employer's CRD (orgPK), so
this joins people to the firms this system already ranks.

Scope control: only individuals employed at in-band registered advisers are
stored, which keeps the table at call-list size instead of half a million rows.
The zip ships as ~16 XML shards; processing is resumable per shard.

    python -m scripts.ingest_contacts [--max-shards N]
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, guard, runlog, snapshot  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_shard (
    source  TEXT NOT NULL,
    member  TEXT NOT NULL,
    rows    INTEGER,
    done_at TEXT,
    PRIMARY KEY (source, member)
);
"""


def target_crds(conn) -> set[str]:
    return {r["crd"] for r in conn.execute(
        "SELECT crd FROM firm_current WHERE is_era=0 AND raum>=25e6 AND raum<500e6")}


def parse_shard(fh, targets: set[str]):
    """Yield contact rows for individuals currently employed at target firms."""
    for _, ind in etree.iterparse(fh, events=("end",), tag="Indvl"):
        info = ind.find("Info")
        if info is None:
            ind.clear()
            continue
        emps = ind.find("CrntEmps")
        if emps is not None:
            for emp in emps.findall("CrntEmp"):
                crd = emp.get("orgPK")
                if crd not in targets:
                    continue
                name = " ".join(x for x in (info.get("firstNm"), info.get("midNm"),
                                            info.get("lastNm")) if x).title()
                since = None
                regs = emp.find("CrntRgstns")
                if regs is not None:
                    dates = [r.get("stDt") for r in regs.findall("CrntRgstn")
                             if r.get("stDt")]
                    since = min(dates) if dates else None
                loc = emp.find("BrnchOfLocs/BrnchOfLoc")
                place = ""
                if loc is not None:
                    place = ", ".join(x for x in (loc.get("city"), loc.get("state")) if x)
                title = "Adviser rep"
                if since:
                    title += f" since {since[:4]}"
                if place:
                    title += f", {place.title()}"
                yield (crd, info.get("indvlPK"), name, title, "ia_indvl")
        ind.clear()
        while ind.getprevious() is not None:
            del ind.getparent()[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-shards", type=int, default=99)
    args = ap.parse_args()

    cfg = config.load()
    conn = db.connect()
    conn.executescript(SCHEMA); conn.commit()

    snap = conn.execute("SELECT * FROM snapshot WHERE source_key='ia_indvl_feed'"
                        " ORDER BY id DESC LIMIT 1").fetchone()
    if snap is None:
        print("individual feed snapshot not held", file=sys.stderr)
        return 1
    path = config.SNAPSHOT_DIR / snap["rel_path"]
    targets = target_crds(conn)
    print(f"target firms: {len(targets):,}")

    z = zipfile.ZipFile(path)
    done = {r["member"] for r in conn.execute(
        "SELECT member FROM ingest_shard WHERE source='ia_indvl'")}
    todo = [m for m in z.namelist() if m.endswith(".xml") and m not in done]
    todo = todo[:args.max_shards]
    print(f"shards: {len(done)} done, {len(todo)} this run")

    with runlog.Run(conn, "contacts", "ingest", cfg.stamp) as run:
        total = 0
        for m in todo:
            rows = []
            with z.open(m) as fh:
                for row in parse_shard(fh, targets):
                    rows.append(row)
            guard.require_rows(len(rows), f"{m}",
                               "shard parsed but no target-firm reps recognised")
            conn.executemany(
                "INSERT OR REPLACE INTO contact VALUES (?,?,?,?,?)", rows)
            conn.execute("INSERT OR REPLACE INTO ingest_shard VALUES"
                         " ('ia_indvl',?,?,datetime('now'))", (m, len(rows)))
            conn.commit()
            total += len(rows)
            print(f"  {m}: {len(rows):,} contacts")
        run.rows_out = total

    n = conn.execute("SELECT COUNT(*) n FROM contact").fetchone()["n"]
    f = conn.execute("SELECT COUNT(DISTINCT crd) n FROM contact").fetchone()["n"]
    print(f"\ncontacts total: {n:,} across {f:,} firms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
