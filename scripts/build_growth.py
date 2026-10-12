"""Build the growth and hiring evidence for every firm (prospect/growth.py).

Three steps, each cheap to repeat:

  1. Filing history gains total staff (Item 5.A) for every filing since 2011
     and clients for every filing since the October 2017 form, read once from
     the Base_A archive already held, and kept current from the weekly feed.
     Clients are the Item 5.D counts summed over client types, the same total
     the firm row carries. (Item 5.C(1) counts only clients without managed
     assets, so it is not the firm's client count.)
  2. Each firm's state registrations and notice filings, with their dates,
     from the newest weekly feeds (re-read only when a new feed arrives).
  3. firm_growth: one row per firm with every figure the Growth tab shows and
     the ATS list scores.

    python -m scripts.build_growth
"""

from __future__ import annotations

import csv
import gzip
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, growth, openroles, runlog, snapshot  # noqa: E402


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _meta(conn, k: str) -> str | None:
    r = conn.execute("SELECT v FROM growth_meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


def _set_meta(conn, k: str, v: str) -> None:
    conn.execute("INSERT INTO growth_meta (k, v) VALUES (?,?) ON CONFLICT (k) DO UPDATE"
                 " SET v=excluded.v", (k, v))
    conn.commit()


# Item 5.D(1): number of clients of each type, (a) individuals to (n) other.
CLIENT_COLS = tuple(f"5D1{c}" for c in "abcdefghijklmn")
# Before the October 2017 amendments Item 5.D asked for percentages, not counts.
COUNTS_FROM = "2017-10-01"
ARCHIVE_KEY = "archive_staff_v2"     # v1 read clients from 5.C(1)


def _clients(rec) -> int | None:
    vals = [_num(rec.get(c)) for c in CLIENT_COLS]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def archive_staff(conn) -> int:
    """Total staff and clients for every archived filing, once."""
    if _meta(conn, ARCHIVE_KEY) in ("done", "no_columns", "missing"):
        return 0
    row = conn.execute("SELECT rel_path FROM snapshot WHERE source_key='adv_filing_crosswalk'"
                       " ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return 0
    path = config.SNAPSHOT_DIR / row["rel_path"]
    if not path.exists():
        # Marked, so the job's backlog does not keep asking for it.
        print(f"archive {path.name} is not on this machine")
        _set_meta(conn, ARCHIVE_KEY, "missing")
        return 0
    from scripts.build_firm_history import _open_text, pdate
    conn.execute("DROP TABLE IF EXISTS growth_hist_fix")
    conn.execute("CREATE TEMP TABLE growth_hist_fix (crd TEXT, filing_date TEXT,"
                 " employees INTEGER, clients INTEGER)")
    batch, n = [], 0
    with _open_text(path) as fh:
        rdr = csv.DictReader(fh)
        cols = set(rdr.fieldnames or [])
        if not {"1E1", "DateSubmitted", "5A", "5D1a"} <= cols:
            print("archive is missing 5A or 5D; staff and client history skipped")
            _set_meta(conn, ARCHIVE_KEY, "no_columns")
            return 0
        for rec in rdr:
            crd = (rec.get("1E1") or "").strip()
            d = pdate(rec.get("DateSubmitted"))
            if not crd or not d:
                continue
            batch.append((crd, d, _num(rec.get("5A")),
                          _clients(rec) if d >= COUNTS_FROM else None))
            if len(batch) >= 20000:
                conn.executemany("INSERT INTO growth_hist_fix VALUES (?,?,?,?)", batch)
                n += len(batch)
                batch.clear()
    if batch:
        conn.executemany("INSERT INTO growth_hist_fix VALUES (?,?,?,?)", batch)
        n += len(batch)
    conn.execute("""UPDATE firm_history h SET employees = t.employees, clients = t.clients
        FROM (SELECT DISTINCT ON (crd, filing_date) * FROM growth_hist_fix
              ORDER BY crd, filing_date) t
        WHERE h.crd = t.crd AND h.filing_date = t.filing_date AND h.source = 'archive'""")
    conn.execute("DROP TABLE IF EXISTS growth_hist_fix")
    conn.commit()
    _set_meta(conn, ARCHIVE_KEY, "done")
    return n


def feed_staff(conn) -> None:
    """Staff and clients for every weekly feed point."""
    conn.execute("""UPDATE firm_history h SET employees = f.total_employees,
            clients = f.clients_total
        FROM (SELECT DISTINCT ON (crd, filing_date) crd, filing_date, total_employees,
                     clients_total
              FROM firm WHERE filing_date IS NOT NULL
              ORDER BY crd, filing_date, snapshot_id DESC) f
        WHERE h.source = 'feed' AND h.crd = f.crd AND h.filing_date = f.filing_date
          AND (h.employees IS DISTINCT FROM f.total_employees
               OR h.clients IS DISTINCT FROM f.clients_total)""")
    conn.commit()


def states(conn) -> int:
    """Every firm's states, from the newest feeds, when a new feed has come."""
    from lxml import etree
    want = []
    for source in ("adv_feed", "adv_state_feed"):
        snap = snapshot.latest(conn, source)
        if snap is not None:
            want.append((source, snap))
    stamp = ",".join(str(s["id"]) for _, s in want)
    if not want or _meta(conn, "states_from") == stamp:
        return 0
    rows = {}
    for source, snap in want:
        path = config.SNAPSHOT_DIR / snap["rel_path"]
        if not path.exists():
            continue
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rb") as fh:
            for _, fm in etree.iterparse(fh, events=("end",), tag="Firm"):
                info = fm.find("Info")
                crd = info.get("FirmCrdNb") if info is not None else None
                if crd:
                    for s in fm.findall("./NoticeFiled/States"):
                        st = (s.get("RgltrCd") or "").strip()
                        if st:
                            rows[(crd, st, "notice")] = (s.get("St"), (s.get("Dt") or "")[:10] or None)
                    for s in fm.findall("./StateRgstn/Rgltrs/Rgltr"):
                        st = (s.get("Cd") or "").strip()
                        if st:
                            rows[(crd, st, "registration")] = (s.get("St"),
                                                               (s.get("Dt") or "")[:10] or None)
                fm.clear()
    if not rows:
        return 0
    conn.execute("DELETE FROM firm_state")
    data = [(c, s, k, st, d) for (c, s, k), (st, d) in rows.items()]
    for i in range(0, len(data), 5000):
        conn.executemany("INSERT INTO firm_state (crd, state, kind, status, filed_date)"
                         " VALUES (?,?,?,?,?)", data[i:i + 5000])
    conn.commit()
    _set_meta(conn, "states_from", stamp)
    return len(data)


def main() -> int:
    cfg = config.load()
    conn = db.connect()
    growth.init(conn)
    openroles.init(conn)
    with runlog.Run(conn, "growth", "derive", cfg.stamp) as run:
        fixed = archive_staff(conn)
        if fixed:
            print(f"staff and clients added to {fixed:,} archived filings")
        feed_staff(conn)
        st = states(conn)
        if st:
            print(f"{st:,} state registrations and notice filings read")
        counts = growth.compute_all(conn)
        run.rows_out = counts["firms"]
        line = (f"growth for {counts['firms']:,} firms: {counts['growing_team']:,} with a growing team,"
                f" {counts['compared']:,} compared with a filing a year or more earlier,"
                f" {counts['with_open_roles']:,} with open roles")
        run.note(line)
        try:
            from prospect import jobs
            jobs.request_run(conn, "rescore")      # the ATS list reads these figures
        except Exception:
            conn.rollback()
    conn.close()
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
