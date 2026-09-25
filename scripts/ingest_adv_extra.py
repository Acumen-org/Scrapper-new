"""Parse the Form ADV answers the product scores read beyond the core firm row.

The firm table carries the fields every screen needs. The product scores need
more of Part 1A, and adding each one as a firm column would mean re-ingesting
every snapshot. So this keeps them separately, for the CURRENT filing only,
rebuilt from the newest snapshot of each feed:

  Item 5.G   advisory services offered (financial planning, portfolio
             management for individuals, selection of other advisers, ...)
  Item 5.L   marketing: whether advertisements include performance results,
             testimonials, endorsements, third-party ratings, hypothetical or
             predecessor performance
  Item 7.A   the kinds of related persons the firm has (a bank, an insurer,
             a broker-dealer), which is how "independent" is judged
  Item 1.I   whether the firm lists any social media pages, and which

Stored as flags rather than raw attributes so a score reads a named field and
the firm page can say in plain words what the firm answered.

    python -m scripts.ingest_adv_extra
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, runlog, snapshot  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS firm_adv_extra (
    crd                 TEXT PRIMARY KEY,
    snapshot_id         INTEGER NOT NULL,
    filing_date         TEXT,
    -- Item 5.G services
    svc_financial_planning   INTEGER,
    svc_portfolio_individuals INTEGER,
    svc_portfolio_institutions INTEGER,
    svc_pooled_vehicles      INTEGER,
    svc_selects_advisers     INTEGER,
    -- Item 5.L marketing
    ad_performance      INTEGER,   -- 5.L(1)(a)
    ad_specific_advice  INTEGER,   -- 5.L(1)(b)
    ad_testimonials     INTEGER,   -- 5.L(1)(c)
    ad_endorsements     INTEGER,   -- 5.L(1)(d)
    ad_ratings          INTEGER,   -- 5.L(1)(e)
    ad_paid_promoters   INTEGER,   -- 5.L(2)
    ad_hypothetical     INTEGER,   -- 5.L(3)
    ad_predecessor      INTEGER,   -- 5.L(4)
    -- Item 7.A related persons
    rel_broker_dealer   INTEGER,
    rel_bank            INTEGER,
    rel_trust_company   INTEGER,
    rel_insurance       INTEGER,
    rel_real_estate     INTEGER,
    rel_pooled_sponsor  INTEGER,
    -- Item 1.I
    social_hosts        TEXT,      -- JSON list of social platforms listed
    web_addresses       INTEGER,
    config_stamp        TEXT NOT NULL
);
"""

SOCIAL = {"linkedin.com": "LinkedIn", "facebook.com": "Facebook",
          "twitter.com": "X", "x.com": "X", "instagram.com": "Instagram",
          "youtube.com": "YouTube", "tiktok.com": "TikTok",
          "medium.com": "Medium", "vimeo.com": "Vimeo", "threads.net": "Threads"}


def yn(el, attr) -> int | None:
    if el is None:
        return None
    v = el.get(attr)
    if v is None:
        return None
    return 1 if v.upper() == "Y" else 0


def parse(fm) -> dict | None:
    info = fm.find("Info")
    p = fm.find("./FormInfo/Part1A")
    if info is None or p is None:
        return None
    g, l, a = p.find("Item5G"), p.find("Item5L"), p.find("Item7A")
    filing = fm.find("Filing")
    socials: list[str] = []
    addrs = p.findall("./Item1/WebAddrs/WebAddr")
    for w in addrs:
        low = (w.text or "").lower()
        for host, name in SOCIAL.items():
            if host in low and name not in socials:
                socials.append(name)
    return {
        "crd": info.get("FirmCrdNb"),
        "filing_date": filing.get("Dt") if filing is not None else None,
        "svc_financial_planning": yn(g, "Q5G1"),
        "svc_portfolio_individuals": yn(g, "Q5G2"),
        "svc_portfolio_institutions": yn(g, "Q5G5"),
        "svc_pooled_vehicles": yn(g, "Q5G4"),
        "svc_selects_advisers": yn(g, "Q5G7"),
        "ad_performance": yn(l, "Q5L1A"),
        "ad_specific_advice": yn(l, "Q5L1B"),
        "ad_testimonials": yn(l, "Q5L1C"),
        "ad_endorsements": yn(l, "Q5L1D"),
        "ad_ratings": yn(l, "Q5L1E"),
        "ad_paid_promoters": yn(l, "Q5L2"),
        "ad_hypothetical": yn(l, "Q5L3"),
        "ad_predecessor": yn(l, "Q5L4"),
        "rel_broker_dealer": yn(a, "Q7A1"),
        "rel_bank": yn(a, "Q7A8"),
        "rel_trust_company": yn(a, "Q7A9"),
        "rel_insurance": yn(a, "Q7A12"),
        "rel_real_estate": yn(a, "Q7A14"),
        "rel_pooled_sponsor": yn(a, "Q7A16"),
        "social_hosts": json.dumps(socials),
        "web_addresses": len(addrs),
    }


COLS = ["crd", "snapshot_id", "filing_date", "svc_financial_planning",
        "svc_portfolio_individuals", "svc_portfolio_institutions",
        "svc_pooled_vehicles", "svc_selects_advisers", "ad_performance",
        "ad_specific_advice", "ad_testimonials", "ad_endorsements", "ad_ratings",
        "ad_paid_promoters", "ad_hypothetical", "ad_predecessor",
        "rel_broker_dealer", "rel_bank", "rel_trust_company", "rel_insurance",
        "rel_real_estate", "rel_pooled_sponsor", "social_hosts", "web_addresses",
        "config_stamp"]


def main() -> int:
    cfg = config.load()
    conn = db.connect()
    db.init(conn)
    conn.executescript(SCHEMA)
    conn.commit()

    total = 0
    with runlog.Run(conn, "firm_adv_extra", "ingest", cfg.stamp) as run:
        # SEC first: a firm in both feeds keeps its SEC answers, the same rule
        # ingest_firms applies to the firm row.
        seen: set[str] = set()
        rows = []
        for source in ("adv_feed", "adv_state_feed"):
            snap = snapshot.latest(conn, source)
            if snap is None:
                continue
            path = config.SNAPSHOT_DIR / snap["rel_path"]
            n = 0
            for _, fm in etree.iterparse(gzip.open(path, "rb"), events=("end",),
                                         tag="Firm"):
                rec = parse(fm)
                fm.clear()
                if not rec or not rec["crd"] or rec["crd"] in seen:
                    continue
                seen.add(rec["crd"])
                rec["snapshot_id"] = int(snap["id"])
                rec["config_stamp"] = cfg.stamp
                rows.append([rec[c] for c in COLS])
                n += 1
            print(f"{source}: {n:,} firms from snapshot {snap['id']}")
        conn.execute("DELETE FROM firm_adv_extra")
        sql = (f"INSERT INTO firm_adv_extra ({','.join(COLS)})"
               f" VALUES ({','.join('?' * len(COLS))})")
        for i in range(0, len(rows), 5000):
            conn.executemany(sql, rows[i:i + 5000])
        conn.commit()
        total = len(rows)
        run.rows_out = total
        stats = conn.execute("""SELECT
            SUM(ad_testimonials) t, SUM(ad_performance) p, SUM(ad_ratings) r,
            SUM(rel_bank) b, SUM(rel_insurance) i,
            SUM(CASE WHEN social_hosts != '[]' THEN 1 ELSE 0 END) s
            FROM firm_adv_extra""").fetchone()
        note = (f"{total} firms; testimonials {stats['t']}, performance ads "
                f"{stats['p']}, ratings {stats['r']}, bank related {stats['b']}, "
                f"insurer related {stats['i']}, social listed {stats['s']}")
        run.note(note)
        print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
