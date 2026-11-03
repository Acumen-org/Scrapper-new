"""Offline checks of the push to Cadence, against the fake Cadence.

Everything runs on an in-memory SQLite copy of the tables and the fake
Cadence of scripts/fake_cadence.py on localhost, on a fake clock, so the
5 s, 30 s and 2 min retries and the per-minute pace are proved in no time.
No real Cadence, database, key or network is touched. scripts.qa_requirements
imports these classes, so the Docker build runs them.

What is proved: the payload (every mapped field, the 14.5 normalisation, the
14.4 limits, no guesses, no shared inboxes, the main-line fallback, a probable
LinkedIn kept to the notes); list:ATS and TOLLBOOTH never sent; each push
result and its state; the check before a push (already in the CRM by email
and by phone blocks the contact, and it is not sent); each error code of
14.9; Idempotency-Key replay and conflict; the retry schedule; changedSince
paging; an opt-out becoming a suppression shown as "Opted out (Cadence)";
the pace under 30 push and 60 status requests a minute; the key never
written anywhere; and a server with no Cadence settings explaining itself.

    python -m scripts.qa_cadence
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from prospect import cadence, contacts
from scripts.fake_cadence import FakeCadence

ROOT = Path(__file__).resolve().parent.parent
KEY = "qa-test-key-" + "x" * 40          # a stand-in; no real key exists anywhere in this repository
ANA = {"login": "ana@acumen-strategy.com", "email": "ana@acumen-strategy.com", "name": "Ana Ruiz",
       "role": "user"}
LEE = {"login": "lee", "email": "lee@acumen-strategy.com", "name": "Lee Park", "role": "owner"}
UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class Clock:
    """Time that moves only when someone sleeps."""

    def __init__(self):
        self.t = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc).timestamp()
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(round(s, 2))
        self.t += max(0.0, s)


def tables(c) -> None:
    """The tables a push reads, as small SQLite copies, plus cadence's own."""
    c.executescript(re.search(r'CREATE TABLE IF NOT EXISTS contact_point \(.*?\n\);',
                              contacts.SCHEMA, re.S).group(0))
    view = re.search(r'CREATE OR REPLACE VIEW usable_contact_point AS(.*?);', contacts.SCHEMA, re.S).group(1)
    c.executescript(f"CREATE VIEW usable_contact_point AS {view};")
    c.executescript("""
        CREATE TABLE people_index (crd TEXT, person_key TEXT, indvl_pk TEXT, src TEXT, name TEXT,
            first_name TEXT, last_name TEXT, title TEXT, desig TEXT, designations TEXT,
            has_disclosure INTEGER, iapd_link TEXT, bcity TEXT, bstate TEXT, email TEXT,
            email_status TEXT, phone TEXT, phone_label TEXT, linkedin TEXT, firm_name TEXT,
            state TEXT, firm_city TEXT, raum INTEGER, products TEXT, cfp INTEGER, cfa INTEGER,
            is_officer INTEGER, start_date TEXT, prior_firm TEXT, priority REAL);
        CREATE TABLE firm_current (crd TEXT PRIMARY KEY, legal_name TEXT, business_name TEXT,
            website TEXT, city TEXT, state TEXT, country TEXT, phone TEXT, raum INTEGER,
            total_employees INTEGER);
        CREATE TABLE firm_status (crd TEXT PRIMARY KEY, status TEXT, owner TEXT, updated_at TEXT);
        CREATE TABLE firm_class (crd TEXT PRIMARY KEY, category TEXT);
    """)
    c.executescript(cadence.SCHEMA)


class CadenceBase(unittest.TestCase):
    def setUp(self):
        if sqlite3.sqlite_version_info < (3, 35):
            self.skipTest("the claim upsert needs SQLite 3.35 (RETURNING)")
        self.clock = Clock()
        self.fake = FakeCadence(KEY, clock=self.clock.time).start()
        self.fake.add_user(ANA["email"], ANA["name"], "senior_fo")
        self.fake.add_user(LEE["email"], LEE["name"], "biz_ops")
        self.c = sqlite3.connect(":memory:", check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        tables(self.c)
        self.firm("105943", "NORTHSTAR WEALTH LLC", website="www.NorthStarWealth.com",
                  phone="(212) 555-0100", raum=1_200_000_000, employees=40)
        env = patch.dict(os.environ, {"CADENCE_API_URL": self.fake.url, "CADENCE_API_KEY": KEY})
        self.patches = [env,
                        patch.object(cadence, "_clock", self.clock.time),
                        patch.object(cadence, "_sleep", self.clock.sleep),
                        patch.object(cadence, "switched_on", lambda: True),
                        patch.object(cadence, "max_per_push", lambda: 200),
                        patch.object(cadence, "daily_per_user", lambda: 500),
                        patch.object(cadence, "_name_of", lambda login: login or "someone"),
                        patch.object(cadence, "owner_choices", lambda: [
                            (ANA["email"], ANA["name"]), (LEE["email"], LEE["name"])])]
        for p in self.patches:
            p.start()
        cadence._PREVIEWS.clear()
        self.ids = 0

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.fake.stop()
        self.c.close()

    # ---------------------------------------------------------- seeding
    def firm(self, crd, legal, website=None, phone=None, raum=None, employees=None, country="United States",
             status=None, category=None):
        self.c.execute("INSERT OR REPLACE INTO firm_current VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (crd, legal, None, website, "NEW YORK", "NY", country, phone, raum, employees))
        if status:
            self.c.execute("INSERT OR REPLACE INTO firm_status VALUES (?,?,?,?)", (crd, status, None, "x"))
        if category:
            self.c.execute("INSERT OR REPLACE INTO firm_class VALUES (?,?)", (crd, category))
        self.c.commit()

    def person(self, name, crd="105943", key=None, email=None, verify="valid", esource="website",
               phone=None, label="direct", linkedin=None, li_status="matched", title="Partner",
               indvl=None, desig=None, role_email=None, guess=None, disclosure=0):
        self.ids += 1
        first, last = name.split()[0], name.split()[-1]
        key = key or (f"i:{indvl}" if indvl else contacts.name_key(name))
        self.c.execute("INSERT INTO people_index (crd, person_key, indvl_pk, src, name, first_name,"
                       " last_name, title, desig, designations, has_disclosure, iapd_link, bcity, bstate)"
                       " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (crd, key, indvl, "sec" if indvl else "web", name, first, last, title,
                        json.dumps(desig or []), "CFP, CFA" if indvl else None, disclosure,
                        f"https://adviserinfo.sec.gov/individual/summary/{indvl}" if indvl else None,
                        "NEW YORK", "NY"))
        rows = []
        if email:
            rows.append(("email", email, None, 0, esource, verify))
        if role_email:
            rows.append(("email", role_email, None, 1, "website", "valid"))
        if guess:
            rows.append(("email", guess, None, 0, "pattern", "unverified"))
        if phone:
            rows.append(("phone", phone, label, 0, "website", "unverified"))
        if linkedin:
            rows.append(("linkedin", linkedin, None, 0, "web_search", li_status))
        for kind, value, lab, role, src, vs in rows:
            self.c.execute("INSERT INTO contact_point (crd, person_key, person_name, title, kind, value,"
                           " label, is_role, source, sources, confidence, verify_status, found_at,"
                           " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (crd, key, name, title, kind, value, lab, role, src, src, 80, vs,
                            "2026-10-01T10:00:00+00:00", "2026-10-01T10:00:00+00:00"))
        self.c.commit()
        return cadence.contact_id(crd, key)

    def push(self, ids, account=ANA, **kw):
        kw.setdefault("background", False)
        return cadence.push(self.c, ids, account=account, **kw)

    def state(self, cid):
        crd, key = cadence.parse_id(cid)
        r = self.c.execute("SELECT * FROM cadence_push WHERE crd=? AND person_key=?", (crd, key)).fetchone()
        return dict(r) if r else None

    def pushed(self, kind="push") -> list[dict]:
        return [json.loads(r["body"]) for r in self.fake.requests_of(kind)]


class PayloadChecks(CadenceBase):
    def test_every_mapped_field_normalised_and_within_limits(self):
        cid = self.person("Jane Doe", indvl="0007712001", email="  Jane.Doe+Work@NorthStarWealth.com ",
                          phone="(212) 555-0142 x 12", linkedin="https://uk.linkedin.com/in/JaneDoe-1/?trk=x",
                          title="Partner " * 40)
        rec = cadence.load_records(self.c, [cid])[cid]
        c = cadence.build_contact(rec, product="phh_1031", note="n" * 900,
                                  base_url="https://bellwether.example.test")
        self.assertEqual(c["bellwetherContactId"], "105943:i:0007712001")
        self.assertEqual((c["firstName"], c["lastName"]), ("Jane", "Doe"))
        self.assertEqual(c["email"], "jane.doe+work@northstarwealth.com")
        self.assertEqual(c["emailVerification"], "valid")
        self.assertEqual((c["phone"], c["phoneExtension"], c["phoneType"]), ("+12125550142", "12", "direct"))
        self.assertEqual(c["linkedinUrl"], "linkedin.com/in/janedoe-1")
        self.assertEqual(c["individualCrd"], "7712001")
        self.assertEqual(c["country"], "US")
        self.assertEqual(c["designations"], ["CFP", "CFA"])
        self.assertEqual(c["productInterest"], ["PHH"])
        self.assertIn("list:PHH 1031", c["tags"])
        self.assertLessEqual(len(c["jobTitle"]), 200)
        self.assertTrue(c["notes"].startswith("n" * 500 + "\n\n"))
        self.assertNotIn("n" * 501, c["notes"])
        self.assertIn("SEC record: https://adviserinfo.sec.gov/", c["notes"])
        self.assertEqual(c["bellwetherUrl"], "https://bellwether.example.test/firm/105943#p-i-0007712001")
        self.assertEqual(c["source"], "SEC IAPD; email from the firm website, mailbox confirmed")
        self.assertEqual(c["collectedAt"], "2026-10-01")
        self.assertIs(c["doNotContact"], False)
        f = c["firm"]
        self.assertEqual((f["name"], f["crd"], f["domain"], f["phone"], f["aumUsd"], f["employees"]),
                         ("Northstar Wealth LLC", "105943", "northstarwealth.com", "+12125550100",
                          1_200_000_000, 40))
        self.assertEqual(f["website"], "https://www.NorthStarWealth.com")
        for k, n in (("tags", 20), ("additionalEmails", 5), ("additionalPhones", 5), ("designations", 20)):
            self.assertLessEqual(len(c[k]), n)
        self.assertTrue(all(len(t) <= 50 for t in c["tags"]))

    def test_no_guess_no_shared_inbox_main_line_fallback_probable_linkedin_in_notes(self):
        cid = self.person("Sam Lee", guess="sam.lee@northstarwealth.com", role_email="info@northstarwealth.com",
                          linkedin="linkedin.com/in/samlee", li_status="probable")
        rec = cadence.load_records(self.c, [cid])[cid]
        c = cadence.build_contact(rec)
        self.assertIsNone(c["email"])
        self.assertNotIn("emailVerification", c)
        self.assertEqual((c["phone"], c["phoneType"]), ("+12125550100", "main"))
        self.assertIsNone(c["linkedinUrl"])
        self.assertIn("Possible LinkedIn, not confirmed: https://www.linkedin.com/in/samlee", c["notes"])
        self.assertIsNone(c["bellwetherUrl"])
        self.assertEqual(c["productInterest"], [])

    def test_email_verification_mapping(self):
        for status, source, want in (("valid", "website", "valid"), ("catch_all", "website", "risky"),
                                     ("unverified", "brochure", "unknown"), ("risky", "website", "unknown")):
            cid = self.person(f"Pat Q{status.replace('_', '')}", email=f"p{status}@northstarwealth.com",
                              verify=status, esource=source)
            rec = cadence.load_records(self.c, [cid])[cid]
            self.assertEqual(cadence.build_contact(rec)["emailVerification"], want, status)

    def test_normalisation_rules(self):
        self.assertEqual(cadence.norm_linkedin("HTTPS://www.LinkedIn.com/in/Jane-Doe/details/"), "linkedin.com/in/jane-doe")
        self.assertIsNone(cadence.norm_linkedin("https://www.linkedin.com/company/acme"))
        self.assertEqual(cadence.norm_crd("000105943"), "105943")
        self.assertEqual(cadence.norm_domain("https://www.Acme.com/about?x=1"), "acme.com")
        self.assertEqual(cadence.norm_phone("+44 20 8366 1177", "GB")[0], "+442083661177")
        self.assertEqual(cadence.norm_phone("212.555.0142 ext. 7")[1], "7")
        self.assertIsNone(cadence.norm_email("not an email"))


class AtsAndTollboothChecks(CadenceBase):
    def test_ats_push_carries_no_product_and_no_ats_tag(self):
        cid = self.person("Ava Stone", email="ava@northstarwealth.com", desig=["ats:head_ta"])
        res = self.push([cid], product="ats")
        self.assertTrue(res["ok"], res)
        sent = self.pushed()[0]["contacts"][0]
        self.assertEqual(sent["productInterest"], [])
        self.assertFalse([t for t in sent["tags"] if t.lower().startswith("list:")])
        for r in self.fake.log:
            self.assertNotIn(b"list:ATS", r["body"])
            self.assertNotIn(b"TOLLBOOTH", r["body"])

    def test_scrub_strips_them_whoever_asks(self):
        env = cadence.envelope(ANA, None, [{"bellwetherContactId": "1:i:2", "productInterest": ["TOLLBOOTH", "PHH"],
                                            "tags": ["list:ATS", "List:ats ", "list:PHH JV"]}])
        c = env["contacts"][0]
        self.assertEqual(c["productInterest"], ["PHH"])
        self.assertEqual(c["tags"], ["list:PHH JV"])
        self.assertIsNone(cadence.push_product("ats"))


class PushResultChecks(CadenceBase):
    def test_each_result_maps_to_its_state(self):
        made = self.person("Ann Made", email="ann@northstarwealth.com")
        dup = self.person("Dee Dup", email="dee@northstarwealth.com")
        self.fake.sandbox["999:n:other"] = {"cadenceContactId": "c-1", "email": "dee@northstarwealth.com",
                                            "linkedinUrl": None, "phone": None, "jobTitle": None,
                                            "individualCrd": None, "direct": set(),
                                            "ownerEmail": "leigh@acumen-strategy.com", "ownerName": "Leigh"}
        crm = self.person("Cal Crm", email="cal@northstarwealth.com")
        res = self.push([made, dup], preview_id=None)
        self.assertEqual(self.state(made)["state"], "in_sandbox")
        self.assertEqual(self.state(made)["cadence_owner"], ANA["email"])
        self.assertEqual(self.state(dup)["state"], "in_sandbox")
        self.assertEqual(self.state(dup)["cadence_owner_name"], "Leigh")
        lines = {r["id"]: r["line"] for r in res["results"]}
        self.assertEqual(lines[made], "Added to sandbox (owner: Ana Ruiz)")
        self.assertEqual(lines[dup], "Already in sandbox (owner: Leigh)")
        # The CRM changes between the preview and the push: the push's own
        # answer is recorded the same way.
        pv = cadence.preview(self.c, [crm], account=ANA)
        self.assertEqual([x["id"] for x in pv["send"]], [crm])
        self.fake.add_crm(emails=["cal@northstarwealth.com"], owner="andrew@acumen-strategy.com")
        res = self.push([crm], preview_id=pv["preview_id"])
        st = self.state(crm)
        self.assertEqual(st["state"], "in_crm")
        self.assertTrue(st["crm_url"].startswith("https://"))
        self.assertIn("Already in the CRM", res["results"][0]["line"])
        self.assertIn("matched on email", res["results"][0]["line"])

    def test_each_rejection_and_suppressed_writes_a_suppression(self):
        reasons = ("invalid_field", "do_not_contact", "suppressed", "blocked_account", "internal_contact",
                   "duplicate_in_request")
        ids = []
        for i, reason in enumerate(reasons):
            cid = self.person(f"Rex R{chr(97 + i)}", email=f"rex{i}@northstarwealth.com")
            self.fake.forced[cid] = {"result": "rejected", "reason": reason,
                                     "detail": "lastName is empty" if reason == "invalid_field" else None}
            ids.append(cid)
        self.push(ids)
        for cid, reason in zip(ids, reasons):
            st = self.state(cid)
            self.assertEqual(st["state"], "rejected", reason)
            self.assertTrue(st["reason"])
        self.assertIn("lastName is empty", self.state(ids[0])["reason"])
        sup = self.c.execute("SELECT * FROM contact_suppression WHERE scope='person' AND value=?",
                             (ids[2],)).fetchone()
        self.assertEqual((sup["source"], sup["channel"]), ("cadence", "all"))
        # Refused and failed may be pushed again, as a new request with a new key.
        del self.fake.forced[ids[0]]
        self.push([ids[0]])
        keys = [r["idem"] for r in self.fake.requests_of("push")]
        self.assertEqual(len(set(keys)), 2)
        self.assertEqual(self.state(ids[0])["state"], "in_sandbox")

    def test_warnings_reach_the_result_line(self):
        cid = self.person("Wes Warn", email="wes@northstarwealth.com")
        self.fake.forced[cid] = {"result": "created", "cadenceContactId": "c-9",
                                 "warnings": [{"code": "phone_dropped", "field": "phone", "crmPersonId": None,
                                               "detail": "x"}]}
        res = self.push([cid])
        self.assertEqual(res["results"][0]["warnings"], ["Cadence dropped the phone"])


class CheckBeforePushChecks(CadenceBase):
    def test_already_in_crm_by_email_and_by_phone_is_blocked_and_not_sent(self):
        by_mail = self.person("Ema Mail", email="ema@northstarwealth.com")
        by_phone = self.person("Pia Phone", email="pia@northstarwealth.com", phone="212-555-0177", label="direct")
        by_office = self.person("Otto Office", email="otto@northstarwealth.com", phone="212-555-0188", label="office")
        main_only = self.person("Mia Main", email="mia@northstarwealth.com")
        fine = self.person("Fin Fine", email="fin@northstarwealth.com")
        self.fake.add_crm(emails=["ema@northstarwealth.com"], owner="andrew@acumen-strategy.com")
        self.fake.add_crm(phones=["+12125550177"], owner="andrew@acumen-strategy.com")
        self.fake.add_crm(phones=["+12125550188"], owner="andrew@acumen-strategy.com")
        # A CRM person reached on the firm's main line: never a match (14.6 rule 4).
        self.fake.add_crm(phones=["+12125550100"], owner="andrew@acumen-strategy.com")
        pv = cadence.preview(self.c, [by_mail, by_phone, by_office, main_only, fine], account=ANA)
        skipped = {x["id"]: x for x in pv["skipped"]}
        self.assertEqual(skipped[by_mail]["why"], "Already in the CRM · matched on email")
        self.assertEqual(skipped[by_phone]["why"], "Already in the CRM · matched on phone")
        self.assertEqual(skipped[by_office]["why"], "Already in the CRM · matched on phone")
        self.assertTrue(skipped[by_mail]["crm_url"].startswith("https://"))
        self.assertEqual({x["id"] for x in pv["send"]}, {main_only, fine})
        self.assertEqual(len(self.fake.requests_of("check")), 1)
        self.assertFalse(self.fake.sandbox, "the check wrote nothing")
        self.push([by_mail, by_phone, by_office, main_only, fine], preview_id=pv["preview_id"])
        sent = {c["bellwetherContactId"] for b in self.pushed() for c in b["contacts"]}
        self.assertEqual(sent, {main_only, fine})
        self.assertIsNone(self.state(by_mail))
        ev = self.c.execute("SELECT outcome FROM cadence_push_event WHERE action='skipped'").fetchall()
        self.assertEqual(sorted(r["outcome"] for r in ev), ["skipped:already_in_crm"] * 3)

    def test_push_without_a_fresh_preview_checks_first(self):
        cid = self.person("Nia New", email="nia@northstarwealth.com")
        self.fake.add_crm(emails=["nia@northstarwealth.com"])
        res = self.push([cid], preview_id="not-a-preview")
        kinds = [r["kind"] for r in self.fake.log]
        self.assertEqual(kinds, ["check"])
        self.assertEqual(res["results"][0]["state"], "skipped")

    def test_check_that_does_not_answer_lets_the_push_go_and_401_disables_it(self):
        cid = self.person("Una Ans", email="una@northstarwealth.com")
        self.fake.then("check", status=503, code="unavailable")
        pv = cadence.preview(self.c, [cid], account=ANA)
        self.assertEqual(pv["check"]["state"], "unavailable")
        self.assertTrue(pv["can_send"])
        self.assertIn("still refuses anyone already in the CRM", pv["check"]["message"])
        self.fake.then("check", status=401, code="unauthorized")
        pv = cadence.preview(self.c, [cid], account=ANA)
        self.assertFalse(pv["can_send"])
        self.assertIn("refused Bellwether's key", pv["problem"])
        self.assertIsNone(cadence.held(self.c), "a preview writes nothing")

    def test_block_rules(self):
        self.firm("200001", "CUSTOMER CO", status="customer")
        self.firm("200002", "DISQ CO", status="disqualified")
        cust = self.person("Cus Tom", crd="200001", email="c@customer.example")
        disq = self.person("Dis Q", crd="200002", email="d@disq.example")
        none = self.person("No Way", crd="200001")
        self.c.execute("UPDATE firm_current SET phone=NULL WHERE crd='200001'")
        dnc = self.person("Dan Nc", email="dan@northstarwealth.com")
        cadence.suppress(self.c, "email", "dan@northstarwealth.com", channel="email", reason="do_not_email")
        moved = self.person("Mo Ved", crd="200002", indvl="555", email="mo@disq.example")
        again = self.person("Mo Ved", crd="105943", indvl="555", email="mo2@northstarwealth.com")
        self.c.commit()
        self.push([moved], confirm_disqualified=True)
        pv = cadence.preview(self.c, [cust, disq, none, dnc, again, cust], account=ANA)
        why = {x["id"]: x["why"] for x in pv["skipped"]}
        self.assertEqual(why[cust], "Firm marked Customer")
        self.assertEqual(why[none], "Firm marked Customer")
        self.assertEqual(why[dnc], "Do not contact")
        self.assertTrue(why[again].startswith("Already pushed from Disq Co"))
        self.assertEqual(pv["disqualified"], ["Disq Co"])
        self.assertEqual(pv["count"], 5)
        res = self.push([disq])
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("needs_confirm"))
        self.assertTrue(self.push([disq], confirm_disqualified=True)["ok"])

    def test_caps_and_everyone_can_push(self):
        ids = [self.person(f"Cap P{chr(97 + i)}", email=f"cap{i}@northstarwealth.com") for i in range(3)]
        with patch.object(cadence, "max_per_push", lambda: 2):
            self.assertFalse(self.push(ids)["ok"])
        with patch.object(cadence, "daily_per_user", lambda: 2):
            res = self.push(ids)
            self.assertFalse(res["ok"])
            self.assertIn("2 more today", res["message"])
        plain = dict(ANA, role="user")
        self.assertTrue(self.push(ids[:1], account=plain)["ok"])
        from prospect import webapp
        self.assertTrue("/settings/cadence".startswith(webapp.ADMIN_PREFIXES))
        self.assertFalse("/api/cadence/push".startswith(webapp.ADMIN_PREFIXES))
        no_mail = {"login": "legacy", "name": "Legacy"}
        self.assertIn("needs an email address", self.push(ids[1:2], account=no_mail)["message"])


class ErrorChecks(CadenceBase):
    def one(self, name="Err Or"):
        n = self.ids + 1
        tag = chr(97 + n % 26) + chr(97 + n // 26 % 26)
        return self.person(f"{name.split()[0]} Q{tag}", email=f"p{n}@northstarwealth.com")

    def req(self):
        return dict(self.c.execute("SELECT * FROM cadence_request ORDER BY first_sent_at DESC, rowid DESC").fetchone())

    def test_401_and_integration_disabled_hold_the_queue_until_ping_answers(self):
        for status, code in ((401, "unauthorized"), (503, "integration_disabled")):
            with self.subTest(code=code):
                cid = self.one("Hol Ding")
                self.fake.then("push", status=status, code=code)
                self.push([cid], preview_id=None)
                r = self.req()
                self.assertEqual((r["state"], r["retries"]), ("retry_wait", 0))
                self.assertEqual(cadence.held(self.c), code)
                self.assertIn("cadence", cadence.alerts(self.c))
                self.assertEqual(cadence.drain(self.c), 0, "nothing is sent while held")
                out = cadence.sync(self.c)
                self.assertIsNone(cadence.held(self.c))
                self.assertEqual(out["sent"], 1)
                self.assertEqual(self.state(cid)["state"], "in_sandbox")

    def test_in_progress_waits_and_resends_without_using_a_retry(self):
        cid = self.one()
        self.fake.then("push", status=409, code="idempotency_in_progress", retry_after=5, times=2)
        self.push([cid])
        self.assertEqual(self.state(cid)["state"], "in_sandbox")
        self.assertEqual(self.req()["retries"], 0)
        keys = {r["idem"] for r in self.fake.requests_of("push")}
        self.assertEqual(len(keys), 1)
        self.assertEqual(self.clock.sleeps.count(5.0), 2)

    def test_contract_errors_fail_with_an_alert(self):
        for status, code in ((409, "idempotency_conflict"), (400, "invalid_json"), (413, "payload_too_large"),
                             (415, "unsupported_media_type"), (422, "invalid_request")):
            with self.subTest(code=code):
                cid = self.one("Fai Led")
                self.fake.then("push", status=status, code=code)
                self.push([cid])
                self.assertEqual(self.state(cid)["state"], "failed")
                r = self.req()
                self.assertEqual(r["state"], "failed")
                self.assertIsNone(r["body"])
                self.assertEqual(r["last_error"], f"http_{status} {code}")
                self.assertEqual(cadence.alerts(self.c)["cadence"]["level"], "bad")

    def test_retryable_answers_back_off_5_30_120_then_fail(self):
        for kind in ({"status": 503, "code": "unavailable"}, {"status": 502, "code": "bad_gateway"},
                     {"action": "drop"}, {"action": "timeout"}):
            with self.subTest(**kind):
                self.clock.sleeps.clear()
                pushes_before = len(self.fake.requests_of("push"))
                cid = self.one("Ret Ry")
                self.fake.then("push", times=4, **kind)
                with patch.object(cadence, "TIMEOUT_S", 0.4):
                    self.fake.slow_s = 0.8
                    self.push([cid])
                    self.assertEqual(self.state(cid)["state"], "retry_wait")
                    cadence.drain(self.c)
                r = self.req()
                self.assertEqual((r["state"], r["retries"]), ("failed", 3))
                self.assertEqual(self.state(cid)["state"], "failed")
                crd, key = cadence.parse_id(cid)
                waits = [json.loads(e["detail"])["after_s"] for e in self.c.execute(
                    "SELECT detail FROM cadence_push_event WHERE action='retry' AND crd=? AND person_key=?"
                    " ORDER BY id", (crd, key))]
                self.assertEqual(waits, [5.0, 30.0, 120.0])
                self.assertEqual(len(self.fake.requests_of("push")) - pushes_before, 4)
                self.fake.script.clear()

    def test_429_honours_retry_after_for_every_sender(self):
        a, b = self.one("Rat Ea"), self.one("Rat Eb")
        self.assertNotEqual(a, b)
        self.fake.then("push", status=429, code="rate_limited", retry_after=40)
        self.push([a])
        r = self.req()
        due = datetime.fromisoformat(r["next_retry_at"]) - datetime.fromtimestamp(self.clock.t, timezone.utc)
        self.assertGreaterEqual(due.total_seconds(), 39)
        self.assertTrue(cadence._state_get(self.c, "backoff_until"))
        cadence.drain(self.c)
        self.assertEqual(self.state(a)["state"], "in_sandbox")
        self.assertIn(40.0, [round(s) for s in self.clock.sleeps])
        self.push([b])
        self.assertEqual(self.state(b)["state"], "in_sandbox")

    def test_500_retries_once_then_fails(self):
        cid = self.one()
        self.fake.then("push", status=500, code="internal", times=2)
        self.push([cid])
        cadence.drain(self.c)
        r = self.req()
        self.assertEqual((r["state"], r["retries"]), ("failed", 1))
        self.assertEqual(len(self.fake.requests_of("push")), 2)


class IdempotencyChecks(CadenceBase):
    def test_a_lost_answer_is_retried_with_the_same_key_and_bytes_and_replayed(self):
        cid = self.person("Los T", email="lost@northstarwealth.com")
        self.fake.then("push", action="lost")
        self.push([cid])
        self.assertEqual(self.state(cid)["state"], "retry_wait")
        cadence.drain(self.c)
        reqs = self.fake.requests_of("push")
        self.assertEqual(len(reqs), 2)
        self.assertEqual(reqs[0]["idem"], reqs[1]["idem"])
        self.assertEqual(reqs[0]["body"], reqs[1]["body"])
        self.assertRegex(reqs[0]["idem"], UUID4)
        self.assertEqual(len(self.fake.sandbox), 1, "one sandbox contact, however many sends")
        self.assertEqual(self.state(cid)["state"], "in_sandbox")
        ev = self.c.execute("SELECT detail FROM cadence_push_event WHERE action='result'").fetchone()
        self.assertTrue(json.loads(ev["detail"])["replayed"])

    def test_same_key_with_another_body_is_a_conflict(self):
        key = str(uuid.uuid4())
        env = cadence.envelope(ANA, None, [{"bellwetherContactId": "105943:n:a b", "firstName": "A",
                                            "lastName": "B", "firm": {"name": "F"}}])
        first = cadence._call(self.c, "POST", "/contacts", cadence.body_bytes(env), idem_key=key)
        again = cadence._call(self.c, "POST", "/contacts", cadence.body_bytes(env), idem_key=key)
        env["contacts"][0]["lastName"] = "C"
        other = cadence._call(self.c, "POST", "/contacts", cadence.body_bytes(env), idem_key=key)
        self.assertEqual((first.status, first.data["replayed"]), (200, False))
        self.assertEqual((again.status, again.data["replayed"]), (200, True))
        self.assertEqual((other.status, other.code), (409, "idempotency_conflict"))

    def test_a_fresh_uuid4_per_chunk_and_none_past_seven_days(self):
        ids = [self.person(f"Chu N{chr(97 + i)}", email=f"chunk{i}@northstarwealth.com") for i in range(3)]
        with patch.object(cadence, "_chunk_size", lambda c: 2):
            res = self.push(ids)
            self.assertTrue(res["pending"])
            cadence.drain(self.c)
        keys = [r["idem"] for r in self.fake.requests_of("push")]
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2)
        self.assertTrue(all(UUID4.match(k) for k in keys))
        self.assertTrue(all(self.state(i)["state"] == "in_sandbox" for i in ids))
        late = self.person("Lat E", email="late@northstarwealth.com")
        self.fake.then("push", status=503, code="unavailable")
        self.push([late])
        old = (datetime.fromtimestamp(self.clock.t, timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
        self.c.execute("UPDATE cadence_request SET first_sent_at=? WHERE state='retry_wait'", (old,))
        self.c.commit()
        before = len(self.fake.requests_of("push"))
        cadence.sync(self.c)
        self.assertEqual(len(self.fake.requests_of("push")), before, "an expired key is never resent")
        self.assertEqual(self.state(late)["state"], "failed")


class StatusChecks(CadenceBase):
    def test_changed_since_pages_to_the_end_and_maps_each_status(self):
        ids = [self.person(f"Sta T{chr(97 + i)}", email=f"st{i}@northstarwealth.com") for i in range(5)]
        self.push(ids)
        self.clock.sleep(60)
        self.fake.set_status(ids[0], "moved_to_crm", crmPersonId="p1", movedToCrmAt="2026-11-04T10:00:00Z",
                             crmUrl="https://twenty.example.test/object/person/p1", ownerName="Andrew")
        self.fake.set_status(ids[1], "already_in_crm", crmPersonId="p2",
                             crmUrl="https://twenty.example.test/object/person/p2")
        self.fake.set_status(ids[2], "removed")
        self.fake.page_size = 2
        out = cadence.sync(self.c)
        self.assertGreaterEqual(out["pages"], 3)
        self.assertEqual(self.state(ids[0])["state"], "moved_to_crm")
        self.assertEqual(self.state(ids[0])["moved_at"], "2026-11-04T10:00:00Z")
        self.assertEqual(self.state(ids[1])["state"], "in_crm")
        self.assertEqual(self.state(ids[2])["state"], "removed")
        self.assertEqual(self.state(ids[3])["state"], "in_sandbox")
        since = cadence._state_get(self.c, "status_since")
        first_page = json.loads(self.fake.requests_of("status")[0]["body"])
        self.assertIn("changedSince", first_page)
        self.assertTrue(since.endswith("Z"))
        # A page that fails keeps the old starting point.
        self.clock.sleep(600)
        self.fake.set_status(ids[3], "removed")
        self.fake.set_status(ids[4], "removed")
        self.fake.page_size = 1
        self.fake.then("status", status=503, code="unavailable")
        out = cadence.sync(self.c)
        self.assertEqual(cadence._state_get(self.c, "status_since"), since)
        self.assertTrue(out.get("error"))
        cadence.sync(self.c)
        self.assertNotEqual(cadence._state_get(self.c, "status_since"), since)
        self.assertEqual(self.state(ids[4])["state"], "removed")
        # A removed contact may be pushed again by anyone (decision 16).
        self.assertTrue(self.push([ids[2]], account=dict(ANA, role="user"))["ok"])

    def test_not_found_by_id_leaves_a_push_in_flight_alone(self):
        cid = self.person("Nof Ound", email="nof@northstarwealth.com")
        self.fake.then("push", status=503, code="unavailable")
        self.push([cid])
        cadence.apply_status(self.c, {"bellwetherContactId": cid, "status": "not_found"}, by_id=True)
        self.assertEqual(self.state(cid)["state"], "retry_wait")


class OptOutChecks(CadenceBase):
    def test_opted_out_in_cadence_becomes_a_suppression_shown_as_opted_out(self):
        from prospect import cadence_view, people_view
        a = self.person("Opt Out", email="opt@northstarwealth.com")
        b = self.person("Opt Byid", email="byid@northstarwealth.com")
        self.push([a, b])
        self.clock.sleep(30)
        self.fake.set_status(a, "opted_out")
        cadence.sync(self.c)
        self.fake.set_status(b, "opted_out")
        self.clock.sleep(120)
        cadence.refresh(self.c, [b], "ana@acumen-strategy.com")
        for cid, email in ((a, "opt@northstarwealth.com"), (b, "byid@northstarwealth.com")):
            rows = self.c.execute("SELECT * FROM contact_suppression WHERE lifted_at IS NULL"
                                  " AND source='cadence' AND channel='all' AND (value=? OR value=?)",
                                  (cid, email)).fetchall()
            self.assertEqual(sorted(r["scope"] for r in rows), ["email", "person"])
            self.assertEqual(self.state(cid)["state"], "opted_out")
        info = cadence.status_map(self.c, [cadence.parse_id(a)])[a]
        self.assertEqual(info["text"], "Opted out (Cadence)")
        self.assertFalse(info["can_push"])
        self.assertIn("Opted out (Cadence)", cadence_view.status_html(info))
        card = cadence_view.card_controls(a, "Opt Out", info)
        self.assertIn("Opted out (Cadence)", card)
        self.assertNotIn('data-cad-push="', card)
        row = dict(self.c.execute("SELECT * FROM people_index WHERE person_key=?",
                                  (cadence.parse_id(a)[1],)).fetchone())
        row.update(firm_name="Northstar", firm_city="NEW YORK", raum=1, products="", cfp=0, cfa=0,
                   is_officer=0, start_date="2020-01-01", prior_firm=None, email=None, phone=None,
                   phone_label=None, linkedin=None, email_status=None)
        html = people_view._person_row(row, [], (), {a: info})
        self.assertIn("Opted out (Cadence)", html)
        self.assertNotIn('data-cad-id="', html)
        pv = cadence.preview(self.c, [a], account=ANA)
        self.assertEqual(pv["skipped"][0]["why"], "Opted out (Cadence)")


class PaceChecks(CadenceBase):
    def test_under_30_push_and_60_status_requests_a_minute(self):
        for _ in range(45):
            cadence._call(self.c, "GET", "/ping", kind="push")
        for _ in range(90):
            cadence._call(self.c, "GET", "/ping", kind="status")
        times = [r["t"] for r in self.fake.log]
        self.assertEqual(len(times), 135)
        pushes = [r for r in self.fake.log[:45]]
        statuses = [r for r in self.fake.log[45:]]

        def most(rows):
            ts = sorted(r["t"] for r in rows)
            return max(sum(1 for u in ts if t <= u < t + 60) for t in ts)
        self.assertLessEqual(most(pushes), 30)
        self.assertLessEqual(most(statuses), 60)


class SecretAndSetupChecks(CadenceBase):
    def test_the_key_is_never_written(self):
        cid = self.person("Kee P", email="keep@northstarwealth.com")
        for code in ("unavailable", "internal"):
            self.fake.then("push", status=503 if code == "unavailable" else 500, code=code)
        self.fake.then("push", status=401, code="unauthorized")
        self.push([cid])
        cadence.drain(self.c)
        cadence.sync(self.c)
        dump = []
        for t in cadence.TABLES:
            dump += [json.dumps(dict(r), default=str) for r in self.c.execute(f"SELECT * FROM {t}")]
        dump.append(json.dumps(cadence.diagnostics(self.c), default=str))
        dump.append(json.dumps(cadence.alerts(self.c), default=str))
        self.assertFalse([d for d in dump if KEY in d])
        self.assertNotIn(KEY, cadence.Answer("http", 401, {"error": {"code": "unauthorized"}}).error())
        self.assertNotIn(KEY, cadence.ping(self.c)[2])

    def test_without_cadence_settings_it_explains_and_sends_nothing(self):
        cid = self.person("Unc Onf", email="unconf@northstarwealth.com")
        with patch.dict(os.environ, {"CADENCE_API_URL": "", "CADENCE_API_KEY": ""}):
            why = cadence.problem()
            self.assertIn("Cadence is not connected", why)
            self.assertIn("CADENCE_API_URL and CADENCE_API_KEY", why)
            pv = cadence.preview(self.c, [cid], account=ANA)
            self.assertEqual(pv["problem"], why)
            self.assertFalse(pv["can_send"])
            self.assertEqual([x["id"] for x in pv["send"]], [cid])
            self.assertFalse(self.push([cid])["ok"])
            self.assertFalse(cadence.refresh(self.c, [cid], "x")["ok"])
            self.assertIn("skipped", cadence.sync(self.c))
            self.assertEqual(cadence.alerts(self.c), {})
            self.assertEqual(cadence.status_map(self.c, [cadence.parse_id(cid)])[cid]["can_push"], True)
        with patch.dict(os.environ, {"CADENCE_API_URL": "http://cadence.example.test", "CADENCE_API_KEY": KEY}):
            self.assertIn("https", cadence.problem())
        with patch.object(cadence, "switched_on", lambda: False):
            self.assertIn("switched off", cadence.problem())
        self.assertEqual(self.fake.log, [])

    def test_job_lane_and_requirement(self):
        from prospect import jobs
        job = jobs.BY_KIND["cadence_sync"]
        self.assertEqual((job.lane, job.every_hours, job.needs), ("cadence", 1 / 6, "cadence"))
        self.assertIn("cadence", jobs.LANES)
        self.assertNotIn("{", jobs._fill(job.backlog_sql, 1))
        with patch.dict(os.environ, {"CADENCE_API_URL": "", "CADENCE_API_KEY": ""}):
            self.assertFalse(jobs.requirement(job)[0])
        self.assertTrue(jobs.requirement(job)[0])


class ContractFixtureChecks(CadenceBase):
    FIXTURE = Path(__file__).with_name("cadence_contract_fixture.json")

    def test_fixture_matches_the_plan_when_the_plan_is_here(self):
        plan = ROOT / "docs" / "outreach-sandbox-plan.md"
        if not plan.exists():
            self.skipTest("docs/ is not in the image")
        t = plan.read_text(encoding="utf-8")
        h = "## Shared contract with Bellwether"
        sec = t[t.index(h):t.index("\n## Field mapping", t.index(h))]
        blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)\n```", sec, re.S)]
        fx = json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(blocks, [fx["ping"], fx["push_response"], fx["status_response"]])

    def test_the_contract_examples_read_with_bellwethers_parser(self):
        fx = json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        ids = [r["bellwetherContactId"] for r in fx["push_response"]["results"]]
        now = cadence._iso()
        self.c.execute("INSERT INTO cadence_request (request_key, batch_id, body, body_sha256, contacts, state,"
                       " first_sent_at) VALUES ('k', 'b', '{}', 'x', 4, 'sending', ?)", (now,))
        for cid in ids:
            crd, _, key = cid.partition(":")
            self.c.execute("INSERT INTO cadence_push (crd, person_key, name, state, pushed_by, first_pushed_at,"
                           " request_key, batch_id) VALUES (?,?,?,'sending','qa',?, 'k', 'b')", (crd, key, cid, now))
        req = dict(self.c.execute("SELECT * FROM cadence_request").fetchone())
        cadence._apply_results(self.c, req, fx["push_response"], "qa")
        got = [self.c.execute("SELECT state FROM cadence_push WHERE crd=? AND person_key=?",
                              tuple(i.split(":", 1))).fetchone()["state"] for i in ids]
        self.assertEqual(got, ["in_sandbox", "in_sandbox", "in_crm", "rejected"])
        item = fx["status_response"]["results"][0]
        self.assertEqual(cadence.apply_status(self.c, item), "moved_to_crm")
        r = self.state(item["bellwetherContactId"])
        self.assertEqual((r["crm_id"], r["moved_at"]), (item["crmPersonId"], item["movedToCrmAt"]))
        self.assertTrue(fx["ping"]["ok"] and fx["ping"]["maxBatch"] == 500)

    def test_a_request_bellwether_builds_keeps_to_the_14_4_tables(self):
        cid = self.person("Con Tract", indvl="42", email="contract@northstarwealth.com", phone="2125550199",
                          linkedin="linkedin.com/in/contract")
        self.push([cid], owner_email=LEE["email"], product="glynac", note="Met at the conference")
        req = self.fake.requests_of("push")[0]
        self.assertEqual(req["key"], KEY)
        self.assertTrue(req["ctype"].startswith("application/json"))
        env = json.loads(req["body"])
        self.assertEqual(set(env), {"pushedBy", "assignTo", "pod", "contacts"})
        self.assertEqual(env["pushedBy"]["email"], ANA["email"])
        self.assertEqual(env["assignTo"], {"email": LEE["email"]})
        c = env["contacts"][0]
        allowed = {"bellwetherContactId", "firstName", "lastName", "jobTitle", "email", "emailVerification",
                   "additionalEmails", "phone", "phoneType", "phoneExtension", "additionalPhones",
                   "linkedinUrl", "city", "state", "country", "individualCrd", "designations",
                   "productInterest", "tags", "notes", "bellwetherUrl", "source", "collectedAt",
                   "doNotContact", "firm"}
        self.assertLessEqual(set(c), allowed)
        self.assertLessEqual(set(c["firm"]), {"name", "crd", "website", "domain", "city", "state", "country",
                                              "phone", "aumUsd", "employees"})
        self.assertIn(c["emailVerification"], ("valid", "risky", "unknown"))
        self.assertIn(c["phoneType"], ("direct", "mobile", "office", "main", "toll_free", "unknown"))
        self.assertTrue(set(c["productInterest"]) <= {"PHH", "ACUBOOTH", "GLYNAC"})
        self.assertEqual(c["productInterest"], ["GLYNAC"])
        self.assertTrue(1 <= len(c["firstName"]) <= 100 and 1 <= len(c["lastName"]) <= 100)
        self.assertTrue(re.fullmatch(r"\d+", c["individualCrd"]))
        self.assertTrue(re.fullmatch(r"[A-Z]{2}", c["country"]))
        # The owner Cadence does not know as a seller: created unclaimed (14.7).
        self.assertIsNone(self.state(cid)["cadence_owner"])
        self.assertEqual(self.push([cid])["results"][0]["state"], "skipped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
