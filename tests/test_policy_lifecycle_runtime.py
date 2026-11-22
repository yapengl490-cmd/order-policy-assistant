"""独立内存数据库及模型响应替身：验证执行期间撤回，不访问正式库或网络。"""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

import httpx

import test_support_collaboration as support_fixture
import test_shopping_runtime as shopping_fixture
from test_buyer_support_service import SupportFixture
from policy_kb import search_policy_chunks, select_policy
from policy_lifecycle import withdraw_policy, correct_policy
from support_runtime import (check_policy_references, policy_lifecycle_blocks,
                             review_support_context)
from support_claims import build_support_claim_catalog

ROOT = Path(__file__).resolve().parents[1]


class LifecycleActions:
    def withdraw(self):
        source = next(iter(self.fact['citation_index'].values()))
        return withdraw_policy(self.engine, source['document_id'],
            expected_checksum=source['document_checksum'], reason='隔离测试人工撤回', actor='offline-test')

    def correct(self):
        source = next(iter(self.fact['citation_index'].values()))
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        body = (ROOT/'knowledge/2026-11-shoes.md').read_text().replace(
            'S001-shoes-2026-11', 'S001-shoes-2026-11-r2').replace('2026-11-v1', '2026-11-v2').replace('336小时', '240小时')
        path = Path(temp.name)/'corrected.md'; path.write_text(body)
        return correct_policy(self.engine, source['document_id'], path,
            expected_checksum=source['document_checksum'], reason='隔离测试更正期限', actor='offline-test')

    def assert_closed(self, got, *, changed='withdrawn'):
        self.assertEqual(got['status'], 'partial')
        self.assertEqual(got['evidence_status'], 'partial_tool_results')
        self.assertTrue(got['facts'])
        guard = next(row for row in got['trace'] if row.get('stage') == 'policy_lifecycle_guard')
        self.assertEqual(guard['check']['status'], changed)
        self.assertFalse(guard['check']['current_policy_verified'])
        self.assertIn('事实保留', got['answer'])
        self.assertNotIn('暂满足', got['answer'])
        self.assertNotIn('运费由买家承担', got['answer'])
        self.assertNotIn('336小时', got['answer'])
        for fact in got['facts']:
            self.assertEqual(fact['result_snapshot_sha256'], hashlib.sha256(json.dumps(
                fact['result'], ensure_ascii=False, sort_keys=True).encode()).hexdigest())


class SupportLifecycleRuntimeTests(LifecycleActions, SupportFixture, unittest.TestCase):
    # 复用独立数据夹具及HTTP替身，不继承其测试项目，避免重复统计旧测试。
    setUp = support_fixture.SupportCollaborationTests.setUp
    cfg = staticmethod(support_fixture.SupportCollaborationTests.cfg)
    tool = staticmethod(support_fixture.SupportCollaborationTests.tool)
    final = staticmethod(support_fixture.SupportCollaborationTests.final)
    body = support_fixture.SupportCollaborationTests.body
    run_case = support_fixture.SupportCollaborationTests.run_case

    def run_single(self, responses=None, after=None):
        from agent_runtime import run_agent
        replies = responses or [self.tool('review_after_sales', {'order_id': 'O_SUPPORT_OK'}), self.final(self.body())]
        requests = []
        def transport(request):
            requests.append(json.loads(request.content))
            if after: after(len(requests))
            reply = replies[len(requests)-1]
            if isinstance(reply, Exception): raise reply
            return httpx.Response(200, json=reply)
        got = run_agent(self.cfg(), self.engine, support_fixture.QUESTION, 'B001', [], lambda: False,
            condition_state=self.state, transport=httpx.MockTransport(transport),
            tool_handlers={'review_after_sales': lambda **kwargs: deepcopy(self.fact)})
        return got, requests

    def test_unchanged_real_policy_reference_and_normal_single_final(self):
        check = check_policy_references(self.engine, self.fact)
        self.assertEqual(check['status'], 'ok'); self.assertTrue(check['current_policy_verified'])
        got, requests = self.run_single()
        self.assertEqual(got['status'], 'completed'); self.assertEqual(len(requests), 2)
        self.assertTrue(got['support_validation']['passed'])

    def test_retrieval_withdrawal_cannot_reuse_first_ok_rules(self):
        def during_search(*args, **kwargs):
            self.withdraw()
            return search_policy_chunks(*args, **kwargs)
        with patch('policy_kb.search_policy_chunks', side_effect=during_search):
            got = review_support_context(self.engine, self.context, state=self.state, keyword_only=True)
        self.assertEqual(got['status'], 'insufficient_evidence')
        self.assertEqual(got['policy_status'], 'withdrawn')
        self.assertEqual(got['order'], self.context['order'])
        self.assertEqual(got['eligibility'], 'undetermined')
        self.assertIsNone(got['shipping']['payer'])
        self.assertFalse(got['citation_index']); self.assertNotIn('applicable_rules', got)
        self.assertEqual(got['frozen_policy_audit']['snapshot']['rules']['return_window_hours'], 336)
        self.assertFalse(got['frozen_policy_audit']['is_current_policy'])
        claims = build_support_claim_catalog(got)
        self.assertTrue(claims['policy_source_unavailable']['required'])
        self.assertEqual(claims['policy_source_unavailable']['kind'], 'program_state')
        self.assertFalse(claims['policy_source_unavailable']['citation_ids'])
        self.assertNotIn('eligibility', claims); self.assertNotIn('shipping', claims)

    def test_retrieval_correction_stops_frozen_rules_instead_of_silent_upgrade(self):
        def during_search(*args, **kwargs):
            self.correct()
            return search_policy_chunks(*args, **kwargs)
        with patch('policy_kb.search_policy_chunks', side_effect=during_search):
            got = review_support_context(self.engine, self.context, state=self.state, keyword_only=True)
        self.assertEqual(got['policy_status'], 'corrected')
        self.assertEqual(got['eligibility'], 'undetermined')
        self.assertNotIn('applicable_rules', got)
        self.assertEqual(select_policy(self.engine, **self.scope)['rules']['return_window_hours'], 240)

    def test_same_version_followup_returns_withdrawn_not_plain_empty_results(self):
        from support_collaboration import lookup_frozen_policy_evidence
        self.fact['retrieval']['results'] = []
        def during_search(*args, **kwargs):
            self.withdraw()
            return search_policy_chunks(*args, **kwargs)
        with patch('policy_kb.search_policy_chunks', side_effect=during_search):
            got = lookup_frozen_policy_evidence(self.engine, self.context, self.fact)
        self.assertEqual(got['status'], 'withdrawn')
        self.assertFalse(got['results']); self.assertFalse(got['decision_changed'])

    def test_single_model_output_after_withdrawal_stops_without_repair(self):
        got, requests = self.run_single(after=lambda n: self.withdraw() if n == 2 else None)
        self.assert_closed(got); self.assertEqual(len(requests), 2)
        frozen = got['facts'][0]['result']
        self.assertEqual(frozen['order_id'], self.fact['order_id'])
        self.assertEqual(frozen['eligibility'], self.fact['eligibility'])
        self.assertEqual(frozen['citation_index'], self.fact['citation_index'])

    def test_single_model_output_after_correction_cannot_keep_old_policy(self):
        got, requests = self.run_single(after=lambda n: self.correct() if n == 2 else None)
        self.assert_closed(got, changed='corrected'); self.assertEqual(len(requests), 2)

    def test_single_repair_error_fallback_rechecks_before_rendering_old_facts(self):
        bad = self.body(); bad['claims'] = []
        replies = [self.tool('review_after_sales', {'order_id': 'O_SUPPORT_OK'}),
                   self.final(bad), httpx.ReadTimeout('offline test response interruption')]
        got, requests = self.run_single(replies, after=lambda n: self.withdraw() if n == 3 else None)
        self.assert_closed(got); self.assertEqual(len(requests), 3)
        self.assertEqual(got['usage']['tool_calls'], 1)
        self.assertTrue(got['support_validation']['repair_attempted'])

    def test_dependency_final_rechecks_policy_and_retains_frozen_order(self):
        got, requests, _ = self.run_case(after=lambda n, _: self.withdraw() if n == 3 else None)
        self.assert_closed(got); self.assertEqual(len(requests), 3)
        self.assertEqual(got['orchestration']['handoff']['sha256'], hashlib.sha256(json.dumps(
            self.context, ensure_ascii=False, sort_keys=True).encode()).hexdigest())

    def test_dependency_followup_withdrawal_stops_before_next_model(self):
        self.fact['retrieval']['results'] = []
        def lookup():
            self.withdraw()
            return {'status': 'withdrawn', 'reason': 'applicable_policy_withdrawn', 'results': []}
        got, requests, _ = self.run_case(responses=[self.tool('read_order_facts'), self.tool('review_frozen_policy'),
            self.tool('lookup_policy_evidence')], handlers={'lookup_policy_evidence': lookup})
        self.assert_closed(got); self.assertEqual(len(requests), 3)
        self.assertEqual(got['orchestration']['followup']['status'], 'withdrawn')

    def test_database_recheck_failure_is_closed_and_double_is_explicit_unverified(self):
        with patch('policy_lifecycle.validate_policy_reference', side_effect=RuntimeError('private database detail')):
            check = check_policy_references(self.engine, self.fact)
            got, requests = self.run_single(responses=[self.tool('review_after_sales', {'order_id': 'O_SUPPORT_OK'})])
        self.assertEqual(check['status'], 'check_failed'); self.assertTrue(policy_lifecycle_blocks(check))
        self.assertNotIn('private database detail', json.dumps(check))
        self.assert_closed(got, changed='check_failed'); self.assertEqual(len(requests), 1)
        no_db = check_policy_references(None, self.fact)
        self.assertEqual(no_db['status'], 'not_checked'); self.assertFalse(no_db['current_policy_verified'])


class ShoppingLifecycleRuntimeTests(LifecycleActions, unittest.TestCase):
    setUp = shopping_fixture.ShoppingRuntimeTests.setUp
    cfg = shopping_fixture.ShoppingRuntimeTests.cfg
    tool_reply = shopping_fixture.ShoppingRuntimeTests.tool_reply
    final = shopping_fixture.ShoppingRuntimeTests.final
    run_case = shopping_fixture.ShoppingRuntimeTests.run_case

    def test_general_policy_model_response_after_withdrawal_is_partial(self):
        got, requests, _ = self.run_case(after=lambda n, _: self.withdraw() if n == 2 else None)
        self.assert_closed(got); self.assertEqual(len(requests), 2)
        self.assertTrue(got['facts'][0]['result']['citation_index'])

    def test_general_policy_repair_after_correction_cannot_render_old_rules(self):
        replies = [self.tool_reply(), self.final({'citation_ids': []}), self.final()]
        got, requests, _ = self.run_case(replies, after=lambda n, _: self.correct() if n == 3 else None)
        self.assert_closed(got, changed='corrected'); self.assertEqual(len(requests), 3)

    def test_catalog_nested_policy_rechecked_after_model(self):
        from catalog_conditions import reconcile_catalog
        from shopping_runtime import run_shopping_agent
        state = reconcile_catalog('推荐M码棉质T恤，预算不超过30美元', None, shopping_fixture.NOW, 'catalog-test')
        fact = {'status': 'matched', 'candidates': [{'variant_id': 'V-A', 'title': '保留商品事实'}],
                'return_policies': {'policy': {'citation_index': deepcopy(self.fact['citation_index'])}}}
        replies = [self.tool_reply('recommend_products'), self.final({'selected_variant_ids': ['V-A']})]
        requests = []
        def transport(request):
            requests.append(json.loads(request.content))
            if len(requests) == 2: self.withdraw()
            return httpx.Response(200, json=replies[len(requests)-1])
        got = run_shopping_agent(self.cfg(), self.engine, '买家导购', 'B_TEST', [], lambda: False,
            condition_state=state, transport=httpx.MockTransport(transport), tool_handlers={'recommend_products': lambda: deepcopy(fact)})
        self.assert_closed(got); self.assertEqual(len(requests), 2)
        self.assertEqual(got['facts'][0]['result']['candidates'], fact['candidates'])

    def test_catalog_empty_after_policy_withdrawal_does_not_claim_no_product(self):
        from shopping_runtime import catalog_answer
        fact = {'status': 'no_match', 'candidates': [], 'return_policy_checks': [
            {'status': 'unknown', 'reason': 'applicable_policy_withdrawn', 'policy_ref': None}]}
        self.assertEqual(check_policy_references(self.engine, fact)['status'], 'withdrawn')
        answer = catalog_answer(fact)
        self.assertIn('政策已撤回', answer); self.assertIn('不代表商品不存在', answer)
        self.assertNotIn('没有已核实满足全部条件的商品', answer)


if __name__ == '__main__':
    unittest.main()
