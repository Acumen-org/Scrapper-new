"""Forward-looking triggers: the diff between two adviser feed snapshots.

Every trigger is a diff between the current pull and the previous one. That is
the entire mechanism, and it is why snapshot storage is immutable.

new_registration is cross-checked two ways before it earns highest priority,
because a CRD absent one week and present the next is not necessarily new:

  1. Archive presence. A CRD appearing anywhere in 13 years of Schedule D
     history is a re-registration or a feed gap, not a breakaway.
  2. The feed's own registration date. The archive ends 2024-12-31, so a firm
     registered in 2025 or early 2026 passes check 1 while being years old.
     The registration date on the current record is the authority on age.

Firms failing either check still surface, as reregistration_or_gap at lower
priority, never silently dropped.

    python -m scripts.diff_snapshots [--old ID --new ID] [--stamp-suffix S]
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, runlog  # noqa: E402


def firm_snapshots(conn, source_key: str) -> list[int]:
    """Snapshot ids OF ONE SOURCE that actually have firm rows, oldest first.

    Two feeds now populate the firm table. Diffing the newest two snapshots
    regardless of source would compare the SEC universe against the state one
    and report twenty thousand phantom registrations, so the pair must always
    come from the same feed."""
    return [r["snapshot_id"] for r in conn.execute(
        "SELECT DISTINCT f.snapshot_id FROM firm f"
        " JOIN snapshot s ON s.id = f.snapshot_id"
        " WHERE s.source_key = ? ORDER BY f.snapshot_id", (source_key,))]


def load_rows(conn, snap_id: int) -> dict[str, dict]:
    return {r["crd"]: dict(r) for r in conn.execute(
        "SELECT crd, legal_name, firm_type, is_era, raum, iar_count,"
        " registered_date, filing_date FROM firm WHERE snapshot_id=?", (snap_id,))}


def archive_crds(conn) -> set[str]:
    return {r["crd"] for r in conn.execute(
        "SELECT DISTINCT crd FROM filing_crd WHERE crd IS NOT NULL")}


def diff(conn, old_id: int, new_id: int, fwd: dict, stamp: str,
         today: date) -> list[tuple]:
    old = load_rows(conn, old_id)
    new = load_rows(conn, new_id)
    arch = archive_crds(conn)
    max_age = timedelta(days=int(fwd.get("max_registration_age_days", 400)))
    W = fwd["direction_weights"]
    out = []

    detected = None
    row = conn.execute("SELECT published_at FROM snapshot WHERE id=?", (new_id,)).fetchone()
    if row and row["published_at"]:
        p = row["published_at"].replace("/", "-")
        parts = p.split("-")
        detected = (f"{parts[2]}-{parts[0]}-{parts[1]}" if len(parts[0]) <= 2
                    else p)
    detected = detected or today.isoformat()

    # ---- new CRDs
    for crd, f in new.items():
        if crd in old or f["is_era"]:
            continue
        reg = f["registered_date"] or ""
        recent = False
        if reg:
            try:
                recent = (today - date.fromisoformat(reg)) <= max_age
            except ValueError:
                recent = False
        in_archive = crd in arch
        if recent and not in_archive:
            ttype, w = "new_registration", W["new_registration"]
            desc = (f"New RIA registration: appeared in this week's feed, registered "
                    f"{reg}, no prior history in 13 years of filings. Breakaway "
                    f"rebuilding its product shelf; contact within 30 days")
        else:
            ttype, w = "reregistration_or_gap", W["reregistration_or_gap"]
            why = ("present in historical filings" if in_archive
                   else f"registered {reg or 'unknown'}, older than the breakaway window")
            desc = f"CRD newly visible in the feed but {why}: re-registration or a feed gap"
        out.append((crd, ttype, detected, None, reg or None, desc, 0, None, w, stamp))

    # ---- changes on firms present in both
    jump = fwd["aum_jump_pct"] / 100.0
    drop = fwd["aum_drop_pct"] / 100.0
    iar_min = int(fwd["iar_min_increase"])
    for crd, f in new.items():
        o = old.get(crd)
        if o is None or f["is_era"]:
            continue
        if o["raum"] and f["raum"]:
            chg = (f["raum"] - o["raum"]) / o["raum"]
            if chg >= jump:
                out.append((crd, "aum_jump", detected, str(o["raum"]), str(f["raum"]),
                            f"Regulatory AUM up {chg*100:.0f}% between filings, "
                            f"${o['raum']/1e6:,.0f}M to ${f['raum']/1e6:,.0f}M",
                            0, None, W["aum_jump"], stamp))
            elif chg <= -drop:
                out.append((crd, "aum_drop", detected, str(o["raum"]), str(f["raum"]),
                            f"Regulatory AUM down {abs(chg)*100:.0f}%, "
                            f"${o['raum']/1e6:,.0f}M to ${f['raum']/1e6:,.0f}M",
                            0, None, W["aum_drop"], stamp))
        if (o["iar_count"] is not None and f["iar_count"] is not None
                and f["iar_count"] - o["iar_count"] >= iar_min):
            out.append((crd, "iar_growth", detected, str(o["iar_count"]),
                        str(f["iar_count"]),
                        f"Adviser rep headcount grew {o['iar_count']} to "
                        f"{f['iar_count']}: hiring, book likely growing",
                        0, None, W["iar_growth"], stamp))
    return out


def apply_recency(conn, stamp_like: str, rec: dict, today: str) -> None:
    half, floor = rec["half_life_days"], rec["floor_weight"]
    conn.execute("""UPDATE trigger_event
        SET age_days = CAST(julianday(?) - julianday(detected_date) AS INTEGER)
        WHERE config_stamp LIKE ?""", (today, stamp_like))
    conn.execute("""UPDATE trigger_event
        SET recency_weight = MAX(?, POWER(0.5, age_days / ?)),
            priority = MAX(?, POWER(0.5, age_days / ?)) * COALESCE(direction_weight, 1.0)
        WHERE config_stamp LIKE ? AND age_days IS NOT NULL""",
        (floor, float(half), floor, float(half), stamp_like))
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", type=int, default=None)
    ap.add_argument("--new", type=int, default=None)
    ap.add_argument("--stamp-suffix", default="")
    ap.add_argument("--source", default="adv_feed",
                    choices=["adv_feed", "adv_state_feed"])
    args = ap.parse_args()

    cfg = config.load()
    sc = yaml.safe_load((config.CONFIG_DIR / "scoring.yml").read_text(encoding="utf-8"))
    fwd = dict(sc["trigger_forward"])
    fwd.setdefault("max_registration_age_days",
                   sc.get("new_registration", {}).get("max_registration_age_days", 400))
    # The stamp keys the idempotent delete-and-rewrite of this pair's events.
    # Each source needs its own stamp or the state diff, running second in the
    # same cycle, would silently erase the SEC diff's events.
    stamp = cfg.stamp + (f"|{args.stamp_suffix}" if args.stamp_suffix else "")
    if args.source != "adv_feed":
        stamp += "|state"
    conn = db.connect()

    snaps = firm_snapshots(conn, args.source)
    if args.old and args.new:
        old_id, new_id = args.old, args.new
    elif len(snaps) >= 2:
        old_id, new_id = snaps[-2], snaps[-1]
    else:
        print(f"only {len(snaps)} {args.source} firm snapshot(s) held; diffing "
              "needs two. Forward triggers for this feed go live after its next "
              "weekly capture.")
        return 0

    with runlog.Run(conn, "diff_snapshots", "triggers", stamp) as run:
        ev = diff(conn, old_id, new_id, fwd, stamp, date.today())
        # idempotent per snapshot pair: clear rows this pair produced earlier
        conn.execute("DELETE FROM trigger_event WHERE config_stamp=? AND trigger_type IN"
                     " ('new_registration','reregistration_or_gap','aum_jump',"
                     "'aum_drop','iar_growth')", (stamp,))
        conn.executemany(
            "INSERT INTO trigger_event (crd,trigger_type,detected_date,before_value,"
            "after_value,description,suppressed,suppression_rule,direction_weight,"
            "config_stamp) VALUES (?,?,?,?,?,?,?,?,?,?)", ev)
        conn.commit()
        apply_recency(conn, stamp, sc["trigger_recency"], date.today().isoformat())
        run.rows_out = len(ev)
        by = {}
        for e in ev:
            by[e[1]] = by.get(e[1], 0) + 1
        run.note(f"snapshots {old_id}->{new_id}: " +
                 ", ".join(f"{k}={v}" for k, v in sorted(by.items())))
        print(f"diff {old_id} -> {new_id}: {len(ev)} events")
        for k, v in sorted(by.items()):
            print(f"  {k:<24} {v:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
