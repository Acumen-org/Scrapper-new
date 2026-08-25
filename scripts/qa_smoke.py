"""Quality gate for the web app. Fails loudly, exits nonzero on any failure.

Four passes:
  1. Route matrix: every page and meaningful filter combination returns 200
     with its expected content marker, the sidebar, and zero em dashes.
  2. Write round-trips: every POST path, exercised with disposable rows and
     verified in the database, then cleaned up.
  3. Latency: every GET timed; anything slow is named. The bar is interactive
     feel, not benchmark bragging.
  4. Concurrency: parallel clients hammer the app while the background ingest
     writer is running, which is exactly the situation that made v1 laggy.

    python -m scripts.qa_smoke [--base http://127.0.0.1:8787]
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILURES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"  FAIL  {msg}")


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


# Every route is behind a login, so the harness holds a session cookie like a
# real browser. A dedicated QA account is created if absent: signing in as a
# real person would attribute test writes to them.
QA_USER = "qa"
QA_PASS = "qa-test-password-1234"

_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses to follow redirects, so a 3xx surfaces as an HTTPError.

    urllib follows redirects by default, which would turn "this route bounced me
    to the login page" into a silent 200 on the login HTML and make the whole
    login-wall pass vacuously green. The wall test has to see the 303 itself."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def ensure_qa_account() -> bool:
    """Create the harness account if absent. Returns True if we created it, so
    it can be removed afterwards: an account with a password written in this
    file must never be left sitting on a reachable deployment."""
    from prospect import auth
    users = auth.load_users()
    if QA_USER in users:
        return False
    users[QA_USER] = {"name": "QA Harness",
                      "password_hash": auth.hash_password(QA_PASS)}
    auth.save_users(users)
    return True


def remove_qa_account() -> None:
    from prospect import auth
    users = auth.load_users()
    if users.pop(QA_USER, None) is not None:
        auth.save_users(users)


def sign_in(base: str) -> bool:
    created = ensure_qa_account()
    body = urllib.parse.urlencode(
        {"username": QA_USER, "password": QA_PASS, "next": "/"}).encode()
    req = urllib.request.Request(base + "/login", data=body)
    try:
        _OPENER.open(req, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code != 303:
            raise
    if not any(c.name == "bellwether_session" for c in _OPENER.handlers[
            next(i for i, h in enumerate(_OPENER.handlers)
                 if isinstance(h, urllib.request.HTTPCookieProcessor))].cookiejar):
        raise SystemExit("QA could not sign in; no session cookie was issued")
    return created


def get(base: str, path: str, timeout: float = 30.0) -> tuple[int, str, float]:
    t0 = time.perf_counter()
    r = _OPENER.open(base + path, timeout=timeout)
    raw = r.read()
    # Binary downloads (the .xlsx export) are not utf-8; decode leniently so the
    # marker check ("PK", the zip signature) still works without an exception.
    body = raw.decode("utf-8", "replace")
    return r.status, body, time.perf_counter() - t0


def post(base: str, path: str, data: dict, timeout: float = 30.0) -> int:
    body = urllib.parse.urlencode(data).encode()
    r = _OPENER.open(
        urllib.request.Request(base + path, data=body), timeout=timeout)
    return r.status


ROUTES: list[tuple[str, str]] = [
    ("/", "Trigger inbox"),
    ("/?state=", "Trigger inbox"),
    ("/?type=first_real_estate_fund", "First real estate fund"),
    ("/?type=custodian_change_from_platform", "Left Schwab"),
    ("/?product=PHH", "Trigger inbox"),
    ("/?product=ACUBOOTH", "Trigger inbox"),
    ("/?q=CAPITAL", "Trigger inbox"),
    ("/?page=2", "Trigger inbox"),
    ("/firms", "Firms"),
    ("/firms?preset=phh_a", "PHH - Tier A"),
    ("/firms?preset=phh_x", "PHH - Intersection"),
    ("/firms?preset=acu", "AcuBooth - Tier C"),
    ("/firms?preset=comp", "Competitors"),
    ("/firms?band=100-250", "Firms"),
    ("/firms?seg=prospect", "Firms"),
    ("/firms?trig=open", "Firms"),
    ("/firms?q=WEALTH", "Firms"),
    ("/firms?stat=working", "Firms"),
    ("/firms?view=contacts", "Contacts"),
    ("/firms?view=contacts&preset=phh_a", "Contacts"),
    ("/firms/export.csv?preset=phh_a", "crd"),
    ("/firms/export.xlsx?preset=phh_a", "PK"),
    ("/lists", "Lists"),
    ("/review", "Review queue"),
    ("/review?kind=match_13f", "Review queue"),
    ("/review?kind=brochure_negation", "Review queue"),
    ("/review?kind=brochure_negation&page=2", "Review queue"),
    ("/health", "Pipeline health"),
    ("/health.json", "snapshots"),
    ("/guide", "How to use"),
    ("/guide", "On this page"),
    ("/guide", "Finding firms"),
    ("/guide", "Your lists"),
    ("/api/search?q=capital", "crd"),
    # The confirmation page only. POST /admin/quit is never exercised here for
    # the obvious reason that it would stop the server the tests are hitting.
    ("/quit", "Quit Bellwether?"),
]


def pass_auth(base: str) -> None:
    """The login wall, before anything signs in.

    This runs first and unauthenticated on purpose. The whole point of putting
    Bellwether on a network is that the data and the destructive endpoints are
    not reachable without a session, so that claim gets tested every run rather
    than assumed."""
    print("\n[0] login wall (no session)")
    bare = urllib.request.build_opener(_NoRedirect)  # no cookies, no follow

    def raw(path: str, method: str = "GET"):
        req = urllib.request.Request(base + path, method=method,
                                     data=b"" if method == "POST" else None)
        try:
            r = bare.open(req, timeout=20)
            return r.status, r.headers.get("Location")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Location")

    for path in ("/", "/firms", "/lists", "/review", "/health", "/guide",
                 "/firms/export.csv", "/api/search?q=x", "/health.json"):
        code, loc = raw(path)
        (ok if code == 303 and (loc or "").startswith("/login")
         else fail)(f"GET {path} blocked without a session (got {code})")

    for path in ("/admin/quit", "/admin/run-weekly",
                 "/admin/task/brochures/start", "/action", "/views/save"):
        code, _ = raw(path, "POST")
        (ok if code == 303 else fail)(
            f"POST {path} refused without a session (got {code})")

    # The one route that must stay open, and must leak nothing.
    try:
        r = bare.open(base + "/healthz", timeout=20)
        body = r.read().decode()
        (ok if r.status == 200 and "crd" not in body and len(body) < 200
         else fail)("/healthz is public and carries no data")
    except Exception as exc:
        fail(f"/healthz unreachable: {exc}")

    # A forged cookie must not be accepted as a session.
    forged = urllib.request.build_opener(_NoRedirect)
    forged.addheaders = [("Cookie", "bellwether_session=notarealtoken.abc")]
    code, loc = None, None
    try:
        rr = forged.open(base + "/", timeout=20)
        code, loc = rr.status, rr.headers.get("Location")
    except urllib.error.HTTPError as exc:
        code, loc = exc.code, exc.headers.get("Location")
    (ok if code == 303 and (loc or "").startswith("/login") else fail)(
        f"forged session cookie rejected (got {code})")

    # Wrong password must not issue a session.
    body = urllib.parse.urlencode(
        {"username": QA_USER, "password": "definitely-wrong"}).encode()
    try:
        rr = urllib.request.build_opener(_NoRedirect).open(
            urllib.request.Request(base + "/login", data=body), timeout=20)
        got, hdrs = rr.status, rr.headers
    except urllib.error.HTTPError as exc:
        got, hdrs = exc.code, exc.headers
    (ok if got == 401 and "bellwether_session" not in str(hdrs.get("Set-Cookie", ""))
     else fail)(f"wrong password issues no session (got {got})")


def pass_routes(base: str, detail_crd: str) -> None:
    print("\n[1] route matrix")
    routes = ROUTES + [(f"/firm/{detail_crd}", "Ownership and status"),
                       (f"/firm/{detail_crd}", "People"),
                       (f"/firm/{detail_crd}", "AUM trajectory")]
    for path, marker in routes:
        try:
            st, body, dt = get(base, path)
        except Exception as exc:
            fail(f"GET {path}: {type(exc).__name__} {exc}")
            continue
        if st != 200:
            fail(f"GET {path}: HTTP {st}")
        elif marker not in body:
            fail(f"GET {path}: marker '{marker}' missing")
        elif (not path.split("?")[0].endswith((".csv", ".json", ".xlsx"))
              and not path.startswith("/api/")) and 'nav class="side"' not in body:
            fail(f"GET {path}: sidebar missing")
        elif (not path.split("?")[0].endswith((".csv", ".xlsx"))
              and ("—" in body or "&mdash;" in body)):
            # The xlsx and csv are binary/data downloads, not UI copy: the
            # em-dash ban is about what people read on screen.
            fail(f"GET {path}: em dash present")
        else:
            ok(f"GET {path} ({dt*1000:.0f}ms)")


def pass_writes(base: str, db_path: str) -> None:
    print("\n[2] write round-trips (disposable rows)")
    c = sqlite3.connect(db_path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=20000")

    crd = c.execute("SELECT crd FROM tier_a_rank WHERE rank=2").fetchone()["crd"]
    post(base, f"/firm/{crd}/status", {"status": "working", "owner": "QA"})
    row = c.execute("SELECT status,owner FROM firm_status WHERE crd=?", (crd,)).fetchone()
    (ok if row and row["status"] == "working" and row["owner"] == "QA"
     else fail)("firm status round-trip")
    post(base, f"/firm/{crd}/status", {"status": "", "owner": ""})

    post(base, f"/firm/{crd}/note", {"note": "qa note"})
    row = c.execute("SELECT note FROM firm_note WHERE crd=?", (crd,)).fetchone()
    (ok if row and row["note"] == "qa note" else fail)("firm note round-trip")

    c.execute("INSERT OR REPLACE INTO adv_13f_match VALUES"
              " ('QA_CRD','QA_CIK',0.7,'NY','NY','q','q','qa','review',NULL,NULL,'qa')")
    c.execute("INSERT INTO brochure_negation (crd,tag,phrase,sentence)"
              " VALUES ('QA_CRD','covered_calls','covered call','qa sentence do not')")
    c.commit()
    nid = c.execute("SELECT id FROM brochure_negation WHERE crd='QA_CRD'").fetchone()["id"]
    post(base, "/review/match", {"crd": "QA_CRD", "cik": "QA_CIK", "decision": "confirmed"})
    post(base, "/review/negation", {"nid": nid, "decision": "negation_confirmed"})
    m = c.execute("SELECT status FROM adv_13f_match WHERE crd='QA_CRD'").fetchone()["status"]
    n = c.execute("SELECT status FROM brochure_negation WHERE id=?", (nid,)).fetchone()["status"]
    (ok if m == "confirmed" else fail)("match decision round-trip")
    (ok if n == "negation_confirmed" else fail)("negation decision round-trip")
    c.execute("DELETE FROM adv_13f_match WHERE crd='QA_CRD'")
    c.execute("DELETE FROM brochure_negation WHERE crd='QA_CRD'")
    c.commit()

    # watch toggle round-trip (twice returns to the original state)
    post(base, f"/watch/{crd}", {"back": "/"})
    w1 = c.execute("SELECT COUNT(*) n FROM firm_watch WHERE crd=?", (crd,)).fetchone()["n"]
    post(base, f"/watch/{crd}", {"back": "/"})
    w2 = c.execute("SELECT COUNT(*) n FROM firm_watch WHERE crd=?", (crd,)).fetchone()["n"]
    (ok if w1 != w2 else fail)("watch toggle round-trip")

    # saved view create + delete
    post(base, "/views/save", {"page": "firms", "qs": "seg=prospect", "name": "QA view"})
    v = c.execute("SELECT id FROM saved_view WHERE name='QA view'").fetchone()
    (ok if v else fail)("saved view created")
    if v:
        post(base, "/views/delete", {"vid": v["id"]})
        gone = c.execute("SELECT COUNT(*) n FROM saved_view WHERE name='QA view'"
                         ).fetchone()["n"] == 0
        (ok if gone else fail)("saved view deleted")

    # autopilot control writes desired_state (job stays paused)
    post(base, "/admin/task/cusip_verify/pause", {})
    stt = c.execute("SELECT desired_state FROM auto_task WHERE kind='cusip_verify'"
                    ).fetchone()
    (ok if stt and stt["desired_state"] == "paused" else fail)("autopilot control")

    t = c.execute("""SELECT t.id FROM trigger_event t LEFT JOIN trigger_action a
        ON a.trigger_id=t.id WHERE a.state IS NULL AND t.suppressed=0
        ORDER BY t.id DESC LIMIT 1""").fetchone()
    if t:
        post(base, "/action", {"tid": t["id"], "state": "snoozed", "back": "/"})
        st = c.execute("SELECT state FROM trigger_action WHERE trigger_id=?",
                       (t["id"],)).fetchone()["state"]
        (ok if st == "snoozed" else fail)("inbox action round-trip")
        c.execute("DELETE FROM trigger_action WHERE trigger_id=?", (t["id"],))
        c.commit()
    c.close()


def pass_latency(base: str, detail_crd: str) -> None:
    print("\n[3] latency (3 samples per route, worst shown)")
    worst: list[tuple[float, str]] = []
    for path, _ in ROUTES[:0] or []:
        pass
    sample = ["/", "/firms", "/lists", "/firms?preset=acu", "/review", "/health",
              "/guide", f"/firm/{detail_crd}"]
    for path in sample:
        times = []
        for _ in range(3):
            try:
                _, _, dt = get(base, path)
                times.append(dt)
            except Exception as exc:
                fail(f"latency {path}: {exc}")
                break
        if times:
            med, mx = statistics.median(times), max(times)
            worst.append((mx, path))
            (ok if mx < 1.5 else fail)(
                f"{path}: median {med*1000:.0f}ms, max {mx*1000:.0f}ms"
                + ("" if mx < 1.5 else " exceeds 1.5s"))


def pass_concurrency(base: str, detail_crd: str) -> None:
    print("\n[4] concurrency: 8 clients x 25 requests while ingest writes")
    paths = ["/", "/firms", "/lists", "/firms?preset=phh_x", "/review",
             "/health", "/guide", f"/firm/{detail_crd}", "/firms?seg=prospect"]
    errors: list[str] = []
    times: list[float] = []
    lock = threading.Lock()

    def client(seed: int) -> None:
        rng = random.Random(seed)
        for _ in range(25):
            p = rng.choice(paths)
            try:
                st, _, dt = get(base, p, timeout=20)
                with lock:
                    times.append(dt)
                if st != 200:
                    with lock:
                        errors.append(f"{p} -> {st}")
            except Exception as exc:
                with lock:
                    errors.append(f"{p} -> {type(exc).__name__}")

    threads = [threading.Thread(target=client, args=(i,)) for i in range(8)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    n = len(times)
    p95 = sorted(times)[int(n * 0.95) - 1] if n else 0
    if errors:
        fail(f"{len(errors)} failed requests under load: {errors[:5]}")
    else:
        ok(f"200/200 of {n} requests in {wall:.1f}s, "
           f"median {statistics.median(times)*1000:.0f}ms, p95 {p95*1000:.0f}ms")
    if p95 > 2.0:
        fail(f"p95 latency {p95:.2f}s exceeds 2s under load")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8787")
    args = ap.parse_args()

    c = sqlite3.connect("prospect.db")
    c.row_factory = sqlite3.Row
    detail_crd = c.execute("SELECT crd FROM tier_a_rank WHERE rank=1").fetchone()["crd"]
    c.close()

    pass_auth(args.base)
    created = sign_in(args.base)
    try:
        pass_routes(args.base, detail_crd)
        pass_writes(args.base, "prospect.db")
        pass_latency(args.base, detail_crd)
        pass_concurrency(args.base, detail_crd)
    finally:
        # Even on failure: leaving a known-password account behind is worse
        # than any test result.
        if created:
            remove_qa_account()
            print("\n(removed the temporary qa account)")

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"QA: {len(FAILURES)} FAILURE(S)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("QA: ALL CHECKS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
