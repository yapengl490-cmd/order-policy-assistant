"""Local policy text/version boundaries; no model, network, or database calls."""
from datetime import date, datetime, timezone
import json
from pathlib import Path
import unittest

from agent_runtime import search_policies
from condition_state import reconcile_conditions, validate_tool_arguments

POLICY_PATH = Path(__file__).resolve().parents[1] / 'policies.json'
OLD_TEXT = '本演示没有商品退货条件、运费承担、退款申请资格或真实平台处理时限的依据。涉及这些问题时应说明证据不足，并请用户通过对应真实商家或平台渠道核对，不得将演示条款作为真实售后承诺。'


class PolicyCopyVersionTests(unittest.TestCase):
    def search(self, query='售后退货运费证据', as_of=None, today=None):
        return search_policies(query, as_of, today=today, policy_path=POLICY_PATH)

    def support_ids(self, result):
        return [p['citation_id'] for p in result['policies'] if p['citation_id'].startswith('DEMO-SUPPORT-')]

    def test_before_correction_returns_unchanged_v1_text_and_citation(self):
        result = self.search(as_of='2026-11-19')
        self.assertEqual(self.support_ids(result), ['DEMO-SUPPORT-2026-v1'])
        old = next(p for p in result['policies'] if p['citation_id'] == 'DEMO-SUPPORT-2026-v1')
        self.assertEqual(old['version'], '1.0')
        self.assertEqual(old['text'], OLD_TEXT)
        self.assertEqual(old['effective_before'], '2026-11-20')

    def test_correction_day_returns_only_v2_of_support_versions(self):
        result = self.search(as_of='2026-11-20')
        self.assertEqual(self.support_ids(result), ['DEMO-SUPPORT-2026-v2'])
        new = next(p for p in result['policies'] if p['citation_id'] == 'DEMO-SUPPORT-2026-v2')
        self.assertEqual(new['version'], '2.0')
        self.assertEqual(new['effective_from'], '2026-11-20')
        for limitation in ('不能直接作为具体订单的售后判断依据', 'S001普通鞋类合成演示', '订单支付时有效的政策版本',
                           '未核实用户自述', '不自动批准退货', '不执行退款', '真实店铺售后规则',
                           '换货、维修、赔偿', '真实资金到账时间或状态，尚未接入'):
            self.assertIn(limitation, new['text'])
        self.assertNotIn('本演示没有商品退货条件', new['text'])

    def test_old_reference_still_resolves_by_citation_and_original_text(self):
        documents = json.loads(POLICY_PATH.read_text())
        index = {p['citation_id']: p for p in documents}
        self.assertEqual(len(index), len(documents))
        self.assertEqual(index['DEMO-SUPPORT-2026-v1']['text'], OLD_TEXT)
        self.assertEqual(index['DEMO-SUPPORT-2026-v1']['version'], '1.0')
        self.assertIn('DEMO-SUPPORT-2026-v2', index)

    def test_refund_and_access_policies_remain_available_on_both_sides(self):
        for day in ('2026-11-19', '2026-11-20'):
            for query, citation in (('退款净支出金额截止', 'DEMO-REFUND-2026-v1'),
                                    ('买家商家权限店铺', 'DEMO-ACCESS-2026-v2')):
                with self.subTest(day=day, query=query):
                    self.assertIn(citation, [p['citation_id'] for p in self.search(query, day)['policies']])

    def test_default_day_and_explicit_historical_day_remain_separate(self):
        self.assertEqual(self.support_ids(self.search(today=date(2026, 11, 20))), ['DEMO-SUPPORT-2026-v2'])
        result = self.search(as_of='2026-11-19', today=date(2026, 11, 20))
        self.assertEqual(result['as_of'], '2026-11-19')
        self.assertEqual(self.support_ids(result), ['DEMO-SUPPORT-2026-v1'])

    def test_validity_upper_bound_and_unknown_terms_keep_insufficient_evidence(self):
        for day in ('2025-12-31', '2027-01-01'):
            result = self.search(as_of=day)
            self.assertEqual(result['status'], 'insufficient_evidence')
            self.assertEqual(result['policies'], [])
        self.assertEqual(self.search('xyzzy', '2026-11-20')['policies'], [])

    def test_server_business_date_injection_selects_v2_without_changing_refund_cutoff(self):
        # UTC Nov 19 has already crossed the Nov 20 Asia/Shanghai boundary.
        now = datetime(2026, 11, 19, 16, 30, tzinfo=timezone.utc)
        state = reconcile_conditions('核查O001，退款截止2026年11月3日零点，并解释退款政策', None, now, 'date-default')
        args = validate_tool_arguments('search_policies', {'query': '售后退货运费证据'}, state)
        self.assertEqual(args['as_of'], '2026-11-20')
        self.assertEqual(state['confirmed']['refund_before']['value'], '2026-11-03T00:00:00+08:00')
        self.assertEqual(self.support_ids(search_policies(**args, policy_path=POLICY_PATH)), ['DEMO-SUPPORT-2026-v2'])

    def test_explicit_policy_date_injection_can_still_retrieve_v1(self):
        now = datetime(2026, 11, 20, tzinfo=timezone.utc)
        state = reconcile_conditions('核查O001，退款截止2026年11月3日零点，并解释退款政策，政策日期2026-11-19', None, now, 'date-explicit')
        args = validate_tool_arguments('search_policies', {'query': '售后退货运费证据'}, state)
        self.assertEqual(args['as_of'], '2026-11-19')
        self.assertEqual(self.support_ids(search_policies(**args, policy_path=POLICY_PATH)), ['DEMO-SUPPORT-2026-v1'])
