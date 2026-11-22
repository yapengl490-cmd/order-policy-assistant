"""第二版售后合同离线回归：不访问数据库、模型、凭据或旧任务。"""
from copy import deepcopy
import unittest

from support_claims import (build_support_claim_catalog, fallback_support_claims,
                            render_support_claims, validate_support_claims)
from support_runtime import expected_decision


class SupportClaimsTests(unittest.TestCase):
    def fact(self):
        return {
            'status': 'ok', 'order_id': 'O_TEST', 'requested_topics': ['eligibility'],
            'eligibility': 'eligible_with_self_report', 'shipping': {'status': 'not_requested', 'payer': None},
            'policy_version': '2026-11-v1', 'as_of': '2026-11-18T13:00:00+00:00',
            'trusted_facts': {'delivered_at': '2026-11-16T02:00:00+00:00', 'is_custom': False},
            'user_claims': {'values': {'is_unworn': True, 'tags_intact': True}, 'verified': False},
            'missing': [], 'return_window': {'before': '2026-11-30T02:00:00+00:00'},
            'applicable_rules': {'return_window_hours': 336, 'requires_unworn': True,
                                'requires_tags': True, 'custom_excluded': True,
                                'shipping_by_reason': {'size': 'buyer', 'preference': 'buyer',
                                                      'merchant_responsibility': 'verified_record_required'}},
            'citation_index': {
                'DOC:return': {'section': 'return_eligibility', 'supports': ['return_window_hours', 'requires_unworn', 'requires_tags', 'custom_excluded'],
                               'text': '收货起336小时内，须未穿着且吊牌完好；定制商品除外。'},
                'DOC:shipping': {'section': 'shipping_cost', 'supports': ['shipping_by_reason'],
                                 'text': '尺码和偏好由买家付运费；商家责任待核实。'},
                'DOC:scope': {'section': 'scope', 'supports': ['allowed_categories'], 'text': '普通鞋类演示。'},
            },
        }

    def body(self, fact, optional=False):
        catalog = build_support_claim_catalog(fact)
        return {'schema_version': 2,
                'status': 'clarification' if fact.get('status') == 'clarification' else 'completed',
                'decision': expected_decision(fact),
                'claims': [{'id': identity, 'citation_ids': item['citation_ids']}
                           for identity, item in catalog.items() if item['required'] or optional]}

    def test_normal_required_claims_preserve_limits_and_sources_once(self):
        fact = self.fact()
        body = self.body(fact)
        self.assertEqual(validate_support_claims(body, fact), [])
        text = render_support_claims(body, fact)
        for phrase in ('暂满足', '仍需售后核验', '2026-11-v1', '336小时', '未穿着', '吊牌完好',
                       '定制商品不适用', '用户自述（未核实）', '未批准、受理或执行'):
            self.assertIn(phrase, text)
        self.assertEqual(text.count('政策例外：'), 1)
        self.assertEqual(text.count('政策条件：'), 1)
        self.assertEqual(text.count('用户自述（未核实）：'), 1)
        self.assertNotIn('待补：', text)
        self.assertNotIn('退货原因', text)
        self.assertNotIn('[DOC:shipping]', text)

    def test_optional_shipping_policy_requires_own_citation_without_personal_decision(self):
        fact = self.fact()
        catalog = build_support_claim_catalog(fact)
        self.assertFalse(catalog['shipping_policy']['required'])
        self.assertEqual(catalog['shipping_policy']['citation_ids'], ['DOC:shipping'])
        self.assertNotIn('shipping', catalog)
        body = self.body(fact, optional=True)
        text = render_support_claims(body, fact)
        self.assertIn('本次未判断本单运费', text)
        self.assertIn('[DOC:shipping]', text)
        self.assertIsNone(body['decision']['shipping_payer'])

    def test_original_omitted_shipping_reference_and_wrong_section_are_rejected(self):
        fact = self.fact()
        for refs in ([], ['DOC:return'], ['DOC:scope'], ['OTHER:shipping'], ['shipping_cost']):
            body = self.body(fact, optional=True)
            next(item for item in body['claims'] if item['id'] == 'shipping_policy')['citation_ids'] = refs
            with self.subTest(refs=refs):
                self.assertIn('wrong_citation:shipping_policy', validate_support_claims(body, fact))
                with self.assertRaises(ValueError):
                    render_support_claims(body, fact)

    def test_exception_and_other_required_claims_cannot_be_omitted(self):
        fact = self.fact()
        for identity in ('eligibility', 'policy_conditions', 'custom_exception', 'order_times', 'self_report', 'boundary'):
            body = self.body(fact)
            body['claims'] = [item for item in body['claims'] if item['id'] != identity]
            with self.subTest(identity=identity):
                self.assertIn('missing_claim:' + identity, validate_support_claims(body, fact))

    def test_free_text_top_level_and_per_claim_bypasses_are_rejected(self):
        fact = self.fact()
        for field in ('answer', 'text', 'citation_ids', 'memory_proposal'):
            body = self.body(fact)
            body[field] = '模型自行添加未核对结论'
            self.assertIn('unexpected_output_fields', validate_support_claims(body, fact))
        for field in ('text', 'answer', 'kind'):
            body = self.body(fact)
            body['claims'][0][field] = '模型自报内容'
            self.assertIn('invalid_claim_fields', validate_support_claims(body, fact))

    def test_model_cannot_choose_unknown_claim_or_change_decision(self):
        fact = self.fact()
        body = self.body(fact)
        body['claims'][0]['id'] = 'refund_approved'
        self.assertIn('unsupported_claim', validate_support_claims(body, fact))
        body = self.body(fact)
        body['decision']['shipping_payer'] = 'buyer'
        self.assertIn('decision_mismatch', validate_support_claims(body, fact))

    def test_duplicate_claims_and_duplicate_citations_are_rejected(self):
        fact = self.fact()
        body = self.body(fact)
        body['claims'].append(deepcopy(body['claims'][0]))
        self.assertIn('duplicate_claim:eligibility', validate_support_claims(body, fact))
        body = self.body(fact)
        body['claims'][0]['citation_ids'] *= 2
        self.assertIn('wrong_citation:eligibility', validate_support_claims(body, fact))

    def test_dates_are_deterministically_converted_and_preserve_precision(self):
        fact = self.fact()
        text = render_support_claims(self.body(fact), fact)
        self.assertIn('退货截止2026-11-30 10:00:00+08:00（不含）', text)
        self.assertNotIn('2026-11-30 02:00:00+08:00', text)
        fact['return_window']['before'] = '2027-02-08T19:15:01.000002Z'
        text = render_support_claims(self.body(fact), fact)
        self.assertIn('2027-02-09 03:15:01.000002+08:00', text)

    def test_missing_delivery_remains_unknown_and_does_not_invent_deadline(self):
        fact = self.fact()
        fact.update(status='clarification', eligibility='undetermined', missing=['delivered_at', 'is_custom'])
        fact['trusted_facts'] = {'delivered_at': None, 'is_custom': None}
        fact['return_window'] = {}
        catalog = build_support_claim_catalog(fact)
        self.assertEqual(catalog['missing']['citation_ids'], [])
        self.assertEqual(catalog['missing']['kind'], 'program_state')
        text = render_support_claims(self.body(fact), fact)
        self.assertIn('收货时间（订单记录）', text)
        self.assertIn('定制状态（订单记录）', text)
        self.assertNotIn('程序计算退货截止', text)
        body = self.body(fact)
        body['claims'] = [item for item in body['claims'] if item['id'] != 'missing']
        self.assertIn('missing_claim:missing', validate_support_claims(body, fact))

    def test_program_and_user_sources_cannot_be_disguised_as_policy_quotes(self):
        fact = self.fact()
        catalog = build_support_claim_catalog(fact)
        for identity, kind in (('order_times', 'tool_facts'), ('self_report', 'user_claim'), ('boundary', 'program_state')):
            self.assertEqual(catalog[identity]['kind'], kind)
            self.assertEqual(catalog[identity]['citation_ids'], [])
            body = self.body(fact)
            next(item for item in body['claims'] if item['id'] == identity)['citation_ids'] = ['DOC:return']
            self.assertIn('wrong_citation:' + identity, validate_support_claims(body, fact))

    def test_not_found_suppresses_even_accidentally_attached_private_facts(self):
        fact = self.fact()
        fact.update(status='not_found', eligibility='undetermined', message='SECRET_OTHER_SHOP_POLICY')
        fact['order'] = {'paid_amount_fen': 998877, 'shop_id': 'OTHER_SHOP'}
        catalog = build_support_claim_catalog(fact)
        self.assertEqual(set(catalog), {'scope_result', 'boundary'})
        text = render_support_claims(self.body(fact), fact)
        for hidden in ('998877', 'OTHER_SHOP', '2026-11-v1', 'DOC:', '336', '收货时间'):
            self.assertNotIn(hidden, text)
        self.assertIn('不能区分订单不存在与无权访问', text)

    def test_shipping_only_does_not_require_unasked_eligibility_or_delivery(self):
        fact = self.fact()
        fact.update(requested_topics=['shipping'], eligibility='not_requested', trusted_facts={}, return_window={})
        fact['shipping'] = {'status': 'conditional_on_self_report', 'payer': 'buyer', 'reason': 'size'}
        fact['user_claims']['values'] = {'reason': 'size'}
        catalog = build_support_claim_catalog(fact)
        self.assertNotIn('eligibility', catalog)
        self.assertNotIn('policy_conditions', catalog)
        self.assertNotIn('order_times', catalog)
        self.assertNotIn('shipping_policy', catalog)
        self.assertEqual(catalog['shipping']['citation_ids'], ['DOC:shipping'])
        text = render_support_claims(self.body(fact), fact)
        self.assertIn('运费由买家承担', text)
        self.assertIn('支付时适用版本2026-11-v1', text)
        self.assertIn('本次仅核对运费，不判断退货资格', text)
        self.assertNotIn('是否未穿着', text)

    def test_unverified_merchant_responsibility_stays_unknown(self):
        fact = self.fact()
        fact.update(status='insufficient_evidence', requested_topics=['eligibility', 'shipping'])
        fact['shipping'] = {'status': 'needs_verified_responsibility', 'payer': None}
        fact['user_claims']['values']['reason'] = 'merchant_responsibility'
        text = render_support_claims(self.body(fact), fact)
        self.assertIn('运费承担尚不能确定', text)
        self.assertIn('不能仅凭自述判定商家承担', text)
        self.assertIn('用户自述（未核实）', text)

    def test_ineligible_keeps_actual_failure_and_exception(self):
        fact = self.fact()
        fact.update(eligibility='ineligible', failed_conditions=['custom_product_excluded'])
        fact['shipping'] = {'status': 'not_applicable', 'payer': None}
        text = render_support_claims(self.body(fact), fact)
        self.assertIn('订单记录为定制商品', text)
        self.assertIn('定制商品不适用', text)
        self.assertIn('不否定其他未接入的售后权利', text)
        self.assertNotIn('补充运费政策', text)

    def test_missing_policy_never_creates_optional_policy_claim(self):
        fact = self.fact()
        fact.update(status='insufficient_evidence', eligibility='undetermined', policy_version=None,
                    citation_index={}, applicable_rules={}, citations={})
        catalog = build_support_claim_catalog(fact)
        self.assertNotIn('shipping_policy', catalog)
        self.assertNotIn('policy_conditions', catalog)
        self.assertNotIn('custom_exception', catalog)
        self.assertIn('evidence_gap', catalog)
        self.assertIn('不能套用其他版本', fallback_support_claims(fact))

    def test_missing_or_conflicting_section_does_not_allow_shipping_supplement(self):
        fact = self.fact()
        del fact['citation_index']['DOC:shipping']
        self.assertNotIn('shipping_policy', build_support_claim_catalog(fact))
        fact = self.fact()
        fact['citation_index']['DOC:shipping']['section'] = 'return_eligibility'
        self.assertNotIn('shipping_policy', build_support_claim_catalog(fact))

    def test_fallback_contains_required_facts_but_not_optional_supplement(self):
        fact = self.fact()
        fallback = fallback_support_claims(fact)
        self.assertEqual(fallback, render_support_claims(self.body(fact), fact))
        self.assertIn('定制商品不适用', fallback)
        self.assertNotIn('补充运费政策', fallback)
        fact['status'] = 'data_error'
        self.assertNotIn('程序计算退货截止', fallback_support_claims(fact))

    def test_contract_rejects_wrong_status_version_types_and_shapes(self):
        fact = self.fact()
        for patch, code in (({'schema_version': 2.0}, 'invalid_schema_version'),
                            ({'schema_version': True}, 'invalid_schema_version'),
                            ({'status': 'clarification'}, 'status_mismatch'),
                            ({'claims': []}, 'invalid_claims'),
                            ({'claims': 'eligibility'}, 'invalid_claims')):
            body = self.body(fact)
            body.update(patch)
            self.assertIn(code, validate_support_claims(body, fact))
        self.assertEqual(validate_support_claims(None, fact), ['invalid_support_structure'])

    def test_model_may_order_claims_and_no_input_result_is_modified(self):
        fact = self.fact()
        before = deepcopy(fact)
        body = self.body(fact)
        body['claims'].reverse()
        body_before = deepcopy(body)
        text = render_support_claims(body, fact)
        self.assertTrue(text.startswith(build_support_claim_catalog(fact)['boundary']['text']))
        self.assertEqual(fact, before)
        self.assertEqual(body, body_before)


if __name__ == '__main__':
    unittest.main()
