"""Does the Postgres adapter still behave like the SQLite it replaced?

Eight checks over prospect.pg, run against a real database rather than a mock,
because every one of them is about a difference between two engines and a mock
would only ever confirm what this file already believes.

Point it at a scratch database, never the live one: it writes rows and it
expects to be the only writer.

    BELLWETHER_DSN=postgresql://... python -m scripts.qa_pg

Exits nonzero on any failure, so it can gate a merge alongside scripts.qa_smoke.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import db, guard, runlog  # noqa: E402

conn = db.connect()
db.init(conn); db.init_firm(conn)
print("1. db.connect() + init() + init_firm() OK")

db.init_firm(conn)
print("2. init_firm idempotent on second call OK")

with runlog.Run(conn, "adv_feed", "ingest", "stamp") as run:
    pass
assert isinstance(run.id, int) and run.id > 0, run.id
r = conn.execute("SELECT status FROM run_log WHERE id = ?", (run.id,)).fetchone()
print(f"3. runlog.Run round-trip OK (id={run.id}, status={r['status']})")

guard.record_columns(conn, "adv_feed", "-", ["a", "b", "c"], None)
conn.commit()
assert guard.known_layouts(conn, "adv_feed", "-") == [["a", "b", "c"]]
print("4. guard.record_columns / known_layouts OK")

cols = {x["name"] for x in conn.execute("PRAGMA table_info(firm)")}
assert {"phone", "regulator", "raum", "crd"} <= cols
conn.execute("SELECT COUNT(*) FROM firm_current").fetchone()
print(f"5. firm schema + firm_current view OK ({len(cols)} cols)")

# INSERT OR IGNORE must not duplicate on a repeat
guard.record_columns(conn, "adv_feed", "-", ["a", "b", "c"], None)
conn.commit()
n = conn.execute("SELECT COUNT(*) AS n FROM source_schema").fetchone()["n"]
assert n == 1, f"OR IGNORE duplicated: {n} rows"
print("6. INSERT OR IGNORE -> ON CONFLICT DO NOTHING is idempotent OK")

sid = conn.execute(
    "INSERT INTO snapshot (source_key, published_at, captured_at, filename,"
    " rel_path, bytes, sha256, config_stamp) VALUES (?,?,?,?,?,?,?,?)"
    " RETURNING id",
    ("adv_feed", "2026-08-01", "2026-08-27", "f.zip", "a/f.zip", 10, "abc", "st")
).lastrowid

# INSERT OR REPLACE must replace on the real key, not duplicate
conn.execute("INSERT OR REPLACE INTO filing_crd (filing_id, crd, snapshot_id)"
             " VALUES (?,?,?)", ("f1", "111", sid))
conn.execute("INSERT OR REPLACE INTO filing_crd (filing_id, crd, snapshot_id)"
             " VALUES (?,?,?)", ("f1", "999", sid))
conn.commit()
row = conn.execute("SELECT crd FROM filing_crd WHERE filing_id='f1'").fetchone()
n = conn.execute("SELECT COUNT(*) AS n FROM filing_crd").fetchone()["n"]
assert n == 1 and row["crd"] == "999", (n, dict(row))
print("7. INSERT OR REPLACE upserts on the real key OK (replaced, not duplicated)")

# A partial INSERT OR REPLACE must refuse, because SQLite and Postgres would
# store different things and neither answer is safe to guess at.
try:
    conn.execute("INSERT OR REPLACE INTO filing_crd (filing_id, crd) VALUES (?,?)",
                 ("f2", "222"))
    raise AssertionError("partial INSERT OR REPLACE was allowed through")
except RuntimeError as e:
    assert "snapshot_id" in str(e), e
    print("8. partial INSERT OR REPLACE refused, naming the unset column OK")

# ---------------------------------------------------------------- Cadence
# The push to Cadence on the real engine: what SQLite (scripts/qa_cadence.py)
# cannot show, two connections racing for the same row.
import threading  # noqa: E402

from prospect import cadence  # noqa: E402

conn.rollback()
for t in reversed(cadence.TABLES):
    conn.execute(f"DROP TABLE IF EXISTS {t}")
conn.commit()
cadence.init(conn)
cadence.init(conn)
print("9. cadence schema created twice without error OK")

CLAIM = ("INSERT INTO cadence_push (crd, person_key, name, state, pushed_by, first_pushed_at, claimed_at)"
         " VALUES ('105943', 'i:1', 'Jane Doe', 'sending', ?, '2026-11-03T15:00:00+00:00',"
         " '2026-11-03T15:00:00+00:00') ON CONFLICT (crd, person_key) DO UPDATE SET state='sending',"
         f" pushed_by=excluded.pushed_by WHERE cadence_push.state IN ({','.join(repr(x) for x in cadence.REPUSH)})"
         " RETURNING id")


def race(fn, n: int = 2) -> list:
    """Run fn(i) on n connections at the same moment; their answers."""
    gate, out = threading.Barrier(n), [None] * n

    def go(i):
        c = db.connect()
        try:
            gate.wait()
            out[i] = fn(c, i)
        finally:
            c.close()
    ts = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


def claim(c, i):
    r = c.execute(CLAIM, (f"user{i}",)).fetchone()
    c.commit()
    return r is not None


wins = race(claim, 4)
assert wins.count(True) == 1, wins
print("10. four connections claim one contact at once: exactly one wins OK")

for st in ("in_sandbox", "rejected", "in_crm"):
    conn.execute("UPDATE cadence_push SET state=?", (st,))
    conn.commit()
    assert conn.execute(CLAIM, ("again",)).fetchone() is None, st
    conn.rollback()
for st in cadence.REPUSH:
    conn.execute("UPDATE cadence_push SET state=?", (st,))
    conn.commit()
    assert conn.execute(CLAIM, ("again",)).fetchone() is not None, st
    conn.commit()
print("11. a send again claims only from failed and removed; a refusal stands (B015) OK")

conn.execute("INSERT INTO cadence_request (request_key, batch_id, body, body_sha256, contacts, state,"
             " first_sent_at, next_retry_at) VALUES ('k1', 'b1', '{}', 'x', 1, 'retry_wait',"
             " '2026-01-01T15:00:00+00:00', '2026-01-01T15:00:05+00:00')")
conn.commit()
sent = race(lambda c, i: cadence.claim_due(c, "k1"), 4)
assert sent.count(True) == 1, sent
print("12. four senders try one due request: exactly one sends OK")

slots: list[float] = []
real_sleep = cadence._sleep
cadence._sleep = lambda s: None


def reserve(c, i):
    return [cadence._reserve(c, "push") for _ in range(10)]


for got in race(reserve, 3):
    slots += got
cadence._sleep = real_sleep
assert len(set(slots)) == len(slots) == 30, len(set(slots))
ordered = sorted(slots)
assert all(b - a >= cadence.PUSH_GAP_S - 1e-6 for a, b in zip(ordered, ordered[1:]))
print("13. three connections reserving pace slots never share one, 2.5 s apart OK")

# A temporary stand-in shadows any real people_index for this session only.
conn.execute("CREATE TEMP TABLE people_index (crd TEXT, person_key TEXT, name TEXT, indvl_pk TEXT,"
             " email TEXT)")
conn.commit()
from prospect import webapp  # noqa: E402,F401  (the views import through the app)
from prospect import people_view  # noqa: E402

# Empty tables make any plan cheap, so the planner is told to avoid scans and
# hash joins: what is left shows an index can answer the lookup per row.
for knob in ("enable_seqscan", "enable_hashjoin", "enable_mergejoin"):
    conn.execute(f"SET {knob} = off")
plan = "\n".join(r[0] for r in conn.execute(
    "EXPLAIN SELECT 1 FROM people_index WHERE " + people_view.CAD_SQL["in"]).fetchall())
assert "ix_cadence_push" in plan or "cadence_push_crd_person_key_key" in plan, plan
for knob in ("enable_seqscan", "enable_hashjoin", "enable_mergejoin"):
    conn.execute(f"RESET {knob}")
print("14. the People Cadence filter reads cadence_push through an index OK")

# Every Cold outreach filter, My sends and the daily cap, in Postgres's own SQL.
conn.execute("INSERT INTO people_index VALUES ('105943', 'i:1', 'Jane Doe', '0001', 'jane@x.example')")
cadence.suppress(conn, "individual", "1", reason="opted_out", source="cadence", for_id="105943:i:1")
conn.commit()
for k, sql in people_view.CAD_SQL.items():
    args = ["user0"] if k == "mine" else []
    conn.execute(f"SELECT COUNT(*) n FROM people_index WHERE {sql}", args).fetchone()
hit = conn.execute("SELECT COUNT(*) n FROM people_index WHERE " + people_view.CAD_SQL["optout"]).fetchone()["n"]
assert hit == 1, hit
assert cadence.used_today(conn, "user0") >= 0
info = cadence.status_map(conn, [("105943", "i:1")])["105943:i:1"]
assert info["text"].startswith("Do not contact") and not info["can_push"], info
conn.rollback()
conn.execute("DROP TABLE pg_temp.people_index")
for t in reversed(cadence.TABLES):
    conn.execute(f"DROP TABLE IF EXISTS {t}")
conn.commit()
print("15. every Cold outreach filter runs, and an opt-out reaches the person by their SEC id (N007) OK")

print("\nINTEGRATION CHECKS PASS")
