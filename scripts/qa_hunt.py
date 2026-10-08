"""Regression checks for the email hunter and AI contact research, with fakes.

Nothing here touches a real mail server, a real website or a real AI
provider: a small SMTP server on localhost plays each firm's mail server, a
small HTTP server plays the web pages and the AI providers (OpenAI-compatible
and Anthropic's Messages API, server tools included).

What is proved:
  hunter     stops at the first confirmed address; a refused address moves on
             to the next pattern; a "try again later" is retried on a later
             run, never counted as a refusal; an accept-all domain stores
             nothing; no address is asked about twice; one conversation asks
             several questions; colleagues with the same name get no address.
  database   (needs BELLWETHER_DSN; runs in a throwaway schema) old guesses are
             asked first and removed when refused, confirmed ones are stored as
             pattern/valid, the screens' view shows no unconfirmed guess, and a
             second run asks nothing new.
  research   a claimed email that is not on its cited page is rejected while
             the real one passes, a firm's own line is not taken as a person's,
             LinkedIn is matched or probable by the page title; JSON from a
             weak model is repaired or retried; failures explain themselves;
             the Anthropic request carries the web tools and resumes a
             pause_turn by sending the turn back.

    python -m scripts.qa_hunt
"""

from __future__ import annotations

import gzip
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# A throwaway schema, chosen before anything opens the connection pool.
BASE_DSN = os.environ.get("BELLWETHER_DSN", "")
QA_SCHEMA = f"qa_hunt_{uuid.uuid4().hex[:8]}"
if BASE_DSN:
    sep = "&" if "?" in BASE_DSN else "?"
    os.environ["BELLWETHER_DSN"] = f"{BASE_DSN}{sep}options=-csearch_path%3D{QA_SCHEMA}"

from prospect import ai, contacts, emailguess, hunt, research, settings, verify  # noqa: E402


# ------------------------------------------------------------------ a fake mail server

class FakeSMTP:
    """Answers RCPT TO from a script: valid mailboxes, accept-all, greylisting
    (451 for the first N asks of an address) or a refused sender."""

    def __init__(self, valid=(), accept_all=False, greylist=None, block=False, spf=False):
        self.valid = {v.lower() for v in valid}
        self.accept_all = accept_all
        self.greylist = dict(greylist or {})
        self.block = block
        self.spf = spf             # refuses any named sender, takes the null one
        self.rcpts: list[str] = []
        self.connections = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(20)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._talk, args=(c,), daemon=True).start()

    def _talk(self, c):
        f = c.makefile("rb")

        def say(line):
            c.sendall((line + "\r\n").encode())
        say("220 fake.qa ESMTP")
        try:
            for raw in f:
                line = raw.decode().strip()
                cmd = line[:4].upper()
                if cmd == "EHLO":
                    c.sendall(b"250-fake.qa\r\n250 SIZE 10000\r\n")
                elif cmd == "HELO":
                    say("250 fake.qa")
                elif cmd == "MAIL":
                    null = line.replace(" ", "").upper().startswith("MAILFROM:<>")
                    if self.block or (self.spf and not null):
                        say("550 5.7.1 sender rejected: SPF does not permit this host")
                    else:
                        say("250 2.1.0 OK")
                elif cmd == "RCPT":
                    addr = line.split(":", 1)[1].strip().strip("<>").lower()
                    self.rcpts.append(addr)
                    if self.greylist.get(addr, 0) > 0:
                        self.greylist[addr] -= 1
                        say("451 4.7.1 greylisted, try again later")
                    elif self.accept_all or addr in self.valid:
                        say("250 2.1.5 OK")
                    else:
                        say("550 5.1.1 user unknown")
                elif cmd == "RSET":
                    say("250 2.0.0 OK")
                elif cmd == "QUIT":
                    say("221 bye")
                    break
                else:
                    say("502 5.5.2 not implemented")
        except OSError:
            pass
        finally:
            c.close()

    def asked(self, addr: str) -> int:
        return self.rcpts.count(addr.lower())


def serve_domain(domain: str, server: FakeSMTP) -> None:
    verify._MX_OVERRIDE[domain] = [f"127.0.0.1:{server.port}"]


def no_pacing():
    """Fakes answer instantly; the politeness waits would only slow the test."""
    return patch.multiple(verify, HOST_GAP_S=0, _per_minute=lambda: 0)


def person(key, first, last, middle="", rank=2, seeds=None):
    f, m, l, others = emailguess.split_name(first, middle, last)
    return hunt.Person(key=key, name=f"{first} {last}", first=f, middle=m, last=l,
                       others=others, rank=rank, seeds=list(seeds or []))


def task(domain, people, *, everyone=None, attempts=None, pattern=None, catch_all=None,
         reserved=None):
    return hunt.Task(crd="QA1", domain=domain, people=people, everyone=everyone or people,
                     pattern=pattern, order=emailguess.ranking(None),
                     attempts=attempts if attempts is not None else {},
                     reserved=reserved or {}, catch_all=catch_all, engine="native",
                     auto=False, deadline=time.monotonic() + 60,
                     stale_before=hunt._ago(hunt.EXHAUSTED_DAYS))


def carry(t: hunt.Task, out: hunt.Outcome) -> dict:
    """The attempts a later run would load from email_attempt."""
    att = dict(t.attempts)
    for c in out.checks:
        att[c["address"]] = {"status": c["status"], "tries": c["tries"],
                             "next_try_at": c["next_try_at"], "checked_at": hunt._now()}
    return att


class HunterChecks(unittest.TestCase):
    def setUp(self):
        self._pace = no_pacing()
        self._pace.start()
        self._cache = patch.object(verify, "_DB_CACHE", False)
        self._cache.start()
        verify._DOMAINS.clear()

    def tearDown(self):
        self._cache.stop()
        self._pace.stop()

    def test_stops_at_first_valid_after_refusals_in_one_conversation(self):
        srv = FakeSMTP(valid={"jsmith@stop-qa.test"})
        serve_domain("stop-qa.test", srv)
        p = person("i:1", "John", "Smith")
        out = hunt.hunt_firm(task("stop-qa.test", [p]))
        self.assertEqual(out.people["i:1"]["state"], "found")
        self.assertEqual(out.people["i:1"]["found"]["address"], "jsmith@stop-qa.test")
        # first.last refused, flast accepted, then one made-up address, nothing after
        self.assertEqual(srv.rcpts[:2], ["john.smith@stop-qa.test", "jsmith@stop-qa.test"])
        self.assertEqual(len(srv.rcpts), 3)
        self.assertTrue(srv.rcpts[2].startswith("bw"))
        self.assertEqual(srv.connections, 1)
        self.assertEqual([c["status"] for c in out.checks], ["invalid", "valid"])
        self.assertEqual(out.pattern, "flast")

    def test_learned_pattern_goes_first_for_the_next_person(self):
        srv = FakeSMTP(valid={"jsmith@learn-qa.test", "mjones@learn-qa.test"})
        serve_domain("learn-qa.test", srv)
        a, b = person("i:1", "John", "Smith"), person("i:2", "Mary", "Jones")
        out = hunt.hunt_firm(task("learn-qa.test", [a, b]))
        self.assertEqual(out.people["i:2"]["found"]["address"], "mjones@learn-qa.test")
        self.assertEqual(srv.asked("mary.jones@learn-qa.test"), 0)

    def test_temporary_failure_is_retried_later_not_counted(self):
        srv = FakeSMTP(valid={"anna.berg@grey-qa.test"},
                       greylist={"anna.berg@grey-qa.test": 1})
        serve_domain("grey-qa.test", srv)
        p = person("i:1", "Anna", "Berg")
        t1 = task("grey-qa.test", [p])
        out1 = hunt.hunt_firm(t1)
        self.assertEqual(out1.firm, "waiting")
        self.assertEqual(out1.checks[0]["status"], "retry")
        self.assertTrue(out1.checks[0]["next_try_at"])
        self.assertNotIn("i:1", out1.people)            # not exhausted, not refused
        self.assertEqual(srv.rcpts, ["anna.berg@grey-qa.test"])
        # Before the retry time a new run waits rather than skipping ahead.
        att = carry(t1, out1)
        out_early = hunt.hunt_firm(task("grey-qa.test", [p], attempts=att))
        self.assertEqual(out_early.people["i:1"]["state"], "searching")
        self.assertEqual(len(srv.rcpts), 1)
        # Once due, the same address is asked again and now passes.
        att["anna.berg@grey-qa.test"]["next_try_at"] = hunt._ago(1)
        out2 = hunt.hunt_firm(task("grey-qa.test", [p], attempts=att))
        self.assertEqual(out2.people["i:1"]["found"]["address"], "anna.berg@grey-qa.test")
        self.assertEqual(out2.checks[0]["tries"], 2)

    def test_accept_all_stores_nothing(self):
        srv = FakeSMTP(accept_all=True)
        serve_domain("all-qa.test", srv)
        out = hunt.hunt_firm(task("all-qa.test", [person("i:1", "Paul", "Green"),
                                                  person("i:2", "Rita", "Stone")]))
        self.assertEqual(out.firm, "accept_all")
        self.assertEqual(out.people, {})
        self.assertEqual([c["status"] for c in out.checks], ["catch_all"])
        self.assertEqual(len(srv.rcpts), 2)          # one guess, one made-up address
        self.assertIs(verify.cached_catch_all("all-qa.test"), True)
        # Known accept-all: no conversation at all next time.
        before = srv.connections
        out2 = hunt.hunt_firm(task("all-qa.test", [person("i:1", "Paul", "Green")],
                                   catch_all=True))
        self.assertEqual(out2.firm, "accept_all")
        self.assertEqual(srv.connections, before)

    def test_no_address_is_asked_twice_and_exhaustion(self):
        srv = FakeSMTP()
        serve_domain("none-qa.test", srv)
        p = person("i:1", "Tom", "Reed")
        t1 = task("none-qa.test", [p])
        out1 = hunt.hunt_firm(t1)
        asked = list(srv.rcpts)
        self.assertEqual(len(asked), len(set(asked)))
        self.assertEqual(out1.people["i:1"]["state"], "exhausted")
        self.assertGreaterEqual(out1.people["i:1"]["tried"], 12)
        out2 = hunt.hunt_firm(task("none-qa.test", [p], attempts=carry(t1, out1)))
        self.assertEqual(srv.rcpts, asked)             # nothing asked again
        self.assertEqual(out2.people["i:1"]["state"], "exhausted")

    def test_namesakes_get_no_address(self):
        srv = FakeSMTP(valid={"ann.lee@twin-qa.test"})
        serve_domain("twin-qa.test", srv)
        a, b = person("i:1", "Ann", "Lee"), person("i:2", "Ann", "Lee")
        out = hunt.hunt_firm(task("twin-qa.test", [a, b]))
        self.assertEqual(srv.rcpts, [])
        self.assertEqual(out.people["i:1"]["state"], "exhausted")
        self.assertIn("colleague", out.people["i:1"]["detail"])

    def test_refused_sender_blocks_the_firm_without_verdicts(self):
        srv = FakeSMTP(block=True)
        serve_domain("block-qa.test", srv)
        out = hunt.hunt_firm(task("block-qa.test", [person("i:1", "Lou", "Park")]))
        self.assertEqual(out.firm, "blocked")
        self.assertEqual(out.checks, [])

    def test_sender_refused_for_spf_falls_back_to_null_sender(self):
        srv = FakeSMTP(valid={"lou.park@spf-qa.test"}, spf=True)
        serve_domain("spf-qa.test", srv)
        out = hunt.hunt_firm(task("spf-qa.test", [person("i:1", "Lou", "Park")]))
        self.assertEqual(out.people["i:1"]["found"]["address"], "lou.park@spf-qa.test")

    def test_middle_name_never_alone(self):
        c = [a for a, _p, _v in emailguess.candidates("john", "gardner", "x.test",
                                                      middle="david")]
        self.assertNotIn("david@x.test", c)
        self.assertIn("david.gardner@x.test", c)
        self.assertIn("jdgardner@x.test", c)

    def test_candidate_order_and_variants(self):
        c = emailguess.candidates("william", "brown", "x.test", preferred="flast",
                                  order=emailguess.ranking(None))
        self.assertEqual(c[0][0], "wbrown@x.test")
        self.assertIn(("bill.brown@x.test", "first.last", "nickname bill"), c)
        self.assertEqual(len({a for a, _p, _v in c}), len(c))
        self.assertEqual(emailguess.parse_full("SMITH, JOHN, Q")[:3], ("john", "q", "smith"))
        self.assertEqual(emailguess.parse_full("Maria De La Cruz")[2], "delacruz")


# ------------------------------------------------------------------ fake web and AI

class Web:
    """Pages plus stub AI endpoints on one local HTTP server."""

    def __init__(self):
        self.requests: list[dict] = []
        self.anthropic_turns = 0
        self.pages: dict[str, str] = {}
        web = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path in web.pages:
                    self._send(200, web.pages[self.path], "text/html; charset=utf-8")
                else:
                    self._send(404, "not found", "text/plain")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                path = self.path.split("?", 1)[0]      # the beta endpoint adds ?beta=true
                web.requests.append({"path": path, "headers": {k.lower(): v for k, v in
                                                               self.headers.items()},
                                     "body": body})
                if path == "/err/chat/completions":
                    self._send(500, json.dumps({"error": {"message": "upstream model overloaded"}}))
                elif path == "/empty/chat/completions":
                    self._send(200, json.dumps({"choices": [{"message": {"content": "",
                               "reasoning_content": "thinking..."}, "finish_reason": "length"}],
                               "usage": {"completion_tokens": 200}}))
                elif path.endswith("/chat/completions"):
                    self._send(200, json.dumps(web.openai_reply(body)))
                elif path == "/v1/messages":
                    self._send(200, json.dumps(web.anthropic_reply(body)))
                else:
                    self._send(404, "{}")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def openai_reply(self, body):
        msgs = body.get("messages") or []
        last = msgs[-1]["content"] if msgs else ""
        kate = "Person: Kate Hill" in json.dumps(msgs)
        if kate and "not one valid JSON object" not in last:
            # A weak model's first answer: prose, no JSON at all.
            text = "Sure! Kate Hill is a senior advisor. Her details are on the team page."
        elif kate:
            text = json.dumps({"emails": [
                {"value": "kate@hillwealth.test", "source_url": f"{self.base}/team.html",
                 "quote": "kate@hillwealth.test"},
                {"value": "kate.secret@acmeqa.test", "source_url": f"{self.base}/team.html",
                 "quote": "kate.secret@acmeqa.test"}],
                "phones": [{"value": "(212) 734-8811", "label": "direct",
                            "source_url": f"{self.base}/team.html", "quote": "Direct: (212) 734-8811"},
                           {"value": "212-734-8800", "label": "direct",
                            "source_url": f"{self.base}/team.html", "quote": "(212) 734-8800"}],
                "linkedin": [{"value": "https://www.linkedin.com/in/kate-hill-qa",
                              "source_url": f"{self.base}/team.html", "quote": "LinkedIn"}],
                "title": [{"value": "Senior Advisor", "source_url": f"{self.base}/team.html",
                           "quote": "Senior Advisor"}]})
        else:
            text = '{"emails": [], "phones": [], "linkedin": [], "title": []}'
        return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20}}

    def anthropic_reply(self, body):
        self.anthropic_turns += 1
        base = {"id": f"msg_{self.anthropic_turns}", "type": "message", "role": "assistant",
                "model": body.get("model"), "stop_sequence": None,
                "usage": {"input_tokens": 120, "output_tokens": 30}}
        if self.anthropic_turns == 1:
            return dict(base, stop_reason="pause_turn", content=[
                {"type": "text", "text": "Searching for Jane Doe."},
                {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search",
                 "input": {"query": "Jane Doe Acmeqa Wealth"}},
                {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
                    {"type": "web_search_result", "url": "https://www.linkedin.com/in/jane-doe-qa",
                     "title": "Jane Doe - Partner - Acmeqa Wealth Partners | LinkedIn",
                     "encrypted_content": "x", "page_age": None},
                    {"type": "web_search_result", "url": f"{self.base}/team.html",
                     "title": "Our Team | Acmeqa Wealth Partners", "encrypted_content": "y",
                     "page_age": None}]}])
        answer = {"emails": [
            {"value": "jane.d@acmeqa.test", "source_url": f"{self.base}/team.html",
             "quote": "jane.d@acmeqa.test"},
            {"value": "jane.private@acmeqa.test", "source_url": f"{self.base}/team.html",
             "quote": "invented"}],
            "phones": [],
            "linkedin": [{"value": "https://www.linkedin.com/in/jane-doe-qa",
                          "source_url": "https://www.linkedin.com/in/jane-doe-qa", "quote": ""}],
            "title": []}
        return dict(base, stop_reason="end_turn", content=[
            {"type": "text", "text": "```json\n" + json.dumps(answer) + "\n```"}])


TEAM = """<html><head><title>Our Team | Acmeqa Wealth Partners</title></head><body>
<div class="card"><h3>Kate Hill</h3><p>Senior Advisor</p><p>Direct: (212) 734-8811</p>
<a href="mailto:kate@hillwealth.test">kate@hillwealth.test</a>
<a href="https://www.linkedin.com/in/kate-hill-qa">LinkedIn</a></div>
<div class="card"><h3>Jane Doe</h3><p>Partner</p><p>Email: jane.d@acmeqa.test</p>
<p>Main office (212) 734-8800</p></div>
</body></html>"""


def target(web: Web, name="Kate Hill", key="i:9007") -> research.Target:
    f, _m, l, _o = emailguess.parse_full(name)
    return research.Target(crd="900101", key=key, name=name, first=f, last=l, title=None,
                           firm="ACMEQA WEALTH PARTNERS LLC", city="New York", state="NY",
                           website=web.base, domain="acmeqa.test",
                           missing=["email", "phone", "linkedin"],
                           colleagues={"doe"} if l == "hill" else {"hill"})


class ResearchOfflineChecks(unittest.TestCase):
    """Claim checking against real fetched pages; no database needed."""

    @classmethod
    def setUpClass(cls):
        cls.web = Web()
        cls.web.pages["/team.html"] = TEAM

    def test_hallucinated_email_rejected_real_one_kept(self):
        t = target(self.web)
        data = self.web.openai_reply({"messages": [
            {"role": "user", "content": "Person: Kate Hill"},
            {"role": "user", "content": "That was not one valid JSON object"}]})
        claims = ai._parse_json(data["choices"][0]["message"]["content"],
                                research.RESULT_SCHEMA)
        reader = research.Reader()
        try:
            got = {(v.kind, v.value): v for v in research.check_claims(t, claims, [], reader)}
        finally:
            reader.close()
        self.assertTrue(got[("email", "kate@hillwealth.test")].ok)
        bad = got[("email", "kate.secret@acmeqa.test")]
        self.assertFalse(bad.ok)
        self.assertEqual(bad.why, "not on its page")
        self.assertTrue(got[("phone", "(212) 734-8811")].ok)
        self.assertFalse(got[("phone", "(212) 734-8800")].ok)     # beside Jane, not Kate
        li = got[("linkedin", "https://www.linkedin.com/in/kate-hill-qa")]
        self.assertTrue(li.ok)
        self.assertEqual(li.status, "probable")    # the page title names the firm, not Kate

    def test_page_that_cannot_be_read_proves_nothing(self):
        t = target(self.web)
        claims = {"emails": [{"value": "kate@hillwealth.test",
                              "source_url": f"{self.web.base}/missing.html", "quote": ""}]}
        reader = research.Reader()
        try:
            v = research.check_claims(t, claims, [], reader)[0]
        finally:
            reader.close()
        self.assertFalse(v.ok)
        self.assertEqual(v.why, "its page could not be read")

    def test_rule_pass_finds_published_details_without_a_model(self):
        page = research.Page(url=f"{self.web.base}/team.html", html=TEAM,
                             text=research._visible(TEAM), title="Our Team", via="cache")
        got = research._rule_claims(target(self.web), [page])
        self.assertEqual([e["value"] for e in got["emails"]], ["kate@hillwealth.test"])
        self.assertEqual([p["value"] for p in got["phones"]], ["(212) 734-8811"])
        self.assertIn("kate-hill-qa", got["linkedin"][0]["value"])

    def test_weak_model_json_is_repaired(self):
        sch = research.RESULT_SCHEMA
        self.assertEqual(ai._parse_json("<think>x</think>[]", sch)["emails"], [])
        out = ai._parse_json('Here:\n```json\n{"Emails": [{"value": "a@b.co", '
                             '"source_url": "u", "quote": "q"},],}\n```', sch)
        self.assertEqual(out["emails"][0]["value"], "a@b.co")
        self.assertEqual(out["phones"], [])


class _DB(unittest.TestCase):
    """Shared fixture: a throwaway schema in the local database."""

    conn = None

    @classmethod
    def setUpClass(cls):
        if not BASE_DSN:
            raise unittest.SkipTest("BELLWETHER_DSN is not set")
        import psycopg
        with psycopg.connect(BASE_DSN, autocommit=True) as c:
            c.execute(f"CREATE SCHEMA IF NOT EXISTS {QA_SCHEMA}")
        from prospect import db, jobs
        cls.conn = db.connect()
        c = cls.conn
        c.executescript("""
            CREATE TABLE firm_scope (crd TEXT PRIMARY KEY, priority REAL);
            CREATE TABLE firm_current (crd TEXT PRIMARY KEY, legal_name TEXT,
                business_name TEXT, city TEXT, state TEXT, website TEXT, phone TEXT);
            CREATE TABLE person (indvl_pk TEXT PRIMARY KEY, name TEXT, first_name TEXT,
                middle_name TEXT, last_name TEXT);
            CREATE TABLE person_employment (indvl_pk TEXT, org_pk TEXT, kind TEXT);
            CREATE TABLE schedule_a (crd TEXT, name TEXT, title TEXT, is_individual INTEGER);
            CREATE TABLE web_contact (id INTEGER PRIMARY KEY, crd TEXT, person TEXT,
                title TEXT, email TEXT, phone TEXT, source_url TEXT, found_at TEXT);
            CREATE TABLE firm_contact_info (id INTEGER PRIMARY KEY, crd TEXT, kind TEXT,
                value TEXT);
            CREATE TABLE web_page (url TEXT PRIMARY KEY, crd TEXT, fetched_at TEXT,
                status INTEGER, cache_path TEXT);
        """)
        c.commit()
        contacts.init(c)
        verify.init(c)
        jobs.init(c)
        ai.init(c)
        research.init(c)

    @classmethod
    def tearDownClass(cls):
        if cls.conn is not None:
            cls.conn.close()
            import psycopg
            with psycopg.connect(BASE_DSN, autocommit=True) as c:
                c.execute(f"DROP SCHEMA IF EXISTS {QA_SCHEMA} CASCADE")


class HuntDatabaseChecks(_DB):
    def test_full_run_on_a_firm(self):
        c = self.conn
        c.executescript("""
            INSERT INTO firm_scope VALUES ('900101', 99), ('900102', 98), ('900103', 97);
            INSERT INTO firm_current VALUES
              ('900101', 'ACMEQA WEALTH PARTNERS LLC', NULL, 'NEW YORK', 'NY', 'https://www.acmeqa.test', '2127348800'),
              ('900102', 'CATCHALL QA LLC', NULL, 'BOSTON', 'MA', 'https://catchall-qa.test', NULL),
              ('900103', 'COMCAST QA LLC', NULL, 'AUSTIN', 'TX', NULL, NULL);
            INSERT INTO person VALUES
              ('9001', 'John Q Smith', 'John', 'Q', 'Smith'), ('9002', 'Jane Doe', 'Jane', NULL, 'Doe'),
              ('9003', 'William Brown', 'William', NULL, 'Brown'), ('9005', 'Ann Lee', 'Ann', NULL, 'Lee'),
              ('9006', 'Ann Lee', 'Ann', NULL, 'Lee'), ('9008', 'Paul Green', 'Paul', NULL, 'Green'),
              ('9009', 'Rob Gray', 'Rob', NULL, 'Gray');
            INSERT INTO person_employment VALUES ('9001','900101','current'), ('9002','900101','current'),
              ('9003','900101','current'), ('9005','900101','current'), ('9006','900101','current'),
              ('9008','900102','current'), ('9009','900103','current');
            INSERT INTO schedule_a VALUES ('900101', 'SMITH, JOHN, Q', 'CEO', 1);
        """)
        contacts.upsert(c, "900101", "email", "info@acmeqa.test", "website")
        contacts.upsert(c, "900101", "email", "mary.major@acmeqa.test", "website",
                        person_key="n:mary major", person_name="Mary Major")
        # What the old job left: one guess a check already refused, one unchecked.
        contacts.upsert(c, "900101", "email", "john@acmeqa.test", "pattern",
                        person_key="i:9001", person_name="John Q Smith", source_ref="pattern:first")
        c.execute("UPDATE contact_point SET verify_status='invalid' WHERE value='john@acmeqa.test'")
        contacts.upsert(c, "900101", "email", "wbrown@acmeqa.test", "pattern",
                        person_key="i:9003", person_name="William Brown", source_ref="pattern:flast")
        contacts.upsert(c, "900102", "email", "pgreen@catchall-qa.test", "pattern",
                        person_key="i:9008", person_name="Paul Green", source_ref="flast")
        contacts.upsert(c, "900103", "email", "office@comcast.net", "brochure")
        c.commit()
        acme = FakeSMTP(valid={"john.smith@acmeqa.test", "jane.doe@acmeqa.test",
                               "bill.brown@acmeqa.test", "mary.major@acmeqa.test"})
        anyone = FakeSMTP(accept_all=True)
        serve_domain("acmeqa.test", acme)
        serve_domain("catchall-qa.test", anyone)
        with no_pacing(), patch.object(verify, "_DOMAINS", {}):
            stats = hunt.run(c, limit=50, seconds=60, engine="native", workers=2)
        print("\n  first run:", hunt.summary(stats))
        rows = {r["value"]: dict(r) for r in c.execute(
            "SELECT value, person_key, source, source_ref, verify_status FROM contact_point"
            " WHERE kind='email'").fetchall()}
        for addr in ("john.smith@acmeqa.test", "jane.doe@acmeqa.test", "bill.brown@acmeqa.test"):
            self.assertEqual((rows[addr]["source"], rows[addr]["verify_status"]),
                             ("pattern", "valid"), addr)
        self.assertEqual(rows["bill.brown@acmeqa.test"]["source_ref"],
                         "first.last (nickname bill)")
        self.assertEqual(rows["john.smith@acmeqa.test"]["person_key"], "i:9001")
        # The refused old guess is gone and logged; the unchecked one was asked
        # right after the firm's own pattern, refused, and removed.
        self.assertNotIn("john@acmeqa.test", rows)
        self.assertNotIn("wbrown@acmeqa.test", rows)
        self.assertEqual(acme.rcpts.index("wbrown@acmeqa.test"), acme.rcpts.index("william.brown@acmeqa.test") + 1)
        self.assertEqual(acme.asked("john@acmeqa.test"), 0)
        att = {r["address"]: r["status"] for r in c.execute(
            "SELECT address, status FROM email_attempt").fetchall()}
        self.assertEqual(att["john@acmeqa.test"], "invalid")
        self.assertEqual(att["wbrown@acmeqa.test"], "invalid")
        # Accept-all firm: nothing stored, the guess removed, the reason recorded.
        self.assertNotIn("pgreen@catchall-qa.test", rows)
        st = {(r["crd"], r["person_key"]): r["state"] for r in c.execute(
            "SELECT crd, person_key, state FROM email_hunt").fetchall()}
        self.assertEqual(st[("900102", "i:9008")], "accept_all_domain")
        self.assertEqual(st[("900103", "i:9009")], "free_mail")
        self.assertEqual(st[("900101", "i:9005")], "exhausted")
        self.assertEqual(st[("900101", "i:9001")], "found")
        # The screens: published addresses and confirmed guesses, nothing else.
        shown = {r["value"] for r in c.execute(
            "SELECT value FROM usable_contact_point WHERE kind='email'").fetchall()}
        self.assertEqual(shown, {"info@acmeqa.test", "mary.major@acmeqa.test",
                                 "john.smith@acmeqa.test", "jane.doe@acmeqa.test",
                                 "bill.brown@acmeqa.test", "office@comcast.net"})
        real = [a for a in acme.rcpts if not a.startswith("bw")]
        self.assertEqual(len(real), len(set(real)))                   # nothing asked twice
        # A second run, forced through every person, asks nothing new.
        asked = list(acme.rcpts)
        with no_pacing():
            hunt.run(c, limit=50, seconds=60, crd="900101", engine="native", workers=1)
        self.assertEqual(acme.rcpts, asked)
        p = hunt.person_status(c, "900101")
        self.assertGreaterEqual(p["i:9003"]["tried"], 2)


class ResearchDatabaseChecks(_DB):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.web = Web()
        cls.web.pages["/team.html"] = TEAM
        c = cls.conn
        c.executescript("""
            INSERT INTO firm_scope VALUES ('900101', 99);
            INSERT INTO firm_current VALUES ('900101', 'ACMEQA WEALTH PARTNERS LLC', NULL,
              'NEW YORK', 'NY', 'https://www.acmeqa.test', '2127348800');
            INSERT INTO person VALUES ('9007', 'Kate Hill', 'Kate', NULL, 'Hill'),
              ('9002', 'Jane Doe', 'Jane', NULL, 'Doe');
            INSERT INTO person_employment VALUES ('9007','900101','current'),
              ('9002','900101','current');
        """)
        contacts.upsert(c, "900101", "phone", "212-734-8800", "adv", label="main")
        contacts.upsert(c, "900101", "email", "info@acmeqa.test", "website")
        cls.cache = Path(tempfile.mkdtemp()) / "team.html.gz"
        cls.cache.write_bytes(gzip.compress(TEAM.encode()))
        c.execute("INSERT INTO web_page VALUES (?, '900101', '2026-10-01', 200, ?)",
                  (f"{cls.web.base}/team.html", str(cls.cache)))
        c.commit()
        cls.mail = FakeSMTP(valid={"kate@hillwealth.test", "jane.d@acmeqa.test"})
        serve_domain("hillwealth.test", cls.mail)
        serve_domain("acmeqa.test", cls.mail)

    def _settings(self, values):
        values = dict({"ai.features": "research", "ai.daily_limit": "400"}, **values)
        return patch.object(settings, "_load", return_value=values)

    def test_openai_compatible_research_with_a_weak_model(self):
        c = self.conn
        # Search stays offline: one result, pointing at the local team page.
        found = [{"url": f"{self.web.base}/team.html", "title": "Our Team", "snippet": ""}]
        with self._settings({"ai.provider": "openai", "ai.base_url": f"{self.web.base}/v1",
                             "ai.model_smart": "weak-qa-model"}), no_pacing(), \
                patch.object(verify, "_auto_pick", return_value="native"), \
                patch.object(research.Reader, "search", return_value=found) as searched:
            stats = research.run(c, limit=10, seconds=120, crd="900101", force=True)
        self.assertTrue(searched.called)
        print("\n  openai research:", research.summary(stats))
        rows = {(r["kind"], r["value"]): dict(r) for r in c.execute(
            "SELECT kind, value, source, source_ref, verify_status, label FROM contact_point"
            " WHERE person_key='i:9007'").fetchall()}
        self.assertEqual(rows[("email", "kate@hillwealth.test")]["source"], "ai_web")
        self.assertEqual(rows[("email", "kate@hillwealth.test")]["verify_status"], "valid")
        self.assertNotIn(("email", "kate.secret@acmeqa.test"), rows)
        self.assertEqual(rows[("phone", "(212) 734-8811")]["label"], "direct")
        self.assertNotIn(("phone", "(212) 734-8800"), rows)
        self.assertEqual(rows[("linkedin", "https://www.linkedin.com/in/kate-hill-qa")]
                         ["verify_status"], "probable")
        log = c.execute("SELECT status, rejected FROM ai_research WHERE person_key='i:9007'"
                        ).fetchone()
        self.assertEqual(log["status"], "ok")
        self.assertGreaterEqual(log["rejected"], 2)
        # The weak model's prose answer was logged as a failure that says why,
        # and the stricter retry produced the JSON.
        errs = [r["error"] for r in c.execute("SELECT error FROM ai_call WHERE ok=0").fetchall()]
        self.assertTrue(any("answer was not JSON" in (e or "") for e in errs), errs)
        shown = {r["value"] for r in c.execute(
            "SELECT value FROM usable_contact_point WHERE person_key='i:9007'").fetchall()}
        self.assertIn("kate@hillwealth.test", shown)
        # Not researched again inside 60 days.
        with self._settings({"ai.provider": "openai", "ai.base_url": f"{self.web.base}/v1",
                             "ai.model_smart": "weak-qa-model"}):
            self.assertEqual(research.targets(c, 10, crd="900101"), [])

    def test_anthropic_request_shape_and_pause_turn(self):
        c = self.conn
        self.web.requests.clear()
        self.web.anthropic_turns = 0
        ai._CLIENTS.clear()
        env = patch.dict(os.environ, {"ANTHROPIC_BASE_URL": self.web.base})
        with env, self._settings({"ai.provider": "anthropic", "ai.api_key": "sk-qa"}), \
                no_pacing(), patch.object(verify, "_auto_pick", return_value="native"):
            t = research._target(c, "900101", "i:9002", "Jane Doe", None,
                                 research._firm(c, "900101"), ["email", "linkedin"])
            self.assertEqual(t.colleagues, {"hill"})
            reader = research.Reader()
            try:
                res = research.research_person(c, t, reader)
            finally:
                reader.close()
        ai._CLIENTS.clear()
        msgs = [r for r in self.web.requests if r["path"] == "/v1/messages"]
        self.assertEqual(len(msgs), 2)
        first, second = msgs[0]["body"], msgs[1]["body"]
        self.assertEqual(first["model"], "claude-opus-5-5")
        self.assertEqual([t["type"] for t in first["tools"]],
                         ["web_search_20260209", "web_fetch_20260209"])
        self.assertEqual(first["tools"][0]["name"], "web_search")
        self.assertEqual(first["output_config"], {"effort": "medium"})
        self.assertEqual(first["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01",
                      msgs[0]["headers"].get("anthropic-beta", ""))
        # pause_turn: the same user message and the paused assistant turn, no
        # extra "continue" message.
        self.assertEqual([m["role"] for m in second["messages"]], ["user", "assistant"])
        self.assertEqual(second["messages"][0], first["messages"][0])
        kinds = [b["type"] for b in second["messages"][1]["content"]]
        self.assertIn("server_tool_use", kinds)
        self.assertIn("web_search_tool_result", kinds)
        rows = {(r["kind"], r["value"]): dict(r) for r in c.execute(
            "SELECT kind, value, verify_status FROM contact_point WHERE person_key='i:9002'"
        ).fetchall()}
        self.assertEqual(rows[("email", "jane.d@acmeqa.test")]["verify_status"], "valid")
        self.assertNotIn(("email", "jane.private@acmeqa.test"), rows)   # invented, not on page
        self.assertEqual(rows[("linkedin", "https://www.linkedin.com/in/jane-doe-qa")]
                         ["verify_status"], "matched")
        self.assertEqual(res["rejected"], 1)

    def test_failures_explain_themselves(self):
        c = self.conn
        with self._settings({"ai.provider": "openai", "ai.base_url": f"{self.web.base}/err",
                             "ai.model_smart": "qa-model"}):
            ok, msg = ai.test_connection()
        self.assertFalse(ok)
        for part in ("HTTP 500", "upstream model overloaded", "qa-model"):
            self.assertIn(part, msg)
        err = c.execute("SELECT error FROM ai_call WHERE ok=0 ORDER BY id DESC LIMIT 1"
                        ).fetchone()["error"]
        self.assertIn("HTTP 500", err)
        with self._settings({"ai.provider": "openai", "ai.base_url": f"{self.web.base}/empty",
                             "ai.model_smart": "qa-model"}):
            ok, msg = ai.test_connection()
        self.assertFalse(ok)
        self.assertIn("finish_reason length", msg)
        self.assertIn("reasoning_content", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
