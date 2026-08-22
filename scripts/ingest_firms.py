"""Parse a captured feed snapshot into the firm table.

One row per firm per snapshot, never updated in place. Two snapshots of the same
firm are two rows, which is what makes the forward-looking triggers a diff rather
than a guess.

Exempt reporting advisers are loaded and labelled, never silently dropped. They
are excluded from scoring by the is_era flag rather than by omission, because
they are useful as competitive intelligence: an ERA advising a real estate fund
is a competing sponsor, and a candidate for Form D tracking.

    python -m scripts.ingest_firms [--snapshot N]
"""

from __future__ import annotations

import argparse
import gzip
import sys
from pathlib import Path

from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, guard, runlog, snapshot  # noqa: E402

# Item 5.D client-type suffixes. A = individuals other than high net worth,
# B = high net worth individuals. Suffix 1 is the client count, 3 the AUM.
# Total clients is summed across all categories: Item 5.C(1) reads 0 on records
# whose 5.D counts are non-zero, so it is not the field it appears to be.
CLIENT_CATS = "ABCDEFGHIJKLMN"


def _i(v):
    if v in (None, ""):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


# Form ADV Item 1.I asks for every website AND every social media page, in one
# list, in no useful order. A third of firms list LinkedIn or Facebook first,
# and taking WebAddrs[0] made linkedin.com the "website" of 4,338 firms, which
# then became the domain of every guessed email address. Pick the first address
# that is not a social platform; fall back to the social one only when the firm
# filed nothing else.
SOCIAL_HOSTS = ("linkedin.com", "facebook.com", "twitter.com", "x.com",
                "instagram.com", "youtube.com", "tiktok.com", "medium.com",
                "vimeo.com", "spotify.com", "pinterest.com", "threads.net",
                "yelp.com")


def pick_website(item1) -> str | None:
    addrs = [(w.text or "").strip() for w in item1.findall("./WebAddrs/WebAddr")
             if w.text and w.text.strip()] if item1 is not None else []
    for a in addrs:
        low = a.lower()
        if not any(h in low for h in SOCIAL_HOSTS):
            return a
    return addrs[0] if addrs else None


def _attr(parent, tag, name):
    """Attribute of a child element, None when either is absent. The state feed
    omits elements the SEC feed always carries (Item5F on a third of records),
    so nothing here may assume presence."""
    el = parent.find(tag) if parent is not None else None
    return el.get(name) if el is not None else None


def parse_firm(fm, regulator: str) -> dict:
    info = fm.find("Info")
    addr = fm.find("MainAddr")
    rgstn = fm.find("Rgstn")
    filing = fm.find("Filing")
    p = fm.find("./FormInfo/Part1A")
    i5d = p.find("Item5D")

    clients_total = 0
    for cat in CLIENT_CATS:
        n = _i(i5d.get(f"Q5D{cat}1")) if i5d is not None else None
        if n:
            clients_total += n

    if rgstn is not None:
        # SEC feed: one Rgstn element with type and approval date.
        ftype = rgstn.get("FirmType")
        registered = rgstn.get("Dt")
    else:
        # State feed: no Rgstn; registration lives in StateRgstn/Rgltrs, one
        # row per state. The earliest approval date is when the firm became an
        # adviser anywhere, which is what the new-registration trigger needs.
        ftype = "State Registered"
        dates = [r.get("Dt") for r in fm.findall("./StateRgstn/Rgltrs/Rgltr")
                 if r.get("Dt")]
        registered = min(dates) if dates else None

    return {
        "crd": info.get("FirmCrdNb"),
        "legal_name": info.get("LegalNm"),
        "business_name": info.get("BusNm"),
        "firm_type": ftype,
        "is_era": 1 if ftype == "ERA" else 0,
        "sec_number": info.get("SECNb"),
        "city": addr.get("City") if addr is not None else None,
        "state": addr.get("State") if addr is not None else None,
        "country": addr.get("Cntry") if addr is not None else None,
        "postal_code": addr.get("PostlCd") if addr is not None else None,
        "website": pick_website(p.find("Item1")),
        "phone": (addr.get("PhNb") or "").strip() or None if addr is not None else None,
        "regulator": regulator,
        "filing_date": filing.get("Dt") if filing is not None else None,
        "registered_date": registered,
        "total_employees": _i(_attr(p, "Item5A", "TtlEmp")),
        "iar_count": _i(_attr(p, "Item5B", "Q5B1")),
        "raum": _i(_attr(p, "Item5F", "Q5F2C")),
        "raum_disc": _i(_attr(p, "Item5F", "Q5F2A")),
        "raum_nondisc": _i(_attr(p, "Item5F", "Q5F2B")),
        "clients_total": clients_total or None,
        "hnw_clients": _i(i5d.get("Q5DB1")) if i5d is not None else None,
        "hnw_aum": _i(i5d.get("Q5DB3")) if i5d is not None else None,
        "retail_clients": _i(i5d.get("Q5DA1")) if i5d is not None else None,
        "retail_aum": _i(i5d.get("Q5DA3")) if i5d is not None else None,
        "q5k3": _attr(p, "Item5K", "Q5K3"),
        "q7b": _attr(p, "Item7B", "Q7B"),
        "disciplinary": _attr(p, "Item11", "Q11"),
    }


COLS = ["snapshot_id", "crd", "legal_name", "business_name", "firm_type", "is_era",
        "sec_number", "city", "state", "country", "postal_code", "website", "phone",
        "regulator", "filing_date", "registered_date", "total_employees", "iar_count",
        "raum", "raum_disc", "raum_nondisc", "clients_total", "hnw_clients", "hnw_aum",
        "retail_clients", "retail_aum", "q5k3", "q7b", "disciplinary"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", type=int, default=None)
    ap.add_argument("--source", default="adv_feed",
                    choices=["adv_feed", "adv_state_feed"])
    args = ap.parse_args()
    source_key = args.source
    regulator = "STATE" if source_key == "adv_state_feed" else "SEC"

    cfg = config.load()
    conn = db.connect()
    db.init(conn)
    db.init_firm(conn)

    if args.snapshot:
        snap = conn.execute("SELECT * FROM snapshot WHERE id=?", (args.snapshot,)).fetchone()
    else:
        snap = snapshot.latest(conn, source_key)
    if snap is None:
        print(f"no {source_key} snapshot held", file=sys.stderr)
        return 1
    snap_id = int(snap["id"])
    path = config.SNAPSHOT_DIR / snap["rel_path"]

    # A firm mid-transition can appear in both feeds. The SEC row wins: it is
    # the same Form ADV either way, and one row per firm in firm_current is a
    # correctness requirement for every count and score downstream.
    skip_crds: set = set()
    if regulator == "STATE":
        skip_crds = {r["crd"] for r in conn.execute("""
            SELECT crd FROM firm WHERE snapshot_id =
              (SELECT MAX(s.id) FROM snapshot s
               WHERE s.source_key='adv_feed'
                 AND EXISTS (SELECT 1 FROM firm f WHERE f.snapshot_id=s.id))""")}

    with runlog.Run(conn, source_key, "ingest_firms", cfg.stamp) as run:
        done = conn.execute("SELECT COUNT(*) c FROM firm WHERE snapshot_id=?",
                            (snap_id,)).fetchone()["c"]
        if done:
            print(f"snapshot {snap_id} already ingested ({done:,} firms)")
            run.skip(f"snapshot {snap_id} already ingested")
            return 0

        print(f"parsing snapshot {snap_id} published {snap['published_at']}")
        exp = cfg.source(source_key)["expected_structure"]
        sql = f"INSERT INTO firm ({','.join(COLS)}) VALUES ({','.join('?'*len(COLS))})"
        batch, n, skipped, checked = [], 0, 0, False
        counts: dict = {}

        for _, fm in etree.iterparse(gzip.open(path, "rb"), events=("end",), tag="Firm"):
            if not checked:
                guard.assert_xml_record(fm, exp, f"{path.name} first <Firm>")
                checked = True
            rec = parse_firm(fm, regulator)
            if rec["crd"] in skip_crds:
                skipped += 1
                fm.clear()
                continue
            counts[rec["firm_type"]] = counts.get(rec["firm_type"], 0) + 1
            batch.append([snap_id] + [rec[c] for c in COLS[1:]])
            n += 1
            if len(batch) >= 5000:
                conn.executemany(sql, batch); conn.commit(); batch.clear()
            fm.clear()
        if batch:
            conn.executemany(sql, batch); conn.commit()

        print(f"  {n:,} firms  " + "  ".join(f"{k}={v:,}" for k, v in counts.items())
              + (f"  ({skipped:,} skipped, already in the SEC feed)" if skipped else ""))
        rc = cfg.source(source_key).get("row_count", {})
        run.check_row_delta(n, rc.get("warn_pct_change", 5.0), rc.get("min_plausible"))
        run.note("; ".join(f"{k}={v}" for k, v in counts.items()))
        if run.flagged:
            print(f"FLAGGED: {run.message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
