"""Find people's public LinkedIn profiles, and addresses their firm's people
published on the open web, through free search engines (prospect.websearch).

Order: in-scope firms by priority, best first; within a firm, Schedule A
officers first, then everyone else on the roster. A first pass gives every
firm its leadership and first PER_FIRM people before going deeper at any one
firm, so a 25,000-person firm near the top of the list cannot hold the queue
for weeks. Any missing verified email, personal phone or matched LinkedIn
keeps a person eligible. The retry interval is editable in Crawling (14 days
by default); blank search responses are retried after 7 days. Unranked firms
are included after the prioritised first pass.

For each firm, before its people:
  - one pair of searches for addresses published at the firm's mail domain,
    stored unverified for the verification job;
  - one firm-wide LinkedIn search, whose results are matched against everyone
    at the firm, Schedule A officers off the roster included. Small firms
    often come back whole from that one query, which spares a query per
    person.

The search engines are asked at most once every 3 seconds; when they start
refusing, the run stops early and the people it did not reach stay queued.

    python -m scripts.search_contacts [--limit N] [--crd CRD] [--seconds S]
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import (config, contacts, db, emailguess, harvest, research, runlog,  # noqa: E402
                      settings, websearch)

RESEARCH_DAYS = 60
EMPTY_RETRY_DAYS = 7     # every search came back blank: as likely a hiccup as a fact
PER_FIRM = 25            # people per firm in the first pass over all firms

# Everyone at an in-scope firm, ranked within the firm (officers first) over
# the whole roster, so the rank does not shift as people are searched. Then
# only those not searched lately and missing at least one channel, first pass
# (rank <= PER_FIRM) before second, best firms first.
TODO_SQL = """
WITH ranked AS (
  SELECT e.org_pk AS crd, e.indvl_pk, p.name, p.first_name, p.middle_name, p.last_name,
         p.other_names, s.priority,
         row_number() OVER (PARTITION BY e.org_pk ORDER BY
           EXISTS (SELECT 1 FROM schedule_a a WHERE a.crd = e.org_pk AND a.is_individual = 1
                   AND upper(a.name) LIKE upper(p.last_name) || ',%' || upper(p.first_name) || '%')
           DESC, p.name, e.indvl_pk) AS rn
    FROM person_employment e
    LEFT JOIN firm_scope s ON s.crd = e.org_pk
    JOIN person p ON p.indvl_pk = e.indvl_pk
   WHERE e.kind = 'current' {crd_filter}
)
SELECT r.* FROM ranked r
 WHERE NOT EXISTS (SELECT 1 FROM contact_search_state x WHERE x.crd = r.crd
                   AND x.person_key = 'i:' || r.indvl_pk
                   AND x.searched_at > CASE WHEN x.status = 'error' THEN ?
                       WHEN x.status = 'empty' THEN ? ELSE ? END)
   AND (NOT EXISTS (SELECT 1 FROM usable_contact_point c WHERE c.crd = r.crd
                   AND c.person_key = 'i:' || r.indvl_pk AND c.kind = 'linkedin'
                   AND c.verify_status = 'matched')
     OR NOT EXISTS (SELECT 1 FROM usable_contact_point c WHERE c.crd = r.crd
                   AND c.person_key = 'i:' || r.indvl_pk AND c.kind = 'email'
                   AND c.verify_status = 'valid' AND c.is_role=0)
     OR NOT EXISTS (SELECT 1 FROM usable_contact_point c WHERE c.crd = r.crd
                   AND c.person_key = 'i:' || r.indvl_pk AND c.kind = 'phone'
                   AND COALESCE(c.label, '') NOT IN ('main','office','toll_free')))
 ORDER BY (r.rn > ?), r.priority DESC NULLS LAST, r.crd, r.rn
 LIMIT ?
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def _json_list(v) -> list:
    import json
    if not v:
        return []
    try:
        x = json.loads(v)
        return [str(i) for i in x] if isinstance(x, list) else []
    except (TypeError, ValueError):
        return []


def person_of(r) -> dict:
    return {"crd": r["crd"], "person_key": f"i:{r['indvl_pk']}", "name": r["name"],
            "first": r["first_name"], "middle": r["middle_name"], "last": r["last_name"],
            "other_names": _json_list(r["other_names"]), "title": None}


def save_state(conn, crd: str, key: str, status: str, found: int, queries: int) -> None:
    conn.execute(
        "INSERT INTO contact_search_state (crd, person_key, searched_at, found, status, queries)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT (crd, person_key) DO UPDATE SET"
        " searched_at=excluded.searched_at, found=excluded.found, status=excluded.status,"
        " queries=excluded.queries", (crd, key, _iso(_now()), found, status, queries))
    conn.commit()


def officers_off_roster(conn, crd: str, roster: list[tuple[str, str]]) -> list[dict]:
    """Schedule A individuals no roster record covers (often the CEO or CCO,
    who need not be registered as an adviser rep), as person dicts."""
    out = []
    for r in conn.execute("SELECT name, title FROM schedule_a WHERE crd=? AND is_individual=1",
                          (crd,)).fetchall():
        name = emailguess.pretty(r["name"])
        if any(harvest.name_fit(name, n) == "strong" for _, n in roster):
            continue
        key = contacts.name_key(name)
        parts = name.split()
        if not key or len(parts) < 2 or any(o["person_key"] == key for o in out):
            continue
        out.append({"crd": crd, "person_key": key, "name": name, "first": parts[0],
                    "middle": " ".join(parts[1:-1]) or None, "last": parts[-1],
                    "other_names": [], "title": r["title"]})
    conn.commit()
    return out


def firm_step(conn, s: websearch.Searcher, crd: str, firm: dict, force: bool,
              tot: Counter) -> list[dict] | None:
    """Published emails and the firm-wide LinkedIn search, once per 60 days
    (or now, with --crd). Returns the firm-wide results for the people loop."""
    cutoff = _iso(_now() - timedelta(days=RESEARCH_DAYS))
    st = conn.execute("SELECT searched_at FROM contact_search_state WHERE crd=?"
                      " AND person_key=''", (crd,)).fetchone()
    conn.commit()
    fq = websearch.firm_query(firm)
    if st and st["searched_at"] > cutoff and not force:
        return s.search(fq, cache_only=True) if fq else None
    roster = websearch.roster_names(conn, crd)
    domain = emailguess.domain_for(conn, crd) or firm.get("domain")
    conn.commit()
    em = websearch.find_published_emails(conn, crd, domain, searcher=s, people=roster)
    tot["emails"] += len(em["emails"])
    queries = em["queries"]
    res = None
    found = len(em["emails"])
    if fq:
        res = s.search(fq)
        queries += 1
    if res:
        # Everyone at the firm against the one result page: roster people
        # without a matched profile, and officers off the roster.
        have = {r["person_key"] for r in conn.execute(
            "SELECT person_key FROM contact_point WHERE crd=? AND kind='linkedin'"
            " AND person_key != '' AND verify_status='matched'", (crd,)).fetchall()}
        conn.commit()
        rows = conn.execute(
            "SELECT e.org_pk AS crd, e.indvl_pk, p.name, p.first_name, p.middle_name,"
            " p.last_name, p.other_names FROM person_employment e"
            " JOIN person p ON p.indvl_pk=e.indvl_pk WHERE e.org_pk=? AND e.kind='current'",
            (crd,)).fetchall()
        conn.commit()
        people = [person_of(r) for r in rows] + officers_off_roster(conn, crd, roster)
        for person in people:
            if person["person_key"] in have:
                continue
            taken = websearch._taken(conn, crd, person["person_key"])
            hit = websearch.best_profile(res, person, firm, taken)
            if not hit:
                continue
            r, verdict = hit
            got = websearch.find_linkedin(conn, person, searcher=s, firm=firm,
                                          extra_results=[r], max_queries=0)
            if got["status"] == "found":
                found += 1
                tot["linkedin_" + got["verdict"]] += 1
                tot["from_firm_query"] += 1
                if got["verdict"] == "matched" or person["person_key"].startswith("n:"):
                    save_state(conn, crd, person["person_key"], "found", 1, 0)
    if res is not None or em["status"] != "unsearched":
        save_state(conn, crd, "", "found" if found else "none", found, queries)
        tot["firms"] += 1
    return res


def todo(conn, limit: int, crd: str | None) -> list:
    days = max(1, min(90, settings.get_int("crawl.contact_retry_days", 14)))
    cutoff = _iso(_now() - timedelta(days=days))
    empty = _iso(_now() - timedelta(days=EMPTY_RETRY_DAYS))
    error = _iso(_now() - timedelta(days=1))
    if crd:
        # One firm, now: everyone on it, whatever the 60-day rule says.
        cutoff = empty = error = _iso(_now() + timedelta(days=1))
        sql = TODO_SQL.format(crd_filter="AND e.org_pk = ?")
        rows = conn.execute(sql, (crd, error, empty, cutoff, PER_FIRM, limit)).fetchall()
    else:
        rows = conn.execute(TODO_SQL.format(crd_filter=""),
                            (error, empty, cutoff, PER_FIRM, limit)).fetchall()
    conn.commit()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="people per run (default 40; 60 with --crd)")
    ap.add_argument("--crd", help="search this one firm now")
    ap.add_argument("--seconds", type=int, default=None,
                    help="stop starting new people after this long (default 480; 450 with --crd,"
                         " which scripts.find_contacts gives 600s)")
    ap.add_argument("--max-queries", type=int, default=None,
                    help="searches per person, at most (default 2; 3 with --crd)")
    ap.add_argument("--interval", type=float, default=None,
                    help="seconds between searches (default BELLWETHER_SEARCH_INTERVAL or 3)")
    args = ap.parse_args()
    limit = args.limit or (60 if args.crd else 40)
    max_queries = args.max_queries if args.max_queries is not None else (3 if args.crd else 2)
    budget = args.seconds or (450 if args.crd else 480)

    cfg = config.load()
    conn = db.connect()
    contacts.init(conn)
    websearch.init(conn)

    with runlog.Run(conn, "web_search", "search", cfg.stamp) as run:
        t0 = time.monotonic()
        try:
            import ddgs  # noqa: F401
        except ImportError:
            print("the ddgs package is not installed; nothing searched")
            run.skip("ddgs not installed")
            return 0
        s = websearch.Searcher(conn, interval=args.interval)
        if s.tripped:
            print(s.tripped)
            run.skip(s.tripped)
            return 0
        rows = todo(conn, limit, args.crd)
        tot: Counter = Counter()
        firms: dict[str, dict] = {}
        firm_results: dict[str, list | None] = {}
        if args.crd and not rows:
            firms[args.crd] = websearch.firm_info(conn, args.crd)
            firm_results[args.crd] = firm_step(conn, s, args.crd, firms[args.crd], True, tot)
        stopped = ""
        reader = research.Reader()
        reader._searcher = s  # one shared rate limiter and search cache
        for r in rows:
            if time.monotonic() - t0 > budget:
                stopped = "time budget used"
                break
            if s.tripped:
                stopped = s.tripped
                break
            crd = r["crd"]
            if crd not in firms:
                firms[crd] = websearch.firm_info(conn, crd)
                firm_results[crd] = firm_step(conn, s, crd, firms[crd], bool(args.crd), tot)
            person = person_of(r)
            have_linkedin = conn.execute("SELECT 1 FROM contact_point WHERE crd=? AND person_key=?"
                            " AND kind='linkedin' AND verify_status='matched'",
                            (crd, person["person_key"])).fetchone()
            conn.commit()
            try:
                got = ({"status": "none", "queries": 0} if have_linkedin else
                       websearch.find_linkedin(conn, person, searcher=s, firm=firms[crd],
                                              extra_results=firm_results.get(crd) or [],
                                              max_queries=max_queries))
                missing = research._missing(research._have(conn, [crd]).get((crd, person['person_key']), {}))
                if any(k in missing for k in ('email', 'phone')):
                    target = research._target(conn, crd, person['person_key'],
                                              emailguess.pretty(person['name']), person.get('title'),
                                              research._firm(conn, crd), missing)
                    if target and not s.tripped and time.monotonic() - t0 < budget:
                        found = research.research_person(conn, target, reader, use_ai=False)
                        for kind in ('emails', 'phones', 'linkedin'):
                            tot['page_' + kind] += found[kind]
                        if any(found[k] for k in ('emails', 'phones', 'linkedin')):
                            got['status'] = 'published'
            except Exception as e:  # one person must not end the slice
                conn.rollback()
                print(f"  {person['name']} ({crd}): {type(e).__name__}: {e}"[:200])
                tot["errors"] += 1
                save_state(conn, crd, person['person_key'], 'error', 0, 0)
                continue
            if got["status"] == "unsearched":
                continue          # the engines could not be asked; stays queued
            tot["people"] += 1
            tot["queries"] += got["queries"]
            if got["status"] == "found":
                tot["linkedin_" + got["verdict"]] += 1
            tot["status_" + got["status"]] += 1
            save_state(conn, crd, person["person_key"], got["status"],
                       1 if got["status"] == "found" else 0, got["queries"])
            if args.crd or tot["people"] <= 5 or got["status"] == "found":
                mark = (f"{got['verdict']}: {got['url']}  [{got['title'][:80]}]"
                        if got["status"] == "found" else "none")
                print(f"  {person['name']} @ {crd}: {mark}", flush=True)
        reader.close()
        took = time.monotonic() - t0
        found = tot["linkedin_matched"] + tot["linkedin_probable"]
        rate = tot["people"] / (took / 60) if took > 0 else 0
        msg = (f"searched {tot['people']} people at {len(firms)} firms: {found} LinkedIn"
               f" ({tot['linkedin_matched']} matched, {tot['linkedin_probable']} probable,"
               f" {tot['from_firm_query']} from firm-wide searches), {tot['status_none']}"
               f" not found, {tot['status_empty']} blank answers; {tot['emails']}"
               f" published emails; page research: {tot['page_emails']} emails,"
               f" {tot['page_phones']} phones, {tot['page_linkedin']} profiles;"
               f" {s.live} live searches, {s.cached} cached,"
               f" {s.errors} errors; {took:.0f}s, {rate:.1f} people/min"
               + (f"; stopped: {stopped}" if stopped else ""))
        print(msg)
        run.rows_out = tot["people"]
        run.note(msg)
        if tot["emails"]:
            try:
                from prospect import jobs
                jobs.request_run(conn, "email_verify")
            except Exception:
                conn.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
