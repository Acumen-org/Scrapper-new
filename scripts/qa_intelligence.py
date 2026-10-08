"""Regressions for names, supported integrations, persistent research and filings.

No external calls. Database checks use qa_hunt's isolated, disposable schema.
"""
import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from scripts.qa_hunt import _DB
from prospect import contacts, harvest, knowledge, names, research, firmtype, settings, websearch
from scripts import autopilot_slice, search_contacts


class NameChecks(unittest.TestCase):
    def test_titles_surname_first_and_suffixes(self):
        for full, want in [('Mr. John Smith', 'John'), ('Dr. XYZ Brown', 'XYZ'),
                           ('Prof. Dr. Jane Smith', 'Jane'), ('SMITH, JANE, ANN', 'Jane'),
                           ('Jane Smith, CFP', 'Jane'), ('Jane Smith, Ph.D.', 'Jane'),
                           ('Anne-Marie O’Neill', 'Anne-Marie'),
                           ('J. Smith', 'J.'), ('Dr.', ''), ('', '')]:
            with self.subTest(full=full):
                self.assertEqual(names.first_name(full), want)
        self.assertEqual(names.first_name('Smith, John', 'Mr.'), 'John')
        self.assertEqual(names.first_name('Smith, John', 'Jonathan'), 'Jonathan')

    def test_mixed_case_is_preserved(self):
        self.assertEqual(names.person_name('Jane McDonald'), 'Jane McDonald')
        self.assertEqual(names.person_name('SMITH, JANE'), 'Jane Smith')
        self.assertEqual(names.person_name('Jane Smith, CFP'), 'Jane Smith CFP')


class PhoneChecks(unittest.TestCase):
    def test_us_international_extensions_and_false_numbers(self):
        self.assertEqual(contacts.norm_phone('+1 650 253 2222 ext. 42'), '(650) 253-2222 x42')
        self.assertEqual(contacts.norm_phone('+44 20 8366 1177'), '+44 20 8366 1177')
        for value in ('200-123-0101', '512-555-0101', '12345', ''):
            self.assertIsNone(contacts.norm_phone(value))

    def test_international_discovery_and_fax_exclusion(self):
        found = harvest._phones_from(['Direct +44 20 8366 1177', 'Fax: 650-253-2222'], [])
        self.assertEqual(found, [{'phone': '+44 20 8366 1177', 'label': 'direct'}])

    def test_extension_does_not_match_someone_elses_line(self):
        page = research.Page('https://example.invalid', '', 'Direct 650-253-2222 x41', '', 'cache')
        self.assertIsNone(research._phone_spots(page, '(650) 253-2222 x42'))
        self.assertTrue(research._phone_spots(page, '(650) 253-2222 x41'))


class IdentityChecks(unittest.TestCase):
    def test_namesake_at_another_employer_is_rejected(self):
        target = research.Target('1', 'i:1', 'Jane Smith', 'jane', 'smith', '',
                                 'Northstar Wealth', '', '', 'https://northstar.invalid', 'northstar.invalid')
        page = research.Page('https://other.invalid/jane', '',
                             'Jane Smith at Meridian Wealth jane.smith@other.invalid', 'Our team', 'fetch')
        reader = Mock()
        reader.get.return_value = page
        verdict = research._check_email(target, {'value': 'jane.smith@other.invalid',
                                                  'source_url': page.url}, reader, None)
        self.assertFalse(verdict.ok)
        self.assertIn('this firm', verdict.why)

    def test_schwab_is_a_custodian_without_misclassifying_its_clients(self):
        entity = knowledge.Entity('schwab', 'Charles Schwab', 'custodian', 99,
                                  patterns=knowledge._compile(['Charles Schwab', 'Schwab Wealth']),
                                  parents=knowledge._compile(['Charles Schwab']))
        kb = knowledge.Matcher([entity])
        self.assertEqual(firmtype.classify({'legal_name': 'CHARLES SCHWAB & CO., INC.'}, kb).category, 'custodian')
        self.assertIsNone(kb.firm(['Independent Wealth Partners']))


class RetryDatabaseChecks(_DB):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        c = cls.conn
        c.execute('ALTER TABLE firm_current ADD COLUMN q5k3 TEXT, ADD COLUMN q7b TEXT')
        c.execute('ALTER TABLE person ADD COLUMN other_names TEXT')
        c.executescript(autopilot_slice.REFRESH_SCHEMA)
        c.execute('ALTER TABLE firm_refresh ADD COLUMN detail TEXT')
        c.commit()
        websearch.init(c)

    def setUp(self):
        c = self.conn
        for table in ('contact_point', 'contact_search_state', 'ai_research', 'email_hunt', 'firm_refresh', 'schedule_a',
                      'person_employment', 'person', 'firm_scope', 'firm_current'):
            c.execute(f'DELETE FROM {table}')
        c.execute("INSERT INTO firm_current(crd,legal_name,q5k3) VALUES ('900701','QA Wealth','Y'),('900702','Other QA','Y')")
        c.execute("INSERT INTO firm_scope VALUES ('900701',90)")
        c.execute("INSERT INTO person(indvl_pk,name,first_name,last_name) VALUES ('1','Jane Smith','Jane','Smith')")
        c.execute("INSERT INTO person_employment VALUES ('1','900701','current')")
        c.commit()

    def put(self, kind, value, status='matched', label=None):
        contacts.upsert(self.conn, '900701', kind, value, 'website', person_key='i:1',
                        person_name='Jane Smith', verify_status=status, label=label)
        self.conn.commit()

    def test_linkedin_does_not_stop_missing_email_or_phone_search(self):
        self.put('linkedin', 'https://www.linkedin.com/in/jane-smith-qa')
        with patch.object(settings, 'get_int', return_value=14):
            self.assertEqual(len(search_contacts.todo(self.conn, 20, None)), 1)
            self.put('email', 'jane@qa.invalid', 'valid')
            self.assertEqual(len(search_contacts.todo(self.conn, 20, None)), 1)
            self.put('phone', '650-253-2222', label='direct')
            self.assertEqual(search_contacts.todo(self.conn, 20, None), [])

    def test_recent_attempt_waits_then_becomes_eligible_again(self):
        now = datetime.now(timezone.utc)
        self.conn.execute('INSERT INTO contact_search_state(crd,person_key,searched_at,found,status,queries) VALUES (?,?,?,?,?,?)',
                          ('900701','i:1',(now-timedelta(days=2)).isoformat(),0,'none',2))
        self.conn.commit()
        with patch.object(settings, 'get_int', return_value=14):
            self.assertEqual(search_contacts.todo(self.conn, 20, None), [])
            self.conn.execute('UPDATE contact_search_state SET searched_at=?', ((now-timedelta(days=15)).isoformat(),))
            self.conn.commit()
            self.assertEqual(len(search_contacts.todo(self.conn, 20, None)), 1)

    def test_unranked_firms_are_also_searched(self):
        self.conn.execute("UPDATE person_employment SET org_pk='900702'")
        self.conn.commit()
        with patch.object(settings, 'get_int', return_value=14):
            self.assertEqual(search_contacts.todo(self.conn, 20, None)[0]['crd'], '900702')

    def test_first_pass_and_oldest_retry_precede_repeating_high_priority_firm(self):
        c = self.conn
        c.execute("INSERT INTO person(indvl_pk,name,first_name,last_name) VALUES ('2','Mary Jones','Mary','Jones')")
        c.execute("INSERT INTO person_employment VALUES ('2','900702','current')")
        c.execute("INSERT INTO contact_search_state(crd,person_key,searched_at,found,status,queries)"
                  " VALUES ('900701','i:1','2020-02-01',0,'none',2)")
        c.commit()
        with patch.object(settings, 'get_int', return_value=14):
            self.assertEqual(search_contacts.todo(c, 1, None)[0]['crd'], '900702')
            c.execute("INSERT INTO contact_search_state(crd,person_key,searched_at,found,status,queries)"
                      " VALUES ('900702','i:2','2020-01-01',0,'none',2)")
            c.commit()
            self.assertEqual(search_contacts.todo(c, 1, None)[0]['crd'], '900702')

    def test_ai_first_pass_reaches_unranked_firms_before_retries(self):
        c = self.conn
        c.execute("INSERT INTO email_hunt(crd,person_key,person_name,state,updated_at) VALUES"
                  " ('900701','i:1','Jane Smith','exhausted','2020-01-01'),"
                  " ('900702','n:mary jones','Mary Jones','exhausted','2020-01-01')")
        c.execute("INSERT INTO ai_research(crd,person_key,researched_at,status)"
                  " VALUES ('900701','i:1','2020-01-01','nothing')")
        c.commit()
        with patch.object(settings, 'get_int', return_value=14):
            self.assertEqual(research.targets(c, 1)[0].crd, '900702')

    def test_complete_contacts_do_not_fill_the_ai_candidate_window(self):
        c = self.conn
        for i in range(201):
            key = f'n:complete {i}'
            c.execute("INSERT INTO email_hunt(crd,person_key,person_name,state,updated_at)"
                      " VALUES ('900701',?,'Jane Smith','found','2020-01-01')", (key,))
            contacts.upsert(c, '900701', 'email', f'person{i}@qa.invalid', 'website',
                            person_key=key, verify_status='valid')
            contacts.upsert(c, '900701', 'phone', f'650-253-2222 x{1000+i}', 'website',
                            person_key=key, label='direct')
            contacts.upsert(c, '900701', 'linkedin', f'https://www.linkedin.com/in/qa-{i}', 'website',
                            person_key=key, verify_status='matched')
        c.execute("INSERT INTO email_hunt(crd,person_key,person_name,state,updated_at)"
                  " VALUES ('900702','n:mary jones','Mary Jones','exhausted','2020-01-01')")
        c.commit()
        with patch.object(settings, 'get_int', return_value=14):
            self.assertEqual(research.targets(c, 1)[0].crd, '900702')

    def test_unverified_research_email_is_hidden(self):
        contacts.upsert(self.conn, '900701', 'email', 'jane@qa.invalid', 'public_research', person_key='i:1')
        self.conn.commit()
        self.assertFalse(self.conn.execute("SELECT 1 FROM usable_contact_point WHERE kind='email'").fetchone())

    def test_custodian_timeout_preserves_old_names_and_other_firm_finishes(self):
        self.conn.execute("INSERT INTO firm_refresh(crd,fetched_at,custodians,status) VALUES ('900701','2020-01-01','Last known custodian','ok')")
        self.conn.commit()
        wrapper = Mock(wraps=self.conn)
        wrapper.close = Mock()
        ok = Mock(stdout=json.dumps({'status':'ok','custodians':'Charles Schwab','pdf_bytes':200,'detail':'1 custodian'}))
        # New firms precede stale ones: new firm succeeds, stale firm times out.
        with patch.object(autopilot_slice.db, 'connect', return_value=wrapper), \
             patch.object(autopilot_slice.subprocess, 'run', side_effect=[ok, subprocess.TimeoutExpired('pdf',110)]):
            msg = autopilot_slice.firm_refresh()
        self.assertIn('1 of 2', msg)
        stale = self.conn.execute("SELECT * FROM firm_refresh WHERE crd='900701'").fetchone()
        self.assertEqual(stale['custodians'], 'Last known custodian')
        self.assertEqual(stale['status'], 'parse_failed')
        now = datetime.now(timezone.utc)
        due = self.conn.execute(autopilot_slice.DUE_SQL, ((now-timedelta(days=1)).isoformat(),
                                  (now-timedelta(days=30)).isoformat(),10)).fetchall()
        self.assertEqual(due, [])
        self.conn.execute("UPDATE firm_refresh SET fetched_at='2020-01-01' WHERE crd='900701'")
        self.conn.commit()
        due = self.conn.execute(autopilot_slice.DUE_SQL, ((now-timedelta(days=1)).isoformat(),
                                  (now-timedelta(days=30)).isoformat(),10)).fetchall()
        self.assertEqual([r['crd'] for r in due], ['900701'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
