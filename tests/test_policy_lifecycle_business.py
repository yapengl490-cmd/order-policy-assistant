"""政策生命周期的业务传播：真实独立SQLite，不接正式库/模型/平台。

验证撤回不删除订单事实、不借其他版本，重修后新查询采用更正来源。
来源记录和旧结果均保留；运行服务、网页及公开版本由独立验收覆盖。
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select
from buyer_support_service import check_buyer_support, evaluate_buyer_support, get_support_context
from catalog_policy import check_catalog_policy
from catalog_recommendations import CatalogScope, CatalogSnapshot, recommend
from general_policy import lookup_general_policy, render_general_policy
from policy_kb import import_policy_files, metadata, policy_documents, select_policy
from test_buyer_support_service import SupportFixture

ROOT = Path(__file__).resolve().parents[1]
AT = datetime.fromisoformat('2026-11-20T12:00:00+08:00')
NOW = datetime.fromisoformat('2026-11-18T08:00:00+08:00')
CLAIMS = {'is_unworn': True, 'tags_intact': True, 'reason': 'size'}
SHOES = 'S001-shoes-2026-11'
TSHIRT = 'DEMO-SHOP-A-tshirt-2026-11'


class PolicyLifecycleBusinessTests(SupportFixture, unittest.TestCase):
    def setUp(self):
        self.setup_support()
        metadata.create_all(self.engine)
        self.paths = [ROOT/'knowledge/2026-10-shoes.md', ROOT/'knowledge/2026-11-shoes.md',
                      ROOT/'knowledge/2026-11-catalog-tshirts.md']
        import_policy_files(self.engine, self.paths)
        directory = tempfile.TemporaryDirectory(prefix='policy-lifecycle-business-')
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        blocker = patch('socket.socket', side_effect=AssertionError('real network forbidden'))
        blocker.start(); self.addCleanup(blocker.stop)

    def document(self, identity):
        with self.engine.connect() as connection:
            return dict(connection.execute(select(policy_documents).where(
                policy_documents.c.document_id == identity)).mappings().one())

    def withdraw(self, identity):
        from policy_lifecycle import withdraw_policy
        return withdraw_policy(self.engine, identity, expected_checksum=self.document(identity)['checksum'],
                               reason='独立测试：来源撤回，等待更正版', actor='offline-test')

    def correct(self, identity, path, hours):
        from policy_lifecycle import correct_policy
        text = path.read_text().replace(identity, identity+'-rev2').replace('2026-11-v1', '2026-11-v2')
        old_hours = '336' if identity == SHOES else '168'
        text = text.replace(old_hours+'小时', str(hours)+'小时')
        replacement = self.folder/(identity+'-rev2.md')
        replacement.write_text(text)
        return correct_policy(self.engine, identity, replacement, expected_checksum=self.document(identity)['checksum'],
                              reason='独立测试：修正时限', actor='offline-test')

    def support(self):
        return check_buyer_support(self.engine, 'O_SUPPORT_OK', buyer_id='B001', claims=CLAIMS, now=NOW)

    def general(self, **changes):
        return lookup_general_policy(self.engine, **({'store_id':'S001', 'category':'ordinary_shoes', 'policy_at':AT}|changes))

    def catalog(self):
        return check_catalog_policy(self.engine, store_id='DEMO-SHOP-A', category='t_shirt', is_custom=False, policy_at=AT)

    def recommend(self, require_returnable):
        snapshot = CatalogSnapshot.load(ROOT/'samples/catalog_policy_demo.json')
        return recommend(snapshot, {'category':'t_shirt', 'currency':'USD', 'budget_max_minor':3000,
            'size':'M', 'material':'cotton', 'cotton_percentage':100, 'require_returnable':require_returnable},
            scope=CatalogScope(frozenset({'DEMO-SHOP-A'}),'US','demo_storefront'),
            as_of=datetime.fromisoformat(snapshot.data()['snapshot_at']),
            policy_lookup=lambda **scope: check_catalog_policy(self.engine, **scope))

    def test_withdrawn_support_retains_order_but_stops_eligibility_and_shipping(self):
        before = self.support()
        order_snapshot = self.fixture.all_snapshot()
        support_snapshot = self.support_snapshot()
        policy_before = self.document(SHOES)
        self.assertEqual(before['eligibility'], 'eligible_with_self_report')
        self.withdraw(SHOES)
        after = self.support()
        self.assertEqual(after['status'], 'insufficient_evidence')
        self.assertEqual(after['policy_status'], 'withdrawn')
        self.assertEqual(after['reason'], 'applicable_policy_withdrawn')
        self.assertEqual(after['policy_lifecycle']['document_id'], SHOES)
        self.assertIn('已撤回', after['message'])
        self.assertEqual(after['eligibility'], 'undetermined')
        self.assertIsNone(after['shipping']['payer'])
        self.assertEqual(after['citations'], {})
        self.assertNotIn('return_window', after)
        self.assertEqual(after['order'], before['order'])
        self.assertEqual(after['trusted_facts'], before['trusted_facts'])
        self.assertEqual(self.fixture.all_snapshot(), order_snapshot)
        self.assertEqual(self.support_snapshot(), support_snapshot)
        self.assertEqual(self.document(SHOES), policy_before)
        self.assertEqual(before['eligibility'], 'eligible_with_self_report')

    def test_general_withdrawal_does_not_substitute_older_rules_or_advertise_coverage(self):
        self.withdraw(SHOES)
        result = self.general()
        self.assertEqual(result['status'], 'withdrawn')
        self.assertEqual(result['policy_status'], 'withdrawn')
        for field in ('rules', 'citations', 'citation_index', 'claims'):
            self.assertFalse(result[field])
        self.assertNotIn('document', result)
        answer = render_general_policy(result, {'answer_style':'detailed'})
        self.assertIn('已撤回', answer)
        self.assertNotIn('168小时', answer)
        self.assertNotIn('336小时', answer)
        coverage = self.general(category=None)
        self.assertEqual(coverage['supported_categories'], [])
        self.assertEqual(coverage['reason'], 'no_supported_policy')
        # A different real interval remains usable; do not globally ban the shop.
        october = self.general(policy_at=datetime.fromisoformat('2026-10-15T12:00:00+08:00'))
        self.assertEqual(october['status'], 'ok')
        self.assertEqual(october['rules']['return_window_hours'], 168)

    def test_returnable_recommendation_stops_but_plain_product_facts_remain(self):
        before = self.recommend(True)
        self.assertTrue(before['candidates'])
        self.withdraw(TSHIRT)
        policy = self.catalog()
        self.assertEqual(policy['status'], 'unknown')
        self.assertEqual(policy['policy_status'], 'withdrawn')
        self.assertEqual(policy['reason'], 'applicable_policy_withdrawn')
        self.assertIn('已撤回', policy['message'])
        self.assertIsNone(policy['document_id'])
        self.assertIsNone(policy['document'])
        self.assertFalse(policy['claims'])
        self.assertFalse(policy['citation_index'])
        stopped = self.recommend(True)
        self.assertEqual(stopped['candidates'], [])
        self.assertFalse(stopped.get('return_policies'))
        self.assertTrue(stopped['return_policy_checks'])
        self.assertTrue(all(x['reason']=='applicable_policy_withdrawn' for x in stopped['return_policy_checks']))
        plain = self.recommend(False)
        self.assertTrue(plain['candidates'])
        self.assertTrue(all(x['return_policy_status']=='unknown' for x in plain['candidates']))
        self.assertFalse(plain.get('return_policies'))
        self.assertEqual(plain['source'], before['source'])

    def test_withdrawal_is_scoped_to_document_not_other_store_or_category(self):
        original = self.catalog()
        self.withdraw(SHOES)
        self.assertEqual(self.catalog(), original)
        self.assertEqual(self.catalog()['status'], 'conditional')

    def test_corrected_policy_used_for_new_support_preserving_original_source(self):
        before = self.support()
        original = self.document(SHOES)
        self.correct(SHOES, self.paths[1], 168)
        after = self.support()
        self.assertEqual(after['policy_version'], '2026-11-v2')
        self.assertEqual(after['order'], before['order'])
        self.assertEqual(after['trusted_facts'], before['trusted_facts'])
        self.assertNotEqual(after['return_window']['before'], before['return_window']['before'])
        self.assertTrue(all(value['document_id']==SHOES+'-rev2' for value in after['citations'].values()))
        self.assertEqual(self.document(SHOES), original)
        general = self.general()
        self.assertEqual(general['rules']['return_window_hours'], 168)
        self.assertEqual(general['document']['document_id'], SHOES+'-rev2')
        self.assertEqual(before['policy_version'], '2026-11-v1')

    def test_corrected_catalog_uses_replacement_citations_not_withdrawn_ones(self):
        before = self.catalog()
        self.correct(TSHIRT, self.paths[2], 336)
        after = self.catalog()
        self.assertEqual(after['status'], 'conditional')
        self.assertEqual(after['document_id'], TSHIRT+'-rev2')
        self.assertEqual(after['rules']['return_window_hours'], 336)
        self.assertFalse(set(after['citation_index']) & set(before['citation_index']))
        result = self.recommend(True)
        self.assertTrue(result['candidates'])
        self.assertEqual(set(result['return_policies']), {TSHIRT+'-rev2'})

    def test_unavailable_replacement_contract_does_not_resurrect_attached_old_rules(self):
        context = get_support_context(self.engine, 'O_SUPPORT_OK', buyer_id='B001')
        scope = {'store_id':'S001', 'category':'ordinary_shoes', 'paid_at':datetime.fromisoformat(context['order']['paid_at'])}
        original = select_policy(self.engine, **scope)
        for status in ('withdrawn', 'superseded_without_valid_replacement'):
            with self.subTest(status=status):
                # Deliberately attach stale original clauses: non-ok must still block their use.
                bundle = deepcopy(original)
                bundle.update(status=status, reason='applicable_policy_withdrawn', lifecycle={'document_id':SHOES})
                result = evaluate_buyer_support(context, bundle, claims=CLAIMS, now=NOW)
                self.assertEqual(result['eligibility'], 'undetermined')
                self.assertFalse(result['citations'])
                self.assertEqual(result['policy_status'], status)
                with patch('general_policy.select_policy', return_value=bundle):
                    general = self.general()
                    self.assertFalse(general['claims'])
                    self.assertEqual(general['policy_status'], status)
                with patch('catalog_policy.select_policy', return_value=bundle):
                    catalog = self.catalog()
                    self.assertFalse(catalog['claims'])
                    self.assertFalse(catalog['citation_index'])
                    self.assertEqual(catalog['policy_status'], status)

    def test_unauthorized_order_does_not_disclose_lifecycle_or_call_selector(self):
        self.withdraw(SHOES)
        with patch('policy_kb.select_policy', side_effect=AssertionError('unauthorized selector call')):
            result = check_buyer_support(self.engine, 'O_SUPPORT_OK', buyer_id='B_OTHER', claims=CLAIMS, now=NOW)
        self.assertEqual(result['status'], 'not_found')
        self.assertFalse(result['order'])
        self.assertFalse(result['trusted_facts'])
        self.assertNotIn('policy_lifecycle', result)


if __name__ == '__main__':
    unittest.main()
