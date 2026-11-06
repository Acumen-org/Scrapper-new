"""Offline checks of the push to Cadence, against the fake Cadence.

Everything runs on an in-memory SQLite copy of the tables and the fake
Cadence of scripts/fake_cadence.py on localhost, on a fake clock, so the
5 s, 30 s and 2 min retries and the per-minute pace are proved in no time.
No real Cadence, database, key or network is touched. scripts.qa_requirements
imports these classes, so the Docker build runs them.

What is proved: the payload (every mapped field, the 14.5 normalisation, the
14.4 limits, no guesses, no shared inboxes, the switchboard only as the
firm's phone, a probable LinkedIn kept to the notes); list:ATS and TOLLBOOTH
never sent; each push result and its state; the check before a push (already
in the CRM by email and by phone blocks the contact, it is not sent, and it
is remembered); each error code of 14.9; Idempotency-Key replay and
conflict; the retry schedule; changedSince paging; an opt-out becoming a
suppression shown as "Do not contact · Cadence"; the pace under 30 push and
60 status requests a minute; the key never written anywhere; and a server
with no Cadence settings explaining itself.

Then the cold-outreach plan's Bellwether side (sections 7.1 to 7.3), one test
per bug, named after its id: contract v1.1 and the fall back to v1, a Do not
contact reaching Cadence, the person-level opt-out, the send dialog's
answers, My sends, the chips, Settings, Cadence, the export, the name keys,
and every call's hard deadline.

    python -m scripts.qa_cadence
"""

from __future__ import annotations

import io
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
            is_officer INTEGER, start_date TEXT, prior_firm TEXT, priority REAL, category TEXT);
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
                        patch.object(cadence, "owner_choices", lambda conn=None: [
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

    def test_B018_no_guess_no_shared_inbox_switchboard_only_as_firm_phone(self):
        cid = self.person("Sam Lee", guess="sam.lee@northstarwealth.com", role_email="info@northstarwealth.com",
                          linkedin="linkedin.com/in/samlee", li_status="probable")
        rec = cadence.load_records(self.c, [cid])[cid]
        c = cadence.build_contact(rec)
        self.assertIsNone(c["email"])
        self.assertNotIn("emailVerification", c)
        # The firm's switchboard is never the person's phone (B018, contract v1.1).
        self.assertIsNone(c["phone"])
        self.assertNotIn("phoneType", c)
        self.assertEqual(c["firm"]["phone"], "+12125550100")
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
        self.assertEqual(lines[made], "Added to Cold outreach (owner: Ana Ruiz)")
        self.assertEqual(lines[dup], "Already in Cold outreach (owner: Leigh)")
        # The CRM changes between the preview and the push: the push's own
        # answer is recorded the same way.
        pv = cadence.preview(self.c, [crm], account=ANA)
        self.assertEqual([x["id"] for x in pv["send"]], [crm])
        self.fake.add_crm(emails=["cal@northstarwealth.com"], owner="andrew@acumen-strategy.com")
        res = self.push([crm], preview_id=pv["preview_id"])
        st = self.state(crm)
        self.assertEqual(st["state"], "in_crm")
        self.assertTrue(st["crm_url"].startswith("https://"))
        self.assertIn("Already in CRM", res["results"][0]["line"])
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
        self.assertEqual((sup["source"], sup["channel"], sup["for_id"]), ("cadence", "all", ids[2]))
        # A refusal stands: the chip says why, and nothing offers to send
        # again (B015, the plan's 7.1). A failure may go again, with a new key.
        del self.fake.forced[ids[0]]
        again = self.push([ids[0]])
        self.assertEqual(again["results"][0]["state"], "skipped")
        self.assertIn("Not sent: a required field is missing", again["results"][0]["line"])
        info = cadence.status_map(self.c, [cadence.parse_id(ids[0])])[ids[0]]
        self.assertEqual((info["tone"], info["can_push"]), ("warn", False))
        self.c.execute("UPDATE cadence_push SET state='failed' WHERE crd=? AND person_key=?",
                       cadence.parse_id(ids[0]))
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
        andrew = "andrew@acumen-strategy.com"
        self.assertEqual(skipped[by_mail]["why"], f"Already in CRM · {andrew} · matched on email")
        self.assertEqual(skipped[by_phone]["why"], f"Already in CRM · {andrew} · matched on phone")
        self.assertEqual(skipped[by_office]["why"], f"Already in CRM · {andrew} · matched on phone")
        self.assertTrue(skipped[by_mail]["crm_url"].startswith("https://"))
        self.assertEqual({x["id"] for x in pv["send"]}, {main_only, fine})
        self.assertEqual(len(self.fake.requests_of("check")), 1)
        self.assertFalse(self.fake.sandbox, "the check wrote nothing")
        self.push([by_mail, by_phone, by_office, main_only, fine], preview_id=pv["preview_id"])
        sent = {c["bellwetherContactId"] for b in self.pushed() for c in b["contacts"]}
        self.assertEqual(sent, {main_only, fine})
        # Remembered, not forgotten (B083): Already in CRM, with its link, and no Send.
        st = self.state(by_mail)
        self.assertEqual((st["state"], st["cadence_owner"]), ("in_crm", andrew))
        self.assertTrue(st["crm_url"].startswith("https://"))
        info = cadence.status_map(self.c, [cadence.parse_id(by_mail)])[by_mail]
        self.assertEqual((info["text"], info["tone"], info["can_push"]), (f"Already in CRM · {andrew}", "sky", False))
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
        self.assertEqual(why[dnc], "Do not contact · set here")
        self.assertTrue(why[again].startswith("Already sent from Disq Co"))
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
        # A removed contact may be sent again by anyone (decision 16), once
        # they tick the question that says it was removed (B086).
        asked = self.push([ids[2]], account=dict(ANA, role="user"))
        self.assertTrue(asked.get("needs_confirm"), asked)
        self.assertTrue(self.push([ids[2]], account=dict(ANA, role="user"), confirm_disqualified=True)["ok"])

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
        self.assertEqual(info["text"], "Do not contact · Cadence")
        self.assertFalse(info["can_push"])
        self.assertIn("Do not contact · Cadence", cadence_view.status_html(info))
        card = cadence_view.card_controls(a, "Opt Out", info)
        self.assertIn("Do not contact · Cadence", card)
        self.assertNotIn('data-cad-push="', card)
        row = dict(self.c.execute("SELECT * FROM people_index WHERE person_key=?",
                                  (cadence.parse_id(a)[1],)).fetchone())
        row.update(firm_name="Northstar", firm_city="NEW YORK", raum=1, products="", cfp=0, cfa=0,
                   is_officer=0, start_date="2020-01-01", prior_firm=None, email=None, phone=None,
                   phone_label=None, linkedin=None, email_status=None)
        html = people_view._person_row(row, [], (), {a: info})
        self.assertIn("Do not contact · Cadence", html)
        self.assertNotIn('data-cad-id="', html)
        pv = cadence.preview(self.c, [a], account=ANA)
        self.assertEqual(pv["skipped"][0]["why"], "Do not contact · Cadence")


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
        """The fixture is what scripts/contract_fixture.py makes of section 14:
        a section copied over from Cadence without a new fixture fails here."""
        plan = ROOT / "docs" / "outreach-sandbox-plan.md"
        if not plan.exists():
            self.skipTest("docs/ is not in the image")
        from scripts import contract_fixture
        fx = json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(contract_fixture.build(plan.read_text(encoding="utf-8")), fx)
        self.assertEqual(fx["ping"]["contracts"], ["1", "1.1"])

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

    def test_the_v11_examples_read_with_bellwethers_parser(self):
        fx = json.loads(self.FIXTURE.read_text(encoding="utf-8"))["v1_1"]
        self.assertIn("1.1", fx["ping"]["contracts"])
        now = cadence._iso()
        for item in fx["status_response"]["results"]:
            crd, _, key = item["bellwetherContactId"].partition(":")
            self.c.execute("INSERT INTO cadence_push (crd, person_key, name, state, pushed_by, first_pushed_at)"
                           " VALUES (?,?,?,'in_sandbox','qa',?)", (crd, key, key, now))
            cadence.apply_status(self.c, item)
        first, removed, refused, erased = (self.state(i["bellwetherContactId"])
                                           for i in fx["status_response"]["results"])
        self.assertEqual((first["last_outcome"], first["cadence_url"][:8]), ("not_interested", "https://"))
        self.assertEqual(first["email_bounced_at"], "2026-11-10T09:00:00Z")
        self.assertEqual((removed["state"], removed["removed_reason"]), ("removed", "Retired"))
        self.assertEqual((refused["state"], refused["reason"]), ("rejected", "Cadence blocks this firm"))
        self.assertEqual((erased["state"], erased["erased_at"]), ("opted_out", "2026-11-10T13:00:00Z"))
        self.assertEqual(cadence.label(erased), ("Erased in Cadence", "muted"))
        body = fx["suppress_request"]["contacts"][0]
        self.assertEqual(set(body), {"bellwetherContactId", "emails", "phones", "individualCrd"})
        self.assertEqual(fx["owners_response"]["owners"][0]["pods"], ["ALISA"])

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


class Borrowed:
    """The test's connection, lent to a view: closing it leaves it open."""

    def __init__(self, c):
        self.c = c

    def __getattr__(self, k):
        return getattr(self.c, k)

    def close(self):
        pass


class Pooled(Borrowed):
    """A pooled connection: release() hands it back until the next statement."""

    def __init__(self, c):
        super().__init__(c)
        self.out = False

    def release(self):
        self.c.commit()
        self.out = True

    def execute(self, *a):
        self.out = False
        return self.c.execute(*a)


class ColdOutreachBase(CadenceBase):
    """Contract v1.1 and the Bellwether screens of the cold-outreach plan
    (section 7). Each test is named after the bug it proves fixed."""

    def speak_11(self, contracts=("1", "1.1")):
        self.fake.contracts = list(contracts)
        ok, _, _ = cadence.ping(self.c)
        self.assertTrue(ok)

    def sent(self, name, **kw):
        kw.setdefault("email", f"{name.split()[0].lower()}@northstarwealth.com")
        cid = self.person(name, **kw)
        self.assertTrue(self.push([cid])["ok"])
        return cid

    def sync_later(self, s=60):
        self.clock.sleep(s)
        return cadence.sync(self.c)

    def view_conn(self):
        from prospect import cadence_view, people_view
        lent = Borrowed(self.c)
        return [patch.object(cadence_view, "conn", lambda: lent), patch.object(people_view, "conn", lambda: lent)]

    def info(self, cid):
        return cadence.status_map(self.c, [cadence.parse_id(cid)])[cid]


class ContractChecks(ColdOutreachBase):
    def test_v11_header_only_once_cadence_lists_it_and_v1_answers_without(self):
        a = self.sent("Una Ver")
        self.assertIsNone(self.fake.requests_of("push")[-1]["contract"], "no ping yet: v1")
        self.speak_11(("1",))
        b = self.sent("Dos Ver")
        self.assertIsNone(self.fake.requests_of("push")[-1]["contract"])
        self.assertFalse(cadence.speaks_11(self.c))
        self.speak_11()
        self.sent("Tres Ver")
        self.assertEqual(self.fake.requests_of("push")[-1]["contract"], "1.1")
        self.assertIn("1.1", cadence.contracts(self.c))
        self.assertTrue(a and b)

    def test_unknown_states_read_as_other_and_offer_nothing(self):
        cid = self.person("Fut Ure", email="future@northstarwealth.com")
        crd, key = cadence.parse_id(cid)
        self.c.execute("INSERT INTO cadence_push (crd, person_key, name, state, pushed_by, first_pushed_at)"
                       " VALUES (?,?,?,?,?,?)", (crd, key, "Fut Ure", "parked_by_v2", "ana", cadence._iso()))
        info = self.info(cid)
        self.assertEqual((info["text"], info["can_push"]), ("Other", False))
        self.assertEqual(self.push([cid])["results"][0]["state"], "skipped")

    def test_B015_a_refusal_stays_refused_on_v1_and_v11(self):
        for contracts in (("1",), ("1", "1.1")):
            with self.subTest(contracts=contracts):
                self.speak_11(contracts)
                cid = self.person(f"Ref Use{len(contracts)}", email=f"refuse{len(contracts)}@northstarwealth.com")
                self.fake.forced[cid] = {"result": "rejected", "reason": "blocked_account"}
                self.push([cid])
                self.clock.sleep(120)
                cadence.refresh(self.c, [cid], "ana")
                st = self.state(cid)
                self.assertEqual(st["state"], "rejected")
                self.assertEqual(st["reason"], "Cadence blocks this firm")
                self.assertEqual(cadence.retry_failed(self.c, "admin", background=False)["sent"], 0)

    def test_B012_internal_error_on_one_contact_fails_only_it_and_it_may_go_again(self):
        bad = self.person("Int Err", email="interr@northstarwealth.com")
        good = self.person("Int Fine", email="intfine@northstarwealth.com")
        self.fake.forced[bad] = {"result": "rejected", "reason": "internal_error"}
        res = self.push([bad, good])
        self.assertEqual(self.state(good)["state"], "in_sandbox")
        self.assertEqual(self.state(bad)["state"], "failed")
        line = next(r["line"] for r in res["results"] if r["id"] == bad)
        self.assertIn("Cadence hit an error on this contact", line)
        self.assertTrue(self.info(bad)["can_push"])

    def test_B062_changed_since_rereads_15_minutes_and_B061_owner_changes_count(self):
        cid = self.sent("Own Er")
        self.sync_later()
        since = cadence._state_get(self.c, "status_since")
        self.clock.sleep(600)
        self.fake.claim(cid, LEE["email"], "Lee Park")
        out = cadence.sync(self.c)
        body = json.loads(self.fake.requests_of("status")[-1]["body"])
        want = datetime.fromisoformat(since.replace("Z", "+00:00")) - timedelta(minutes=15)
        self.assertEqual(body["changedSince"], want.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(out["changed"], 1)
        self.assertEqual(self.state(cid)["cadence_owner_name"], "Lee Park")
        self.assertEqual(self.info(cid)["text"], "In Cold outreach · Lee Park")
        self.fake.claim(cid, ANA["email"], "Ana Ruiz")
        self.clock.sleep(120)
        self.assertEqual(cadence.refresh(self.c, [cid], "ana")["owners"], 1)

    def test_B022_owners_list_and_unclaimed_verdict(self):
        cid = self.person("Own List", email="ownlist@northstarwealth.com")
        # v1: the check says Lee (Biz Ops) cannot own, so they arrive unclaimed.
        pv = cadence.preview(self.c, [cid], account=ANA, owner_email=LEE["email"])
        self.assertTrue(pv["owner_verdict"]["unclaimed"])
        self.assertIn("unclaimed", pv["verdict"])
        # v1.1: Cadence's own list, without Biz Ops.
        self.speak_11()
        listed = cadence.owners(self.c)
        self.assertEqual([o["email"] for o in listed], [ANA["email"]])
        self.assertEqual(self.fake.requests_of("owners")[-1]["contract"], "1.1")

    def test_B176_open_in_cadence_from_cadence_url_and_last_outcome(self):
        cid = self.sent("Lin K")
        self.fake.set_status(cid, "removed", lastOutcome="not_interested", lastOutcomeAt="2026-11-03T17:00:00Z",
                             removedBy="Alisa", removedAt="2026-11-03T18:00:00Z", removedReason="Wrong person")
        self.sync_later()
        st = self.state(cid)
        self.assertTrue(st["cadence_url"].startswith("https://cadence.example.test/cold-outreach/"))
        info = self.info(cid)
        from prospect import cadence_view
        html = cadence_view.status_html(info)
        self.assertIn("Open in Cadence", html)
        self.assertEqual(info["text"], "Removed · by Alisa: Wrong person")
        self.assertEqual(info["outcome"], "Not interested · 3 Nov")
        # B086: sending again asks first, and says why.
        pv = cadence.preview(self.c, [cid], account=ANA)
        self.assertIn("Removed from Cold outreach by Alisa on 3 Nov: Wrong person.", pv["again"][0]["why"])
        self.assertIn("called on 3 Nov: Not interested", pv["again"][0]["why"])
        self.assertTrue(self.push([cid]).get("needs_confirm"))

    def test_N037_undo_clears_the_crm_fields_and_says_so(self):
        cid = self.sent("Und O")
        self.fake.set_status(cid, "moved_to_crm", crmPersonId="p9", movedToCrmAt="2026-11-04T10:00:00Z",
                             crmUrl="https://twenty.example.test/object/person/p9")
        self.sync_later()
        self.assertEqual(self.state(cid)["crm_id"], "p9")
        self.fake.set_status(cid, "in_sandbox", crmPersonId=None, crmUrl=None, movedToCrmAt=None)
        self.sync_later()
        st = self.state(cid)
        self.assertEqual((st["state"], st["crm_id"], st["crm_url"], st["moved_at"]), ("in_sandbox", None, None, None))
        from prospect import cadence_view
        self.assertNotIn("Open in CRM", cadence_view.status_html(self.info(cid)))
        crd, key = cadence.parse_id(cid)
        self.assertIn("Move undone in Cadence", " ".join(cadence.history(self.c, crd)[key]))

    def test_N039_erased_blanks_their_details_and_says_erased(self):
        self.firm("200009", "OTHER CO")
        cid = self.sent("Era Sed", phone="2125550111", indvl="8801")
        crd, key = cadence.parse_id(cid)
        other = self.person("Era Sed", crd="200009", indvl="8801", email="erased@other.example")
        self.fake.set_status(cid, "opted_out", erasedAt="2026-11-04T10:00:00Z")
        self.sync_later()
        left = self.c.execute("SELECT COUNT(*) n FROM contact_point WHERE person_key=? AND kind IN ('email', 'phone')",
                              (key,)).fetchone()["n"]
        self.assertEqual(left, 0, "every firm's row of the person, blanked")
        self.assertIsNone(self.state(cid)["email"])
        self.assertEqual(self.info(cid)["text"], "Erased in Cadence")
        self.assertFalse(self.info(cid)["can_push"])
        self.assertFalse(self.info(other)["can_push"])

    def test_N040_a_bounce_marks_the_address_invalid(self):
        cid = self.sent("Bou Nce")
        self.fake.set_status(cid, "in_sandbox", emailBounced=True, emailBouncedAt="2026-11-04T10:00:00Z")
        self.sync_later()
        r = self.c.execute("SELECT verify_status, verify_detail FROM contact_point WHERE value='bou@northstarwealth.com'"
                           ).fetchone()
        self.assertEqual((r["verify_status"], r["verify_detail"]), ("invalid", "Bounced in Cadence"))
        from prospect import cadence_view
        self.assertIn("Bounced in Cadence", cadence_view.status_html(self.info(cid)))
        self.assertEqual(cadence.load_records(self.c, [cid])[cid]["emails"], [])

    def test_opt_out_lifted_in_cadence_lifts_only_its_own_rows(self):
        cid = self.sent("Lif Ted")
        self.fake.set_status(cid, "opted_out")
        self.sync_later()
        cadence.suppress(self.c, "firm", "105943", reason="other", source="manual", by="karson")
        self.c.commit()
        self.fake.set_status(cid, "in_sandbox", optOutLiftedAt="2026-11-04T10:00:00Z")
        self.sync_later()
        active = self.c.execute("SELECT source FROM contact_suppression WHERE lifted_at IS NULL").fetchall()
        self.assertEqual([r["source"] for r in active], ["manual"], "a lift never removes one set here")
        self.assertEqual(self.state(cid)["opt_out_lifted_at"], "2026-11-04T10:00:00Z")

    def test_B063_the_fake_answers_like_cadence(self):
        cid = self.person("Fak E", email="fake@northstarwealth.com")
        self.fake.sandbox_enabled = False
        ok, _, words = cadence.ping(self.c)
        self.assertFalse(ok)
        self.assertIn("switched off", words)
        self.fake.sandbox_enabled = True
        v7 = "018f3b6e-8a5b-7c2d-9e1f-0a1b2c3d4e5f"
        env = cadence.envelope(ANA, None, [{"bellwetherContactId": cid, "firstName": "F", "lastName": "E",
                                            "firm": {"name": "F"}}])
        self.assertEqual(cadence._call(self.c, "POST", "/contacts", cadence.body_bytes(env), idem_key=v7).status, 200)
        self.fake.add_crm(emails=["fake2@northstarwealth.com"])
        dup = self.person("Fak Ea", email="fake2@northstarwealth.com")
        self.fake.forced.clear()
        env = cadence.envelope(ANA, None, [cadence.build_contact(cadence.load_records(self.c, [dup])[dup])])
        cadence._call(self.c, "POST", "/contacts", cadence.body_bytes(env), idem_key=str(uuid.uuid4()))
        self.clock.sleep(5)
        ans = cadence._call(self.c, "POST", "/contacts/status", cadence.body_bytes({"bellwetherContactIds": [dup]}),
                            kind="status")
        self.assertEqual(ans.data["results"][0]["status"], "already_in_crm", "a refusal is kept, never not_found")


class DoNotContactChecks(ColdOutreachBase):
    def test_B003_do_not_contact_reaches_cadence(self):
        cid = self.sent("Dnc Sent", phone="2125550123", indvl="7001")
        self.speak_11()
        cadence.suppress(self.c, "person", cid, reason="opted_out", source="manual", by="karson")
        sid = self.c.execute("SELECT id FROM contact_suppression WHERE value=?", (cid,)).fetchone()["id"]
        told = cadence.tell_cadence(self.c, sid, "karson")
        self.assertEqual(told["state"], "sent")
        body = self.fake.suppress_calls[-1]
        fx = json.loads(ContractFixtureChecks.FIXTURE.read_text(encoding="utf-8"))["v1_1"]["suppress_request"]
        self.assertEqual(set(body), set(fx), "the shape the fixture shows Cadence")
        item = body["contacts"][0]
        self.assertEqual(set(item), set(fx["contacts"][0]))
        self.assertEqual((item["bellwetherContactId"], item["individualCrd"]), (cid, "7001"))
        self.assertIn("dnc@northstarwealth.com", item["emails"])
        self.assertIn("+12125550123", item["phones"])
        self.assertEqual((body["reason"], body["actor"]["email"]), ("opted_out", "karson"))
        self.assertEqual(self.fake.statuses[cid]["status"], "opted_out")
        info = self.info(cid)
        self.assertEqual(info["text"], "Do not contact · set here")
        self.assertIn("Cadence blocks this person too", info["tip"])
        self.assertIsNone(info["live"])

    def test_B003_interim_on_a_v1_cadence_then_told_once_it_speaks_v11(self):
        cid = self.sent("Dnc Old")
        self.speak_11(("1",))
        cadence.suppress(self.c, "person", cid, reason="opted_out", source="manual", by="karson")
        sid = self.c.execute("SELECT id FROM contact_suppression WHERE value=?", (cid,)).fetchone()["id"]
        self.assertEqual(cadence.tell_cadence(self.c, sid, "karson")["state"], "not_sent")
        self.assertEqual(self.fake.suppress_calls, [])
        self.assertEqual(self.info(cid)["live"], "Still live in Cold outreach with Ana Ruiz: Cadence has not been told yet")
        self.fake.contracts = ["1", "1.1"]
        out = self.sync_later()
        self.assertEqual(out["told"], 1)
        r = self.c.execute("SELECT to_cadence FROM contact_suppression WHERE id=?", (sid,)).fetchone()
        self.assertEqual(r["to_cadence"], "sent")

    def test_B003_nobody_sent_tells_nobody(self):
        cid = self.person("Dnc Never", email="never@northstarwealth.com")
        self.speak_11()
        cadence.suppress(self.c, "person", cid, reason="other", source="manual", by="karson")
        sid = self.c.execute("SELECT id FROM contact_suppression WHERE value=?", (cid,)).fetchone()["id"]
        self.assertIsNone(cadence.tell_cadence(self.c, sid, "karson")["state"])
        self.assertEqual(self.fake.suppress_calls, [])

    def test_B074_no_lift_on_a_cadence_opt_out(self):
        from prospect import cadence_view
        cid = self.sent("Lif No")
        self.fake.set_status(cid, "opted_out")
        self.sync_later()
        sid = self.c.execute("SELECT id FROM contact_suppression WHERE source='cadence' AND scope='person'"
                             ).fetchone()["id"]
        done, words = cadence.lift(self.c, sid, "admin")
        self.assertFalse(done)
        self.assertIn("only be lifted there", words)
        card = cadence_view.card_controls(cid, "Lif No", self.info(cid))
        self.assertNotIn("/api/cadence/lift", card)
        self.assertNotIn("data-cad-dnc", card)
        self.assertIn("Only Cadence can lift it", self.info(cid)["tip"])

    def test_B075_no_channel_and_the_whole_firm_needs_a_confirm(self):
        from prospect import cadence_view
        import inspect
        self.assertNotIn("channel", inspect.signature(cadence_view.api_suppress).parameters)
        cid = self.person("Fir M", email="firm@northstarwealth.com")
        with self.view_conn()[0], patch.object(cadence_view, "current_user", lambda: "karson"):
            no = json.loads(cadence_view.api_suppress(id=cid, scope="firm", reason="other", note="",
                                                      confirm="").body)
            self.assertFalse(no["ok"])
            yes = json.loads(cadence_view.api_suppress(id=cid, scope="firm", reason="other", note="",
                                                       confirm="1").body)
        self.assertTrue(yes["ok"])
        self.assertEqual(self.info(cid)["text"], "Do not contact · whole firm")
        js = (ROOT / "prospect" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('name="channel"', js)

    def test_N007_an_opt_out_follows_the_person_to_their_other_firm(self):
        from prospect import people_view
        self.firm("300001", "SECOND FIRM LLC")
        a = self.sent("Lee Two", indvl="9901")
        b = self.person("Lee Two", crd="300001", indvl="9901", email="lee2@second.example")
        self.fake.set_status(a, "opted_out")
        self.sync_later()
        info = self.info(b)
        self.assertEqual(info["text"], "Do not contact · Cadence at Northstar Wealth LLC")
        self.assertFalse(info["can_push"])
        self.assertIn("while at Northstar Wealth LLC", info["tip"])
        pv = cadence.preview(self.c, [b], account=ANA)
        self.assertEqual(pv["skipped"][0]["why"], "Do not contact · Cadence at Northstar Wealth LLC")
        f = people_view._filters(cad="optout")
        where, args = people_view._where(f, set())
        keys = {r["person_key"] + "@" + r["crd"] for r in self.c.execute(
            f"SELECT crd, person_key FROM people_index WHERE {where}", args)}
        self.assertIn("i:9901@300001", keys)

    def test_N041_each_line_credits_its_own_actor(self):
        from prospect import cadence_view
        cid = self.sent("Cre Dit")
        cadence.suppress(self.c, "person", cid, reason="opted_out", source="manual", by="karson")
        self.c.commit()
        info = self.info(cid)
        card = cadence_view.card_controls(cid, "Cre Dit", info)
        self.assertIn("Sent by", card)
        self.assertIn("Set in Bellwether by karson", info["tip"])


class ContractV11Checks(ColdOutreachBase):
    """Contract v1.1 as section 14 now says it (S9): the pod, the outcome
    words, and the push rules both apps run."""

    def test_S3_the_owners_pod_goes_with_the_send(self):
        self.fake.add_user(ANA["email"], ANA["name"], "senior_fo", pods=["ALISA", "LEIGH"])
        self.speak_11()
        a = self.person("Pod One", email="podone@northstarwealth.com")
        pv = cadence.preview(self.c, [a], account=ANA)
        self.assertEqual((pv["pods"], pv["pod"]), (["ALISA", "LEIGH"], None), "two pods: the sender chooses")
        self.assertEqual(cadence.preview(self.c, [a], account=ANA, pod="leigh")["pod"], "LEIGH")
        self.assertTrue(self.push([a], pod="leigh")["ok"])
        self.assertEqual(self.pushed()[-1]["pod"], "LEIGH")
        self.assertEqual(self.pushed("check")[-1]["pod"], "LEIGH")
        # One pod: it goes without asking. A pod the owner does not work never goes.
        self.fake.add_user(ANA["email"], ANA["name"], "senior_fo", pods=["ALISA"])
        self.clock.sleep(cadence.OWNERS_TTL_S + 1)
        b = self.person("Pod Two", email="podtwo@northstarwealth.com")
        self.assertTrue(self.push([b], pod="ANDREW")["ok"])
        self.assertEqual(self.pushed()[-1]["pod"], "ALISA")
        # A Cadence that speaks v1 only has no owners list: no pod, as before.
        self.speak_11(("1",))
        c = self.person("Pod Three", email="podthree@northstarwealth.com")
        self.assertTrue(self.push([c])["ok"])
        self.assertIsNone(self.pushed()[-1]["pod"])

    def test_contract_v11_last_outcome_in_cadences_words(self):
        cid = self.sent("Out Come")
        self.fake.set_status(cid, "in_sandbox", lastOutcome="no_conversation", lastOutcomeAt="2026-11-03T17:00:00Z")
        self.sync_later()
        self.assertEqual(self.info(cid)["outcome"], "Line connected, no conversation · 3 Nov")
        self.assertEqual(cadence.outcome_words("do_not_contact"), "Asked not to be contacted")
        self.assertEqual(cadence.outcome_words("left_message_with_pa"), "Left message with pa")

    def test_B169_linkedin_is_decoded_before_it_is_lower_cased(self):
        self.assertEqual(cadence.norm_linkedin("https://www.linkedin.com/in/%C3%89lodie-D/?trk=x"),
                         "linkedin.com/in/\u00e9lodie-d")
        self.assertEqual(cadence.norm_linkedin("UK.LinkedIn.com/in/JaneDoe-1"), "linkedin.com/in/janedoe-1")

    def test_B172_B174_titles_cut_at_a_word_and_names_in_any_alphabet(self):
        from prospect import roles
        from prospect.names import nice_name
        long = "MANAGING DIRECTOR AND SENIOR VICE PRESIDENT OF PRIVATE WEALTH MANAGEMENT, INSTITUTIONAL CLIENT SERVICES"
        self.assertEqual(roles.clean_title(long),
                         "Managing Director and Senior Vice President of Private Wealth Management, Institutional")
        cid = self.person("Ti Tle", email="title@northstarwealth.com", title=long + " AND OPERATIONS" * 10)
        c = cadence.build_contact(cadence.load_records(self.c, [cid])[cid])
        self.assertLessEqual(len(c["jobTitle"]), 200)
        self.assertTrue(roles.clean_title(long + " AND OPERATIONS" * 10, 400).startswith(c["jobTitle"] + " "))
        self.assertEqual(nice_name("\u00d6STERREICH CAPITAL LLC"), "\u00d6sterreich Capital LLC")
        self.assertEqual(nice_name("Z\u00dcRICH WEALTH PARTNERS AG"), "Z\u00fcrich Wealth Partners AG")

    def test_B173_the_firm_that_uses_the_domain_keeps_the_email(self):
        self.firm("300003", "OTHER ADVISORS LLC", website="other.example")
        away = self.person("Jan Away", crd="300003", email="jan@northstarwealth.com")
        home = self.person("Jan Home", email="jan@northstarwealth.com")
        pv = cadence.preview(self.c, [away, home], account=ANA)
        self.assertEqual([x["id"] for x in pv["send"]], [home], "whatever the selection order")
        self.assertEqual([(x["id"], x["rule"]) for x in pv["skipped"]], [(away, "same_in_push")])
        self.assertIn("whose firm uses that address", pv["skipped"][0]["why"])

    def test_B017_cadences_possible_duplicate_names_who(self):
        a = self.person("Dup Lic", email="duplic@northstarwealth.com")
        warning = {"code": "possible_sandbox_duplicate", "field": None, "crmPersonId": None,
                   "detail": "Possibly the same person as Bob Lic, already in Cadence at the same firm."}
        real = self.fake._one
        with patch.object(self.fake, "_one", lambda c, *x, **k: dict(real(c, *x, **k), warnings=[warning])):
            pv = cadence.preview(self.c, [a], account=ANA)
        self.assertEqual(pv["send"][0]["warnings"], ["Possibly the same person as Bob Lic, already in Cadence at the same firm."])


class SendChecks(ColdOutreachBase):
    def test_N092_one_send_per_sec_person_and_counts_of_people(self):
        self.firm("300002", "THIRD FIRM LLC", website="third.example")
        home = self.person("Dua L", indvl="4401", email="dual@northstarwealth.com")
        away = self.person("Dua L", crd="300002", indvl="4401", email="dual@other.example", phone="2125550190")
        pv = cadence.preview(self.c, [away, home], account=ANA)
        self.assertEqual(pv["count"], 1)
        self.assertEqual([x["id"] for x in pv["send"]], [home], "the row at their own firm goes")
        self.assertEqual(pv["skipped"][0]["rule"], "same_person")
        self.push([away, home])
        sent = self.pushed()[-1]["contacts"]
        self.assertEqual(len(sent), 1)
        self.assertIn("dual@other.example", sent[0]["additionalEmails"])
        self.assertIn("+12125550190", [p["number"] for p in sent[0]["additionalPhones"]])

    def test_B001_B002_every_phone_and_the_crd_go(self):
        cid = self.person("All Pho", indvl="5501", email="allpho@northstarwealth.com", phone="2125550140",
                          label="direct")
        crd, key = cadence.parse_id(cid)
        self.c.execute("INSERT INTO contact_point (crd, person_key, person_name, kind, value, label, is_role, source,"
                       " sources, confidence, verify_status, found_at, updated_at) VALUES"
                       " (?,?,?,'phone','2125550141','office',0,'website','website',80,'unverified','x','x')",
                       (crd, key, "All Pho"))
        c = cadence.build_contact(cadence.load_records(self.c, [cid])[cid])
        self.assertEqual(c["individualCrd"], "5501")
        self.assertEqual(c["additionalPhones"], [{"number": "+12125550141", "type": "office"}])

    def test_N093_the_cap_is_a_calendar_day_of_people(self):
        crd, key = "105943", "n:cap day"
        # 15:00 UTC is 09:00 in Chicago, whose day began at 06:00 UTC: 04:00 UTC was yesterday there.
        for at in ("2026-11-03T04:00:00+00:00", "2026-11-03T06:00:00+00:00", "2026-11-03T07:00:00+00:00"):
            self.c.execute("INSERT INTO cadence_push_event (at, actor, crd, person_key, action) VALUES"
                           " (?,?,?,?,'push')", (at, ANA["login"], crd, key))
        self.assertEqual(cadence.used_today(self.c, ANA["login"]), 1, "one person, today")
        with patch.object(cadence, "daily_per_user", lambda: 1):
            cid = self.person("Cap Over", email="capover@northstarwealth.com")
            pv = cadence.preview(self.c, [cid], account=ANA)
            self.assertEqual(pv["left_today"], 0)
            self.assertFalse(pv["can_send"])
            self.assertIn("midnight", pv["over_cap"])
            self.assertIn("0 more today", self.push([cid])["message"])

    def test_B065_a_new_owner_reuses_the_check_and_429_says_so(self):
        cid = self.person("Reu Se", email="reuse@northstarwealth.com")
        pv = cadence.preview(self.c, [cid], account=ANA)
        again = cadence.preview(self.c, [cid], account=ANA, owner_email=LEE["email"], reuse=pv["preview_id"])
        self.assertEqual(len(self.fake.requests_of("check")), 1)
        self.assertEqual(again["check"]["state"], "ok")
        self.fake.then("check", status=429, code="rate_limited", retry_after=12)
        busy = cadence.preview(self.c, [cid], account=ANA)
        self.assertIn("Too many sends a minute", busy["check"]["message"])
        self.assertIn("12 seconds", busy["check"]["message"])

    def test_B084_nothing_to_send_says_why_and_B181_title(self):
        cid = self.person("Not Hing", email="nothing@northstarwealth.com")
        cadence.suppress(self.c, "person", cid, reason="other", source="manual", by="k")
        pv = cadence.preview(self.c, [cid], account=ANA)
        self.assertFalse(pv["can_send"])
        self.assertIsNone(pv["verdict"])
        self.assertTrue(pv["nothing"])
        res = self.push([cid])
        self.assertEqual((res["title"], res["summary"]), ("Nothing sent", "1 skipped"))
        ok = self.person("Som Ething", email="something@northstarwealth.com")
        res = self.push([ok, cid])
        self.assertEqual(res["title"], "Sent 1, skipped 1")

    def test_B082_the_checks_warnings_show_before_the_send(self):
        cid = self.person("War N", email="warn@northstarwealth.com")
        orig = self.fake._one

        def warned(c, *a):
            r = orig(c, *a)
            r["warnings"] = [{"code": "possible_crm_match", "field": None, "crmPersonId": "p1", "detail": "Jane Doe"}]
            return r
        with patch.object(self.fake, "_one", warned):
            pv = cadence.preview(self.c, [cid], account=ANA)
        self.assertEqual(pv["send"][0]["warnings"], ["Possible match in the CRM: Jane Doe"])

    def test_B077_the_first_ones_of_a_filter_over_the_cap(self):
        from prospect import cadence_view, people_index
        for i in range(3):
            self.person(f"Fir St{chr(97 + i)}", email=f"first{i}@northstarwealth.com")
        with patch.object(cadence, "max_per_push", lambda: 2), self.view_conn()[0], \
                patch.object(people_index, "exists", lambda c: True), \
                patch.object(people_index, "columns", lambda c: set()):
            ids, qs, err, over = cadence_view._selection({"filter": ""})
            self.assertIn("Send the first 2", err)
            self.assertTrue(over)
            ids, qs, err, over = cadence_view._selection({"filter": "", "first": True})
            self.assertEqual((len(ids), err, over), (2, None, False))
            self.assertIn("on this page", cadence_view._selection({"filter": "", "origin": "firm"})[2])

    def test_B078_a_send_is_found_by_its_key_after_a_gateway_error(self):
        from prospect import cadence_view
        cid = self.person("Gat Eway", email="gateway@northstarwealth.com")
        first = self.push([cid], client_key="dialog-key-1")
        again = self.push([cid], client_key="dialog-key-1")
        self.assertEqual(first["batch_id"], again["batch_id"], "the same send, not a second one")
        with self.view_conn()[0], patch.object(cadence_view, "current_user", lambda: ANA["login"]):
            got = json.loads(cadence_view.api_batch_by_key(key="dialog-key-1").body)
        self.assertEqual(got["batch_id"], first["batch_id"])

    def test_B085_a_hold_says_what_cadence_did(self):
        cid = self.person("Hol D", email="hold@northstarwealth.com")
        self.fake.then("push", status=503, code="integration_disabled")
        res = self.push([cid])
        self.assertIn("Waiting: Cadence has the integration switched off", res["results"][0]["line"])
        self.assertEqual(self.info(cid)["text"], "Waiting · Cadence has the integration switched off")

    def test_B177_a_shared_inbox_alone_says_so(self):
        self.firm("300003", "INBOX CO")
        cid = self.person("Sha Red", crd="300003", role_email="info@inbox.example")
        pv = cadence.preview(self.c, [cid], account=ANA)
        self.assertEqual(pv["skipped"][0]["why"], "Only a shared inbox, and no valid phone")

    def test_B217_admin_retry_asks_and_counts_against_nobody(self):
        self.firm("300004", "LATER DISQ CO")
        cid = self.person("Ret Ry", crd="300004", email="retry@later.example")
        self.fake.then("push", status=422, code="invalid_request")
        self.push([cid])
        self.assertEqual(self.state(cid)["state"], "failed")
        self.firm("300004", "LATER DISQ CO", status="disqualified")
        before = cadence.used_today(self.c, ANA["login"])
        ask = cadence.retry_failed(self.c, "admin", background=False)
        self.assertIn("Disqualified", ask["ask"])
        self.assertEqual(ask["sent"], 0)
        with patch("prospect.users.get", lambda login: ANA if login == ANA["login"] else None):
            done = cadence.retry_failed(self.c, "admin", background=False, confirm=True)
            cadence.drain(self.c)
        self.assertEqual(done["sent"], 1)
        self.assertEqual(cadence.used_today(self.c, ANA["login"]), before)
        ev = self.c.execute("SELECT actor FROM cadence_push_event WHERE action='resend'").fetchone()
        self.assertEqual(ev["actor"], "admin")

    def test_B205_days_are_cadences_days(self):
        self.assertEqual(cadence._day("2026-11-04T03:00:00Z"), "3 Nov")
        self.assertEqual(cadence._clock_time("2026-11-03T23:05:00Z"), "17:05")


class ScreenChecks(ColdOutreachBase):
    def test_B019_hidden_always_hides(self):
        css = (ROOT / "prospect" / "static" / "app.css").read_text(encoding="utf-8")
        self.assertIn("[hidden]{display:none!important}", css)

    def test_B079_do_not_contact_is_a_red_stop_and_the_details_are_greyed(self):
        from prospect import cadence_view, people_view
        cid = self.person("Sto P", email="stop@northstarwealth.com")
        cadence.suppress(self.c, "person", cid, reason="opted_out", source="manual", by="k")
        info = self.info(cid)
        html = cadence_view.status_html(info)
        self.assertIn('class="cad-chip stop"', html)
        self.assertIn("<svg", html)
        row = dict(self.c.execute("SELECT * FROM people_index WHERE person_key=?",
                                  (cadence.parse_id(cid)[1],)).fetchone())
        row.update(firm_name="Northstar", firm_city="NEW YORK", raum=1, products="", cfp=0, cfa=0,
                   is_officer=0, start_date="2020-01-01", prior_firm=None, email="stop@northstarwealth.com",
                   phone=None, phone_label=None, linkedin=None, email_status="valid")
        self.assertIn('class="cad-stopped"', people_view._person_row(row, [], (), {cid: info}))

    def test_B080_my_sends_filters_and_groups_by_send(self):
        from prospect import people_view
        a = self.sent("Min E")
        self.fake.then("push", status=422, code="invalid_request")
        b = self.sent("Max E")
        self.assertEqual(self.state(b)["state"], "failed")
        f = dict(people_view._filters(cad="mine"), me=ANA["login"])
        where, args = people_view._where(f, set())
        rows = [dict(r) for r in self.c.execute(
            f"SELECT * FROM people_index WHERE {where} ORDER BY {people_view._order(f, set())}", args)]
        self.assertEqual({cadence.contact_id(r["crd"], r["person_key"]) for r in rows}, {a, b})
        f2 = people_view._filters(cad="failed")
        w2, a2 = people_view._where(f2, set())
        self.assertEqual([r["person_key"] for r in self.c.execute(f"SELECT person_key FROM people_index WHERE {w2}", a2)],
                         [cadence.parse_id(b)[1]])
        for r in rows:
            r.update(firm_name="Northstar", firm_city="NEW YORK", raum=1, products="", cfp=0, cfa=0,
                     is_officer=0, start_date="2020-01-01", prior_firm=None, phone_label=None, linkedin=None,
                     email_status=None)
        cad = cadence.status_map(self.c, [(r["crd"], r["person_key"]) for r in rows])
        with self.view_conn()[1]:
            html = people_view._rows_html(rows, [], f, cad)
        self.assertEqual(html.count('class="cad-group"'), 2)
        self.assertIn(f'data-cad-retry="{b}"', html)

    def test_B021_the_export_has_a_do_not_contact_column_in_words(self):
        from prospect import people_index, people_view
        a = self.sent("Exp Ort")
        b = self.person("Exp Dnc", email="expdnc@northstarwealth.com")
        self.c.execute("UPDATE people_index SET email='expdnc@northstarwealth.com' WHERE person_key=?",
                       (cadence.parse_id(b)[1],))
        cadence.suppress(self.c, "person", b, reason="other", source="manual", by="k")
        self.c.commit()
        with self.view_conn()[1], patch.object(people_index, "exists", lambda c: True), \
                patch.object(people_index, "columns", lambda c: set()):
            out = people_view.people_export().body.decode("utf-8")
        import csv
        rows = {r["name"]: r for r in csv.DictReader(io.StringIO(out))}
        self.assertEqual(rows["Exp Dnc"]["do_not_contact"], "Do not contact · set here")
        self.assertEqual(rows["Exp Dnc"]["email"], "", "never handed on")
        self.assertEqual(rows["Exp Ort"]["cold_outreach"], "In Cold outreach")
        self.assertEqual(rows["Exp Ort"]["do_not_contact"], "")
        self.assertTrue(a)

    def test_B081_newer_details_say_what_and_what_to_do(self):
        cid = self.sent("New Er")
        words = cadence.newer_details(self.state(cid), "new.er@northstarwealth.com", None)
        self.assertIn("new.er@northstarwealth.com", words)
        self.assertIn("tell the owner", words)

    def test_B178_N094_a_refresh_keeps_the_history_and_the_card(self):
        from prospect import cadence_view
        cid = self.sent("His Tory")
        self.fake.set_status(cid, "removed")
        self.clock.sleep(120)
        cadence.refresh(self.c, [cid], "ana")
        line = cadence_view._lines(self.c, [cid], cards=True)[cid]
        self.assertIn("data-tip", line["html"])
        self.assertIn("Removed from Cold outreach", line["html"])
        self.assertIn(f'data-cad-push="{cid}"', line["card"], "Send again is back without a reload")
        self.assertIn("Send again", line["card"])

    def test_B216_recent_activity_is_one_row_a_send(self):
        from prospect import cadence_view
        ids = [self.person(f"Gro Up{chr(97 + i)}", email=f"group{i}@northstarwealth.com") for i in range(3)]
        self.push(ids)
        html = cadence_view._sends(self.c, True)
        self.assertEqual(html.count('<details class="cad-send">'), 1)
        self.assertEqual(html.count("<li>"), 3)

    def test_B020_the_banner_shows_the_last_real_test_and_gates_the_switch(self):
        from prospect import cadence_view
        self.assertIn("Never tested", cadence_view._banner(self.c, True, None))
        self.assertIn("Test the connection first", cadence.cannot_switch_on(self.c))
        self.fake.key = "another-key"
        cadence.ping(self.c, by="admin")
        self.assertIn("Failed", cadence_view._banner(self.c, True, None))
        self.assertIn("refused the key", cadence.cannot_switch_on(self.c))
        self.fake.key = KEY
        cadence.ping(self.c, by="admin")
        self.assertIn("Connected: Cadence answered", cadence_view._banner(self.c, True, None))
        self.assertIsNone(cadence.cannot_switch_on(self.c))
        self.clock.sleep(25 * 3600)
        self.assertIn("more than a day old", cadence.cannot_switch_on(self.c))

    def test_B066_a_lifted_hold_sends_now(self):
        from prospect import cadence_view
        cid = self.person("Now Go", email="nowgo@northstarwealth.com")
        self.fake.then("push", status=401, code="unauthorized")
        self.push([cid])
        self.assertEqual(cadence.held(self.c), "unauthorized")
        drained = []
        with self.view_conn()[0], patch.object(cadence, "drain_now", lambda: drained.append(1)):
            res = json.loads(cadence_view.settings_test().body)
        self.assertTrue(res["ok"])
        self.assertIsNone(cadence.held(self.c))
        self.assertEqual(drained, [1])
        self.assertIn("going now", res["message"])


class NameKeyChecks(ColdOutreachBase):
    def test_B064_names_in_any_script_get_keys_and_go(self):
        self.assertEqual(contacts.name_key("José Müller"), "n:jose muller")
        self.assertEqual(contacts.name_key("Mary O'Brien"), "n:mary brien", "ASCII keys unchanged")
        self.assertEqual(contacts.name_key("Иван Петров"), "n:иван петров")
        self.assertEqual(contacts.name_key("王伟"), "n:王伟")
        self.assertNotEqual(contacts.name_key("王伟"), contacts.name_key("李娜"))
        cid = self.person("王伟", key="n:王伟", email="wang@northstarwealth.com")
        # Found on a website: no filed first and last name, only the name as written.
        self.c.execute("UPDATE people_index SET first_name=NULL, last_name=NULL WHERE person_key='n:王伟'")
        self.assertEqual(cadence.parse_id(cid), ("105943", "n:王伟"))
        self.assertEqual(cadence.anchor("n:王伟"), "p-n-王伟")
        self.assertTrue(self.push([cid])["ok"])
        sent = self.pushed()[-1]["contacts"][0]
        self.assertEqual((sent["firstName"], sent["lastName"]), ("伟", "王"))

    def test_B064_rekey_moves_only_the_never_sent(self):
        crd = "105943"
        for name, old, sent in (("José Müller", "n:jos ller", False), ("Zoë Ångström", "n:zo ngstr m", True)):
            self.c.execute("INSERT INTO contact_point (crd, person_key, person_name, kind, value, is_role, source,"
                           " sources, confidence, verify_status, found_at, updated_at) VALUES"
                           " (?,?,?,'email',?,0,'website','website',80,'valid','x','x')",
                           (crd, old, name, f"{old[2:5].strip()}@northstarwealth.com"))
            if sent:
                self.c.execute("INSERT INTO cadence_push (crd, person_key, name, state, pushed_by, first_pushed_at)"
                               " VALUES (?,?,?,'in_sandbox','ana','x')", (crd, old, name))
        self.c.commit()
        out = cadence.rekey_names(self.c)
        self.assertEqual((out["moved"], out["kept_sent"]), (1, 1))
        keys = {r["person_name"]: r["person_key"] for r in self.c.execute(
            "SELECT person_name, person_key FROM contact_point")}
        self.assertEqual(keys["José Müller"], "n:jose muller")
        self.assertEqual(keys["Zoë Ångström"], "n:zo ngstr m", "Cadence knows them by the old key")
        self.assertEqual(cadence.rekey_names(self.c), {"done": False}, "once")


class BackfillChecks(ColdOutreachBase):
    def test_N007_N037_the_release_backfill_widens_old_opt_outs_and_clears_stale_links(self):
        old = self.person("Old Opt", indvl="6601", email="oldopt@northstarwealth.com", phone="2125550177")
        crd, key = cadence.parse_id(old)
        undone = self.person("Old Undo", email="oldundo@northstarwealth.com")
        ucrd, ukey = cadence.parse_id(undone)
        for c, k, st, url in ((crd, key, "opted_out", None),
                              (ucrd, ukey, "in_sandbox", "https://twenty.example.test/object/person/p1")):
            self.c.execute("INSERT INTO cadence_push (crd, person_key, indvl_pk, name, email, state, crm_url,"
                           " pushed_by, first_pushed_at) VALUES (?,?,?,?,?,?,?,'ana','x')",
                           (c, k, "6601" if st == "opted_out" else None, "x", None, st, url))
        # How the build before this release wrote an opt-out: the row only.
        self.c.execute("INSERT INTO contact_suppression (scope, value, channel, reason, source, set_at)"
                       " VALUES ('person', ?, 'all', 'opted_out', 'cadence', 'x')", (old,))
        self.c.commit()
        out = cadence.backfill(self.c)
        self.assertEqual((out["opt_outs"], out["linked"], out["crm_cleared"]), (1, 1, 1))
        scopes = {r["scope"]: r["for_id"] for r in self.c.execute(
            "SELECT scope, for_id FROM contact_suppression WHERE lifted_at IS NULL")}
        self.assertEqual(scopes, {"person": old, "individual": old, "email": old, "phone": old})
        self.assertIsNone(self.state(undone)["crm_url"])
        self.assertEqual(cadence.backfill(self.c), {"done": False}, "once")


class DeadlineChecks(ColdOutreachBase):
    def test_B016_every_call_ends_by_its_deadline(self):
        import time as real
        self.fake.then("push", action="timeout")
        self.fake.slow_s = 3
        with patch.object(cadence, "TIMEOUT_S", 0.5):
            t = real.monotonic()
            ans = cadence._call(self.c, "POST", "/contacts", b"{}", idem_key=str(uuid.uuid4()))
            self.assertEqual(ans.kind, "timeout")
            self.assertLess(real.monotonic() - t, 2.0)

    def test_B016_a_slow_name_lookup_is_inside_the_deadline(self):
        import socket
        import time as real
        slow_dns = socket.getaddrinfo

        def slow(*a, **k):
            real.sleep(3)
            return slow_dns(*a, **k)
        with patch.object(socket, "getaddrinfo", slow), patch.object(cadence, "PING_TIMEOUT_S", 0.5):
            t = real.monotonic()
            ok, _, words = cadence.ping(self.c)
            self.assertFalse(ok)
            self.assertIn("did not answer within 0.5 seconds", words)
            self.assertLess(real.monotonic() - t, 2.0)

    def test_B016_no_pooled_connection_is_held_during_a_call(self):
        pooled = Pooled(self.c)
        seen = []
        real_fetch = cadence._fetch

        def spy(*a, **k):
            seen.append(pooled.out)
            return real_fetch(*a, **k)
        cid = self.person("Poo Led", email="pooled@northstarwealth.com")
        with patch.object(cadence, "_fetch", spy):
            cadence.push(pooled, [cid], account=ANA, background=False)
            cadence.ping(pooled)
            cadence.refresh(pooled, [cid], "ana")
        self.assertTrue(seen and all(seen), seen)


if __name__ == "__main__":
    unittest.main(verbosity=2)
