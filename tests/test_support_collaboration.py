"""依赖协作的离线验收：独立数据库与HTTP替身，不接模型或正式数据。"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from buyer_support_service import get_support_context, evaluate_buyer_support
from condition_state import reconcile_conditions
from execution_budget import ExecutionBudget
from policy_kb import metadata, import_policy_files, select_policy, search_policy_chunks
from support_claims import build_support_claim_catalog
from support_runtime import citation_index, expected_decision
from test_buyer_support_service import SupportFixture

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.fromisoformat('2026-11-18T08:00:00+08:00')
QUESTION = 'O_SUPPORT_OK能退货吗，退货运费谁承担？未穿着，吊牌完好，尺码太小'


class SupportCollaborationTests(SupportFixture, unittest.TestCase):
    def setUp(self):
        self.setup_support()
        metadata.create_all(self.engine)
        import_policy_files(self.engine, [ROOT/'knowledge/2026-10-shoes.md', ROOT/'knowledge/2026-11-shoes.md'])
        self.state = reconcile_conditions(QUESTION, None, NOW, 'dependent-test')
        self.context = get_support_context(self.engine, 'O_SUPPORT_OK', buyer_id='B001')
        self.scope = dict(store_id='S001', category='ordinary_shoes',
                          paid_at=datetime.fromisoformat(self.context['order']['paid_at']))
        self.bundle = select_policy(self.engine, **self.scope)
        self.fact = evaluate_buyer_support(self.context, self.bundle,
            claims={'is_unworn': True, 'tags_intact': True, 'reason': 'size'}, now=NOW,
            topics=('eligibility', 'shipping'))
        self.fact.update(applicable_rules=deepcopy(self.bundle['rules']),
            retrieval=search_policy_chunks(self.engine, QUESTION, **self.scope, mode='keyword', limit=4),
            rule_evidence_mode='mandatory_clauses_plus_retrieved_context')
        # 正常替身明确提供全部三章，用于区别“已有依据”与后续空召回故障。
        # 原文仍来自本测试独立库，不把该替身当实际搜索排名。
        self.fact['retrieval']['results'] = deepcopy(self.bundle['chunks'])
        self.fact['decision'] = expected_decision(self.fact)
        self.fact['citation_index'] = citation_index(self.fact)
        self.fact['required_citation_ids'] = [key for key, item in self.fact['citation_index'].items()
            if item['section'] in {'return_eligibility', 'shipping_cost'}]
        patcher = patch('socket.socket', side_effect=AssertionError('real network forbidden'))
        patcher.start(); self.addCleanup(patcher.stop)

    @staticmethod
    def cfg(**changes):
        return SimpleNamespace(**({'model_api_key': 'offline-not-a-real-credential',
            'model_base_url': 'https://mock.invalid', 'model_name': 'offline-model',
            'max_model_calls': 4, 'max_tool_calls': 6, 'max_output_tokens': 1200,
            'model_timeout_seconds': 1, 'task_timeout_seconds': 90} | changes))

    @staticmethod
    def tool(name, args=None, identity=None):
        return {'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
            'tool_calls': [{'type': 'function', 'id': identity or name,
                'function': {'name': name, 'arguments': json.dumps({} if args is None else args)}}]}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}

    def body(self, fact=None):
        fact = self.fact if fact is None else fact
        catalog = build_support_claim_catalog(fact)
        return {'schema_version': 2, 'status': 'clarification' if fact.get('status') == 'clarification' else 'completed',
            'decision': expected_decision(fact),
            'claims': [{'id': key, 'citation_ids': item['citation_ids']} for key, item in catalog.items() if item['required']]}

    @staticmethod
    def final(body):
        return {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': json.dumps(body)}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}

    def run_case(self, responses=None, *, state=None, context=None, fact=None, settings=None,
                 handlers=None, cancel=None, shared=None, after=None, history=None, buyer_id='B001',
                 access_role='buyer', shop_id=None, real_handlers=False, preference_snapshot=None):
        from support_collaboration import run_support_collaboration
        requests, tools = [], []
        context = self.context if context is None else context
        fact = self.fact if fact is None else fact
        responses = responses if responses is not None else [self.tool('read_order_facts'),
            self.tool('review_frozen_policy'), self.final(self.body(fact))]
        def execute(name, value):
            def handler():
                tools.append(name)
                if isinstance(value, Exception): raise value
                return deepcopy(value)
            return handler
        supplied = {'read_order_facts': execute('read_order_facts', context),
            'review_frozen_policy': execute('review_frozen_policy', fact)}
        supplied.update(handlers or {})
        def send(request):
            self.assertEqual(request.url.host, 'mock.invalid')
            payload = json.loads(request.content); requests.append(payload)
            self.assertLessEqual(len(requests), len(responses), 'unexpected additional model request')
            if after: after(len(requests), payload)
            reply = responses[len(requests)-1]
            if isinstance(reply, Exception): raise reply
            if isinstance(reply, httpx.Response): return reply
            return httpx.Response(200, json=reply)
        result = run_support_collaboration(settings or self.cfg(), self.engine, QUESTION, buyer_id,
            history or [], cancel or (lambda: False), access_role=access_role, shop_id=shop_id,
            transport=httpx.MockTransport(send), tool_handlers=None if real_handlers else supplied,
            condition_state=self.state if state is None else state, shared_budget=shared,
            preference_snapshot=preference_snapshot)
        return result, requests, tools

    def test_every_json_mode_request_includes_provider_format_precondition(self):
        def provider_contract(_number, payload):
            if payload.get('response_format', {}).get('type') == 'json_object':
                instruction = '\n'.join(str(m.get('content') or '') for m in payload['messages']
                                        if m['role'] in {'system', 'user'})
                self.assertIn('json', instruction.lower(), 'provider rejects JSON mode without format instruction')
        got, requests, _ = self.run_case(after=provider_contract)
        self.assertEqual(got['status'], 'completed')
        self.assertEqual(len(requests), 3)

    def test_http_rejection_records_diagnosis_without_inventing_usage_or_running_tool(self):
        error = httpx.Response(400, json={'error': {'type': 'invalid_request_error',
            'code': 'invalid_request', 'message': 'JSON format instruction required'}})
        got, requests, tools = self.run_case(responses=[error])
        self.assertEqual(got['status'], 'failed')
        self.assertEqual(tools, [])
        event = next(e for e in got['trace'] if e.get('stage') == 'model')
        self.assertEqual(event['http_status'], 400)
        self.assertEqual(event['provider_error']['code'], 'invalid_request')
        self.assertEqual(got['usage']['unmetered_requests'], 1)
        self.assertEqual(got['usage']['total_tokens'], 0)

    def test_three_requests_two_tools_dependency_and_complete_claims(self):
        got, requests, tools = self.run_case()
        self.assertEqual(got['status'], 'completed')
        self.assertEqual(tools, ['read_order_facts', 'review_frozen_policy'])
        self.assertEqual(len(requests), 3)
        self.assertEqual(got['usage']['model_requests'], 3)
        self.assertEqual(got['usage']['tool_calls'], 2)
        self.assertEqual(got['usage']['total_tokens'], 45)
        self.assertEqual(got['budget']['used']['model_requests'], 3)
        self.assertEqual(got['orchestration']['effective'], 'dependent')
        self.assertEqual([row['name'] for row in got['orchestration']['branches']], ['order_facts', 'policy'])
        self.assertEqual(got['orchestration']['handoff']['order_id'], 'O_SUPPORT_OK')
        self.assertEqual(len(got['orchestration']['handoff']['sha256']), 64)
        self.assertTrue(got['support_validation']['passed'])
        self.assertIn('定制商品', got['answer'])
        self.assertIn('未批准', got['answer'])
        self.assertNotEqual(requests[0]['messages'][0], requests[1]['messages'][0])
        self.assertIn('O_SUPPORT_OK', json.dumps(requests[1]['messages'], ensure_ascii=False))
        self.assertNotIn('read_order_facts', [x['function']['name'] for x in requests[1].get('tools', [])])

    def test_handoff_and_facts_do_not_mutate_originals_and_history_is_preserved(self):
        original_context, original_fact, original_state = deepcopy(self.context), deepcopy(self.fact), deepcopy(self.state)
        history = [{'role': 'user', 'content': '前问'}, {'role': 'assistant', 'content': '前答'}]
        got, _, _ = self.run_case(history=history)
        self.assertEqual(self.context, original_context)
        self.assertEqual(self.fact, original_fact)
        self.assertEqual(self.state, original_state)
        self.assertEqual(got['history'][:2], history)
        self.assertEqual(got['history'][-1]['content'], got['answer'])
        self.assertEqual(json.loads(json.dumps(got))['facts'], got['facts'])

    def test_real_policy_service_uses_authorized_order_payment_version(self):
        got, requests, _ = self.run_case(real_handlers=True)
        self.assertEqual(got['status'], 'completed')
        fact = next(x['result'] for x in got['facts'] if x['tool'] == 'review_after_sales')
        self.assertEqual(fact['policy_version'], '2026-11-v1')
        self.assertEqual(fact['order_id'], 'O_SUPPORT_OK')
        self.assertEqual(fact['eligibility'], 'eligible_with_self_report')
        self.assertTrue(got['support_validation']['passed'])

    def test_fact_exception_blocks_policy_and_no_second_model(self):
        got, requests, tools = self.run_case(responses=[self.tool('read_order_facts')], context=RuntimeError('private-db-detail'))
        self.assertEqual(got['status'], 'failed')
        self.assertEqual(len(requests), 1)
        self.assertEqual(tools, ['read_order_facts'])
        self.assertNotIn('private-db-detail', json.dumps(got))

    def test_fact_out_of_scope_blocks_dependent_policy(self):
        context = {'status': 'not_found', 'order_id': 'O_SUPPORT_OK', 'scope': {'role': 'buyer', 'buyer_id': 'B001'}}
        got, requests, tools = self.run_case(responses=[self.tool('read_order_facts')], context=context)
        self.assertEqual(len(requests), 1)
        self.assertEqual(tools, ['read_order_facts'])
        self.assertNotIn('policy_version', got['answer'])
        self.assertNotIn('10000', got['answer'])

    def test_policy_failure_keeps_order_fact_and_partial_status(self):
        got, requests, tools = self.run_case(responses=[self.tool('read_order_facts'), self.tool('review_frozen_policy')],
                                           fact=RuntimeError('secret-policy-details'))
        self.assertEqual(got['status'], 'partial')
        self.assertEqual(len(requests), 2)
        self.assertEqual(tools, ['read_order_facts', 'review_frozen_policy'])
        self.assertTrue(got['facts'])
        self.assertNotIn('secret-policy-details', json.dumps(got))

    def test_scope_arguments_are_rejected_before_handler(self):
        for name, args in [('read_order_facts', {'order_id': 'OTHER'}),
                           ('read_order_facts', {'buyer_id': 'B002'}),
                           ('read_order_facts', {'parameters': {'shop_id': 'OTHER'}})]:
            with self.subTest(args=args):
                got, requests, tools = self.run_case([self.tool(name, args)])
                self.assertEqual(got['status'], 'failed')
                self.assertEqual(tools, [])
                self.assertEqual(len(requests), 1)

    def test_policy_cannot_change_order_scope_or_payment_time(self):
        got, requests, tools = self.run_case([self.tool('read_order_facts'),
            self.tool('review_frozen_policy', {'paid_at': '2026-12-01T00:00:00+08:00'})])
        self.assertNotEqual(got['status'], 'completed')
        self.assertEqual(tools, ['read_order_facts'])
        self.assertEqual(len(requests), 2)

    def test_cancel_after_facts_prevents_policy_call(self):
        flag = [False]
        def facts():
            flag[0] = True
            return deepcopy(self.context)
        got, requests, _ = self.run_case([self.tool('read_order_facts')],
            handlers={'read_order_facts': facts}, cancel=lambda: flag[0])
        self.assertEqual(got['status'], 'cancelled')
        self.assertEqual(len(requests), 1)
        self.assertEqual(got['usage']['tool_calls'], 1)

    def test_preconsumed_shared_model_budget_is_not_reset_for_policy(self):
        budget = ExecutionBudget(self.cfg(), lambda: False)
        for _ in range(3): self.assertTrue(budget.reserve_model(1200))
        got, requests, tools = self.run_case([self.tool('read_order_facts')], shared=budget)
        self.assertIn(got['status'], {'limited', 'partial'})
        self.assertEqual(len(requests), 1)
        self.assertEqual(tools, ['read_order_facts'])
        self.assertEqual(got['budget']['used']['model_requests'], 4)

    def test_shared_tool_budget_prevents_policy_execution(self):
        cfg = self.cfg(max_tool_calls=1)
        got, requests, tools = self.run_case([self.tool('read_order_facts'), self.tool('review_frozen_policy')], settings=cfg)
        self.assertIn(got['status'], {'limited', 'partial'})
        self.assertEqual(tools, ['read_order_facts'])
        self.assertEqual(got['budget']['used']['tool_calls'], 1)

    def test_final_wrong_citation_gets_one_format_repair_without_tools(self):
        bad = self.body(); bad['claims'][0]['citation_ids'] = ['invented']
        got, requests, tools = self.run_case([self.tool('read_order_facts'), self.tool('review_frozen_policy'),
            self.final(bad), self.final(self.body())])
        self.assertEqual(got['status'], 'completed')
        self.assertTrue(got['support_validation']['passed'])
        self.assertEqual(len(requests), 4)
        self.assertEqual(len(tools), 2)
        self.assertNotIn('tools', requests[-1])

    def test_tools_during_format_repair_are_not_executed(self):
        bad = self.body(); bad['claims'] = []
        got, requests, tools = self.run_case([self.tool('read_order_facts'), self.tool('review_frozen_policy'),
            self.final(bad), self.tool('lookup_policy_evidence')])
        self.assertNotEqual(got['status'], 'completed')
        self.assertEqual(len(tools), 2)
        self.assertEqual(len(requests), 4)

    def test_followup_without_gap_is_not_executed(self):
        got, requests, tools = self.run_case([self.tool('read_order_facts'), self.tool('review_frozen_policy'),
            self.tool('lookup_policy_evidence')])
        self.assertNotEqual(got['status'], 'completed')
        self.assertEqual(len(tools), 2)
        self.assertEqual(got['orchestration']['followup']['attempts'], 0)

    def test_missing_identity_and_unready_conditions_use_no_model(self):
        bad = deepcopy(self.state); bad['needs_clarification'] = True; bad['clarification'] = '请补订单'
        for kwargs in ({'buyer_id': ''}, {'access_role': 'admin'}, {'state': bad}):
            with self.subTest(kwargs=kwargs):
                got, requests, tools = self.run_case([], **kwargs)
                self.assertIn(got['status'], {'failed', 'clarification'})
                self.assertEqual(requests, [])
                self.assertEqual(tools, [])

    def test_gap_allows_one_real_scoped_keyword_lookup_without_changing_decision(self):
        fact = deepcopy(self.fact); fact['retrieval']['results'] = []
        lookups = []
        def lookup():
            lookups.append(deepcopy(self.scope))
            result = search_policy_chunks(self.engine,
                '适用店铺 普通鞋类 退货申请时限 未穿着 吊牌 定制 退货运费 尺码',
                **self.scope, mode='keyword', limit=4)
            return {**result, 'decision_changed': False}
        got, requests, tools = self.run_case([self.tool('read_order_facts'), self.tool('review_frozen_policy'),
            self.tool('lookup_policy_evidence'), self.final(self.body(fact))],
            fact=fact, handlers={'lookup_policy_evidence': lookup})
        self.assertEqual(got['status'], 'completed')
        self.assertEqual(len(requests), 4)
        self.assertEqual(got['usage']['tool_calls'], 3)
        self.assertEqual(len(lookups), 1)
        self.assertEqual(lookups[0], self.scope)
        self.assertEqual(got['orchestration']['followup']['attempts'], 1)
        self.assertEqual(got['orchestration']['followup']['limit'], 1)
        final_fact = next(item['result'] for item in got['facts'] if item['tool'] == 'review_after_sales')
        self.assertEqual(final_fact['decision'], self.fact['decision'])
        self.assertEqual(final_fact['order'], self.fact['order'])
        self.assertEqual(final_fact['policy_version'], '2026-11-v1')
        self.assertTrue(got['support_validation']['passed'])

    def test_second_followup_is_rejected_even_if_model_budget_would_allow_it(self):
        fact = deepcopy(self.fact); fact['retrieval']['results'] = []
        count = []
        def lookup():
            count.append(1)
            return {'status': 'insufficient_evidence', 'results': [],
                    'payment_version': '2026-11-v1', 'decision_changed': False}
        got, requests, tools = self.run_case([self.tool('read_order_facts'), self.tool('review_frozen_policy'),
            self.tool('lookup_policy_evidence'), self.tool('lookup_policy_evidence')],
            fact=fact, settings=self.cfg(max_model_calls=6), handlers={'lookup_policy_evidence': lookup})
        self.assertNotEqual(got['status'], 'completed')
        self.assertEqual(count, [1])
        self.assertEqual(got['usage']['tool_calls'], 3)
        self.assertEqual(got['orchestration']['followup']['attempts'], 1)

    def test_unknown_self_report_is_not_a_policy_lookup_gap(self):
        fact = deepcopy(self.fact)
        fact.update(status='clarification', missing=['reason'])
        fact['retrieval']['results'] = []
        fact['decision'] = expected_decision(fact)
        got, requests, tools = self.run_case([self.tool('read_order_facts'), self.tool('review_frozen_policy'),
            self.final(self.body(fact))], fact=fact)
        self.assertEqual(got['status'], 'clarification')
        offered = [t['function']['name'] for t in requests[-1].get('tools', [])]
        self.assertNotIn('lookup_policy_evidence', offered)
        self.assertEqual(got['orchestration']['followup']['attempts'], 0)

    def test_no_applicable_policy_is_not_repaired_by_querying_another_version(self):
        fact = deepcopy(self.fact)
        fact.update(status='insufficient_evidence', policy_version=None, applicable_rules={}, citations={}, citation_index={})
        fact['retrieval']['results'] = []
        fact['decision'] = expected_decision(fact)
        got, requests, tools = self.run_case([self.tool('read_order_facts'), self.tool('review_frozen_policy'),
            self.final(self.body(fact))], fact=fact)
        self.assertEqual(got['status'], 'completed')
        self.assertNotIn('lookup_policy_evidence', [t['function']['name'] for t in requests[-1].get('tools', [])])
        self.assertEqual(got['orchestration']['followup']['attempts'], 0)
        self.assertIn('不能', got['answer'])

    def test_production_followup_reads_scoped_sources_and_keeps_original_fact(self):
        from support_collaboration import lookup_frozen_policy_evidence
        fact = deepcopy(self.fact); fact['retrieval']['results'] = []
        before = deepcopy(fact)
        got = lookup_frozen_policy_evidence(self.engine, self.context, fact)
        self.assertEqual(got['status'], 'ok')
        self.assertTrue(got['results'])
        self.assertEqual(got['payment_version'], '2026-11-v1')
        self.assertFalse(got['decision_changed'])
        for chunk in got['results']:
            self.assertEqual(chunk['store_id'], 'S001')
            self.assertEqual(chunk['category'], 'ordinary_shoes')
            self.assertEqual(chunk['version'], '2026-11-v1')
            self.assertIn(chunk['chunk_id'], self.fact['citation_index'])
        self.assertEqual(fact, before)

    def test_production_followup_rejects_changed_source_and_foreign_scope(self):
        from support_collaboration import lookup_frozen_policy_evidence
        fact = deepcopy(self.fact); fact['retrieval']['results'] = []
        baseline = search_policy_chunks(self.engine, '退货 运费 普通鞋类', **self.scope, mode='keyword', limit=4)
        self.assertTrue(baseline['results'])
        changes = [('store_id', 'S_OTHER'), ('category', 'shirts'), ('version', '2026-10-v1'),
                   ('text', '篡改成不需要吊牌'), ('checksum', '0'*64), ('line_start', 999),
                   ('line_end', 999), ('section', 'scope'), ('chunk_id', 'unapproved-source')]
        for key, value in changes:
            with self.subTest(key=key):
                changed = deepcopy(baseline)
                # 确保改动确实改变了字段，而不是测试值恰好等于原值。
                if changed['results'][0].get(key) == value: value = 'other-section'
                changed['results'][0][key] = value
                with patch('policy_kb.search_policy_chunks', return_value=changed):
                    got = lookup_frozen_policy_evidence(self.engine, self.context, fact)
                self.assertEqual(got['status'], 'rejected')
                self.assertEqual(got['results'], [])
                self.assertFalse(got['decision_changed'])

    def test_production_followup_missing_evidence_is_not_invented(self):
        from support_collaboration import lookup_frozen_policy_evidence
        fact = deepcopy(self.fact); fact['retrieval']['results'] = []
        with patch('policy_kb.search_policy_chunks', return_value={'status':'insufficient_evidence', 'results': []}):
            got = lookup_frozen_policy_evidence(self.engine, self.context, fact)
        self.assertEqual(got['status'], 'insufficient_evidence')
        self.assertEqual(got['results'], [])
        self.assertFalse(got['decision_changed'])

    def test_nonempty_followup_does_not_close_a_different_missing_section(self):
        from support_collaboration import lookup_frozen_policy_evidence
        fact = deepcopy(self.fact)
        fact['retrieval']['results'] = [chunk for chunk in fact['retrieval']['results']
                                       if chunk['section'] != 'shipping_cost']
        baseline = search_policy_chunks(self.engine, '未穿着 吊牌 定制',
                                        **self.scope, mode='keyword', limit=4)
        baseline['results'] = [chunk for chunk in baseline['results']
                               if chunk['section'] == 'return_eligibility']
        self.assertTrue(baseline['results'])
        with patch('policy_kb.search_policy_chunks', return_value=baseline):
            got = lookup_frozen_policy_evidence(self.engine, self.context, fact)
        self.assertEqual(got['requested_sections'], ['shipping_cost'])
        self.assertEqual(got['status'], 'insufficient_evidence')
        self.assertEqual(got['remaining_sections'], ['shipping_cost'])
        self.assertNotIn('shipping_cost', got['covered_sections'])
        self.assertTrue(got['results'])
        self.assertFalse(got['decision_changed'])

    def test_production_followup_cancel_prevents_query(self):
        from support_collaboration import lookup_frozen_policy_evidence
        fact = deepcopy(self.fact); fact['retrieval']['results'] = []
        with patch('policy_kb.search_policy_chunks') as query:
            with self.assertRaises(InterruptedError):
                lookup_frozen_policy_evidence(self.engine, self.context, fact, should_cancel=lambda: True)
            query.assert_not_called()

    def test_model_disabled_performs_no_model_or_business_tool_calls(self):
        got, requests, tools = self.run_case([], settings=self.cfg(model_enabled=False))
        self.assertEqual(got['status'], 'failed')
        self.assertEqual(requests, [])
        self.assertEqual(tools, [])
        self.assertEqual(got['usage']['model_requests'], 0)
        self.assertEqual(got['usage']['tool_calls'], 0)

    def test_invalid_skill_prevents_paid_model_and_tool_calls(self):
        with patch('skill_loader.load_skill', side_effect=ValueError('untrusted_private_path')):
            got, requests, tools = self.run_case([])
        self.assertEqual(got['status'], 'failed')
        self.assertEqual(requests, [])
        self.assertEqual(tools, [])
        self.assertNotIn('untrusted_private_path', json.dumps(got))

    def test_policy_context_loads_trusted_skill_with_tool_mapping(self):
        got, requests, _ = self.run_case()
        self.assertEqual(got['skills'][0]['id'], 'buyer_support')
        self.assertEqual(got['skills'][0]['path'], 'skills/buyer_support.md')
        self.assertEqual(got['skills'][0]['sha256'], hashlib.sha256((ROOT/'skills/buyer_support.md').read_bytes()).hexdigest())
        policy_prompt = requests[1]['messages'][0]['content']
        self.assertIn('买家订单与适用政策核对', policy_prompt)
        self.assertIn('## 职责', policy_prompt)
        self.assertIn('review_after_sales由review_frozen_policy实现', policy_prompt)
        self.assertIn('不重复读订单', policy_prompt)

    def test_independent_contexts_can_reuse_vendor_call_id(self):
        got, requests, tools = self.run_case([self.tool('read_order_facts', identity='call-1'),
            self.tool('review_frozen_policy', identity='call-1'), self.final(self.body())])
        self.assertEqual(got['status'], 'completed')
        self.assertEqual(len(requests), 3)
        self.assertEqual(tools, ['read_order_facts', 'review_frozen_policy'])

    def test_rejected_followup_remains_partial_with_explicit_gap_in_answer(self):
        fact = deepcopy(self.fact); fact['retrieval']['results'] = []
        def lookup():
            return {'status': 'rejected', 'reason': 'followup_source_changed_or_outside_handoff',
                    'results': [], 'decision_changed': False}
        got, requests, tools = self.run_case([self.tool('read_order_facts'), self.tool('review_frozen_policy'),
            self.tool('lookup_policy_evidence'), self.final(self.body(fact))], fact=fact,
            handlers={'lookup_policy_evidence': lookup})
        self.assertEqual(got['status'], 'partial')
        self.assertEqual(got['orchestration']['followup']['status'], 'rejected')
        self.assertEqual(got['orchestration']['followup']['attempts'], 1)
        self.assertIn('补查未取得可采纳的新资料', got['answer'])
        self.assertIn('不能视为补查成功', got['answer'])
        self.assertEqual(got['evidence_status'], 'partial_tool_results')

    def test_preference_snapshot_is_read_only_and_passed_to_policy_context(self):
        snapshot = {'role': 'buyer', 'values': {'answer_style': 'brief'}, 'version': 1,
            'updated_at': '2026-11-18T00:00:00+00:00',
            'source': {'kind': 'user_settings', 'text': 'private preference source'}}
        original = deepcopy(snapshot)
        got, requests, _ = self.run_case(preference_snapshot=snapshot)
        self.assertEqual(got['memory']['status'], 'loaded')
        self.assertEqual(got['memory']['effective_values'], {'answer_style': 'brief'})
        self.assertIn('"answer_style": "brief"', requests[1]['messages'][0]['content'])
        self.assertNotIn('private preference source', json.dumps(requests))
        self.assertEqual(snapshot, original)
        self.assertNotIn('memory_proposal', got)

