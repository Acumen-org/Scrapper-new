"""The two background jobs that use the AI provider, each one slice per run.

  briefs   write or refresh the AI brief for the best firms on the lists whose
           data changed since their brief was written (a hash of the dossier
           decides), so the brief on a firm page is ready before anyone asks
  clean    tidy titles the rules could not place and sort those people into
           roles, in batches; stored with source 'ai'

Both stop as soon as the day's AI allowance is spent, and do nothing at all
when no provider is connected.

    python -m scripts.ai_jobs briefs [--limit N]
    python -m scripts.ai_jobs clean [--limit N]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import ai, assistant, db, roles  # noqa: E402


def briefs(limit: int) -> str:
    if not ai.enabled("brief"):
        return "AI briefs are not enabled"
    c = db.connect()
    ai.init(c)
    # Best firm on each list first, then by overall priority, skipping firms
    # whose brief is younger than a week (the hash check inside brief() catches
    # data changes; this keeps a slice from re-hashing the same firms).
    rows = c.execute("""
        SELECT s.crd FROM firm_scope s
        LEFT JOIN ai_note n ON n.crd = s.crd AND n.kind = 'brief'
        WHERE n.crd IS NULL OR n.created_at < (NOW() - INTERVAL '7 days')::text
        ORDER BY s.priority DESC LIMIT ?""", (limit,)).fetchall()
    c.commit()
    made = 0
    for r in rows:
        if ai.budget_left() <= 0:
            break
        try:
            b = assistant.brief(c, r["crd"], who="ai_briefs job")
        except ai.AIError as e:
            return f"stopped: {e}"
        if b:
            made += 1
    c.close()
    return f"briefed {made} firms"


def clean(limit: int) -> str:
    c = db.connect()
    roles.init(c)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Every distinct title not yet in the map, placed by rule first: free and
    # instant. Titles repeat across firms, so this is a few thousand rows.
    seen = {r["title_key"] for r in c.execute("SELECT title_key FROM title_role")}
    titles = [r["t"] for r in c.execute(
        "SELECT DISTINCT title AS t FROM schedule_a WHERE is_individual=1 AND title IS NOT NULL"
        " UNION SELECT DISTINCT title AS t FROM contact_point WHERE title IS NOT NULL")]
    c.commit()
    ruled = 0
    for t in titles:
        k = roles.key(t)
        if not k or k in seen:
            continue
        seen.add(k)
        c.execute("INSERT INTO title_role (title_key, title_raw, title_clean, role, source,"
                  " updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                  (k, t, roles.clean_title(t), roles.classify(t), "rules", now))
        ruled += 1
    c.commit()
    msg = f"placed {ruled} new titles by rule"
    if not ai.enabled("clean"):
        return msg
    # Then the model, for what the rules could only call 'other'.
    todo = c.execute("""SELECT title_key, title_raw FROM title_role
        WHERE role = 'other' AND source = 'rules' ORDER BY title_key LIMIT ?""",
                     (limit,)).fetchall()
    c.commit()
    if not todo:
        return msg
    items = [(str(i), r["title_raw"] or "") for i, r in enumerate(todo)]
    try:
        out = ai.classify_titles(items)
    except ai.AIError as e:
        return f"{msg}; AI stopped: {e}"
    fixed = 0
    for i, r in enumerate(todo):
        got = out.get(str(i))
        if not got:
            continue
        title, role = got
        if role not in roles.ROLE_LABEL:
            continue
        c.execute("UPDATE title_role SET title_clean=?, role=?, source='ai', updated_at=?"
                  " WHERE title_key=?", (title[:90], role, now, r["title_key"]))
        fixed += 1
    c.commit()
    c.close()
    return f"{msg}; AI placed {fixed} more"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["briefs", "clean"])
    ap.add_argument("--limit", type=int, default=15)
    a = ap.parse_args()
    print(briefs(a.limit) if a.what == "briefs" else clean(a.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
