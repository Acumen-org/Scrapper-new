"""The email hunter: keep asking until a mail server confirms an address.

No guessed address is ever shown. The old job wrote one guess per person into
contact_point and left it there, unverified, which on the live server came to
some 160,000 addresses nobody could trust. This replaces it. For each person
at an in-scope firm (best firms first) who has no usable email:

  1. The firm's mail domain (emailguess.domain_for). A consumer mail service,
     a domain with no mail server, or no domain at all ends the hunt for the
     whole firm, with the reason recorded for every person.
  2. Whether the domain accepts any address at all. A server that says yes to
     a made-up mailbox cannot confirm a real one, so nothing is guessed there:
     each person is recorded as accept_all_domain instead. The answer is
     remembered per domain (verify's mail_domain) and asked again after 30 days.
  3. The candidates, most likely first (emailguess.candidates): the firm's own
     pattern, then the rest by how common they are, then nicknames, middle
     names and the halves of double surnames.
  4. One conversation with the firm's mail server asks about them in turn
     (verify.MailSession: RSET, MAIL FROM, RCPT TO for each, never DATA). A
     refused mailbox moves on to the next candidate. An accepted one is
     followed at once by a made-up address in the same conversation: only if
     that is refused does the acceptance count, and the address becomes a
     contact_point row (source pattern, verify_status valid) and the firm's
     pattern for everyone after. A "try again later" (greylisting, rate
     limits, a dropped line) is retried with growing delays, never counted as
     a refusal.
  5. Every question asked is one row in email_attempt, unique on the address,
     so no address is asked about twice and the screens can say "tried 4
     addresses". When every candidate is refused the person is exhausted and
     looked at again after 60 days, when new candidates or a new mailbox may
     exist.

Through Reacher when one is configured and answering, the same plan runs one
address per Reacher call (Reacher does its own made-up-address test), else
over this machine's own port 25. Everything is bounded: a time budget per run,
a cap on people per run and per firm, a cap on conversations per firm, the
per-minute cap and the one-conversation-per-server rule from prospect.verify.

The network work happens in a few threads, one firm each, that never touch the
database; the main thread prepares each firm and stores what came back.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import contacts, emailguess, verify

SCHEMA = """
CREATE TABLE IF NOT EXISTS email_attempt (
    id          INTEGER PRIMARY KEY,
    crd         TEXT NOT NULL,
    person_key  TEXT NOT NULL,
    address     TEXT NOT NULL,
    pattern     TEXT,               -- first.last, flast ..., with any variant
    status      TEXT NOT NULL,      -- valid | invalid | retry | unknown | catch_all
    detail      TEXT,               -- the server's reply, shortened
    tries       INTEGER NOT NULL DEFAULT 1,
    engine      TEXT,               -- native | reacher
    next_try_at TEXT,               -- retry only: when to ask again
    checked_at  TEXT NOT NULL,
    UNIQUE (address)
);
CREATE INDEX IF NOT EXISTS ix_ea_person ON email_attempt (crd, person_key);
CREATE TABLE IF NOT EXISTS email_hunt (
    crd         TEXT NOT NULL,
    person_key  TEXT NOT NULL,
    person_name TEXT,
    rank        INTEGER,            -- 0 filed officer, 1 on the website, 2 roster, 3 legacy guess only
    state       TEXT NOT NULL,      -- see STATE_LABEL
    domain      TEXT,
    tried       INTEGER NOT NULL DEFAULT 0,
    found       TEXT,               -- the confirmed address
    detail      TEXT,               -- why, in words, for the screens
    next_try_at TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (crd, person_key)
);
CREATE INDEX IF NOT EXISTS ix_eh_state ON email_hunt (state, next_try_at);
CREATE TABLE IF NOT EXISTS email_hunt_firm (
    crd         TEXT PRIMARY KEY,
    domain      TEXT,
    state       TEXT NOT NULL,      -- active | done | accept_all | no_mail_server | no_domain
                                    -- | free_mail | blocked | unreachable | waiting | dns | error
    pattern     TEXT,               -- the pattern confirmed addresses follow here
    fails       INTEGER NOT NULL DEFAULT 0,
    found       INTEGER NOT NULL DEFAULT 0,
    detail      TEXT,
    next_try_at TEXT,
    checked_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ehf_due ON email_hunt_firm (next_try_at);
"""

STATE_LABEL = {
    "queued": "Waiting for the email hunt",
    "searching": "Checking likely addresses with the firm's mail server",
    "found": "Verified address found",
    "exhausted": "Every likely address was turned away",
    "unverifiable": "The mail server would not give a clear answer",
    "accept_all_domain": "The firm's mail server accepts any address, so a guess cannot be confirmed",
    "no_mail_server": "The firm's domain has no mail server",
    "no_domain": "No mail domain known for the firm",
    "free_mail": "The firm uses a consumer mail service",
    "blocked": "The firm's mail server refuses verification checks",
    "unnamed": "The name on file is too short to build an address",
}
FINAL = ("valid", "invalid", "unknown", "catch_all")   # attempt verdicts never re-asked

EXHAUSTED_DAYS = 60
ACCEPT_ALL_DAYS = verify.CATCH_ALL_DAYS
NO_MX_DAYS = 30
NO_DOMAIN_DAYS = 14
FREE_MAIL_DAYS = 30
FIRM_RECHECK_DAYS = 7          # a finished firm is looked at again for new people
UNNAMED_DAYS = 180
VALID_REUSE_DAYS = 90          # a confirmed address is trusted this long without asking again
RETRY_MINUTES = (15, 60, 240, 960)   # then the address is given up as unknown
FULL_RETRY_MINUTES = 1440      # a full mailbox exists; ask again tomorrow
GREYLIST_MINUTES = 15
BLOCK_BACKOFF_HOURS = (1, 3, 8, 24)
YIELD_MINUTES = 20             # a big firm steps aside after its share of a run
PEOPLE_PER_FIRM = 40
SESSIONS_PER_FIRM = 3
# One firm asked for now (the firm page's button) gets a bigger share: it is
# the only firm in that run.
PEOPLE_PER_FIRM_NOW = 200
SESSIONS_PER_FIRM_NOW = 12
WORKERS = 4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _later(**kw) -> str:
    return (datetime.now(timezone.utc) + timedelta(**kw)).isoformat(timespec="seconds")


def init(conn) -> None:
    """Create the hunter's tables. Asks the catalogue first, so the call costs
    nothing (and takes no lock) once they exist."""
    try:
        r = conn.execute("SELECT to_regclass('ix_ehf_due') IS NOT NULL AS ok").fetchone()
        if r and r["ok"]:
            return
    except Exception:
        conn.rollback()
    conn.executescript(SCHEMA)
    conn.commit()


class NoEngine(RuntimeError):
    """Neither Reacher nor port 25 is available, so nothing can be confirmed."""


# ------------------------------------------------------------------ people

@dataclass
class Person:
    key: str
    name: str
    first: str
    middle: str
    last: str
    others: list = field(default_factory=list)
    title: str | None = None
    rank: int = 2
    aliases: set = field(default_factory=set)
    # (address, label) to ask about before any pattern: an address AI research
    # found printed on a page (label 'ai_web'), or an old job's unconfirmed
    # guess (label: its pattern).
    seeds: list = field(default_factory=list)


def pattern_of(ref: str | None) -> str:
    """The pattern name inside a stored source_ref ('pattern:flast',
    'best-guess pattern (first)', 'first.last (nickname bill)')."""
    ref = (ref or "").lower()
    for pat in sorted(emailguess.PATTERNS, key=len, reverse=True):
        if re.search(r"(^|[\s:(])" + re.escape(pat) + r"($|[\s),])", ref):
            return pat
    return "earlier guess"


def _person(key: str, name: str, parts, rank: int, title: str | None = None) -> Person | None:
    if not parts:
        return None
    f, m, l, others = parts
    if not l:
        return None
    return Person(key=key, name=name, first=f, middle=m, last=l, others=list(others),
                  title=title, rank=rank)


def firm_people(conn, crd: str, *, only_seeded: bool = False) -> list[Person]:
    """Everyone at the firm worth an address, officers first: the Schedule A
    officers, the people its website names, the registered roster, and anyone
    an earlier job guessed an address for. One Person per name: a website
    entry or officer that matches a roster name joins that roster record."""
    people: dict[str, Person] = {}
    by_name: dict[tuple, str] = {}

    def add(p: Person | None, alias: str | None = None) -> Person | None:
        if p is None:
            return None
        nk = (p.first, p.last)
        if p.key in people:
            have = people[p.key]
        elif (nk in by_name and p.first
              and not (p.key.startswith("i:") and by_name[nk].startswith("i:"))):
            # The same name from another source is the same person; two roster
            # records with one name are namesakes, and stay two people.
            have = people[by_name[nk]]
        else:
            people[p.key] = p
            if p.first:
                by_name.setdefault(nk, p.key)
            return p
        if p.key != have.key:
            have.aliases.add(p.key)
        if alias and alias != have.key:
            have.aliases.add(alias)
        have.rank = min(have.rank, p.rank)
        have.title = have.title or p.title
        return have

    try:
        for r in conn.execute(
                "SELECT p.indvl_pk, p.name, p.first_name, p.middle_name, p.last_name"
                " FROM person_employment e JOIN person p ON p.indvl_pk = e.indvl_pk"
                " WHERE e.org_pk = ? AND e.kind = 'current'", (crd,)).fetchall():
            parts = emailguess.split_name(r["first_name"], r["middle_name"], r["last_name"])
            add(_person(f"i:{r['indvl_pk']}", r["name"], parts, 2))
    except Exception:
        conn.rollback()
    try:
        for r in conn.execute("SELECT name, title FROM schedule_a WHERE crd=? AND is_individual=1",
                              (crd,)).fetchall():
            full = emailguess.pretty(r["name"])
            key = contacts.name_key(full)
            if key:
                add(_person(key, full, emailguess.parse_full(r["name"]), 0, r["title"]))
    except Exception:
        conn.rollback()
    for r in conn.execute(
            "SELECT person_key, MAX(person_name) AS person_name, MAX(title) AS title"
            " FROM contact_point WHERE crd=? AND person_key != '' AND person_name IS NOT NULL"
            " AND source NOT IN ('pattern') GROUP BY person_key", (crd,)).fetchall():
        key = r["person_key"]
        if key in people:
            people[key].rank = min(people[key].rank, 1)
            people[key].title = people[key].title or r["title"]
            continue
        add(_person(key, r["person_name"], emailguess.parse_full(r["person_name"]), 1,
                    r["title"]))
    try:
        for r in conn.execute("SELECT person, MAX(title) AS title FROM web_contact"
                              " WHERE crd=? AND person IS NOT NULL GROUP BY person",
                              (crd,)).fetchall():
            key = contacts.name_key(r["person"])
            if key:
                add(_person(key, r["person"], emailguess.parse_full(r["person"]), 1,
                            r["title"]))
    except Exception:
        conn.rollback()
    seeded: set[str] = set()
    for r in conn.execute(
            "SELECT person_key, person_name, title, value, source, source_ref FROM contact_point"
            " WHERE crd=? AND kind='email' AND source IN ('pattern', 'ai_web')"
            " AND verify_status IN ('unverified', 'queued', 'unknown', 'risky')"
            " AND person_key != '' ORDER BY source = 'pattern', id", (crd,)).fetchall():
        key = r["person_key"]
        p = people.get(key) or next((x for x in people.values() if key in x.aliases), None)
        if p is None:
            p = add(_person(key, r["person_name"] or "",
                            emailguess.parse_full(r["person_name"] or ""), 3, r["title"]))
        if p is not None:
            label = "ai_web" if r["source"] == "ai_web" else pattern_of(r["source_ref"])
            p.seeds.append((r["value"], label))
            seeded.add(p.key)
    out = [p for p in people.values() if not only_seeded or p.key in seeded]
    return sorted(out, key=lambda p: (p.rank, p.name or "", p.key))


# ------------------------------------------------------------------ one firm, off the database

@dataclass
class Task:
    crd: str
    domain: str
    people: list                  # Person, to hunt now, in order
    everyone: list                # every Person at the firm, for name clashes
    pattern: str | None
    order: list
    attempts: dict                # address -> {status, tries, next_try_at, checked_at}
    reserved: dict                # address -> keys already holding it (published or confirmed)
    catch_all: bool | None
    engine: str
    auto: bool
    deadline: float
    stale_before: str             # a refusal older than this may be asked again
    more: bool = False            # people beyond this run's share are still waiting
    wake: str | None = None       # earliest time someone not due now becomes due
    sessions: int = SESSIONS_PER_FIRM


@dataclass
class Outcome:
    crd: str
    domain: str
    engine: str = ""
    firm: str = "active"          # active | accept_all | no_mail_server | blocked | unreachable
                                  # | waiting | dns | error
    detail: str = ""
    retry_at: str | None = None
    hosts: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    people: dict = field(default_factory=dict)    # key -> {state, next_try_at, detail, tried, found}
    pattern: str | None = None
    capped: bool = False
    sessions: int = 0
    rcpts: int = 0


class _Asker:
    """The questions for one firm, through whichever engine runs. Native
    questions share conversations (verify.MailSession); Reacher answers one
    address per call with its own made-up-address test already done."""

    def __init__(self, task: Task, hosts: list[str]):
        self.task, self.hosts = task, hosts
        self.sess: verify.MailSession | None = None
        self.sessions = 0
        self.rcpts = 0

    def ask(self, address: str, *, need: int = 1, same: bool = False) -> tuple[str, int | None, str]:
        """(kind, code, text). same=True insists on the current conversation:
        a made-up address only means something next to the real one."""
        t = self.task
        if not same and time.monotonic() >= t.deadline:
            return "late", None, "out of time"
        if t.engine == "reacher":
            r = verify.check_one(address, "reacher", t.auto)
            self.rcpts += 1
            self.sessions += 1
            smtp = r.get("smtp") or {}
            kind = {"valid": "valid", "invalid": "invalid", "catch_all": "accept_all",
                    "no_mail_server": "no_mx"}.get(r["status"])
            if kind is None:
                kind = "full" if smtp.get("full_inbox") else "temp"
            return kind, smtp.get("code"), r.get("reason") or ""
        if same:
            if self.sess is None:
                return "dropped", None, "the conversation ended"
        elif self.sess is None or self.sess.left < need:
            self.close()
            if self.sessions >= t.sessions or time.monotonic() >= t.deadline:
                return "capped", None, "this run's share for the firm is used"
            s = verify.MailSession(t.domain, self.hosts)
            self.sessions += 1
            if not s.open():
                f = s.failure or {}
                kind = "blocked" if f.get("connected") else "unreachable"
                return kind, f.get("code"), f.get("message") or f.get("stage") or ""
            self.sess = s
        kind, code, text = self.sess.ask(address)
        self.rcpts += 1
        if kind == "dropped":
            self.sess = None
        return kind, code, text

    def close(self) -> None:
        if self.sess is not None:
            self.sess.close()
            self.sess = None


def _tokens(q: Person) -> set[str]:
    """Every name an address for this person can be built from."""
    out = {q.first, q.last, q.middle, *q.others, *emailguess.NICK_FORMS.get(q.first, ())}
    return {t for t in out if t and len(t) > 1}


def _clash_index(everyone: list) -> dict[str, list]:
    by_token: dict[str, list] = {}
    for q in everyone:
        for t in _tokens(q):
            by_token.setdefault(t, []).append(q)
    return by_token


def _taken_by_others(p: Person, task: Task, index) -> set[str]:
    """Addresses that would fit someone else at the firm as well: two Johns
    both make john@, and a confirmed john@ belongs to neither of them for
    certain. Only colleagues who share some name with this person (first,
    middle, surname or nickname) can clash, so only they are expanded."""
    near: dict[str, Person] = {}
    for t in _tokens(p):
        for q in index.get(t, []):
            if q.key != p.key and q.key not in p.aliases and p.key not in q.aliases:
                near[q.key] = q
    out: set[str] = set()
    for q in near.values():
        out.update(a for a, _p, _v in emailguess.candidates(
            q.first, q.last, task.domain, middle=q.middle, other_lasts=q.others,
            order=task.order, limit=10_000))
        out.update(a for a, _p in q.seeds)
    mine = {p.key} | set(p.aliases)
    out.update(a for a, keys in task.reserved.items() if keys and not keys & mine)
    return out


def _plan(p: Person, task: Task, pattern: str | None) -> list[tuple[str, str, str]]:
    """The order to ask in: an address research found printed on a page, the
    firm's own pattern, an old job's guess, then the rest."""
    made = emailguess.candidates(p.first, p.last, task.domain, middle=p.middle,
                                 other_lasts=p.others, preferred=pattern, order=task.order)
    found = [(a, lab, "") for a, lab in p.seeds
             if lab == "ai_web" and a.endswith("@" + task.domain)]
    guesses = [(a, lab, "") for a, lab in p.seeds
               if lab != "ai_web" and a.endswith("@" + task.domain)]
    head = made[:1] if pattern else []
    out: dict[str, tuple[str, str, str]] = {}
    for c in found + head + guesses + made:
        out.setdefault(c[0], c)
    return list(out.values())


def _retry_at(tries: int, minutes: tuple = RETRY_MINUTES) -> str | None:
    if tries > len(minutes):
        return None
    return _later(minutes=minutes[tries - 1])


def _note(task: Task, out: Outcome, p: Person, cand: tuple, status: str,
          code: int | None, text: str, *, retry_minutes: int | None = None) -> dict:
    addr, pat, variant = cand
    prev = task.attempts.get(addr) or {}
    tries = int(prev.get("tries") or 0) + 1
    nxt = None
    if status == "retry":
        nxt = _later(minutes=retry_minutes) if retry_minutes else _retry_at(tries)
        if nxt is None or tries > len(RETRY_MINUTES):
            status, nxt = "unknown", None       # asked enough; no clear answer exists
    rec = {"status": status, "tries": tries, "next_try_at": nxt, "checked_at": _now()}
    task.attempts[addr] = rec
    out.checks.append({"person_key": p.key, "address": addr,
                       "pattern": pat + (f" ({variant})" if variant else ""),
                       "status": status, "tries": tries, "next_try_at": nxt,
                       "detail": (f"{code} " if code else "") + (text or "")[:200]})
    return rec


def _tried(p: Person, plan: list, task: Task) -> int:
    return sum(1 for a, _p, _v in plan if (task.attempts.get(a) or {}).get("status") in FINAL)


def _hunt_person(p: Person, task: Task, out: Outcome, asker: _Asker, pattern: str | None,
                 index) -> dict | None:
    """Walk one person's candidates. Returns their new state, or None when the
    firm stopped before anything conclusive happened for them."""
    plan = _plan(p, task, pattern)
    if not plan:
        return {"state": "unnamed", "next_try_at": _later(days=UNNAMED_DAYS),
                "detail": STATE_LABEL["unnamed"], "tried": 0}
    clash = _taken_by_others(p, task, index)
    now = _now()
    clashed = 0
    for cand in plan:
        addr = cand[0]
        if addr in clash:
            clashed += 1
            continue
        prev = task.attempts.get(addr) or {}
        st = prev.get("status")
        if st == "valid" and (prev.get("checked_at") or "") >= _ago(VALID_REUSE_DAYS):
            return _found(p, cand, task, plan, "confirmed earlier")
        if st in ("invalid", "unknown") and (prev.get("checked_at") or "") >= task.stale_before:
            continue
        if st == "catch_all" and (prev.get("checked_at") or "") >= task.stale_before:
            continue
        if st == "retry" and (prev.get("next_try_at") or "") > now:
            # Candidates are asked in order: a later one is not asked while an
            # earlier one waits out a "try again later".
            return {"state": "searching", "next_try_at": prev["next_try_at"],
                    "detail": "Waiting to ask the mail server again about a likely address",
                    "tried": _tried(p, plan, task)}
        kind, code, text = asker.ask(addr, need=2)
        if kind == "dropped":
            kind, code, text = asker.ask(addr, need=2)     # once more, in a new conversation
            if kind == "dropped":
                out.firm, out.retry_at = "waiting", _later(minutes=GREYLIST_MINUTES)
                out.detail = "The mail server kept hanging up"
                return None
        if kind == "ok":
            pk, pc, pt = asker.ask(verify.probe_address(task.domain), same=True)
            if pk in ("invalid", "disabled"):
                verify.remember(task.domain, asker.hosts, True, False)
                _note(task, out, p, cand, "valid", code, text)
                return _found(p, cand, task, plan, f"{code} {text}; made-up address: {pc} {pt}")
            if pk == "ok":
                verify.remember(task.domain, asker.hosts, True, True)
                _note(task, out, p, cand, "catch_all", code, text)
                out.firm = "accept_all"
                out.detail = "The mail server accepted a made-up address as well"
                return None
            _note(task, out, p, cand, "retry", code, text)
            out.firm, out.retry_at = "waiting", _later(minutes=GREYLIST_MINUTES)
            out.detail = "The mail server accepted an address but would not answer the follow-up test"
            return None
        if kind == "valid":
            _note(task, out, p, cand, "valid", code, text)
            return _found(p, cand, task, plan, text)
        if kind == "accept_all":
            _note(task, out, p, cand, "catch_all", code, text)
            out.firm, out.detail = "accept_all", text
            return None
        if kind in ("invalid", "disabled"):
            verify.remember(task.domain, asker.hosts, True, None)
            _note(task, out, p, cand, "invalid", code, text)
            continue
        if kind == "full":
            rec = _note(task, out, p, cand, "retry", code, text, retry_minutes=FULL_RETRY_MINUTES)
            return {"state": "searching", "next_try_at": rec["next_try_at"] or _later(days=1),
                    "detail": "A likely mailbox exists but is full; asking again later",
                    "tried": _tried(p, plan, task)}
        if kind == "temp":
            _note(task, out, p, cand, "retry", code, text)
            out.firm, out.retry_at = "waiting", _later(minutes=GREYLIST_MINUTES)
            out.detail = "The mail server asked us to try again later"
            return None
        if kind in ("policy", "blocked"):
            out.firm, out.detail = "blocked", f"{code or ''} {text}".strip()[:200]
            return None
        if kind == "unreachable":
            out.firm, out.detail = "unreachable", (text or "")[:200]
            return None
        if kind == "no_mx":
            out.firm, out.detail = "no_mail_server", text
            return None
        if kind == "capped":
            out.capped = True
            return None
        if kind == "late":
            return None
        _note(task, out, p, cand, "unknown", code, text)   # a reply nobody can read
    tried = _tried(p, plan, task)
    if not tried and clashed:
        return {"state": "exhausted", "next_try_at": _later(days=EXHAUSTED_DAYS), "tried": 0,
                "detail": "Every likely address would also fit a colleague with a similar "
                          "name, so none can be tied to this person"}
    refused = sum(1 for a, _p, _v in plan if (task.attempts.get(a) or {}).get("status") == "invalid")
    if not refused:
        return {"state": "unverifiable", "next_try_at": _later(days=EXHAUSTED_DAYS),
                "tried": tried, "detail": STATE_LABEL["unverifiable"]}
    return {"state": "exhausted", "next_try_at": _later(days=EXHAUSTED_DAYS), "tried": tried,
            "detail": f"Tried {tried} likely addresses at {task.domain}; the mail server "
                      f"turned every one away. Looking again after {EXHAUSTED_DAYS} days."}


def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _found(p: Person, cand: tuple, task: Task, plan: list, evidence: str) -> dict:
    addr, pat, variant = cand
    learned = pat if pat in emailguess.PATTERNS else None
    if learned is None:
        # A confirmed address from research still teaches the firm's pattern.
        learned = emailguess._detect(p.first, p.last, addr.split("@", 1)[0])
    return {"state": "found", "next_try_at": None, "tried": _tried(p, plan, task),
            "detail": f"Confirmed by the mail server for {task.domain}",
            "found": {"address": addr, "pattern": pat, "variant": variant,
                      "evidence": evidence[:300]},
            "pattern": learned}


def hunt_firm(task: Task) -> Outcome:
    """Everything network-side for one firm. Never touches the database, so it
    can run in a worker thread; never raises."""
    out = Outcome(task.crd, task.domain, engine=task.engine)
    asker = None
    try:
        hosts, state = verify.mail_hosts(task.domain)
        if state == "dns":
            out.firm, out.retry_at = "dns", _later(minutes=30)
            out.detail = f"Could not look up the mail servers for {task.domain}"
            return out
        if state in ("none", "null"):
            out.firm = "no_mail_server"
            out.detail = f"{task.domain} has no mail server"
            return out
        out.hosts = hosts
        if task.catch_all is True:
            out.firm = "accept_all"
            return out
        asker = _Asker(task, hosts)
        pattern = task.pattern
        index = _clash_index(task.everyone)
        for p in task.people:
            if out.firm != "active" or out.capped or time.monotonic() >= task.deadline:
                break
            res = _hunt_person(p, task, out, asker, pattern, index)
            if res is None:
                continue
            out.people[p.key] = res
            if res.get("pattern"):
                pattern = out.pattern = res["pattern"]
    except Exception as e:      # one firm must never sink the run
        out.firm, out.retry_at = "error", _later(hours=1)
        out.detail = f"{type(e).__name__}: {e}"[:200]
    finally:
        if asker is not None:
            asker.close()
            out.sessions, out.rcpts = asker.sessions, asker.rcpts
    return out


# ------------------------------------------------------------------ database side

def _upsert_person(conn, crd: str, p: Person, state: str, domain: str | None, detail: str,
                   next_try_at: str | None, tried: int = 0, found: str | None = None) -> None:
    conn.execute(
        "INSERT INTO email_hunt (crd, person_key, person_name, rank, state, domain, tried,"
        " found, detail, next_try_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT (crd, person_key) DO UPDATE SET person_name=excluded.person_name,"
        " rank=excluded.rank, state=excluded.state, domain=excluded.domain,"
        " tried=GREATEST(email_hunt.tried, excluded.tried), found=excluded.found,"
        " detail=excluded.detail, next_try_at=excluded.next_try_at,"
        " updated_at=excluded.updated_at",
        (crd, p.key, p.name, p.rank, state, domain, tried, found, detail, next_try_at, _now()))


def _bulk_people(conn, crd: str, people: list, state: str, domain: str | None,
                 detail: str, next_try_at: str | None) -> None:
    ts = _now()
    rows = [(crd, p.key, p.name, p.rank, state, domain, detail, next_try_at, ts) for p in people]
    for i in range(0, len(rows), 2000):
        conn.executemany(
            "INSERT INTO email_hunt (crd, person_key, person_name, rank, state, domain,"
            " detail, next_try_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (crd, person_key) DO UPDATE SET state=excluded.state,"
            " rank=excluded.rank, domain=excluded.domain, detail=excluded.detail,"
            " next_try_at=excluded.next_try_at, updated_at=excluded.updated_at",
            rows[i:i + 2000])


def _set_firm(conn, crd: str, domain: str | None, state: str, next_try_at: str | None,
              detail: str = "", pattern: str | None = None, found: int = 0,
              failed: bool = False) -> None:
    conn.execute(
        "INSERT INTO email_hunt_firm (crd, domain, state, pattern, fails, found, detail,"
        " next_try_at, checked_at) VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT (crd) DO UPDATE SET domain=excluded.domain, state=excluded.state,"
        " pattern=COALESCE(excluded.pattern, email_hunt_firm.pattern),"
        " fails=CASE WHEN ? THEN email_hunt_firm.fails + 1 ELSE 0 END,"
        " found=email_hunt_firm.found + excluded.found, detail=excluded.detail,"
        " next_try_at=excluded.next_try_at, checked_at=excluded.checked_at",
        (crd, domain, state, pattern, 1 if failed else 0, found, detail[:300], next_try_at,
         _now(), failed))


def _drop_guesses(conn, crd: str, *, values=None, person_key: str | None = None,
                  keep: str | None = None) -> int:
    """Remove unconfirmed guesses: all at a firm, some addresses, or one
    person's (keeping the confirmed one)."""
    sql = ("DELETE FROM contact_point WHERE crd=? AND kind='email' AND source='pattern'"
           " AND verify_status != 'valid'")
    args: list = [crd]
    if person_key is not None:
        sql += " AND person_key=?"
        args.append(person_key)
    if keep:
        sql += " AND value != ?"
        args.append(keep)
    if values is not None:
        values = list(values)
        if not values:
            return 0
        sql += f" AND value IN ({','.join('?' * len(values))})"
        args += values
    cur = conn.execute(sql, args)
    return max(cur.rowcount or 0, 0)


def note_check(conn, crd: str, person_key: str, address: str, pattern: str,
               res: dict) -> None:
    """Log a check made outside the hunter (scripts/verify_emails on a guessed
    row) as an attempt, so the hunter never repeats it."""
    status = {"valid": "valid", "invalid": "invalid", "catch_all": "catch_all"}.get(
        res.get("status"), "retry")
    smtp = res.get("smtp") or {}
    detail = (f"{smtp.get('code')} " if smtp.get("code") else "") + (res.get("reason") or "")
    nxt = _later(minutes=RETRY_MINUTES[0]) if status == "retry" else None
    conn.execute(
        "INSERT INTO email_attempt (crd, person_key, address, pattern, status, detail, tries,"
        " engine, next_try_at, checked_at) VALUES (?,?,?,?,?,?,1,?,?,?)"
        " ON CONFLICT (address) DO UPDATE SET status=excluded.status, detail=excluded.detail,"
        " tries=email_attempt.tries + 1, engine=excluded.engine,"
        " next_try_at=excluded.next_try_at, checked_at=excluded.checked_at",
        (crd, person_key or "", address, pattern, status, detail[:300], res.get("engine"),
         nxt, _now()))


def cleanup_guesses(conn) -> int:
    """Guesses an earlier job left that a check already turned down, or that sit
    on a domain with no mail server or one that accepts anything: logged as
    attempts where the server refused them, then removed. Unchecked guesses
    stay (hidden from every screen) until the hunter asks about them first."""
    ts = _now()
    conn.execute(
        "INSERT INTO email_attempt (crd, person_key, address, pattern, status, detail, tries,"
        " checked_at) SELECT DISTINCT ON (value) crd, person_key, value, source_ref,"
        " CASE verify_status WHEN 'catch_all' THEN 'catch_all' ELSE 'invalid' END,"
        " 'checked before the hunter ran', 1, COALESCE(verified_at, ?)"
        " FROM contact_point WHERE kind='email' AND source='pattern'"
        " AND verify_status IN ('invalid','catch_all') ORDER BY value"
        " ON CONFLICT (address) DO NOTHING", (ts,))
    cur = conn.execute("DELETE FROM contact_point WHERE kind='email' AND source='pattern'"
                       " AND verify_status IN ('invalid','no_mail_server','catch_all')")
    conn.commit()
    return max(cur.rowcount or 0, 0)


def queue_firm(conn, crd: str, *, reset: bool = True) -> int:
    """Hunt this firm next, for the firm page's "find emails" button. With
    reset, people given up on (exhausted, accept-all and so on) are looked at
    again now rather than after their wait."""
    init(conn)
    ts = _now()
    n = 0
    if reset:
        cur = conn.execute("UPDATE email_hunt SET next_try_at=? WHERE crd=? AND state != 'found'",
                           (ts, crd))
        n = max(cur.rowcount or 0, 0)
    conn.execute("INSERT INTO email_hunt_firm (crd, state, next_try_at, checked_at)"
                 " VALUES (?, 'active', ?, ?) ON CONFLICT (crd) DO UPDATE SET"
                 " next_try_at=excluded.next_try_at, fails=0", (crd, ts, ts))
    conn.commit()
    if reset:
        # A fresh look at the domain too: it may have stopped accepting anything.
        try:
            r = conn.execute("SELECT domain FROM email_hunt_firm WHERE crd=?", (crd,)).fetchone()
            if r and r["domain"]:
                conn.execute("UPDATE mail_domain SET checked_at=NULL WHERE domain=?",
                             (r["domain"],))
            conn.commit()
        except Exception:
            conn.rollback()
    return n


def queue_person(conn, crd: str, person_key: str) -> None:
    """Ask about this person's addresses on the next run (research found one
    to check). The caller commits."""
    init(conn)
    ts = _now()
    conn.execute("UPDATE email_hunt SET state='queued', next_try_at=?, updated_at=?"
                 " WHERE crd=? AND person_key=? AND state != 'found'", (ts, ts, crd, person_key))
    conn.execute("INSERT INTO email_hunt_firm (crd, state, next_try_at, checked_at)"
                 " VALUES (?, 'active', ?, ?) ON CONFLICT (crd) DO UPDATE SET"
                 " next_try_at=excluded.next_try_at", (crd, ts, ts))


def person_status(conn, crd: str) -> dict[str, dict]:
    """For the screens: person_key -> {state, label, detail, tried, found,
    next_try_at}. `tried` counts addresses a mail server gave a verdict on."""
    out: dict[str, dict] = {}
    try:
        tried = {r["person_key"]: r["n"] for r in conn.execute(
            "SELECT person_key, COUNT(*) n FROM email_attempt WHERE crd=?"
            " AND status IN ('valid','invalid','unknown','catch_all') GROUP BY person_key",
            (crd,)).fetchall()}
        for r in conn.execute("SELECT person_key, state, detail, found, next_try_at, tried"
                              " FROM email_hunt WHERE crd=?", (crd,)).fetchall():
            out[r["person_key"]] = {
                "state": r["state"], "label": STATE_LABEL.get(r["state"], r["state"]),
                "detail": r["detail"], "found": r["found"], "next_try_at": r["next_try_at"],
                "tried": max(int(r["tried"] or 0), int(tried.get(r["person_key"], 0)))}
    except Exception:
        conn.rollback()
    return out


def _attempts(conn, crd: str) -> dict[str, dict]:
    return {r["address"]: dict(r) for r in conn.execute(
        "SELECT address, status, tries, next_try_at, checked_at FROM email_attempt"
        " WHERE crd=?", (crd,)).fetchall()}


def build_task(conn, crd: str, *, firm_pat: dict, order: list, engine: str, auto: bool,
               deadline: float, budget: int, only_seeded: bool = False,
               force: bool = False, stats: Counter | None = None) -> Task | None:
    """Read what the hunt needs for one firm, settling on the spot anything
    that needs no mail server (nobody to look for, no domain, a known
    accept-all domain). Returns None when nothing is left to ask."""
    stats = stats if stats is not None else Counter()
    now = _now()
    everyone = firm_people(conn, crd, only_seeded=only_seeded)
    usable = {r["person_key"] for r in conn.execute(
        "SELECT DISTINCT person_key FROM usable_contact_point WHERE crd=? AND kind='email'"
        " AND person_key != ''", (crd,)).fetchall()}
    states = {r["person_key"]: dict(r) for r in conn.execute(
        "SELECT person_key, state, next_try_at FROM email_hunt WHERE crd=?", (crd,)).fetchall()}

    wake: list[str] = []

    def due(p: Person) -> bool:
        if p.key in usable or p.aliases & usable:
            return False
        st = states.get(p.key)
        if force or st is None or st["state"] in ("found", "queued"):
            return True
        if (st["next_try_at"] or "") <= now:
            return True
        wake.append(st["next_try_at"])
        return False

    need = [p for p in everyone if due(p)]
    domain = emailguess.domain_for(conn, crd)
    if not need:
        _set_firm(conn, crd, domain, "done", min(wake + [_later(days=FIRM_RECHECK_DAYS)]),
                  "Nobody here is waiting for an address")
        return None
    if not domain:
        _bulk_people(conn, crd, need, "no_domain", None, STATE_LABEL["no_domain"],
                     _later(days=NO_DOMAIN_DAYS))
        _set_firm(conn, crd, None, "no_domain", _later(days=NO_DOMAIN_DAYS),
                  STATE_LABEL["no_domain"])
        stats["firms_no_domain"] += 1
        return None
    if verify.is_free_provider(domain):
        _bulk_people(conn, crd, need, "free_mail", domain, STATE_LABEL["free_mail"],
                     _later(days=FREE_MAIL_DAYS))
        _set_firm(conn, crd, domain, "free_mail", _later(days=FREE_MAIL_DAYS),
                  STATE_LABEL["free_mail"])
        _drop_guesses(conn, crd)
        stats["firms_free_mail"] += 1
        return None
    # Guesses an earlier job made on some other domain are stale: the firm's
    # domain is this one now.
    stale = [a for p in need for a, _ in p.seeds if not a.endswith("@" + domain)]
    if stale:
        stats["stale_guesses_removed"] += _drop_guesses(conn, crd, values=stale)
        for p in need:
            p.seeds = [(a, pat) for a, pat in p.seeds if a.endswith("@" + domain)]
    verify.load_domains(conn, [domain])
    catch_all = verify.cached_catch_all(domain)
    if catch_all is True:
        settle_accept_all(conn, crd, domain, need, stats)
        return None
    reserved: dict[str, set] = {}
    for r in conn.execute(
            "SELECT value, person_key FROM contact_point WHERE crd=? AND kind='email'"
            " AND (source != 'pattern' OR verify_status = 'valid')", (crd,)).fetchall():
        reserved.setdefault(r["value"], set()).add(r["person_key"] or "")
    pattern = None
    fp = firm_pat.get(crd)
    if fp and fp[0] == domain:
        pattern = fp[1]
    if pattern is None:
        r = conn.execute("SELECT pattern FROM email_hunt_firm WHERE crd=? AND domain=?",
                         (crd, domain)).fetchone()
        pattern = r["pattern"] if r and r["pattern"] in emailguess.PATTERNS else None
    take = need[:max(0, min(PEOPLE_PER_FIRM_NOW if force else PEOPLE_PER_FIRM, budget))]
    return Task(crd=crd, domain=domain, people=take, everyone=everyone, pattern=pattern,
                order=order, attempts=_attempts(conn, crd), reserved=reserved,
                catch_all=catch_all, engine=engine, auto=auto, deadline=deadline,
                stale_before=_ago(EXHAUSTED_DAYS), more=len(need) > len(take),
                wake=min(wake) if wake else None,
                sessions=SESSIONS_PER_FIRM_NOW if force else SESSIONS_PER_FIRM)


def settle_accept_all(conn, crd: str, domain: str, need: list, stats: Counter) -> None:
    detail = (f"{domain} accepts mail for any address, so no guessed address can be "
              f"confirmed there. Published addresses still count.")
    _bulk_people(conn, crd, need, "accept_all_domain", domain, detail,
                 _later(days=ACCEPT_ALL_DAYS))
    _set_firm(conn, crd, domain, "accept_all", _later(days=ACCEPT_ALL_DAYS), detail)
    stats["guesses_removed"] += _drop_guesses(conn, crd)
    stats["firms_accept_all"] += 1


def _store_found(conn, crd: str, p: Person, domain: str, res: dict, engine: str) -> None:
    f = res["found"]
    label = f["pattern"] + (f" ({f['variant']})" if f["variant"] else "")
    key = p.key
    if f["pattern"] == "ai_web":
        # Research found it printed on a page; it keeps that provenance (and
        # its page in source_ref) and only gains the confirmation.
        r = conn.execute("SELECT person_key FROM contact_point WHERE crd=? AND kind='email'"
                         " AND value=? AND source='ai_web' ORDER BY id LIMIT 1",
                         (crd, f["address"])).fetchone()
        key = r["person_key"] if r else p.key
    else:
        contacts.upsert(conn, crd, "email", f["address"], "pattern", person_key=p.key,
                        person_name=p.name or None, title=p.title, source_ref=label,
                        confidence=95, is_role=False, verify_status="valid")
    row = conn.execute("SELECT id FROM contact_point WHERE crd=? AND kind='email' AND value=?"
                       " AND person_key=?", (crd, f["address"], key)).fetchone()
    if row:
        detail = {"email": f["address"], "status": "valid", "engine": engine,
                  "reason": f"The mail server for {domain} accepted this mailbox and "
                            f"refused a made-up address at the same domain.",
                  "hunt": {"pattern": label, "tried": res.get("tried", 0),
                           "evidence": f["evidence"]},
                  "checked_at": _now()}
        contacts.set_verification(conn, row["id"], "valid", verify.detail_json(detail))
    _drop_guesses(conn, crd, person_key=p.key, keep=f["address"])
    for alias in p.aliases:
        _drop_guesses(conn, crd, person_key=alias)


def apply_outcome(conn, task: Task, out: Outcome, stats: Counter) -> None:
    """Store one firm's results. The caller commits."""
    crd, domain = task.crd, task.domain
    ts = _now()
    for c in out.checks:
        conn.execute(
            "INSERT INTO email_attempt (crd, person_key, address, pattern, status, detail,"
            " tries, engine, next_try_at, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (address) DO UPDATE SET crd=excluded.crd,"
            " person_key=excluded.person_key, pattern=excluded.pattern,"
            " status=excluded.status, detail=excluded.detail, tries=excluded.tries,"
            " engine=excluded.engine, next_try_at=excluded.next_try_at,"
            " checked_at=excluded.checked_at",
            (crd, c["person_key"], c["address"], c["pattern"], c["status"], c["detail"],
             c["tries"], out.engine, c["next_try_at"], ts))
        stats[f"checked_{c['status']}"] += 1
    stats["addresses_checked"] += len(out.checks)
    stats["sessions"] += out.sessions
    # A refused or unreadable guess an earlier job stored goes now. An address
    # research found on a page stays, marked with what the server said.
    dead = [c for c in out.checks if c["status"] in ("invalid", "unknown", "catch_all")]
    stats["guesses_removed"] += _drop_guesses(conn, crd, values=[c["address"] for c in dead])
    for c in dead:
        conn.execute("UPDATE contact_point SET verify_status=?, verify_detail=?, verified_at=?,"
                     " confidence=CASE WHEN ?='invalid' THEN 0 ELSE confidence END"
                     " WHERE crd=? AND kind='email' AND source='ai_web' AND value=?",
                     (c["status"], c["detail"], ts, c["status"], crd, c["address"]))
    by_key = {p.key: p for p in task.people}
    found = 0
    for key, res in out.people.items():
        p = by_key[key]
        if res.get("found"):
            _store_found(conn, crd, p, domain, res, out.engine)
            found += 1
        _upsert_person(conn, crd, p, res["state"], domain, res["detail"], res["next_try_at"],
                       res.get("tried", 0), (res.get("found") or {}).get("address"))
        stats[f"people_{res['state']}"] += 1
    stats["found"] += found
    left = [p for p in task.people if p.key not in out.people]
    if out.firm == "accept_all":
        settle_accept_all(conn, crd, domain, left, stats)
        return
    if out.firm == "no_mail_server":
        detail = out.detail or STATE_LABEL["no_mail_server"]
        _bulk_people(conn, crd, left, "no_mail_server", domain, detail, _later(days=NO_MX_DAYS))
        _set_firm(conn, crd, domain, "no_mail_server", _later(days=NO_MX_DAYS), detail)
        stats["guesses_removed"] += _drop_guesses(conn, crd)
        stats["firms_no_mail_server"] += 1
        return
    if out.firm in ("blocked", "unreachable", "error"):
        r = conn.execute("SELECT fails FROM email_hunt_firm WHERE crd=?", (crd,)).fetchone()
        fails = int(r["fails"]) if r else 0
        hours = BLOCK_BACKOFF_HOURS[min(fails, len(BLOCK_BACKOFF_HOURS) - 1)]
        nxt = _later(hours=hours)
        if out.firm == "blocked":
            _bulk_people(conn, crd, left, "blocked", domain,
                         f"{STATE_LABEL['blocked']}; asking again later", nxt)
        _set_firm(conn, crd, domain, out.firm, nxt, out.detail, out.pattern, found, failed=True)
        stats[f"firms_{out.firm}"] += 1
        return
    if out.firm in ("waiting", "dns"):
        nxt = out.retry_at or _later(minutes=GREYLIST_MINUTES)
        _set_firm(conn, crd, domain, out.firm, nxt, out.detail, out.pattern, found)
        stats[f"firms_{out.firm}"] += 1
        return
    # Still active: back when the next waiting person is due, straight away
    # when the clock ran out, after a pause when the firm used its share.
    pending = [res["next_try_at"] for res in out.people.values()
               if res["state"] == "searching" and res.get("next_try_at")]
    pending += [task.wake] if task.wake else []
    if left and not out.capped:
        nxt = ts                                  # the clock ran out mid-firm
    elif out.capped or task.more:
        nxt = _later(minutes=YIELD_MINUTES)
    elif pending:
        nxt = min(pending)
    else:
        nxt = _later(days=FIRM_RECHECK_DAYS)
    _set_firm(conn, crd, domain, "active", nxt, "", out.pattern, found)


def due_firms(conn, limit: int, crd: str | None = None) -> list[tuple[str, bool]]:
    """(crd, only_seeded) in the order to hunt: in-scope firms by priority, then
    firms outside the scope that still hold unchecked guesses from the old job
    (only those people are hunted there)."""
    if crd:
        return [(crd, False)]
    now = _now()
    out = [(r["crd"], False) for r in conn.execute(
        "SELECT s.crd FROM firm_scope s"
        " LEFT JOIN email_hunt_firm h ON h.crd = s.crd"
        " LEFT JOIN firm_refresh_request r ON r.crd = s.crd"
        " WHERE h.crd IS NULL OR h.next_try_at IS NULL OR h.next_try_at <= ?"
        "    OR r.requested_at > h.checked_at"
        " ORDER BY s.priority DESC NULLS LAST, s.crd LIMIT ?", (now, limit)).fetchall()]
    if len(out) < limit:
        try:
            out += [(r["crd"], True) for r in conn.execute(
                "SELECT DISTINCT cp.crd FROM contact_point cp"
                " LEFT JOIN email_hunt_firm h ON h.crd = cp.crd"
                " WHERE cp.kind='email' AND cp.source='pattern' AND cp.verify_status != 'valid'"
                " AND (h.crd IS NULL OR h.next_try_at IS NULL OR h.next_try_at <= ?)"
                " AND NOT EXISTS (SELECT 1 FROM firm_scope s WHERE s.crd = cp.crd)"
                " LIMIT ?", (now, limit - len(out))).fetchall()]
        except Exception:
            conn.rollback()
    return out


def run(conn, *, limit: int = 300, seconds: float = 540, crd: str | None = None,
        engine: str | None = None, workers: int = WORKERS) -> Counter:
    """One bounded pass of the hunt. Returns aggregate counts only."""
    eng, auto = verify.resolve_engine(engine)
    if eng == "dns":
        st = verify.engine_status()
        raise NoEngine("No mailbox check can run here: " + "; ".join(
            x for x in (st.get("reacher_detail"), st.get("port25_detail")) if x))
    verify.init(conn)
    init(conn)
    stats: Counter = Counter()
    stats["engine:" + eng] += 1
    stats["guesses_removed"] += cleanup_guesses(conn)
    firm_pat, order = emailguess.observed_ranked(conn)
    conn.commit()
    started = time.monotonic()
    deadline = started + seconds
    budget = limit
    firms = due_firms(conn, max(limit, 50) * 4, crd)
    conn.commit()
    pending: dict = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:

        def settle(done) -> None:
            nonlocal budget
            for fut in done:
                task = pending.pop(fut)
                budget += len(task.people) - len(fut.result().people)
                try:
                    apply_outcome(conn, task, fut.result(), stats)
                    verify.save_domains(conn)
                    conn.commit()
                except Exception:
                    # Lost results are asked again next run; one firm's bad
                    # row must not stop the others being stored.
                    conn.rollback()
                    stats["store_errors"] += 1

        for firm, only_seeded in firms:
            if budget <= 0 or time.monotonic() >= deadline - 5:
                break
            try:
                task = build_task(conn, firm, firm_pat=firm_pat, order=order, engine=eng,
                                  auto=auto, deadline=deadline, budget=budget,
                                  only_seeded=only_seeded, force=bool(crd), stats=stats)
                conn.commit()
            except Exception:
                conn.rollback()
                stats["firm_errors"] += 1
                continue
            stats["firms"] += 1
            if task is None:
                continue
            # Charged now, refunded for anyone the firm never got to (a
            # refusal at the door, the clock), so blocked firms do not use up
            # the run's share of people.
            budget -= len(task.people)
            stats["people"] += len(task.people)
            pending[pool.submit(hunt_firm, task)] = task
            while len(pending) >= max(1, workers):
                done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                settle(done)
        while pending:
            done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            settle(done)
    stats["seconds"] = int(time.monotonic() - started)
    return stats


def summary(stats: Counter) -> str:
    """One line of numbers, no addresses or names."""
    eng = next((k.split(":", 1)[1] for k in stats if k.startswith("engine:")), "?")
    parts = [f"{stats.get('firms', 0)} firms", f"{stats.get('people', 0)} people hunted",
             f"{stats.get('addresses_checked', 0)} addresses asked",
             f"{stats.get('found', 0)} confirmed",
             f"{stats.get('checked_invalid', 0)} refused",
             f"{stats.get('checked_retry', 0)} to retry"]
    extra = [(k, v) for k, v in sorted(stats.items())
             if v and (k.startswith("firms_") or k in ("guesses_removed", "stale_guesses_removed",
                                                       "people_exhausted", "sessions",
                                                       "firm_errors", "store_errors"))]
    parts += [f"{k} {v}" for k, v in extra]
    return f"email hunt via {eng} in {stats.get('seconds', 0)}s: " + ", ".join(parts)

