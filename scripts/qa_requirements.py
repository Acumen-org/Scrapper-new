"""Offline regressions for enrichment, scoring, seats and job scheduling.

Run with python -m scripts.qa_requirements. No provider keys, database writes,
email probes or external requests are made.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from prospect import ai, contacts, directory, harvest, jobs, msauth, products, users, verify


class EnrichmentChecks(unittest.TestCase):
    def test_personal_and_role_mailboxes_stay_distinct(self):
        self.assertEqual(harvest.classify('morgan@northstarwealth.com')[1], 'personal')
        for local in ('info', 'hello', 'compliance'):
            self.assertTrue(contacts.is_role_email(local+'@northstarwealth.com'))
        self.assertFalse(harvest.classify('logo@2x.png')[0])

    def test_vcard_keeps_person_and_direct_contacts(self):
        people = harvest.extract_people(
            'BEGIN:VCARD\nVERSION:3.0\nFN:Morgan Ellis\nTITLE:Managing Partner\n'
            'EMAIL:morgan@northstarwealth.com\nTEL:512-234-6789\nEND:VCARD',
            'https://northstarwealth.com/morgan.vcf')
        self.assertEqual(people[0].name, 'Morgan Ellis')
        self.assertEqual(people[0].email, 'morgan@northstarwealth.com')
        self.assertTrue(people[0].phone)

    def test_directory_structured_contacts(self):
        html = '<script type="application/ld+json">{"@type":"Person", "name":"Morgan Ellis", "email":"morgan@northstarwealth.com", "telephone":"512-555-0101", "worksFor":{"@type":"Organization","name":"Northstar Wealth"}}</script>'
        rows = directory.page_records(html, 'https://directory.invalid/advisors')
        self.assertEqual(rows[0]['person_name'], 'Morgan Ellis')
        self.assertEqual(rows[0]['email'], 'morgan@northstarwealth.com')


class VerificationChecks(unittest.TestCase):
    def result(self, reach='safe', catch_all=False, **smtp):
        body = {'is_reachable':reach,'syntax':{'is_valid_syntax':True},
                'smtp':dict(can_connect_smtp=True,is_deliverable=True,is_catch_all=catch_all,**smtp)}
        with patch.object(verify, '_remember'):
            return verify._from_reacher(verify._blank('morgan@qa.invalid','reacher'), body, 'qa.invalid', [])['status']

    def test_explicit_mailbox_and_negative_catchall_are_valid(self):
        self.assertEqual(self.result(), 'valid')

    def test_catchall_never_becomes_valid(self):
        self.assertEqual(self.result(catch_all=True), 'catch_all')

    def test_missing_catchall_evidence_is_not_verified(self):
        self.assertEqual(self.result(catch_all=None), 'unknown')

    def test_disabled_mailbox_is_not_verified(self):
        self.assertNotEqual(self.result(is_disabled=True), 'valid')

    def test_dns_only_never_claims_verified(self):
        r = verify._dns_verdict(verify._blank('morgan@qa.invalid','dns'),'qa.invalid')
        self.assertEqual(r['status'],'unknown')


class SchedulerChecks(unittest.TestCase):
    def setUp(self):
        self.job = jobs.BY_KIND['web_enrich']
        self.future = (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()

    def test_failure_with_backlog_honours_retry_time(self):
        for status in ('failed','timeout'):
            self.assertFalse(jobs.due(self.job, {'last_status':status,'next_run_at':self.future}, 80))

    def test_successful_backlog_continues(self):
        self.assertTrue(jobs.due(self.job, {'last_status':'ok','next_run_at':self.future}, 80))

    def test_pause_and_manual_force(self):
        self.assertFalse(jobs.due(self.job, {'desired_state':'paused'},80))
        self.assertTrue(jobs.due(self.job, {'desired_state':'paused','force':1},80))

    def test_due_schedule_runs_without_a_manual_click(self):
        self.assertTrue(jobs.due(self.job, {'next_run_at':'2020-01-01T00:00:00+00:00'},0))
        self.assertFalse(jobs.due(self.job, {'next_run_at':self.future},0))


class ScoringChecks(unittest.TestCase):
    def test_edited_builtin_level_changes_actual_points(self):
        criterion={'key':'hnw_fit','levels':[[85,'Strong'],[60,'Medium'],[40,'Low'],[10,'Minimal']]}
        self.assertEqual(products.configured_points('phh_fund',criterion,75),60)
        self.assertEqual(products.configured_points('phh_fund',criterion,100),85)

    def test_reordered_values_keep_their_condition_identity(self):
        criterion={'key':'hnw_fit','level_inputs':[100,75,50,25],
                   'levels':[[60,'Strong'],[80,'Medium'],[40,'Low'],[10,'Minimal']]}
        self.assertEqual(products.configured_points('phh_fund',criterion,100),60)
        self.assertEqual(products.configured_points('phh_fund',criterion,75),80)

    def test_default_scales_do_not_change_scores(self):
        for key,p in products.base_cfg()['products'].items():
            for c in p['criteria']:
                for points in (0,20,33,55.5,75,100):
                    self.assertAlmostEqual(products.configured_points(key,c,points),points)

    def test_unknown_weight_is_not_dropped_from_denominator(self):
        product = {'criteria':[{'key':'known','label':'Known','weight':40},
                               {'key':'missing','label':'Missing','weight':60}]}
        with patch.object(products,'product',return_value=product), \
             patch.dict(products.CRITERIA, {'known':lambda *a:(100,'Evidence',True),
                                            'missing':lambda *a:(100,'No data',False)}), \
             patch.object(products,'signals_for',return_value=[]), \
             patch.object(products,'pitch_for',return_value=''):
            r=products.evaluate('qa',{'overrides':{}})
        self.assertEqual(r.score,40)
        self.assertEqual(r.coverage,40)
        self.assertEqual(r.potential,100)
        self.assertEqual(r.missing,['Missing'])

    def test_shipped_weights_are_valid(self):
        for key, product in products.base_cfg()['products'].items():
            self.assertAlmostEqual(sum(c['weight'] for c in product['criteria']),100,msg=key)


class SeatsChecks(unittest.TestCase):
    def test_rahul_gets_admin(self):
        with patch.object(users.settings,'get_list',return_value=['rahul.gopan@acumen-strategy.com']):
            account=users.effective({'login':'rahul.gopan@acumen-strategy.com','role':'user'})
        self.assertTrue(users.is_admin(account))

    def test_owner_can_only_edit_assigned_product_family(self):
        owner={'role':'owner','families':['PHH']}
        self.assertTrue(users.can_edit_product(owner,'PHH'))
        self.assertFalse(users.can_edit_product(owner,'Glynac'))
        self.assertFalse(users.is_admin(owner))
        self.assertFalse(users.can_edit_product({'role':'user'},'PHH'))

    def test_microsoft_rejects_another_tenant(self):
        with patch.object(msauth.settings,'get',return_value='11111111-1111-1111-1111-111111111111'):
            with self.assertRaises(msauth.SignInError):
                msauth.check({'tid':'22222222-2222-2222-2222-222222222222',
                              'email':'rahul.gopan@acumen-strategy.com'})


class AITransportChecks(unittest.TestCase):
    def test_eden_uses_gateway_and_returns_provider_response(self):
        response=Mock(status_code=200)
        response.json.return_value={'choices':[{'message':{'content':'Grounded answer'}}],
                                    'usage':{'prompt_tokens':12,'completion_tokens':3}}
        with patch('requests.post',return_value=response) as post, \
             patch.object(ai.settings,'get',return_value='test-key'):
            text, tin, tout=ai._call_openai_compatible('System',[],'provider/model',50,None,ai.EDEN_BASE)
        self.assertEqual(post.call_args.args[0],'https://api.edenai.run/v3/chat/completions')
        self.assertEqual((text,tin,tout),('Grounded answer',12,3))


if __name__ == '__main__':
    unittest.main(verbosity=2)
