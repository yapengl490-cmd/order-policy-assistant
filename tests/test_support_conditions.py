from datetime import datetime, timedelta, timezone
import unittest
from condition_state import reconcile_conditions, validate_tool_arguments, ConditionValidationError

NOW=datetime(2026,11,18,8,0,tzinfo=timezone.utc)
class SupportConditionsTests(unittest.TestCase):
    def state(self,message,previous=None,now=NOW):
        return reconcile_conditions(message,previous,now,'turn')
    def test_explicit_claims_have_source_and_no_refund_cutoff(self):
        text='O_SUPPORT_001能退货吗？鞋子没穿过，吊牌完好，因为尺码不合适，运费谁付？'
        s=self.state(text)
        self.assertTrue(s['ready']);self.assertEqual(s['allowed_tools'],['review_after_sales'])
        self.assertEqual({k:v['value'] for k,v in s['claims'].items()},{'is_unworn':True,'tags_intact':True,'reason':'size'})
        for item in s['claims'].values():
            a,b=item['source']['span'];self.assertEqual(text[a:b],item['source']['quote'])
        self.assertNotIn('refund_before',s['defaults'])
    def test_followup_keeps_order_and_does_not_mark_user_claim_trusted(self):
        s=self.state('O_SUPPORT_001可以退货吗？运费呢？')
        s=self.state('没穿过，吊牌完好，太小',s)
        self.assertEqual(s['confirmed']['order_id']['value'],'O_SUPPORT_001')
        self.assertEqual(s['claims']['reason']['value'],'size')
        self.assertNotIn('delivered_at',s['confirmed'])
    def test_switch_order_drops_previous_claims(self):
        a=self.state('O_SUPPORT_001退货，没穿过，吊牌完好')
        b=self.state('再看看O_SUPPORT_002能退吗',a)
        self.assertTrue(b['new_query']);self.assertEqual(b['claims'],{})
    def test_conflict_blocks_and_later_correction_recovers(self):
        a=self.state('O_SUPPORT_001退货，没穿过，但已经穿过，吊牌完好')
        self.assertFalse(a['ready']);self.assertIn('conflicting_is_unworn',a['issues'])
        b=self.state('更正：已经穿过',a)
        self.assertTrue(b['ready']);self.assertFalse(b['claims']['is_unworn']['value'])
    def test_quoted_hypothetical_and_negated_cause_not_claims(self):
        a=self.state('O_SUPPORT_001退货，手册说“没穿过，吊牌完好”；不是太小')
        self.assertEqual(a['claims'],{})
    def test_missing_order_requires_clarify_then_resume(self):
        a=self.state('我的鞋子能退货吗')
        self.assertTrue(a['needs_clarification'])
        b=self.state('订单号O_SUPPORT_001',a)
        self.assertTrue(b['ready'])
    def test_expiry_drops_identity_and_claims(self):
        a=self.state('O_SUPPORT_001退货，没穿，吊牌完好')
        b=self.state('继续',a,NOW+timedelta(hours=1))
        self.assertTrue(b['expired']);self.assertIn('order_id',b['missing']);self.assertEqual(b['claims'],{})
    def test_switch_to_amount_query_drops_support_state(self):
        a=self.state('O_SUPPORT_001退货，没穿，吊牌完好')
        b=self.state('核查O001退款金额，退款截止2026-11-03零点',a)
        self.assertEqual(b['kind'],'order');self.assertTrue(b['switched']);self.assertNotIn('claims',b)
    def test_model_cannot_inject_scope_or_change_order(self):
        s=self.state('O_SUPPORT_001退货')
        for args in ({'order_id':'O001'},{'order_id':'O_SUPPORT_001','buyer_id':'B2'}):
            with self.assertRaises(ConditionValidationError):
                validate_tool_arguments('review_after_sales',args,s)
    def test_current_policy_cannot_override_payment_version(self):
        s=self.state('O_SUPPORT_001退货，采用最新政策')
        self.assertFalse(s['ready'])
    def test_arbitrary_receipt_claim_not_accepted_as_database_fact(self):
        s=self.state('O_SUPPORT_001退货，我说收货时间2026-11-17T00:00:00+08:00')
        self.assertNotIn('assessment_at',s['confirmed']);self.assertNotIn('delivered_at',s['confirmed'])
    def test_explicit_historical_assessment_is_separate(self):
        s=self.state('O_SUPPORT_001退货，判断时间2026-11-17T00:00:00+08:00')
        self.assertEqual(s['confirmed']['assessment_at']['value'],'2026-11-17T00:00:00+08:00')
    def test_eligibility_alone_does_not_request_shipping_reason(self):
        self.assertEqual(self.state('O_SUPPORT_001可以退货吗')['topics'],['eligibility'])

    def test_changed_tags_clear_old_true(self):
        a=self.state('O001退货，没穿过，吊牌完整，太小了')
        b=self.state('吊牌已经拆了',a)
        self.assertFalse(b['claims']['tags_intact']['value'])
    def test_negated_reason_not_inferred(self):
        a=self.state('O001退货，我没有质量问题，吊牌完整，没穿过，运费谁出？')
        self.assertNotIn('reason',a['claims'])
    def test_multiple_assessment_times_clarifies(self):
        a=self.state('O001退货，判断时间2026-11-16T00:00:00Z，判断时间2026-11-17T00:00:00Z')
        self.assertFalse(a['ready']);self.assertIn('conflicting_assessment_time',a['issues'])

    def test_shipping_only_does_not_silently_request_eligibility(self):
        self.assertEqual(self.state('订单O_SUPPORT_OK运费谁出，尺码太小')['topics'],['shipping'])

    def test_return_shipping_compound_and_no_action_instruction_do_not_add_eligibility(self):
        text = ('请只核对 O_SUPPORT_OK 的退货运费谁承担。尺码不合适；'
                '判断时间2026-11-18T21:00:00+08:00。不要代我办理退货。')
        state = self.state(text, now=datetime(2026,11,19,tzinfo=timezone.utc))
        self.assertEqual(state['topics'], ['shipping'])
        self.assertTrue(state['ready'])
        self.assertEqual(state['claims']['reason']['value'], 'size')
        self.assertNotIn('is_unworn', state['claims'])
        self.assertNotIn('tags_intact', state['claims'])
        start, end = state['claims']['reason']['source']['span']
        self.assertEqual(text[start:end], '尺码不合适')

    def test_compound_return_shipping_variants_remain_fee_queries(self):
        for text in ('O001退货运费谁承担？', 'O001退货的运费谁出？',
                     '只查O001退货 运费谁付，尺码太小'):
            with self.subTest(text=text):
                self.assertEqual(self.state(text)['topics'], ['shipping'])

    def test_explicit_no_action_variants_do_not_cancel_fee_question(self):
        for refusal in ('不要代我办理退货', '不用替我申请退货',
                        '别帮我办理退货', '无需为我处理退货'):
            with self.subTest(refusal=refusal):
                state = self.state('O001退货运费谁承担？' + refusal + '。')
                self.assertEqual(state['topics'], ['shipping'])

    def test_independent_eligibility_question_beside_return_shipping_is_preserved(self):
        for text in ('O001能否退货？退货运费谁承担？',
                     '核对O001退货资格和退货运费，先不要代我办理退货',
                     'O001我想办理退货，同时运费谁承担？'):
            with self.subTest(text=text):
                self.assertEqual(self.state(text)['topics'], ['eligibility','shipping'])

    def test_negative_question_is_not_a_no_action_instruction(self):
        for text in ('O001不能退货吗？', 'O001不可以退货吗？', 'O001不需要退货吗？'):
            with self.subTest(text=text):
                state = self.state(text)
                self.assertEqual(state['kind'], 'support')
                self.assertEqual(state['topics'], ['eligibility'])

    def test_ambiguous_orders_cannot_transfer_old_claims(self):
        a=self.state('O001退货，没穿过吊牌完好')
        b=self.state('O001或O002能退货吗',a)
        c=self.state('订单O002',b)
        self.assertEqual(c['claims'],{})

    def test_current_ambiguous_order_claims_not_bound_to_later_selection(self):
        a=self.state('O001或O002退货，没穿过，吊牌完整')
        b=self.state('订单O002',a)
        self.assertEqual(b['claims'],{})

    def test_unsupported_exchange_arrival_compensation_and_repair_do_not_call_return_tool(self):
        for text in ('订单O001能换货吗？', 'O001退货后多久能到账？', 'O001退款什么时候退回？',
                     'O001能退货吗，能赔偿多少钱？', 'O001想维修，吊牌完好。', 'O001退款到账了吗？'):
            with self.subTest(text=text):
                state=self.state(text)
                self.assertEqual(state['kind'],'support')
                self.assertFalse(state['ready'])
                self.assertEqual(state['allowed_tools'],[])
                self.assertIn('unsupported_support_topic',state['issues'])
                self.assertIn('不能用退货条件代替回答',state['clarification'])
    def test_unknown_business_without_order_explains_scope_not_pointless_order_question(self):
        state=self.state('我想换货')
        self.assertIn('尚未接入换货',state['clarification'])
        self.assertNotEqual(state['clarification'],'请给出要核对的一个订单编号。')
    def test_mixed_supported_unsupported_request_not_marked_fully_answerable(self):
        state=self.state('O001能否退货，运费谁承担，此外能赔付吗？')
        self.assertFalse(state['ready']);self.assertEqual(state['unsupported_topics'],['compensation'])
    def test_continuation_cannot_silently_drop_unsupported_scope(self):
        old=self.state('O001能换货吗，没穿过，吊牌完好')
        for text in ('继续','吊牌完整','再查一下'):
            state=self.state(text,old)
            self.assertFalse(state['ready']);self.assertEqual(state['allowed_tools'],[])
        state=self.state('那只问O001能否退货',old)
        self.assertTrue(state['ready']);self.assertEqual(state['unsupported_topics'],[])
        self.assertEqual(state['topics'],['eligibility'])
    def test_explicitly_excluded_unknown_topics_do_not_block_supported_task(self):
        for text in ('O001不需要换货，只问能否退货', '不问到账时间，O001能退货吗？',
                     '不是问赔偿，O001能退货吗？'):
            with self.subTest(text=text):
                state=self.state(text)
                self.assertTrue(state['ready']);self.assertEqual(state['topics'],['eligibility'])
    def test_negative_question_is_still_an_unsupported_request(self):
        state=self.state('O001不能换货吗？')
        self.assertFalse(state['ready']);self.assertIn('exchange',state['unsupported_topics'])
    def test_negated_exchange_does_not_add_eligibility_to_shipping_only(self):
        state=self.state('O001不需要换货，只问运费谁承担？')
        self.assertTrue(state['ready']);self.assertEqual(state['topics'],['shipping'])
    def test_quoted_unknown_topics_do_not_become_current_request(self):
        state=self.state('手册标题“换货赔偿”；O001能退货吗？')
        self.assertTrue(state['ready']);self.assertEqual(state['unsupported_topics'],[])
    def test_pure_amount_request_not_rerouted_by_negated_exchange(self):
        state=self.state('不用换货，核查O001退款金额，退款截止2026-11-03零点')
        self.assertEqual(state['kind'],'order');self.assertEqual(state['allowed_tools'],['check_order'])
    def test_existing_evidence_explanation_not_mistaken_for_actual_arrival_check(self):
        text='核查O001截至2026-11-03T00:00:00+08:00的支付金额、退款金额和净额，并说明退款记录能否证明真实资金到账'
        state=self.state(text)
        self.assertEqual(state['kind'],'order');self.assertIn('check_order',state['allowed_tools'])

    def test_changing_order_alone_does_not_change_unsupported_business(self):
        old=self.state('O001能换货吗')
        state=self.state('订单O002',old)
        self.assertFalse(state['ready']);self.assertEqual(state['unsupported_topics'],['exchange'])
    def test_double_negative_does_not_exclude_unknown_business(self):
        state=self.state('我不是不想换货，O001能处理吗？')
        self.assertFalse(state['ready']);self.assertEqual(state['unsupported_topics'],['exchange'])

    def test_future_assessment_issue_survives_claim_only_followup_without_default(self):
        a=self.state('O001可以退货吗，判断时间2026-11-20T00:00:00+08:00')
        self.assertIn('invalid_assessment_time',a['issues'])
        for text in ('未穿着，吊牌完好','继续','订单O001'):
            a=self.state(text,a)
            self.assertFalse(a['ready']);self.assertEqual(a['allowed_tools'],[])
            self.assertIn('invalid_assessment_time',a['issues'])
            self.assertNotIn('assessment_at',a['defaults'])
            self.assertIn('不晚于当前时刻',a['clarification'])
        b=self.state('判断时间2026-11-17T00:00:00+08:00',a)
        self.assertTrue(b['ready']);self.assertEqual(b['issues'],[])
        self.assertEqual(b['confirmed']['assessment_at']['value'],'2026-11-17T00:00:00+08:00')

    def test_conflicting_times_require_explicit_valid_time(self):
        a=self.state('O001退货，判断时间2026-11-16T00:00:00Z，判断时间2026-11-17T00:00:00Z')
        b=self.state('没穿过，吊牌完好',a)
        self.assertIn('conflicting_assessment_time',b['issues']);self.assertEqual(b['defaults'],{})
        self.assertIn('只保留一个',b['clarification'])
        c=self.state('判断时间2026-11-17T00:00:00Z',b)
        self.assertTrue(c['ready']);self.assertEqual(c['issues'],[])

    def test_malformed_explicit_time_cannot_silently_default_or_clear_previous_issue(self):
        old=self.state('O001退货，判断时间2026-11-20T00:00:00+08:00')
        for text in ('判断时间不是日期','判断时间2026-02-30T00:00:00+08:00'):
            s=self.state(text,old)
            self.assertIn('invalid_assessment_time',s['issues']);self.assertFalse(s['ready'])
            self.assertEqual(s['defaults'],{})

    def test_new_order_or_explicit_reset_clears_previous_time_issue(self):
        old=self.state('O001退货，判断时间2026-11-20T00:00:00+08:00')
        new=self.state('核对O002退货条件',old)
        self.assertTrue(new['new_query']);self.assertNotIn('invalid_assessment_time',new['issues'])
        self.assertIn('assessment_at',new['defaults'])
        reset=self.state('重置查询',old)
        self.assertTrue(reset['reset']);self.assertEqual(reset['issues'],[])
        self.assertFalse(reset['ready'])

    def test_time_issue_does_not_expire_into_an_implicit_now(self):
        old=self.state('O001退货，判断时间2026-11-20T00:00:00+08:00')
        expired=self.state('继续',old,NOW+timedelta(hours=1))
        self.assertTrue(expired['expired']);self.assertIn('order_id',expired['missing'])
        again=self.state('O001退货，没穿过，吊牌完好',expired,NOW+timedelta(hours=1))
        self.assertFalse(again['ready']);self.assertIn('invalid_assessment_time',again['issues'])
        self.assertEqual(again['defaults'],{})

    def test_reuse_explicit_assessment_preserves_original_source_and_span(self):
        first = reconcile_conditions('O001退货，判断时间2026-11-17T00:00:00+08:00', None, NOW, 'first')
        text = '已经穿过，吊牌仍完好；判断时间沿用刚才'
        state = reconcile_conditions(text, first, NOW + timedelta(minutes=1), 'second')
        self.assertTrue(state['ready'])
        self.assertEqual(state['defaults'], {})
        item = state['confirmed']['assessment_at']
        self.assertEqual(item['value'], first['confirmed']['assessment_at']['value'])
        self.assertEqual(item['source']['origin'], first['confirmed']['assessment_at']['source'])
        self.assertEqual(item['source']['rule'], 'reuse_previous_assessment_time')
        a, b = item['source']['span']
        self.assertEqual(text[a:b], item['source']['quote'])
        self.assertFalse(state['claims']['is_unworn']['value'])
        self.assertTrue(state['claims']['tags_intact']['value'])

    def test_reuse_default_freezes_selected_time_without_claiming_execution(self):
        first = self.state('O001可以退货吗')
        state = self.state('判断时间沿用刚才', first, NOW + timedelta(minutes=1))
        self.assertTrue(state['ready'])
        self.assertEqual(state['confirmed']['assessment_at']['value'], first['defaults']['assessment_at']['value'])
        origin = state['confirmed']['assessment_at']['source']['origin']
        self.assertEqual(origin, first['defaults']['assessment_at']['source'])
        self.assertEqual(origin['rule'], 'program_task_time')
        self.assertIsNone(origin['quote'])

    def test_repeated_reuse_has_bounded_origin_and_does_not_mutate_previous(self):
        from copy import deepcopy
        import json
        first = self.state('O001退货')
        saved = deepcopy(first)
        state = first
        for number in range(20):
            state = reconcile_conditions('判断时间沿用刚才', state, NOW + timedelta(minutes=number + 1), f'ref-{number}')
            self.assertTrue(state['ready'])
            origin = state['confirmed']['assessment_at']['source']['origin']
            self.assertEqual(origin, first['defaults']['assessment_at']['source'])
            self.assertNotIn('origin', origin)
            self.assertLess(len(json.dumps(state['confirmed']['assessment_at']['source'])), 500)
        self.assertEqual(first, saved)

    def test_reuse_requires_same_unexpired_order_and_active_session(self):
        from copy import deepcopy
        first = self.state('O001退货')
        inactive = deepcopy(first); inactive['active'] = False
        cases = [
            ('O001退货，判断时间沿用刚才', None, NOW),
            ('判断时间沿用刚才', first, NOW + timedelta(minutes=30)),
            ('O001退货，判断时间沿用刚才', first, NOW + timedelta(hours=1)),
            ('O002退货，判断时间沿用刚才', first, NOW),
            ('重置查询，O001退货，判断时间沿用刚才', first, NOW),
            ('O001或O002退货，判断时间沿用刚才', first, NOW),
            ('判断时间沿用刚才', inactive, NOW),
            ('O001退货，判断时间沿用刚才', self.state('O001退款金额'), NOW),
            ('O001退货，判断时间沿用刚才', self.state('退货'), NOW),
        ]
        for text, previous, now in cases:
            with self.subTest(text=text, previous=previous, now=now):
                state = self.state(text, previous, now)
                self.assertFalse(state['ready'])
                self.assertEqual(state['allowed_tools'], [])
                self.assertEqual(state['defaults'], {})
                self.assertNotIn('assessment_at', state['confirmed'])
                self.assertIn('missing_assessment_reference', state['issues'])

    def test_failed_reference_stays_pending_until_explicit_correction(self):
        first = self.state('O001退货，判断时间沿用刚才')
        followup = self.state('吊牌仍然完好', first)
        self.assertFalse(followup['ready'])
        self.assertIn('missing_assessment_reference', followup['issues'])
        self.assertEqual(followup['defaults'], {})
        corrected = self.state('判断时间2026-11-17T00:00:00+08:00', followup)
        self.assertTrue(corrected['ready'])
        self.assertEqual(corrected['issues'], [])

    def test_reuse_cannot_clear_previous_time_issues_even_at_context_boundaries(self):
        for initial, expected in (
            ('O001退货，判断时间2026-11-20T00:00:00+08:00', 'invalid_assessment_time'),
            ('O001退货，判断时间2026-11-16T00:00:00Z，判断时间2026-11-17T00:00:00Z', 'conflicting_assessment_time'),
        ):
            first = self.state(initial)
            for text, now in (
                ('判断时间沿用刚才', NOW),
                ('O002退货，判断时间沿用刚才', NOW),
                ('重置查询，O001退货，判断时间沿用刚才', NOW),
                ('O001退货，判断时间沿用刚才', NOW + timedelta(hours=1)),
                ('判断时间沿用刚才；判断时间2026-11-17T00:00:00+08:00', NOW),
            ):
                with self.subTest(initial=initial, text=text):
                    state = self.state(text, first, now)
                    self.assertFalse(state['ready'])
                    self.assertIn(expected, state['issues'])
                    self.assertEqual(state['defaults'], {})
                    self.assertNotIn('assessment_at', state['confirmed'])

    def test_invalid_saved_assessment_never_becomes_now(self):
        from copy import deepcopy
        first = self.state('O001退货，判断时间2026-11-17T00:00:00+08:00')
        for invalid in ('not-a-date', '2026-11-17T00:00:00', '2026-02-30T00:00:00+08:00',
                        '2026-11-20T00:00:00+08:00', None):
            previous = deepcopy(first)
            previous['confirmed']['assessment_at']['value'] = invalid
            with self.subTest(invalid=invalid):
                state = self.state('判断时间沿用刚才', previous)
                self.assertFalse(state['ready'])
                self.assertIn('invalid_assessment_time', state['issues'])
                self.assertEqual(state['defaults'], {})

    def test_two_incompatible_saved_sources_require_clarification(self):
        from copy import deepcopy
        first = self.state('O001退货，判断时间2026-11-17T00:00:00+08:00')
        previous = deepcopy(first)
        previous['defaults'] = self.state('O001退货')['defaults']
        state = self.state('判断时间沿用刚才', previous)
        self.assertFalse(state['ready'])
        self.assertIn('conflicting_assessment_time', state['issues'])
        self.assertEqual(state['defaults'], {})

    def test_default_from_blocked_previous_task_is_not_reused(self):
        previous = self.state('O001退货，没穿过，但已经穿过')
        self.assertFalse(previous['ready'])
        state = self.state('判断时间沿用刚才', previous)
        self.assertFalse(state['ready'])
        self.assertIn('missing_assessment_reference', state['issues'])
        self.assertEqual(state['defaults'], {})

    def test_explicit_new_time_replaces_reference_and_old_time_issue(self):
        first = self.state('O001退货')
        prior = self.state('判断时间沿用刚才', first)
        new = self.state('判断时间改为2026-11-16T00:00:00+08:00', prior)
        self.assertTrue(new['ready'])
        self.assertEqual(new['confirmed']['assessment_at']['value'], '2026-11-16T00:00:00+08:00')
        self.assertEqual(new['confirmed']['assessment_at']['source']['rule'], 'iso_time_explicit_timezone')
        self.assertNotIn('origin', new['confirmed']['assessment_at']['source'])

    def test_new_time_and_reference_conflict_without_selecting_either(self):
        previous = self.state('O001退货，判断时间2026-11-17T00:00:00+08:00')
        for text in ('判断时间沿用刚才；判断时间2026-11-16T00:00:00+08:00',
                     '判断时间2026-11-16T00:00:00+08:00；判断时间沿用刚才'):
            state = self.state(text, previous)
            self.assertFalse(state['ready'])
            self.assertIn('conflicting_assessment_time', state['issues'])
            self.assertEqual(state['defaults'], {})
            self.assertNotIn('assessment_at', state['confirmed'])

    def test_reference_and_same_instant_with_different_timezone_are_consistent(self):
        previous = self.state('O001退货，判断时间2026-11-17T00:00:00+08:00')
        state = self.state('判断时间沿用刚才；判断时间2026-11-16T16:00:00Z', previous)
        self.assertTrue(state['ready'])
        self.assertEqual(state['confirmed']['assessment_at']['value'], '2026-11-17T00:00:00+08:00')

    def test_reference_cannot_hide_bare_invalid_or_unscoped_dates(self):
        previous = self.state('O001退货，判断时间2026-11-17T00:00:00+08:00')
        for text in ('判断时间沿用刚才，2026-11-16',
                     '判断时间沿用刚才；另外这里有一个时间2026-11-16T00:00:00+08:00',
                     '判断时间沿用刚才；判断时间2026-02-30T00:00:00+08:00'):
            state = self.state(text, previous)
            self.assertFalse(state['ready'])
            self.assertEqual(state['defaults'], {})
            self.assertNotIn('assessment_at', state['confirmed'])

    def test_reference_requires_complete_assertion_not_prefix_or_question(self):
        previous = self.state('O001退货，判断时间2026-11-17T00:00:00+08:00')
        for text in ('判断时间沿用刚才的明天', '判断时间沿用刚才再加一天',
                     '判断时间沿用刚才？', '是否判断时间沿用刚才', '不要判断时间沿用刚才'):
            state = self.state(text, previous)
            self.assertFalse(state['ready'])
            self.assertEqual(state['defaults'], {})
            self.assertNotIn('assessment_at', state['confirmed'])

    def test_quoted_or_hypothetical_reference_does_not_suppress_explicit_new_time(self):
        previous = self.state('O001退货，判断时间2026-11-17T00:00:00+08:00')
        for prefix in ("'判断时间沿用刚才'只是例子；", '‘判断时间沿用刚才’只是例子；',
                       '“判断时间沿用刚才”只是例子；', '如果判断时间沿用刚才；',
                       '例如判断时间沿用刚才；'):
            text = prefix + '判断时间2026-11-16T00:00:00+08:00'
            state = self.state(text, previous)
            self.assertTrue(state['ready'])
            self.assertEqual(state['confirmed']['assessment_at']['value'], '2026-11-16T00:00:00+08:00')

    def test_still_intact_variants_remain_unverified_and_preserve_original_span(self):
        for phrase in ('吊牌仍完好', '吊牌仍然完好', '吊牌完好', '吊牌还在'):
            text = 'O001能退货吗？' + phrase + '。'
            state = self.state(text)
            item = state['claims']['tags_intact']
            self.assertTrue(item['value'])
            self.assertEqual(item['source']['rule'], 'explicit_user_claim_unverified')
            self.assertNotIn('tags_intact', state['confirmed'])
            a, b = item['source']['span']
            self.assertEqual(text[a:b], phrase)
            self.assertEqual(item['source']['quote'], phrase)

    def test_still_intact_negations_questions_quotes_and_hypotheses_are_not_true(self):
        phrases = ('不是吊牌仍完好', '并非吊牌仍然完好', '吊牌仍不完好', '吊牌仍然不完好',
                   '吊牌仍完好吗', '吊牌仍然完好？', '吊牌仍完好还是破损',
                   '是否吊牌仍完好', '不能确认吊牌仍完好', '吊牌仍完好不属实',
                   '“吊牌仍完好”', '‘吊牌仍完好’', "'吊牌仍完好'", '「吊牌仍然完好」',
                   '```吊牌仍完好```', '如果吊牌仍完好', '假设吊牌仍然完好',
                   '假如吊牌仍完好', '他说吊牌仍完好', '例如吊牌仍完好')
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                state = self.state('O001退货；' + phrase)
                self.assertIsNot(state['claims'].get('tags_intact', {}).get('value'), True)

    def test_still_intact_conflict_drops_previous_true(self):
        previous = self.state('O001退货，吊牌完整')
        for phrase in ('吊牌仍完好，但吊牌已经拆了', '吊牌仍然完好，吊牌不完好'):
            state = self.state(phrase, previous)
            self.assertFalse(state['ready'])
            self.assertIn('conflicting_tags_intact', state['issues'])
            self.assertNotIn('tags_intact', state['claims'])

    def test_tag_extension_does_not_reroute_amount_query(self):
        previous = self.state('O001退货，吊牌仍完好')
        state = self.state('核查O001退款金额，退款截止2026-11-03零点；吊牌仍然完好', previous)
        self.assertEqual(state['kind'], 'order')
        self.assertEqual(state['allowed_tools'], ['check_order'])
        self.assertNotIn('claims', state)
