"""售后修正循环边界回归：HTTP替身，不访问模型、数据库或本机凭据。"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest

import httpx
import test_support_runtime as runtime_fixtures
import test_support_claims as claim_fixtures
from agent_runtime import run_agent
from condition_state import reconcile_conditions
from execution_budget import ExecutionBudget
from support_claims import build_support_claim_catalog, fallback_support_claims


class SupportRepairLimitTests(unittest.TestCase):
    def fixture(self):
        helper = runtime_fixtures.SupportRuntimeTests()
        fact = helper.fact()
        return helper.cfg(), fact, helper.claim_body(fact)

    @staticmethod
    def final(body):
        return {'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': json.dumps(body, ensure_ascii=False)}}]}

    @staticmethod
    def tool_reply(identity='first-tool'):
        return {'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': identity, 'type': 'function', 'function': {
                'name': 'review_after_sales', 'arguments': '{"order_id":"O_SUPPORT_OK"}'}}]}}]}

    def run_case(self, repair, *, cfg=None, shared=None, cancel=None, after=None, fact=None):
        settings, original, good = self.fixture()
        settings = cfg or settings
        fact = fact or original
        bad = deepcopy(good)
        bad['claims'] = []
        responses = [self.tool_reply(), self.final(bad), repair]
        requests, tool_calls = [], []

        def send(request):
            payload = json.loads(request.content)
            requests.append(payload)
            self.assertLessEqual(len(requests), 3, '不得进行第二次输出修正')
            response = responses[len(requests) - 1]
            if after:
                after(len(requests), payload)
            if isinstance(response, Exception):
                raise response
            if isinstance(response, httpx.Response):
                return response
            return httpx.Response(200, json=response)

        def tool(**arguments):
            tool_calls.append(arguments)
            return deepcopy(fact)

        state = reconcile_conditions('O_SUPPORT_OK退货运费谁承担？没穿，吊牌完好，尺码太小', None,
                                     datetime(2026, 11, 18, tzinfo=timezone.utc), 'repair-limits')
        got = run_agent(settings, None, state['original_question'], 'B001', [], cancel or (lambda: False),
                        condition_state=state, transport=httpx.MockTransport(send),
                        tool_handlers={'review_after_sales': tool}, shared_budget=shared)
        return got, requests, tool_calls

    def test_preconsumed_shared_model_budget_is_not_reissued_for_repair(self):
        cfg, _, good = self.fixture()
        shared = ExecutionBudget(cfg, lambda: False)
        self.assertTrue(shared.reserve_model(cfg.max_output_tokens))
        self.assertTrue(shared.reserve_model(cfg.max_output_tokens))
        got, requests, tools = self.run_case(self.final(good), shared=shared)
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(tools), 1)
        self.assertEqual(got['usage']['model_requests'], 2)
        self.assertEqual(got['budget']['used']['model_requests'], 4)
        self.assertFalse(got['support_validation']['repair_attempted'])
        self.assertIn('repair_budget_unavailable', got['support_validation']['errors'])
        self.assertIn('程序依据事实', got['answer'])

    def test_shared_output_reservation_can_stop_repair_before_call_cap(self):
        cfg, _, good = self.fixture()
        budget_cfg = deepcopy(cfg)
        budget_cfg.max_output_tokens = 600
        shared = ExecutionBudget(budget_cfg, lambda: False)
        got, requests, _ = self.run_case(self.final(good), shared=shared)
        self.assertEqual(len(requests), 2)
        self.assertEqual(got['budget']['used']['model_requests'], 2)
        self.assertEqual(got['budget']['used']['reserved_output_tokens'], 2400)
        self.assertEqual(got['budget']['last_denial'], 'reserved_output_tokens_limit')
        self.assertIn('repair_budget_unavailable', got['support_validation']['errors'])

    def test_successful_repair_uses_last_slot_and_does_not_requery(self):
        cfg, _, good = self.fixture()
        cfg.max_model_calls = 3
        got, requests, tools = self.run_case(self.final(good), cfg=cfg)
        self.assertTrue(got['support_validation']['passed'])
        self.assertEqual(got['budget']['used']['model_requests'], 3)
        self.assertEqual(got['budget']['used']['reserved_output_tokens'], 3600)
        self.assertEqual(len(tools), 1)
        self.assertNotIn('tools', requests[2])
        self.assertNotIn('tool_choice', requests[2])

    def test_cancellation_after_bad_response_prevents_repair(self):
        _, _, good = self.fixture()
        flag = [False]
        def after(number, _payload):
            if number == 2:
                flag[0] = True
        got, requests, tools = self.run_case(self.final(good), cancel=lambda: flag[0], after=after)
        self.assertEqual(got['status'], 'cancelled')
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(tools), 1)
        self.assertEqual(len(got['facts']), 1)
        self.assertNotIn('暂满足', got['answer'])

    def test_cancellation_during_repair_is_not_reported_as_completed(self):
        _, _, good = self.fixture()
        flag = [False]
        def after(number, _payload):
            if number == 3:
                flag[0] = True
        got, requests, tools = self.run_case(self.final(good), cancel=lambda: flag[0], after=after)
        self.assertEqual(got['status'], 'cancelled')
        self.assertEqual(len(requests), 3)
        self.assertEqual(len(tools), 1)
        self.assertEqual(got['usage']['model_requests'], 3)
        self.assertEqual(len(got['facts']), 1)

    def test_deadline_during_repair_failure_takes_precedence_over_fallback(self):
        cfg, _, _ = self.fixture()
        clock = [0.0]
        shared = ExecutionBudget(cfg, lambda: False, clock=lambda: clock[0])
        def after(number, _payload):
            if number == 3:
                clock[0] = 1000.0
        got, requests, tools = self.run_case(httpx.ReadTimeout('offline timeout'), shared=shared, after=after)
        self.assertEqual(got['status'], 'limited')
        self.assertEqual(got['budget']['stop_reason'], 'deadline')
        self.assertEqual(len(requests), 3)
        self.assertEqual(len(tools), 1)
        self.assertEqual(len(got['facts']), 1)

    def test_repair_transport_or_json_failure_falls_back_once_with_facts(self):
        for response in (httpx.Response(503, json={'error': 'offline'}),
                         httpx.Response(200, text='not-json'), httpx.ReadTimeout('offline timeout')):
            with self.subTest(response=type(response).__name__):
                got, requests, tools = self.run_case(response)
                self.assertEqual(got['status'], 'completed')
                self.assertFalse(got['support_validation']['passed'])
                self.assertIn('repair_request_failed', got['support_validation']['errors'])
                self.assertEqual(got['support_validation']['repair_count'], 1)
                self.assertEqual(len(requests), 3)
                self.assertEqual(len(tools), 1)
                self.assertEqual(len(got['facts']), 1)
                self.assertIn('程序依据事实', got['answer'])

    def test_incomplete_repair_response_cannot_be_a_valid_final(self):
        _, _, good = self.fixture()
        truncated = self.final(good)
        truncated['choices'][0]['finish_reason'] = 'length'
        for response in ({'choices': []}, {'choices': [None]}, truncated):
            with self.subTest(response=response):
                got, requests, _ = self.run_case(response)
                self.assertIn('repair_response_incomplete', got['support_validation']['errors'])
                self.assertFalse(got['support_validation']['passed'])
                self.assertEqual(len(requests), 3)

    def test_model_requested_tool_in_repair_is_never_executed(self):
        got, requests, tools = self.run_case(self.tool_reply('forbidden-second-tool'))
        self.assertEqual(len(tools), 1)
        self.assertEqual(got['usage']['tool_calls'], 1)
        self.assertIn('repair_tool_call_not_allowed', got['support_validation']['errors'])
        self.assertEqual(len(requests), 3)
        self.assertIn('forbidden-second-tool', json.dumps(got['trace']))

    def test_tool_contract_is_only_in_model_copy_and_raw_attempts_survive(self):
        _, fact, good = self.fixture()
        before = deepcopy(fact)
        got, requests, _ = self.run_case(self.final(good), fact=fact)
        self.assertEqual(fact, before)
        self.assertNotIn('answer_contract', got['facts'][0]['result'])
        tool_message = next(item for item in requests[1]['messages'] if item['role'] == 'tool')
        self.assertIn('answer_contract', json.loads(tool_message['content']))
        model_events = [event for event in got['trace'] if event.get('stage') == 'model']
        bad = json.loads(model_events[1]['response']['choices'][0]['message']['content'])
        self.assertEqual(bad['claims'], [])
        self.assertEqual(len(got['support_validation']['attempts']), 2)

    def test_wrong_owner_tool_result_stops_before_any_final_or_repair(self):
        _, fact, good = self.fixture()
        fact['buyer_id'] = 'B_OTHER'
        got, requests, tools = self.run_case(self.final(good), fact=fact)
        self.assertEqual(got['status'], 'failed')
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(tools), 1)
        self.assertEqual(got['facts'], [])
        self.assertNotIn('B_OTHER', json.dumps(got, ensure_ascii=False))

    def test_missing_exception_rule_support_cannot_publish_exception_or_positive_decision(self):
        fact = claim_fixtures.SupportClaimsTests().fact()
        fact['citation_index']['DOC:return']['supports'] = ['return_window_hours', 'requires_unworn', 'requires_tags']
        catalog = build_support_claim_catalog(fact)
        text = fallback_support_claims(fact)
        self.assertNotIn('定制商品不适用', text)
        self.assertNotIn('暂满足', text)
        self.assertTrue(any(item['kind'] == 'program_state' and '缺少' in item['text']
                            and '政策条款' in item['text'] for item in catalog.values()))

    def test_partial_return_rule_coverage_cannot_support_all_policy_conditions(self):
        fact = claim_fixtures.SupportClaimsTests().fact()
        fact['citation_index']['DOC:return']['supports'] = ['return_window_hours']
        text = fallback_support_claims(fact)
        self.assertNotIn('须未穿着', text)
        self.assertNotIn('定制商品不适用', text)
        self.assertNotIn('暂满足', text)

    def test_direct_rule_source_must_match_index_instead_of_section_name_only(self):
        fact = claim_fixtures.SupportClaimsTests().fact()
        fact['citations'] = {
            rule: {'chunk_id': ref}
            for ref, source in fact['citation_index'].items()
            for rule in source['supports']
        }
        self.assertIn('custom_exception', build_support_claim_catalog(fact))
        fact['citations']['custom_excluded'] = {'chunk_id': 'DOC:shipping'}
        catalog = build_support_claim_catalog(fact)
        self.assertNotIn('custom_exception', catalog)
        self.assertNotIn('暂满足', fallback_support_claims(fact))

    def test_section_without_explicit_supports_cannot_supply_policy_conclusion(self):
        fact = claim_fixtures.SupportClaimsTests().fact()
        fact['citation_index']['DOC:shipping'].pop('supports')
        self.assertNotIn('shipping_policy', build_support_claim_catalog(fact))


if __name__ == '__main__':
    unittest.main()
