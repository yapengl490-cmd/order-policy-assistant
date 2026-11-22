"""商品政策关联的接口验收；真实业务函数/隔离库，模型HTTP为替身。"""
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import delete, update
from sqlalchemy.exc import OperationalError

import test_shopping_chat as fixture
from policy_kb import import_policy_files, policy_documents, policy_chunks
from catalog_recommendations import CatalogSnapshot
from condition_state import reconcile_conditions


ROOT = Path(__file__).resolve().parents[1]
QUESTION = "推荐M码纯棉T恤，预算不超过30美元，还要求能退"
DOC = "DEMO-SHOP-A-tshirt-2026-11"


class CatalogPolicyChatTests(unittest.TestCase):
    # 只复用无业务断言的隔离入口；不继承并重复执行旧测试组。
    auth = fixture.ShoppingChatHTTPTests.auth
    actual_agent = fixture.ShoppingChatHTTPTests.actual_agent
    forbidden_legacy_branch = fixture.ShoppingChatHTTPTests.forbidden_legacy_branch
    client = fixture.ShoppingChatHTTPTests.client
    ask = fixture.ShoppingChatHTTPTests.ask
    export = fixture.ShoppingChatHTTPTests.export

    def setUp(self):
        self.forge_selection = False
        fixture.ShoppingChatHTTPTests.setUp(self)
        self.settings = replace(self.settings, catalog_policy_demo_enabled=True)
        import_policy_files(self.engine, [ROOT / 'knowledge/2026-11-catalog-tshirts.md'])

    def model_http(self, request):
        response = fixture.ShoppingChatHTTPTests.model_http(self, request)
        body = response.json()
        if self.forge_selection and body['choices'][0]['finish_reason'] == 'stop':
            body['choices'][0]['message']['content'] = json.dumps({'selected_variant_ids': ['V-FORGED']})
            return httpx.Response(200, json=body)
        return response

    @staticmethod
    def catalog(task):
        return next(f['result'] for f in task['facts'] if f['tool'] == 'recommend_products')

    def test_positive_link_excludes_custom_and_unknown_and_keeps_clause_sources(self):
        with self.client() as (client, _):
            task = self.ask(client, QUESTION)
            self.assertEqual(task['status'], 'completed', task)
            fact = self.catalog(task)
            self.assertEqual([v['variant_id'] for v in fact['candidates']], ['V-A'])
            self.assertEqual(fact['candidates'][0]['price']['currency'], 'USD')
            self.assertEqual(fact['candidates'][0]['price']['amount_minor'], 2500)
            self.assertEqual(fact['candidates'][0]['return_eligibility'], 'unknown')
            self.assertEqual(fact['candidates'][0]['return_policy_status'], 'conditional')
            checks = {item['variant_id']: item for item in fact['return_policy_checks']}
            self.assertEqual(checks['V-B']['status'], 'excluded')
            self.assertEqual(checks['V-BOUNDARY']['status'], 'unknown')
            self.assertNotIn('V-OTHER-SHOP', checks)
            policy = fact['return_policies'][DOC]
            self.assertEqual(policy['store_id'], 'DEMO-SHOP-A')
            self.assertEqual(policy['category'], 't_shirt')
            self.assertEqual(policy['rules']['return_window_hours'], 168)
            self.assertEqual(policy['applicability']['selection_basis'], 'catalog_policy_at')
            self.assertNotIn('paid_at', policy['applicability'])
            self.assertEqual(policy['policy_at'], fact['evaluated_at'])
            for expected in ('25.00 USD', '未穿着', '吊牌完好', '定制商品不适用', '买家承担', '不是退货批准', '支付时适用版本'):
                self.assertIn(expected, task['answer'])
            self.assertNotIn('保证可退', task['answer'])
            self.assertNotIn('S001-shoes', task['answer'])
            source = (ROOT / 'knowledge/2026-11-catalog-tshirts.md').read_text()
            for ref, citation in policy['citation_index'].items():
                self.assertIn('[' + ref + ']', task['answer'])
                self.assertEqual(citation['text'], '\n'.join(source.splitlines()[citation['line_start']-1:citation['line_end']]))
                self.assertEqual(citation['document_checksum'], hashlib.sha256(source.encode()).hexdigest())
            self.assertTrue(task['shopping_validation']['passed'])
            self.assertEqual(len(self.requests), 2)

    def test_no_return_requirement_does_not_hide_price_candidates_or_claim_eligibility(self):
        with self.client() as (client, _):
            task = self.ask(client, "推荐M码纯棉T恤，预算不超过30美元")
            self.assertEqual(task['status'], 'completed', task)
            fact = self.catalog(task)
            self.assertEqual([v['variant_id'] for v in fact['candidates']], ['V-A', 'V-B', 'V-BOUNDARY'])
            self.assertEqual([v['return_policy_status'] for v in fact['candidates']], ['conditional', 'excluded', 'unknown'])
            self.assertIn('已知为定制规格', task['answer'])
            self.assertIn('未确认满足退货要求', task['answer'])
            self.assertTrue(all(v['return_eligibility'] == 'unknown' for v in fact['candidates']))

    def test_currency_clarification_resumes_return_condition_then_material_change_preserves_it(self):
        with self.client() as (client, _):
            first = self.ask(client, "推荐M码纯棉T恤，预算30，还要求能退")
            self.assertEqual(first['status'], 'clarification', first)
            self.assertEqual(len(self.requests), 0)
            second = self.ask(client, '美元')
            self.assertEqual(second['status'], 'completed', second)
            self.assertEqual([v['variant_id'] for v in self.catalog(second)['candidates']], ['V-A'])
            third = self.ask(client, '改为M码涤纶T恤，预算20美元')
            self.assertEqual(third['status'], 'completed', third)
            fact = self.catalog(third)
            self.assertTrue(fact['requirements']['require_returnable'])
            self.assertEqual(fact['requirements']['material'], 'polyester')
            self.assertEqual(fact['candidates'], [])
            self.assertEqual(fact['return_policy_checks'][0]['variant_id'], 'V-POLY')
            self.assertEqual(fact['return_policy_checks'][0]['reason'], 'unknown_custom_status')
            self.assertIn('没有已核实满足全部条件', third['answer'])

    def test_source_history_and_export_consistent_with_cross_account_access_denied(self):
        with self.client() as (client, _):
            task = self.ask(client, QUESTION)
            exported = self.export(client, task)
            self.assertEqual(exported['answer'], task['answer'])
            self.assertEqual(exported['result']['facts'], task['facts'])
            self.assertEqual(exported['result']['history'][-1]['content'], task['answer'])
            again = client.get('/api/tasks/' + task['id'], headers=self.auth()).json()
            self.assertEqual(again['facts'], task['facts'])
            self.assertEqual(again['answer'], task['answer'])
            raw = (ROOT / 'samples/catalog_policy_demo.json').read_bytes()
            self.assertEqual(self.catalog(task)['source']['sha256'], hashlib.sha256(raw).hexdigest())
            for suffix in ('', '/export'):
                denied = client.get('/api/tasks/' + task['id'] + suffix, headers=self.auth('B002'))
                self.assertEqual(denied.status_code, 404)
                self.assertNotIn('V-A', denied.text)

    def test_missing_tshirt_policy_does_not_reuse_shoe_policy(self):
        with self.engine.begin() as connection:
            connection.execute(delete(policy_chunks).where(policy_chunks.c.document_id == DOC))
            connection.execute(delete(policy_documents).where(policy_documents.c.document_id == DOC))
        with self.client() as (client, _):
            task = self.ask(client, QUESTION)
            self.assertEqual(task['status'], 'completed', task)
            fact = self.catalog(task)
            self.assertEqual(fact['candidates'], [])
            self.assertTrue(all(item['reason'] == 'no_applicable_policy' for item in fact['return_policy_checks']))
            self.assertEqual(fact.get('return_policies', {}), {})
            self.assertNotIn('336', task['answer'])
            self.assertNotIn('S001', json.dumps(fact))

    def test_snapshot_time_at_policy_end_does_not_keep_expired_return_terms(self):
        source = json.loads((ROOT / 'samples/catalog_policy_demo.json').read_text())
        # 隔离副本将所有观察时刻同步改变，排除库存过期掩盖政策截止边界。
        source['snapshot_at'] = '2026-12-01T00:00:00+08:00'
        for product in source['products']:
            for variant in product['variants']:
                variant['observed_at'] = source['snapshot_at']
        snapshot = CatalogSnapshot('offline-boundary-catalog.json', json.dumps(source).encode())
        with patch('catalog_recommendations.CatalogSnapshot.load', return_value=snapshot):
            with self.client() as (client, _):
                task = self.ask(client, QUESTION)
                self.assertEqual(task['status'], 'completed', task)
                fact = self.catalog(task)
                self.assertEqual(fact['candidates'], [])
                self.assertTrue(fact['return_policy_checks'])
                self.assertTrue(all(item['reason'] == 'no_applicable_policy' for item in fact['return_policy_checks']))
                self.assertEqual(fact.get('return_policies', {}), {})
                self.assertEqual(fact['evaluated_at'], '2026-11-30T16:00:00+00:00')
                self.assertNotIn('168小时', task['answer'])

    def test_fabricated_selection_gets_one_repair_then_factual_fallback(self):
        self.forge_selection = True
        with self.client() as (client, _):
            task = self.ask(client, QUESTION)
            self.assertEqual(task['status'], 'completed', task)
            self.assertFalse(task['shopping_validation']['passed'])
            self.assertTrue(task['shopping_validation']['repair_attempted'])
            self.assertIn('unknown_selection_or_citation', task['shopping_validation']['errors'])
            self.assertNotIn('V-FORGED', task['answer'])
            self.assertIn('25.00 USD', task['answer'])
            self.assertEqual(len(self.requests), 3)
            self.assertNotIn('tools', self.requests[-1])
            self.assertEqual(task['usage']['tool_calls'], 1)

    def test_policy_storage_failure_is_partial_not_normal_absence(self):
        with patch('catalog_policy.select_policy', side_effect=OperationalError('offline failure', {}, RuntimeError('unavailable'))):
            with self.client() as (client, _):
                task = self.ask(client, QUESTION)
                self.assertEqual(task['status'], 'partial', task)
                fact = self.catalog(task)
                self.assertEqual(fact['candidates'], [])
                self.assertTrue(fact['policy_errors'])
                self.assertIn('未完成', task['answer'])

    def test_saved_policy_chunk_corruption_is_partial_and_not_silently_replaced(self):
        with self.engine.begin() as connection:
            connection.execute(update(policy_chunks).where(policy_chunks.c.document_id == DOC)
                               .values(text='tampered source not a real policy'))
        with self.client() as (client, _):
            task = self.ask(client, QUESTION)
            self.assertEqual(task['status'], 'partial', task)
            fact = self.catalog(task)
            self.assertEqual(fact['candidates'], [])
            self.assertEqual(fact.get('return_policies', {}), {})
            self.assertTrue(fact['policy_errors'])
            self.assertNotIn('tampered', task['answer'])
            self.assertIn('未完成', task['answer'])

    def test_policy_followup_keeps_catalog_scope_and_sources_without_requesting_order(self):
        with self.client() as (client, _):
            for index, message in enumerate(('这款商品退货运费谁承担？', '那退货政策是什么？')):
                with self.subTest(message=message):
                    session = 'policy-followup-' + str(index)
                    first = self.ask(client, QUESTION, session=session)
                    self.assertEqual(first['status'], 'completed', first)
                    followup = self.ask(client, message, session=session)
                    self.assertEqual(followup['status'], 'completed', followup)
                    self.assertEqual(followup['conditions']['kind'], 'catalog')
                    fact = self.catalog(followup)
                    self.assertTrue(fact['policy_followup'])
                    self.assertEqual([v['variant_id'] for v in fact['candidates']], ['V-A'])
                    self.assertTrue(fact['requirements']['require_returnable'])
                    self.assertEqual(set(fact['return_policies']), {DOC})
                    for required in ('未穿着', '吊牌完好', '定制商品不适用', '买家承担'):
                        self.assertIn(required, followup['answer'])
                    self.assertNotIn('订单编号', followup['answer'])
                    self.assertNotIn('S001', json.dumps(fact))
                    self.assertEqual(self.export(client, followup)['answer'], followup['answer'])

    def test_policy_question_preserves_unset_or_cancelled_return_filter_and_all_candidates(self):
        with self.client() as (client, _):
            for index, initial in enumerate(('推荐M码纯棉T恤，预算不超过30美元',
                                            '推荐M码纯棉T恤，预算不超过30美元，不要求退货')):
                with self.subTest(initial=initial):
                    session = 'unfiltered-policy-' + str(index)
                    first = self.ask(client, initial, session=session)
                    self.assertEqual(first['status'], 'completed', first)
                    prior = first['conditions']['confirmed'].get('require_returnable')
                    followup = self.ask(client, '那退货政策是什么？', session=session)
                    self.assertEqual(followup['status'], 'completed', followup)
                    self.assertEqual(followup['conditions']['confirmed'].get('require_returnable'), prior)
                    fact = self.catalog(followup)
                    self.assertFalse(fact['requirements'].get('require_returnable', False))
                    self.assertEqual([v['variant_id'] for v in fact['candidates']], ['V-A', 'V-B', 'V-BOUNDARY'])
                    self.assertIn('多个候选', followup['answer'])
                    self.assertIn('未替你', followup['answer'])
                    self.assertIn('已知为定制规格', followup['answer'])
                    self.assertIn('未确认满足退货要求', followup['answer'])

    def test_explicit_order_followup_leaves_catalog_and_does_not_guess_order_conditions(self):
        with self.client() as (client, _):
            first = self.ask(client, QUESTION)
            self.assertEqual(first['status'], 'completed', first)
            before = len(self.requests)
            followup = self.ask(client, '我的订单O_SUPPORT_OK退货政策是什么，判断时间不确定')
            self.assertEqual(followup['status'], 'clarification', followup)
            self.assertEqual(followup['conditions']['kind'], 'support')
            self.assertTrue(followup['conditions']['switched'])
            self.assertEqual(followup['conditions']['confirmed']['order_id']['value'], 'O_SUPPORT_OK')
            self.assertNotIn('budget_max_minor', followup['conditions']['confirmed'])
            self.assertEqual(followup['facts'], [])
            self.assertEqual(len(self.requests), before)
            self.assertEqual(self.invocations[-1]['history'], [])

    def test_cross_shop_and_unknown_new_requirement_cannot_hide_in_policy_followup(self):
        with self.client() as (client, _):
            for index, message in enumerate(('这款商品退货运费谁承担，跨店查询',
                                             '这款商品退货运费谁承担，还要适合骑行')):
                with self.subTest(message=message):
                    session = 'unsafe-followup-' + str(index)
                    first = self.ask(client, QUESTION, session=session)
                    self.assertEqual(first['status'], 'completed', first)
                    before = len(self.requests)
                    followup = self.ask(client, message, session=session)
                    self.assertEqual(followup['status'], 'clarification', followup)
                    self.assertFalse(followup['conditions'].get('policy_followup', False))
                    self.assertEqual(followup['facts'], [])
                    self.assertEqual(len(self.requests), before)

    def test_expired_catalog_session_does_not_reuse_recommendation_for_policy_question(self):
        with self.client() as (client, _):
            for index, message in enumerate(('这款商品退货运费谁承担？', '那退货政策是什么？')):
                with self.subTest(message=message):
                    session = 'expired-followup-' + str(index)
                    first = self.ask(client, QUESTION, session=session)
                    self.assertEqual(first['status'], 'completed', first)
                    before = len(self.requests)
                    # 只推进条件层时钟，不等待、不修改存档，也不复用过期条件。
                    def after_expiration(message, previous, now, turn_id):
                        return reconcile_conditions(message, previous, now + timedelta(hours=1), turn_id)
                    with patch('app_server.reconcile_conditions', side_effect=after_expiration):
                        followup = self.ask(client, message, session=session)
                    self.assertEqual(followup['status'], 'clarification', followup)
                    self.assertTrue(followup['conditions']['expired'])
                    self.assertFalse(followup['conditions'].get('policy_followup', False))
                    for field in ('category', 'currency', 'budget_max_minor', 'material', 'size'):
                        self.assertNotIn(field, followup['conditions']['confirmed'])
                    self.assertEqual(followup['facts'], [])
                    self.assertEqual(len(self.requests), before)
                    self.assertEqual(self.invocations[-1]['history'], [])


if __name__ == '__main__':
    unittest.main()
