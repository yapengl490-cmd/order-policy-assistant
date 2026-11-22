"""独立SQLite验证商品与条款关联；不连接真实库、模型或Shopify。"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, insert, select, update

from catalog_policy import check_catalog_policy
from policy_documents import CATEGORY, PolicyDocumentError, parse_policy_text
from policy_kb import metadata, import_policy_files, select_policy, policy_documents, policy_chunks


ROOT = Path(__file__).resolve().parents[1]
AT = datetime.fromisoformat('2026-11-20T12:00:00+08:00')
SCOPE = {'store_id': 'DEMO-SHOP-A', 'category': 't_shirt', 'policy_at': AT}


class CatalogPolicyTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite+pysqlite:///:memory:').execution_options(schema_translate_map={'public': None})
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.original = (ROOT/'knowledge/2026-11-shoes.md').read_text()
        self.source = (self.original.replace('S001', 'DEMO-SHOP-A').replace('shoes', 'tshirts')
                       .replace('ordinary_tshirts', 't_shirt').replace('普通鞋类', 'T恤'))
        self.path = Path(folder.name)/'policy.md'; self.path.write_text(self.source)
        import_policy_files(self.engine, [self.path])
        network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        network.start(); self.addCleanup(network.stop)

    def check(self, **changes):
        return check_catalog_policy(self.engine, **(SCOPE | {'is_custom': False} | changes))

    def bundle(self):
        return select_policy(self.engine, store_id=SCOPE['store_id'], category=SCOPE['category'], paid_at=AT)

    def test_normal_has_exact_rules_sources_and_purchase_time_boundary(self):
        got = self.check()
        self.assertEqual(got['status'], 'conditional')
        self.assertEqual(got['reason'], 'applicable_return_policy')
        self.assertEqual(got['policy_version'], '2026-11-v1')
        self.assertEqual(got['rules']['allowed_categories'], ['t_shirt'])
        self.assertEqual(got['selection_basis'], 'catalog_policy_at')
        self.assertEqual(got['applicability']['selection_basis'], 'catalog_policy_at')
        self.assertNotIn('paid_at', got['applicability'])
        self.assertEqual(got['order_eligibility'], 'not_evaluated')
        self.assertFalse(got['refund_execution'])
        self.assertEqual(got['model_requests'], 0)
        self.assertEqual(got['embedding_requests'], 0)

    def test_claims_keep_conditions_exceptions_shipping_and_source_lines(self):
        got = self.check(); text = '\n'.join(c['text'] for c in got['claims'])
        for expected in ['336小时', '未穿着且吊牌完好', '定制商品不适用', '运费由买家承担', '已核实']:
            self.assertIn(expected, text)
        lines = self.source.splitlines()
        for claim in got['claims']:
            self.assertTrue(claim['citation_ids'])
            for identity in claim['citation_ids']:
                cited = got['citation_index'][identity]
                self.assertEqual(cited['category'], 't_shirt')
                self.assertEqual(cited['store_id'], 'DEMO-SHOP-A')
                self.assertEqual(cited['text'], '\n'.join(lines[cited['line_start']-1:cited['line_end']]))

    def test_known_custom_excluded_and_claims_remain_general(self):
        normal, custom = self.check(), self.check(is_custom=True)
        self.assertEqual(custom['status'], 'excluded')
        self.assertEqual(custom['reason'], 'custom_product_excluded')
        self.assertEqual(custom['claims'], normal['claims'])
        self.assertTrue(custom['decision_citation_ids'])
        self.assertIn('定制商品不适用', custom['citation_index'][custom['decision_citation_ids'][0]]['text'])

    def test_unknown_custom_keeps_policy_but_never_promotes_to_eligible(self):
        for value in [None, 'false', 0, 1, {}, []]:
            with self.subTest(value=value):
                got = self.check(is_custom=value)
                self.assertEqual(got['status'], 'unknown')
                self.assertEqual(got['reason'], 'unknown_custom_status')
                self.assertTrue(got['claims'])
                self.assertEqual(got['document_id'], self.check()['document_id'])

    def test_scope_does_not_fall_back_to_another_shop_or_category(self):
        for changes in [{'store_id': 'DEMO-SHOP-B'}, {'category': 'ordinary_shoes'}]:
            with self.subTest(changes=changes):
                got = self.check(**changes)
                self.assertEqual(got['status'], 'unknown')
                self.assertEqual(got['reason'], 'no_applicable_policy')
                self.assertFalse(got['citation_index'])
                self.assertIsNone(got['document'])

    def test_start_inclusive_and_end_exclusive(self):
        for when, expected in [('2026-10-31T23:59:59+08:00', 'unknown'),
                               ('2026-11-01T00:00:00+08:00', 'conditional'),
                               ('2026-11-30T23:59:59+08:00', 'conditional'),
                               ('2026-12-01T00:00:00+08:00', 'unknown')]:
            with self.subTest(when=when):
                self.assertEqual(self.check(policy_at=datetime.fromisoformat(when))['status'], expected)

    def test_invalid_scope_and_time_do_not_read_database(self):
        with patch('catalog_policy.select_policy', side_effect=AssertionError('not allowed')):
            for changes in [{'store_id': None}, {'store_id': '../S001'}, {'category': []},
                            {'category': 'shirts'}, {'policy_at': None}, {'policy_at': AT.isoformat()},
                            {'policy_at': datetime(2026, 11, 20)}]:
                with self.subTest(changes=changes):
                    self.assertEqual(self.check(**changes)['status'], 'unknown')

    def test_no_library_is_unknown_not_no_returns(self):
        engine = create_engine('sqlite+pysqlite:///:memory:').execution_options(schema_translate_map={'public': None})
        self.addCleanup(engine.dispose)
        got = check_catalog_policy(engine, **SCOPE, is_custom=False)
        self.assertEqual(got['status'], 'unknown')
        self.assertEqual(got['reason'], 'policy_storage_unavailable')

    def test_conflicting_versions_are_unknown_without_sources(self):
        with self.engine.begin() as conn:
            row = dict(conn.execute(select(policy_documents)).mappings().one())
            row.update(document_id='conflicting-version', version='another-version')
            conn.execute(insert(policy_documents).values(**row))
        got = self.check()
        self.assertEqual(got['status'], 'unknown')
        self.assertEqual(got['reason'], 'overlapping_policy_versions')
        self.assertFalse(got['claims'])

    def test_damaged_stored_clause_never_used(self):
        with self.engine.begin() as conn:
            conn.execute(update(policy_chunks).where(policy_chunks.c.section == 'return_eligibility')
                         .values(text='任何商品都能退'))
        got = self.check()
        self.assertEqual(got['status'], 'unknown')
        self.assertEqual(got['reason'], 'stored_content_mismatch')
        self.assertFalse(got['citation_index'])

    def test_untrusted_source_kind_is_unknown(self):
        with self.engine.begin() as conn:
            conn.execute(update(policy_documents).values(source_kind='real_platform'))
        self.assertEqual(self.check()['status'], 'unknown')

    def test_returned_scope_metadata_is_rechecked(self):
        good = self.bundle()
        mutations = [lambda x: x['document'].update(store_id='OTHER'),
                     lambda x: x['document'].update(category='ordinary_shoes'),
                     lambda x: x['document'].update(effective_before='2026-11-02T00:00:00+00:00'),
                     lambda x: x['applicability'].update(store_id='OTHER'),
                     lambda x: x.update(payment_version='made-up')]
        for mutate in mutations:
            bad = deepcopy(good); mutate(bad)
            with self.subTest(bad=bad['payment_version']), patch('catalog_policy.select_policy', return_value=bad):
                got = self.check()
                self.assertEqual(got['status'], 'unknown')
                self.assertFalse(got['claims'])

    def test_returned_rules_citation_and_chunks_are_rechecked(self):
        good = self.bundle()
        mutations = [lambda x: x['rules'].update(return_window_hours=999),
                     lambda x: x['rule_citations']['return_window_hours'].update(text='无限期可退'),
                     lambda x: x['chunks'].pop()]
        for mutate in mutations:
            bad = deepcopy(good); mutate(bad)
            with patch('catalog_policy.select_policy', return_value=bad):
                self.assertEqual(self.check()['reason'], 'invalid_policy')

    def test_unrecognized_bundle_is_unknown(self):
        for bundle in [None, {}, {'status': 'ok'}, {'status': 'conflict', 'reason': 'private data'}]:
            with self.subTest(bundle=bundle), patch('catalog_policy.select_policy', return_value=bundle):
                got = self.check()
                self.assertEqual(got['status'], 'unknown')
                self.assertFalse(got['claims'])
                self.assertNotIn('private data', str(got))

    def test_returned_data_are_detached_from_reused_bundle(self):
        bundle = self.bundle(); before = deepcopy(bundle)
        with patch('catalog_policy.select_policy', return_value=bundle):
            result = self.check(); result['rules']['return_window_hours'] = 1
            result['claims'][0]['text'] = 'changed'
        self.assertEqual(bundle, before)

    def test_old_shoes_category_and_original_source_unchanged(self):
        self.assertEqual(CATEGORY, 'ordinary_shoes')
        doc = parse_policy_text(self.original)
        self.assertEqual(doc['rules']['allowed_categories'], ['ordinary_shoes'])
        self.assertEqual(doc['rule_citations']['return_window_hours']['category'], 'ordinary_shoes')
        self.assertEqual((ROOT/'knowledge/2026-11-shoes.md').read_text(), self.original)

    def test_tshirt_metadata_and_scope_label_must_both_match(self):
        self.assertEqual(parse_policy_text(self.source)['rules']['allowed_categories'], ['t_shirt'])
        for source in [self.source.replace('适用品类：T恤（t_shirt）', '适用品类：普通鞋类（ordinary_shoes）'),
                       self.source.replace('"category": "t_shirt"', '"category": "shirts"'),
                       self.source.replace('"category": "t_shirt"', '"category": []')]:
            with self.subTest(source=source[:100]), self.assertRaises(PolicyDocumentError):
                parse_policy_text(source)

    def test_tshirt_still_rejects_missing_exception_or_new_semantics(self):
        for source in [self.source.replace('例外：定制商品不适用本条退货资格。', ''),
                       self.source.replace('未穿着且吊牌完好', '未穿着或吊牌完好'),
                       self.source.replace('买家承担', '商家承担'),
                       self.source.replace('"source_kind": "synthetic_demo"', '"source_kind": "real_platform"')]:
            with self.assertRaises(PolicyDocumentError):
                parse_policy_text(source)


if __name__ == '__main__':
    unittest.main()
