"""The Form ADV answers that say what kind of firm an adviser is.

The firm row carries totals: assets, clients, HNW and other individuals. To
tell a wealth manager from a mutual fund house, a hedge fund, a broker-dealer
or a robo-adviser, the classifier (prospect/firmtype.py) needs more of Part 1A
than any screen does, so this keeps it for the CURRENT filing of every firm,
rebuilt from the newest snapshot of each weekly feed:

  Item 1.O   whether the firm itself has $1 billion or more on its balance sheet
  Item 2.A   why it registers with the SEC: adviser to a registered fund (2A5),
             pension consultant (2A7), internet adviser (2A11), and so on
  Item 5.B   advisory staff, and how many are registered reps of a
             broker-dealer (5B2) or licensed insurance agents (5B5)
  Item 5.D   every client type with its count and assets: individuals, HNW,
             banks, registered funds, pooled vehicles, pension plans,
             charities, governments, other advisers, insurers, sovereigns,
             corporations
  Item 5.E   how it is paid (commissions, performance fees)
  Item 5.G   the services it offers (planning, portfolio management for
             individuals, for funds, for institutions, pension consulting,
             choosing other advisers)
  Item 5.I   wrap fee programs it sponsors or manages
  Item 6.A   other businesses the firm itself is in: broker-dealer, bank,
             trust company, insurance agent and more
  Item 7.A   the kinds of related persons it has
  Item 7.B   whether it advises private funds
  Item 9.D   whether it or a related person acts as a qualified custodian

Stored as one JSON object of raw answers per firm (attribute name to value,
exactly as the feed spells them), so the rules can read any of them by its
form number and nothing here interprets an answer. A firm in both feeds keeps
its SEC answers, the rule ingest_firms applies to the firm row.

    python -m scripts.ingest_adv_profile [--force]
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, runlog, snapshot  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS firm_adv_profile (
    crd          TEXT PRIMARY KEY,
    snapshot_id  INTEGER NOT NULL,
    source       TEXT NOT NULL,      -- adv_feed | adv_state_feed
    filing_date  TEXT,
    answers      TEXT NOT NULL       -- JSON: Part 1A answers, by form attribute
);
"""

SOURCES = ("adv_feed", "adv_state_feed")

# The Part 1A elements whose attributes are kept, whole.
ITEMS = ("Item2A", "Item5A", "Item5B", "Item5D", "Item5E", "Item5G", "Item5I",
         "Item6A", "Item6B", "Item7A", "Item7B", "Item9D")


def parse(fm) -> dict | None:
    """One <Firm> element to {crd, filing_date, answers}."""
    info = fm.find("Info")
    p = fm.find("./FormInfo/Part1A")
    if info is None or p is None:
        return None
    ans: dict[str, str] = {}
    # Registration status, as the feed states it. The state feed carries
    # state exempt reporting advisers too: an <ERA> regulator marked ACTIVE
    # with no approved state registration is an ERA, whatever the firm row's
    # firm_type says.
    rg = fm.find("Rgstn")
    if rg is not None:
        ans["_rgstn"] = f"{rg.get('FirmType') or ''}:{rg.get('St') or ''}"
    st = [r.get("St") or "" for r in fm.findall("./StateRgstn/Rgltrs/Rgltr")]
    era = [f"{r.get('Cd')}:{r.get('St')}" for r in fm.findall("./ERA/Rgltrs/Rgltr")]
    if st:
        ans["_state_rgstn"] = ",".join(sorted(set(st)))[:80]
    if era:
        ans["_state_era"] = ",".join(era)[:80]
    one = p.find("Item1")
    if one is not None:
        for k in ("Q1O", "Q1ODesc"):
            if one.get(k):
                ans[k] = one.get(k)
    for item in ITEMS:
        el = p.find(item)
        if el is None:
            continue
        # A present element with no attributes still says the item was
        # answered; the marker lets the rules tell "all zero" from "missing".
        ans["_" + item] = "1"
        for k, v in el.attrib.items():
            if v not in (None, ""):
                ans[k] = v[:160]
    filing = fm.find("Filing")
    return {"crd": info.get("FirmCrdNb"),
            "filing_date": filing.get("Dt") if filing is not None else None,
            "answers": ans}


def latest_ids(conn) -> dict[str, int]:
    out = {}
    for source in SOURCES:
        snap = snapshot.latest(conn, source)
        if snap is not None:
            out[source] = int(snap["id"])
    return out


def is_current(conn) -> bool:
    """True when the table already holds the newest snapshot of both feeds."""
    want = set(latest_ids(conn).values())
    if not want:
        return True                    # nothing to build from
    try:
        have = {r["snapshot_id"] for r in conn.execute(
            "SELECT DISTINCT snapshot_id FROM firm_adv_profile").fetchall()}
    except Exception:
        conn.rollback()
        return False
    return want <= have


def build(conn, quiet: bool = False) -> int:
    """Rebuild the table from the newest snapshots. Returns firms stored."""
    conn.executescript(SCHEMA)
    conn.commit()
    seen: set[str] = set()
    rows = []
    for source in SOURCES:
        snap = snapshot.latest(conn, source)
        if snap is None:
            continue
        path = config.SNAPSHOT_DIR / snap["rel_path"]
        if not path.exists():
            if not quiet:
                print(f"{source}: snapshot file {path.name} is not on this machine")
            continue
        n = 0
        for _, fm in etree.iterparse(gzip.open(path, "rb"), events=("end",), tag="Firm"):
            rec = parse(fm)
            fm.clear()
            if not rec or not rec["crd"] or rec["crd"] in seen:
                continue
            seen.add(rec["crd"])
            rows.append((rec["crd"], int(snap["id"]), source, rec["filing_date"],
                         json.dumps(rec["answers"], separators=(",", ":"))))
            n += 1
        if not quiet:
            print(f"{source}: {n:,} firms from snapshot {snap['id']}")
    if not rows:
        return 0
    conn.execute("DELETE FROM firm_adv_profile")
    sql = ("INSERT INTO firm_adv_profile (crd, snapshot_id, source, filing_date, answers)"
           " VALUES (?,?,?,?,?)")
    for i in range(0, len(rows), 5000):
        conn.executemany(sql, rows[i:i + 5000])
    conn.commit()
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="rebuild even when the newest snapshots are already loaded")
    args = ap.parse_args()
    cfg = config.load()
    conn = db.connect()
    db.init(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    if not args.force and is_current(conn):
        print("firm_adv_profile already holds the newest feeds")
        return 0
    with runlog.Run(conn, "firm_adv_profile", "ingest", cfg.stamp) as run:
        n = build(conn)
        run.rows_out = n
        run.note(f"{n} firms")
        print(f"{n:,} firms stored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
