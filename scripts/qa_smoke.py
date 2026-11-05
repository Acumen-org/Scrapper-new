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

    python -m scripts.qa_smoke [--base http://127.0.0.1:8787] [--cadence-port 8799]

With --cadence-port, a fifth pass pushes to a fake Cadence this script starts
on that port (the app must point CADENCE_API_URL at it, with the same
CADENCE_API_KEY). --only cadence runs that pass alone, on a small database.
"""

from __future__ import annotations

import argparse
import json
import os
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
QA_USER = "qa-harness"
QA_PASS = "qa-test-password-1234"
QA_PLAIN = "qa-harness-user"     # a plain user seat, for the role checks

_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses to follow redirects, so a 3xx surfaces as an HTTPError.

    urllib follows redirects by default, which would turn "this route bounced me
    to the login page" into a silent 200 on the login HTML and make the whole
    login-wall pass vacuously green. The wall test has to see the 303 itself."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def ensure_qa_account() -> bool:
    """Create the harness accounts if absent: an admin for the routes and a
    plain user for the role checks. Returns True if we created them, so they
    can be removed afterwards: an account with a password written in this file
    must never be left sitting on a reachable deployment."""
    from prospect import auth, db, users
    c = db.connect()
    users.init(c)
    c.close()
    created = False
    for login, role in ((QA_USER, "admin"), (QA_PLAIN, "user")):
        if users.get(login):
            continue
        users.create(login, "QA Harness", role=role,
                     password_hash=auth.hash_password(QA_PASS))
        created = True
    return created


def remove_qa_account() -> None:
    from prospect import users
    for login in (QA_USER, QA_PLAIN):
        if users.get(login):
            users.delete(login)


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
    ("/", "Product lists"),
    ("/", "Latest intelligence"),
    ("/", "Data coverage"),
    ("/ask", "Bellwether AI"),
    ("/lists/phh_fund", "PHH Fund I"),
    ("/lists/phh_fund?cov=full", "Why it scores"),
    ("/lists/phh_fund?view=scoring", "Scored factors"),
    ("/lists/phh_fund?view=disqualified", "Removed"),
    ("/lists/phh_1031", "PHH 1031"),
    ("/lists/phh_jv?cov=gaps", "PHH JV"),
    ("/lists/acubooth", "AcuBooth"),
    ("/lists/acubooth?sort=potential", "AcuBooth"),
    ("/lists/acubooth?sig=1&reach=email", "AcuBooth"),
    ("/lists/glynac", "Glynac"),
    ("/lists/glynac?q=capital&owner=none", "Glynac"),
    ("/lists/glynac/export.csv", "rank"),
    ("/lists/ats", "ATS"),
    ("/lists/ats?hiring=1", "Hiring now"),
    ("/signals?window=15", "Last 15 days"),
    ("/signals?window=30", "Last 30 days"),
    ("/lists/acubooth/export.xlsx?cov=full", "PK"),
    ("/signals", "Signals"),
    ("/signals?state=", "Signals"),
    ("/signals?type=first_real_estate_fund", "First real estate fund"),
    ("/signals?type=custodian_change_from_platform", "Left Schwab"),
    ("/signals?product=acubooth&window=365", "Signals"),
    ("/signals?page=2", "Signals"),
    ("/firms", "Firms"),
    ("/firms?on=phh_fund&sort=hires", "Firms"),
    ("/firms?size=1-5b&reg=SEC", "Firms"),
    ("/firms?on=none", "Firms"),
    ("/firms?trig=open", "Firms"),
    ("/firms?q=WEALTH", "Firms"),
    ("/firms?stat=working", "Firms"),
    ("/firms?reach=verified", "Firms"),
    ("/firms?view=contacts", "Contacts"),
    ("/firms?view=contacts&on=acubooth", "Contacts"),
    ("/firms/export.csv?on=phh_fund", "crd"),
    ("/firms/export.xlsx?on=phh_fund", "PK"),
    ("/people", "People"),
    ("/people?joined=365&on=any", "People"),
    ("/people?officers=1&reach=email", "People"),
    ("/people/export.csv?on=acubooth", "name"),
    ("/saved", "Saved lists"),
    ("/enrichment", "Built-in sources"),
    ("/enrichment/websites", "Firm websites"),
    ("/enrichment/verify", "Check addresses now"),
    ("/settings", "Settings"),
    ("/settings/users", "Users and roles"),
    ("/settings/signin", "Microsoft"),
    ("/settings/ai", "What it may do"),
    ("/settings/verify", "Right now"),
    ("/settings/crawl", "Crawling"),
    ("/settings/jobs", "Weekly SEC cycle"),
    ("/settings/system", "Recent runs"),
    ("/settings/system.json", "snapshots"),
    ("/settings/cadence", "Do not contact"),
    ("/people?cad=none", "People"),
    ("/settings/review", "Review queue"),
    ("/settings/review?kind=match_13f", "Review queue"),
    ("/settings/review?kind=brochure_negation&page=2", "Review queue"),
    ("/api/search?q=capital", "href"),
    ("/api/jobs", "kind"),
    # The confirmation page only. POST /admin/quit is never exercised here for
    # the obvious reason that it would stop the server the tests are hitting.
    ("/quit", "Bellwether"),
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

    for path in ("/", "/firms", "/lists/acubooth", "/signals", "/saved", "/people",
                 "/settings", "/settings/review", "/enrichment", "/firms/export.csv",
                 "/lists/glynac/export.csv", "/settings/system.json", "/ask"):
        code, loc = raw(path)
        (ok if code == 303 and (loc or "").startswith("/login")
         else fail)(f"GET {path} blocked without a session (got {code})")

    for path in ("/admin/quit", "/settings/jobs/rescore/run", "/settings/save",
                 "/settings/users/add", "/lists/glynac/scoring", "/signals/action",
                 "/views/save", "/firm/1/level", "/enrichment/add"):
        code, _ = raw(path, "POST")
        (ok if code == 303 else fail)(
            f"POST {path} refused without a session (got {code})")
    for path in ("/api/ai/ask", "/api/firm/1/verify", "/api/firm/1/crawl"):
        code, _ = raw(path, "POST")
        (ok if code == 401 else fail)(
            f"POST {path} answers 401 without a session (got {code})")

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

    # Response hardening: set by the app, so it holds behind any gateway.
    bare = urllib.request.build_opener(_NoRedirect)
    try:
        rr = bare.open(base + "/login", timeout=20)
        h = rr.headers
    except urllib.error.HTTPError as exc:
        h = exc.headers
    for name in ("Content-Security-Policy", "X-Frame-Options",
                 "X-Content-Type-Options", "X-Robots-Tag", "Referrer-Policy"):
        (ok if h.get(name) else fail)(f"header {name} present")
    try:
        robots = bare.open(base + "/robots.txt", timeout=20).read().decode()
    except Exception as exc:
        robots = f"{exc}"
    (ok if "Disallow: /" in robots else fail)("robots.txt is public and disallows all")
    # The sign-in form must never carry an off-site destination forward.
    try:
        page_html = bare.open(base + "/login?next=//evil.example", timeout=20
                              ).read().decode()
    except urllib.error.HTTPError as exc:
        page_html = exc.read().decode()
    (ok if 'value="//evil.example"' not in page_html else fail)(
        "login next rejects an off-site target")


def pass_roles(base: str) -> None:
    """Seats: a plain user works the lists and is refused everything that
    configures Bellwether, whatever the URL."""
    print("\n[0b] seats (a plain user)")
    jar = urllib.request.HTTPCookieProcessor()
    op = urllib.request.build_opener(jar, _NoRedirect)
    body = urllib.parse.urlencode({"username": QA_PLAIN, "password": QA_PASS,
                                   "next": "/"}).encode()
    try:
        op.open(urllib.request.Request(base + "/login", data=body), timeout=20)
    except urllib.error.HTTPError:
        pass

    def code(path, data=None):
        try:
            r = op.open(urllib.request.Request(base + path, data=data), timeout=20)
            return r.status
        except urllib.error.HTTPError as exc:
            return exc.code

    for path in ("/", "/lists/acubooth", "/people", "/ask"):
        (ok if code(path) == 200 else fail)(f"user can open {path}")
    for path in ("/settings", "/settings/users", "/settings/jobs", "/enrichment"):
        (ok if code(path) == 403 else fail)(f"user is refused {path}")
    (ok if code("/settings/save", b"_group=ai&ai.provider=none") == 403 else fail)(
        "user cannot change settings")
    st = code("/lists/glynac/scoring", b"note=x")
    (ok if st == 303 else fail)(f"user cannot save scoring (got {st}, bounced)")


def pass_routes(base: str, detail_crd: str) -> None:
    print("\n[1] route matrix")
    routes = ROUTES + [(f"/firm/{detail_crd}", "Pipeline"),
                       (f"/firm/{detail_crd}", "Hiring needs"),
                       (f"/firm/{detail_crd}", "Discovery, sources and contact tools"),
                       (f"/firm/{detail_crd}", 'data-scope="firm:'),
                       (f"/firm/{detail_crd}?p=phh_fund", "Gates passed")]
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


def pass_markup(base: str) -> None:
    """Markup and styling, from scripts.qa_html.

    A page can return 200 with all the right words in it and still be visually
    broken: a duplicate style attribute silently dropped, a class with no rule,
    an unclosed tag reflowing the layout. Run here so one command covers both,
    while the session from sign_in is still live."""
    print("\n[1b] markup, CSS and template checks")
    from scripts import qa_html
    before = len(qa_html.FAILURES)
    for pth in qa_html.PAGES + qa_html.detail_pages():
        qa_html.check_page(base, pth, _OPENER)
    found = qa_html.FAILURES[before:]
    FAILURES.extend(found)
    if not found:
        ok(f"{len(qa_html.PAGES) + 1} pages: markup, CSS and templates clean")


def pass_writes(base: str) -> None:
    print("\n[2] write round-trips (disposable rows)")
    from prospect import db
    c = db.connect()

    crd = c.execute("SELECT crd FROM product_score WHERE product='phh_fund'"
                    " AND status='scored' ORDER BY rank LIMIT 1 OFFSET 1").fetchone()["crd"]
    prior = c.execute("SELECT status, owner FROM firm_status WHERE crd=?", (crd,)).fetchone()
    post(base, f"/firm/{crd}/status", {"status": "working", "owner": "QA"})
    c.commit()
    row = c.execute("SELECT status,owner FROM firm_status WHERE crd=?", (crd,)).fetchone()
    (ok if row and row["status"] == "working" and row["owner"] == "QA"
     else fail)("firm status round-trip")
    post(base, f"/firm/{crd}/status", {"status": (prior or {}).get("status") or "",
                                       "owner": (prior or {}).get("owner") or ""})
    if not prior:
        c.execute("DELETE FROM firm_status WHERE crd=?", (crd,))
        c.commit()

    # A manual level: set it, see the score move and carry the attribution,
    # then clear it and see the score come back.
    before = c.execute("SELECT score FROM product_score WHERE crd=? AND"
                       " product='phh_fund'", (crd,)).fetchone()["score"]
    post(base, f"/firm/{crd}/level", {"product": "phh_fund", "criterion": "relationship",
                                      "points": "100", "note": "qa"})
    c.commit()
    ov = c.execute("SELECT points FROM score_override WHERE crd=? AND product='phh_fund'"
                   " AND criterion='relationship'", (crd,)).fetchone()
    after = c.execute("SELECT score FROM product_score WHERE crd=? AND"
                      " product='phh_fund'", (crd,)).fetchone()["score"]
    (ok if ov and after >= before else fail)(
        f"manual level round-trip ({before:.1f} -> {after:.1f})")
    post(base, f"/firm/{crd}/level", {"product": "phh_fund", "criterion": "relationship",
                                      "points": ""})
    c.commit()
    back = c.execute("SELECT score FROM product_score WHERE crd=? AND"
                     " product='phh_fund'", (crd,)).fetchone()["score"]
    (ok if abs(back - before) < 0.05 else fail)(f"manual level cleared ({back:.1f})")

    prior_note = c.execute("SELECT note FROM firm_note WHERE crd=?", (crd,)).fetchone()
    post(base, f"/firm/{crd}/note", {"note": "qa note"})
    c.commit()
    row = c.execute("SELECT note FROM firm_note WHERE crd=?", (crd,)).fetchone()
    (ok if row and row["note"] == "qa note" else fail)("firm note round-trip")
    if prior_note:
        post(base, f"/firm/{crd}/note", {"note": prior_note["note"] or ""})
    else:
        c.execute("DELETE FROM firm_note WHERE crd=?", (crd,))
    c.commit()

    c.execute("INSERT OR REPLACE INTO adv_13f_match VALUES"
              " ('QA_CRD','QA_CIK',0.7,'NY','NY','q','q','qa','review',NULL,NULL,'qa')")
    c.execute("INSERT INTO brochure_negation (crd,tag,phrase,sentence)"
              " VALUES ('QA_CRD','covered_calls','covered call','qa sentence do not')")
    c.commit()
    nid = c.execute("SELECT id FROM brochure_negation WHERE crd='QA_CRD'").fetchone()["id"]
    post(base, "/review/match", {"crd": "QA_CRD", "cik": "QA_CIK", "decision": "confirmed"})
    post(base, "/review/negation", {"nid": nid, "decision": "negation_confirmed"})
    c.commit()
    m = c.execute("SELECT status FROM adv_13f_match WHERE crd='QA_CRD'").fetchone()["status"]
    n = c.execute("SELECT status FROM brochure_negation WHERE id=?", (nid,)).fetchone()["status"]
    (ok if m == "confirmed" else fail)("match decision round-trip")
    (ok if n == "negation_confirmed" else fail)("negation decision round-trip")
    c.execute("DELETE FROM adv_13f_match WHERE crd='QA_CRD'")
    c.execute("DELETE FROM brochure_negation WHERE crd='QA_CRD'")
    c.commit()

    # watch toggle round-trip (twice returns to the original state)
    post(base, f"/watch/{crd}", {"back": "/"})
    c.commit()
    w1 = c.execute("SELECT COUNT(*) n FROM firm_watch WHERE crd=?", (crd,)).fetchone()["n"]
    post(base, f"/watch/{crd}", {"back": "/"})
    c.commit()
    w2 = c.execute("SELECT COUNT(*) n FROM firm_watch WHERE crd=?", (crd,)).fetchone()["n"]
    (ok if w1 != w2 else fail)("watch toggle round-trip")

    # saved view create + delete
    post(base, "/views/save", {"page": "list:acubooth", "qs": "tier=A", "name": "QA view"})
    c.commit()
    v = c.execute("SELECT id FROM saved_view WHERE name='QA view'").fetchone()
    (ok if v else fail)("saved view created")
    if v:
        post(base, "/views/delete", {"vid": v["id"]})
        c.commit()
        gone = c.execute("SELECT COUNT(*) n FROM saved_view WHERE name='QA view'"
                         ).fetchone()["n"] == 0
        (ok if gone else fail)("saved view deleted")

    # job control: pause writes desired_state, resume puts it back
    post(base, "/settings/jobs/cusip_verify/pause", {})
    c.commit()
    stt = c.execute("SELECT desired_state FROM auto_task WHERE kind='cusip_verify'"
                    ).fetchone()
    (ok if stt and stt["desired_state"] == "paused" else fail)("job pause")
    post(base, "/settings/jobs/cusip_verify/resume", {})
    c.commit()
    stt = c.execute("SELECT desired_state FROM auto_task WHERE kind='cusip_verify'"
                    ).fetchone()
    (ok if stt and stt["desired_state"] == "running" else fail)("job resume")

    # scoring editor: a total that is not 100 is refused and stores nothing;
    # a valid edit is stored with its author, then reset to the defaults
    from prospect import products
    products.reload()
    p0 = products.product("glynac")
    form = {f"c-{i}-weight": str(cr["weight"]) for i, cr in enumerate(p0["criteria"])}
    form.update({f"c-{i}-label": cr["label"] for i, cr in enumerate(p0["criteria"])})
    bad = dict(form, **{"c-0-weight": str(float(p0["criteria"][0]["weight"]) + 5)})
    post(base, "/lists/glynac/scoring", bad)
    c.commit()
    stored = c.execute("SELECT COUNT(*) n FROM scoring_config WHERE product='glynac'").fetchone()["n"]
    (ok if stored == 0 else fail)("scoring edit totalling 105 refused")
    good = dict(form, note="qa", **{"c-0-label": "QA label"})
    post(base, "/lists/glynac/scoring", good)
    c.commit()
    row = c.execute("SELECT body, updated_by FROM scoring_config WHERE product='glynac'").fetchone()
    (ok if row and "QA label" in row["body"] else fail)("scoring edit stored")
    post(base, "/lists/glynac/scoring/reset", {})
    c.commit()
    gone = c.execute("SELECT COUNT(*) n FROM scoring_config WHERE product='glynac'").fetchone()["n"]
    (ok if gone == 0 else fail)("scoring reset to defaults")
    c.execute("DELETE FROM scoring_history WHERE note IN ('qa','reset to defaults')"
              " AND updated_by='QA Harness'")
    c.commit()

    t = c.execute("""SELECT t.id FROM trigger_event t LEFT JOIN trigger_action a
        ON a.trigger_id=t.id WHERE a.state IS NULL AND t.suppressed=0
        ORDER BY t.id DESC LIMIT 1""").fetchone()
    if t:
        post(base, "/signals/action", {"tid": t["id"], "state": "snoozed",
                                       "back": "/signals"})
        c.commit()
        st = c.execute("SELECT state FROM trigger_action WHERE trigger_id=?",
                       (t["id"],)).fetchone()["state"]
        (ok if st == "snoozed" else fail)("signal action round-trip")
        c.execute("DELETE FROM trigger_action WHERE trigger_id=?", (t["id"],))
        c.commit()
    c.close()


def pass_latency(base: str, detail_crd: str) -> None:
    print("\n[3] latency (3 samples per route, worst shown)")
    worst: list[tuple[float, str]] = []
    for path, _ in ROUTES[:0] or []:
        pass
    sample = ["/", "/lists/phh_fund", "/lists/acubooth", "/lists/glynac", "/signals",
              "/firms", "/people", "/settings/jobs", "/settings/review", f"/firm/{detail_crd}"]
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
    paths = ["/", "/lists/phh_fund", "/lists/acubooth?cov=full", "/signals", "/firms",
             "/people", "/settings/jobs", f"/firm/{detail_crd}", "/firms?on=glynac"]
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


def _json(base: str, path: str, payload: dict | None = None, opener=None, timeout: float = 90.0):
    """POST JSON (or GET without a payload) and read the JSON answer."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, headers={
        "Content-Type": "application/json", "Accept": "application/json"})
    try:
        r = (opener or _OPENER).open(req, timeout=timeout)
        return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def pass_cadence(base: str, port: int) -> None:
    """The push to Cadence through the running app, against the fake Cadence
    this process starts on `port`. The app must run with CADENCE_API_URL set to
    http://127.0.0.1:<port> and the same CADENCE_API_KEY as this process.
    Needs six people with an email in the People index who were never pushed."""
    print("\n[5] Cadence: preview, push, errors, retries, polling, seats")
    from prospect import cadence, db, users
    from scripts.fake_cadence import FakeCadence
    fake = FakeCadence(os.environ.get("CADENCE_API_KEY", ""), port=port).start()
    # pushedBy.email is required (14.4): the harness accounts get a test address.
    changed = False
    for login in (QA_USER, QA_PLAIN):
        if not (users.get(login) or {}).get("email"):
            users.save(login, email=f"{login}@acumen-strategy.test")
            changed = True
    if changed:
        time.sleep(users.TTL_S + 1)    # the app's account cache
    fake.add_user(f"{QA_USER}@acumen-strategy.test", "QA Harness", "senior_fo")
    c = db.connect()
    try:
        # Sending is switched on only after a test passes (B020).
        st, ping = _json(base, "/settings/cadence/test", {})
        (ok if st == 200 and ping.get("ok") else fail)(f"Test the connection: {ping.get('message')}")
        post(base, "/settings/save", {"_group": "cadence", "cadence.enabled": "on",
                                      "cadence.max_per_push": "200", "cadence.daily_per_user": "500"})
        ids = [cadence.contact_id(r["crd"], r["person_key"]) for r in c.execute(
            "SELECT p.crd, p.person_key FROM people_index p WHERE p.email IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM cadence_push x WHERE x.crd = p.crd"
            " AND x.person_key = p.person_key) ORDER BY p.crd, p.person_key LIMIT 6").fetchall()]
        c.commit()
        if len(ids) < 6:
            fail(f"Cadence pass needs six people with an email; found {len(ids)}")
            return
        plain = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        plain.open(urllib.request.Request(base + "/login", data=urllib.parse.urlencode(
            {"username": QA_PLAIN, "password": QA_PASS, "next": "/"}).encode()), timeout=20)
        st, pv = _json(base, "/api/cadence/preview", {"ids": ids[:1]}, opener=plain)
        (ok if st == 200 and "send" in pv else fail)(f"a plain user can open the push preview ({st})")
        st, _ = _json(base, "/settings/cadence", None, opener=plain)
        (ok if st == 403 else fail)(f"a plain user is refused Settings, Cadence ({st})")
        # One contact goes; the CRM check stops another before it goes.
        st, pv = _json(base, "/api/cadence/preview", {"ids": ids[:2]})
        (ok if st == 200 and pv.get("can_send") and len(pv["send"]) == 2 else fail)(
            f"preview: {len(pv.get('send', []))} would go")
        email = c.execute("SELECT email FROM people_index WHERE crd || ':' || person_key = ?",
                          (ids[1],)).fetchone()["email"]
        c.commit()
        fake.add_crm(emails=[email], owner="crm-owner@acumen-strategy.test")
        st, pv = _json(base, "/api/cadence/preview", {"ids": ids[:2]})
        crm = [x for x in pv.get("skipped", []) if x["id"] == ids[1]]
        (ok if crm and crm[0]["why"].startswith("Already in CRM") else fail)(
            "the check blocks a contact already in the CRM")
        st, res = _json(base, "/api/cadence/push", {"ids": ids[:2], "preview_id": pv.get("preview_id")})
        states = {r["id"]: r["state"] for r in res.get("results", [])}
        (ok if states.get(ids[0]) == "in_sandbox" and states.get(ids[1]) == "skipped" else fail)(
            f"push one: {res.get('summary')}")
        sent = {ct["bellwetherContactId"] for rq in fake.requests_of("push")
                for ct in json.loads(rq["body"])["contacts"]}
        (ok if ids[1] not in sent else fail)("the blocked contact was not sent")
        # A 429, retried in the background with the same key, then a partial refusal.
        fake.forced[ids[2]] = {"result": "rejected", "reason": "blocked_account"}
        fake.then("push", status=429, code="rate_limited", retry_after=1)
        st, res = _json(base, "/api/cadence/push", {"ids": ids[2:4]})
        bid, b = res.get("batch_id"), {}
        for _ in range(60):
            st, b = _json(base, f"/api/cadence/batch/{bid}", None)
            if b.get("finished"):
                break
            time.sleep(2)
        states = {r["id"]: r["state"] for r in b.get("results", [])}
        keys = [rq["idem"] for rq in fake.requests_of("push")][-2:]
        (ok if states.get(ids[2]) == "rejected" and states.get(ids[3]) == "in_sandbox" else fail)(
            f"batch polled to the end, 429 retried, a partial refusal: {b.get('summary')}")
        (ok if len(set(keys)) == 1 else fail)("the retry after 429 used the same Idempotency-Key")
        st, b2 = _json(base, f"/api/cadence/batch/{bid}", None, opener=plain)
        (ok if not b2.get("ok") else fail)("a plain user cannot read someone else's push")
        # A timeout, retried with the same key and body.
        fake.slow_s = 65
        fake.then("push", action="timeout")
        st, res = _json(base, "/api/cadence/push", {"ids": ids[5:6]}, timeout=200)
        for _ in range(60):
            row = c.execute("SELECT state FROM cadence_push WHERE crd || ':' || person_key = ?",
                            (ids[5],)).fetchone()
            c.commit()
            if row and row["state"] == "in_sandbox":
                break
            time.sleep(2)
        last = fake.requests_of("push")[-2:]
        (ok if row and row["state"] == "in_sandbox" and last[0]["idem"] == last[1]["idem"]
         and last[0]["body"] == last[1]["body"] else fail)("a timeout is retried with the same key and body")
        fake.slow_s = 1.5
        # 401 holds the queue; Test the connection lifts it and the held push goes.
        fake.then("push", status=401, code="unauthorized")
        _json(base, "/api/cadence/push", {"ids": ids[4:5]})
        held = cadence.held(c)
        c.commit()
        (ok if held == "unauthorized" else fail)("401 holds the queue")
        _json(base, "/settings/cadence/test", {})
        row = None
        for _ in range(30):
            row = c.execute("SELECT state FROM cadence_push WHERE crd || ':' || person_key = ?",
                            (ids[4],)).fetchone()
            c.commit()
            if row and row["state"] == "in_sandbox":
                break
            cadence.drain(c, max_s=5)
            time.sleep(1)
        (ok if row and row["state"] == "in_sandbox" else fail)(
            f"the hold lifts and the held push goes ({row['state'] if row else None})")
        # Pages show where people stand; an opt-out in Cadence comes back.
        fake.set_status(ids[0], "opted_out")
        st, rf = _json(base, "/api/cadence/refresh", {"ids": [ids[0]]})
        line = (rf.get("lines") or {}).get(ids[0], {}).get("html", "")
        (ok if "Do not contact · Cadence" in line else fail)("an opt-out read by id shows as Do not contact · Cadence")
        st, body, _ = get(base, "/people?cad=optout")
        (ok if "Do not contact · Cadence" in body else fail)("People shows Do not contact · Cadence")
        crd = ids[0].split(":", 1)[0]
        st, body, _ = get(base, f"/firm/{crd}")
        (ok if "data-cad-push" in body and "Do not contact · Cadence" in body else fail)(
            "the firm page shows Send buttons and the opt-out")
        st, body, _ = get(base, "/settings/cadence")
        (ok if "Recent sends" in body else fail)("Settings, Cadence shows the sends")
        from scripts import qa_html
        before = len(qa_html.FAILURES)
        for pth in ("/people", "/people?cad=in", "/people?cad=optout", f"/firm/{crd}", "/settings/cadence"):
            qa_html.check_page(base, pth, _OPENER)
        found = qa_html.FAILURES[before:]
        FAILURES.extend(found)
        (ok if not found else fail)("Cadence pages: markup, CSS and templates clean")
    finally:
        c.close()
        fake.stop()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8787")
    ap.add_argument("--cadence-port", type=int, default=0,
                    help="also run the Cadence pass, with the fake Cadence on this port")
    ap.add_argument("--only", choices=("cadence",), default=None,
                    help="cadence: sign in and run the Cadence pass alone, for a small test database")
    args = ap.parse_args()

    from prospect import db
    if args.only == "cadence":
        created = sign_in(args.base)
        try:
            pass_cadence(args.base, args.cadence_port or 8799)
        finally:
            if created:
                remove_qa_account()
                print("\n(removed the temporary qa account)")
        print("\n" + "=" * 60)
        print(f"QA: {len(FAILURES)} FAILURE(S)" if FAILURES else "QA: ALL CHECKS PASS")
        for f in FAILURES:
            print(f"  - {f}")
        return 1 if FAILURES else 0

    c = db.connect()
    detail_crd = c.execute("SELECT crd FROM product_score WHERE product='phh_fund'"
                           " AND status='scored' ORDER BY rank LIMIT 1").fetchone()["crd"]
    c.close()

    pass_auth(args.base)
    created = sign_in(args.base)
    try:
        pass_roles(args.base)
        pass_routes(args.base, detail_crd)
        pass_markup(args.base)
        pass_writes(args.base)
        pass_latency(args.base, detail_crd)
        pass_concurrency(args.base, detail_crd)
        if args.cadence_port:
            pass_cadence(args.base, args.cadence_port)
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
