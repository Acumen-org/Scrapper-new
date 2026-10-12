"""Open roles at every firm: careers pages, tracking systems and job boards.

Two modes, two jobs (prospect/openroles.py has the rules):

  site    (the "open_roles" job, Websites lane) reads each firm's website for
          its careers page and the openings on it or on the tracking system it
          links (Greenhouse, Lever, BambooHR and the rest). Every firm with a
          website is checked, those never checked first, then growing firms;
          each is checked again after RECHECK_DAYS.
  boards  (the "job_boards" job, Search and AI lane, so it shares the search
          engines' pacing with the contact search) searches the job boards
          for firms whose own site lists nothing, growing firms first, each
          again after BOARD_DAYS.

    python -m scripts.find_open_roles [--limit 40] [--seconds 600] [--workers 3]
    python -m scripts.find_open_roles --crd 123456        one firm, now
    python -m scripts.find_open_roles --boards [--limit 20]
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, openroles, runlog, websearch  # noqa: E402
from prospect.openroles import (CAREERS_PATH_RE, ats_api, ats_of, board_of,  # noqa: E402
                                careers_links, embedded_titles, role_titles, says_no_openings)

RECHECK_DAYS = 21
RETRY_UNREACHABLE_DAYS = 4
BOARD_DAYS = 30
MAX_PAGES = 5               # careers pages and boards read per firm, at most
BOARD_MIN_PEOPLE = 3        # firms smaller than this rarely post on job boards


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


# ------------------------------------------------------------------ site

def check_site(crawler, website: str) -> dict:
    """{status, careers_url, ats, roles: [(title, url, source)], error}."""
    res = {"status": "no_page", "careers_url": None, "ats": None, "roles": [], "error": None}
    if not (website or "").strip():
        res["status"] = "no_website"
        return res
    home, _ = crawler._start(website)
    if home is None:
        res["status"] = "no_website"
        return res
    if not home.ok:
        res["status"] = "unreachable"
        res["error"] = home.error or f"HTTP {home.status}"
        return res
    base = home.final_url or home.url
    root = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
    seen_titles: set[str] = set()
    visited: set[str] = set()
    state = {"no_openings": False}

    def add(title: str, url: str, source: str) -> None:
        t = openroles._clean(title)
        if len(res["roles"]) >= openroles.FEED_MAX:
            return
        if 3 <= len(t) <= 120 and t.lower() not in seen_titles:
            seen_titles.add(t.lower())
            res["roles"].append((t, url, source))

    def read(url: str, depth: int, probe: bool = False) -> bool:
        """Read one careers page or board. True when it was a careers page."""
        url = url.split("#")[0]
        if url in visited or len(visited) >= MAX_PAGES:
            return False
        visited.add(url)
        ats = ats_of(url)
        if ats:
            res["ats"] = res["ats"] or ats
            res["careers_url"] = res["careers_url"] or url
            feed = ats_api(url)
            if feed is not None:
                for title, link in feed:
                    add(title, link, "ats_feed")
                if not feed:
                    state["no_openings"] = True
                return True
        elif board_of(url):
            # A LinkedIn or Indeed company page: the firm points there for its
            # openings, which the job-board search reads.
            res["careers_url"] = res["careers_url"] or url
            return True
        if not crawler.allowed(url):
            return False
        page = crawler.fetch(url, "careers")
        if not page.ok or page.status >= 400:
            return False
        final = (page.final_url or url).split("#")[0]
        if probe and not CAREERS_PATH_RE.search(urlparse(final).path or ""):
            return False        # the address forwarded to the home page
        res["careers_url"] = res["careers_url"] or final
        source = "ats" if ats else "website"
        for title, link in role_titles(page.html, final) + embedded_titles(page.html, final):
            add(title, link, source)
        if says_no_openings(page.text):
            state["no_openings"] = True
        if depth == 0:
            subs = careers_links(page.html, final)
            # The tracking system this page links or embeds, then (when the
            # page itself listed nothing) its own openings page.
            for sub in [s for s in subs if ats_of(s)][:2]:
                read(sub, 1)
            if not res["roles"]:
                own = [s for s in subs if not ats_of(s) and not board_of(s)
                       and urlparse(s).netloc == urlparse(final).netloc]
                for sub in own[:2]:
                    read(sub, 1)
        return True

    links = careers_links(home.html, base)
    for url in links[:3]:
        read(url, 0)
        if res["roles"]:
            break
    if not res["careers_url"]:
        for path in openroles.PROBE_PATHS:
            if read(root + path, 0, probe=True):
                break
    # A tracking system's feed is the firm's own list of openings: when one was
    # read, the headings on its careers page (benefits, teams, stories) go.
    if any(src == "ats_feed" for _, _, src in res["roles"]):
        res["roles"] = [(ti, u, "ats") for ti, u, src in res["roles"] if src == "ats_feed"]
    if res["roles"]:
        res["status"] = "roles"
    elif state["no_openings"]:
        res["status"] = "no_openings"
    elif res["careers_url"]:
        res["status"] = "page"
    return res


def save_site(conn, crd: str, res: dict) -> None:
    now = _now()
    prev = conn.execute("SELECT status FROM firm_careers WHERE crd=?", (crd,)).fetchone()
    if res["status"] == "unreachable" and prev and prev["status"] in ("roles", "no_openings", "page"):
        # A site that is down today keeps what it showed last time.
        conn.execute("UPDATE firm_careers SET checked_at=? WHERE crd=?", (now, crd))
        conn.commit()
        return
    conn.execute(
        "INSERT INTO firm_careers (crd, checked_at, status, careers_url, ats, roles)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT (crd) DO UPDATE SET checked_at=excluded.checked_at,"
        " status=excluded.status, careers_url=excluded.careers_url, ats=excluded.ats,"
        " roles=excluded.roles",
        (crd, now, res["status"], res["careers_url"], res["ats"], len(res["roles"])))
    for title, url, source in res["roles"]:
        conn.execute(
            "INSERT INTO firm_job_posting (crd, title, function, url, source, host, first_seen,"
            " last_seen, active) VALUES (?,?,?,?,?,?,?,?,1) ON CONFLICT (crd, title, url) DO"
            " UPDATE SET function=excluded.function, source=excluded.source, host=excluded.host,"
            " last_seen=excluded.last_seen, active=1",
            (crd, title[:200], openroles.function_of(title), url[:500], source,
             openroles._host(url), now, now))
    conn.execute("UPDATE firm_job_posting SET active=0 WHERE crd=? AND source IN ('website','ats')"
                 " AND active=1 AND last_seen < ?", (crd, now))
    conn.commit()


def todo_sites(conn, limit: int) -> list[dict]:
    rows = conn.execute("""
        SELECT f.crd, f.website FROM firm_current f
        LEFT JOIN firm_careers c ON c.crd = f.crd
        LEFT JOIN firm_growth g ON g.crd = f.crd
        LEFT JOIN firm_scope s ON s.crd = f.crd
        WHERE c.crd IS NULL OR c.checked_at < ?
           OR (c.status = 'unreachable' AND c.checked_at < ?)
        ORDER BY (c.crd IS NOT NULL),
                 (COALESCE(f.website, '') = ''),
                 COALESCE(g.joined_12m, 0) - COALESCE(g.left_12m, 0) DESC,
                 s.priority DESC NULLS LAST, COALESCE(c.checked_at, '')
        LIMIT ?""", (_ago(RECHECK_DAYS), _ago(RETRY_UNREACHABLE_DAYS), limit)).fetchall()
    conn.commit()
    return [dict(r) for r in rows]


def run_sites(conn, rows: list[dict], workers: int, budget: float, run) -> str:
    from prospect.crawl import Crawler
    t0 = time.monotonic()
    work: queue.Queue = queue.Queue()
    for r in rows:
        work.put(r)
    done: queue.Queue = queue.Queue()

    def worker() -> None:
        with Crawler(max_pages=MAX_PAGES + 2) as crawler:
            while time.monotonic() - t0 < budget:
                try:
                    r = work.get_nowait()
                except queue.Empty:
                    return
                started = time.monotonic()
                try:
                    res = check_site(crawler, (r["website"] or "").strip())
                except Exception as e:  # one bad site must not end the slice
                    res = {"status": "unreachable", "careers_url": None, "ats": None, "roles": [],
                           "error": f"{type(e).__name__}: {e}"[:200]}
                res["seconds"] = time.monotonic() - started
                done.put((r["crd"], res))

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, workers))]
    for t in threads:
        t.start()
    tot = {"firms": 0, "roles": 0, "with_roles": 0, "pages": 0, "unreachable": 0, "no_website": 0,
           "no_openings": 0, "errors": 0}
    while any(t.is_alive() for t in threads) or not done.empty():
        try:
            crd, res = done.get(timeout=1)
        except queue.Empty:
            continue
        try:
            save_site(conn, crd, res)
        except Exception as e:
            conn.rollback()
            tot["errors"] += 1
            print(f"  {crd}: not saved: {type(e).__name__}: {e}"[:200], flush=True)
            continue
        tot["firms"] += 1
        tot["roles"] += len(res["roles"])
        tot["with_roles"] += bool(res["roles"])
        tot["pages"] += bool(res["careers_url"])
        tot["unreachable"] += res["status"] == "unreachable"
        tot["no_website"] += res["status"] == "no_website"
        tot["no_openings"] += res["status"] == "no_openings"
        if res["status"] != "no_website":
            extra = (f" {len(res['roles'])} roles" if res["roles"] else "")
            extra += f" via {res['ats']}" if res["ats"] else ""
            print(f"  {crd}: {res['status']}{extra} ({res['seconds']:.0f}s)", flush=True)
    for t in threads:
        t.join(timeout=5)
    took = time.monotonic() - t0
    run.rows_out = tot["firms"]
    return (f"checked {tot['firms']} firms: {tot['pages']} careers pages, {tot['with_roles']} with"
            f" open roles ({tot['roles']} roles), {tot['no_openings']} saying no openings,"
            f" {tot['unreachable']} sites not answering, {tot['no_website']} without a website;"
            f" {took:.0f}s" + (f"; {tot['errors']} not saved" if tot["errors"] else ""))


# ---------------------------------------------------------------- boards

def todo_boards(conn, limit: int) -> list[dict]:
    rows = conn.execute("""
        SELECT c.crd FROM firm_careers c
        JOIN firm_current f ON f.crd = c.crd
        LEFT JOIN firm_growth g ON g.crd = c.crd
        WHERE c.status != 'roles' AND (c.searched_at IS NULL OR c.searched_at < ?)
          AND COALESCE(g.headcount, f.iar_count, 0) >= ?
        ORDER BY (c.searched_at IS NOT NULL),
                 COALESCE(g.joined_12m, 0) - COALESCE(g.left_12m, 0) DESC,
                 COALESCE(g.team_growth, 0) DESC, COALESCE(g.headcount, 0) DESC
        LIMIT ?""", (_ago(BOARD_DAYS), BOARD_MIN_PEOPLE, limit)).fetchall()
    conn.commit()
    return [dict(r) for r in rows]


def firm_states(conn, crd: str) -> set[str]:
    """Where the firm is: its main office, the offices it files, and the
    branches its registered people work from."""
    out: set[str] = set()
    for sql in ("SELECT state FROM firm_current WHERE crd=?",
                "SELECT DISTINCT state FROM firm_office WHERE crd=?",
                "SELECT DISTINCT state FROM person_employment WHERE org_pk=? AND kind='current'"):
        try:
            out |= {(r["state"] or "").strip().upper() for r in conn.execute(sql, (crd,))}
        except Exception:
            conn.rollback()
    conn.commit()
    return {x for x in out if x in openroles.US_STATES}


def run_boards(conn, rows: list[dict], budget: float, run) -> str:
    t0 = time.monotonic()
    s = websearch.Searcher(conn)
    if s.tripped:
        run.skip(s.tripped)
        return s.tripped
    tot = {"firms": 0, "postings": 0, "with": 0}
    stopped = ""
    for r in rows:
        if time.monotonic() - t0 > budget:
            stopped = "time budget used"
            break
        if s.tripped:
            stopped = s.tripped
            break
        crd = r["crd"]
        info = websearch.firm_info(conn, crd)
        name = (info["short"] or info["names"] or [""])[0]
        if not name:
            continue
        results = s.search(openroles.board_query(name))
        if results is None:
            continue             # the engines could not be asked; stays queued
        found = openroles.board_postings(results, info["names"] + info["short"],
                                         firm_states(conn, crd))
        now = _now()
        try:
            for title, url, board in found:
                conn.execute(
                    "INSERT INTO firm_job_posting (crd, title, function, url, source, host,"
                    " first_seen, last_seen, active) VALUES (?,?,?,?,'job_board',?,?,?,1)"
                    " ON CONFLICT (crd, title, url) DO UPDATE SET last_seen=excluded.last_seen,"
                    " active=1, function=excluded.function",
                    (crd, title[:200], openroles.function_of(title), url[:500], board, now, now))
            conn.execute("UPDATE firm_job_posting SET active=0 WHERE crd=? AND source='job_board'"
                         " AND active=1 AND last_seen < ?", (crd, now))
            conn.execute("UPDATE firm_careers SET searched_at=? WHERE crd=?", (now, crd))
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"  {crd}: not saved: {type(e).__name__}: {e}"[:200], flush=True)
            continue
        tot["firms"] += 1
        tot["postings"] += len(found)
        tot["with"] += bool(found)
        if found:
            print(f"  {crd} {name}: " + "; ".join(f"{t} ({b})" for t, _, b in found[:4]), flush=True)
    run.rows_out = tot["firms"]
    return (f"searched the job boards for {tot['firms']} firms: {tot['with']} with postings"
            f" ({tot['postings']} roles); {s.live} live searches, {s.cached} cached;"
            f" {time.monotonic() - t0:.0f}s" + (f"; stopped: {stopped}" if stopped else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boards", action="store_true", help="search the job boards instead")
    ap.add_argument("--limit", type=int, default=None, help="firms per run (40 sites, 20 searches)")
    ap.add_argument("--seconds", type=int, default=600, help="stop starting new firms after this")
    ap.add_argument("--workers", type=int, default=3, help="websites read at once")
    ap.add_argument("--crd", help="check this one firm now")
    ap.add_argument("--also-boards", action="store_true",
                    help="with --crd: search the job boards too when the site lists nothing")
    args = ap.parse_args()

    cfg = config.load()
    conn = db.connect()
    openroles.init(conn)
    websearch.init(conn)
    from prospect import growth
    growth.init(conn)        # todo orders by firm_growth, which may not exist yet

    stage = "boards" if args.boards else "site"
    with runlog.Run(conn, "open_roles", stage, cfg.stamp) as run:
        if args.boards:
            try:
                import ddgs  # noqa: F401
            except ImportError:
                run.skip("ddgs not installed")
                print("the ddgs package is not installed; nothing searched")
                return 0
            rows = ([{"crd": args.crd}] if args.crd else todo_boards(conn, args.limit or 20))
            if not rows:
                run.skip("every firm searched recently")
                print("no firm due for a job-board search")
                return 0
            msg = run_boards(conn, rows, args.seconds, run)
        else:
            if args.crd:
                r = conn.execute("SELECT crd, website FROM firm_current WHERE crd=?",
                                 (args.crd,)).fetchone()
                rows = [dict(r)] if r else []
            else:
                rows = todo_sites(conn, args.limit or 40)
            if not rows:
                run.skip("every firm checked recently")
                print("no firm due for a careers check")
                return 0
            msg = run_sites(conn, rows, args.workers, args.seconds, run)
            if args.crd and args.also_boards and rows:
                have = conn.execute("SELECT status FROM firm_careers WHERE crd=?",
                                    (args.crd,)).fetchone()
                conn.commit()
                if have and have["status"] != "roles":
                    msg += "; " + run_boards(conn, [{"crd": args.crd}], args.seconds, run)
        print(msg)
        run.note(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
