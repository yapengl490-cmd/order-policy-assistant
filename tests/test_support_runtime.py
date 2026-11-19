from datetime import datetime, timezone
import json
import unittest
import httpx
from types import SimpleNamespace
from agent_runtime import run_agent
from condition_state import reconcile_conditions
from support_runtime import expected_decision, validate_support_answer

class SupportRuntimeTests(unittest.TestCase):
    def fact(self,status='ok'):
        return {'status':status,'order_id':'O_SUPPORT_OK','buyer_id':'B001','eligibility':'eligible_with_self_report',
            'shipping':{'payer':'buyer'},'policy_version':'v2','message':'演示','citations':{},
            'requested_topics':['eligibility','shipping'],
            'applicable_rules':{'return_window_hours':336,'requires_unworn':True,'requires_tags':True,'custom_excluded':True},
            'citation_index':{'DOC:2':{'chunk_id':'DOC:2','text':'未穿且吊牌完整；定制除外。','line_start':4,'line_end':6}}}
    def cfg(self):
        return SimpleNamespace(model_api_key='sk-offline-testing',model_base_url='https://api.deepseek.com',model_name='deepseek-flash',
            max_model_calls=4,max_tool_calls=6,model_timeout_seconds=30,max_output_tokens=1200)
    def execute(self, fact=None, body=None, calls=True):
        fact=fact or self.fact(); body=body or {'status':'completed','answer':'依订单记录和未核实自述，暂满足演示条件。','decision':expected_decision(fact),'citation_ids':['DOC:2']}
        replies=[]
        if calls:replies.append({'choices':[{'finish_reason':'tool_calls','message':{'role':'assistant','content':None,'tool_calls':[{
            'id':'a','type':'function','function':{'name':'review_after_sales','arguments':'{"order_id":"O_SUPPORT_OK"}'}}]}}]})
        replies.append({'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(body,ensure_ascii=False)}}]})
        requests=[]
        def send(req):
            requests.append(json.loads(req.content));return httpx.Response(200,json=replies.pop(0))
        state=reconcile_conditions('O_SUPPORT_OK退货运费谁承担？没穿，吊牌完好，尺码太小',None,datetime(2026,11,18,tzinfo=timezone.utc),'t')
        result=run_agent(self.cfg(),None,state['original_question'],'B001',[],lambda:False,
              condition_state=state,transport=httpx.MockTransport(send),tool_handlers={'review_after_sales':lambda **kwargs:fact})
        return result,requests
    def test_model_tool_loop_brief_answer_and_full_sources_preserved(self):
        result,requests=self.execute()
        self.assertEqual(result['status'],'completed');self.assertTrue(result['support_validation']['passed'])
        self.assertIn('[DOC:2]',result['answer']);self.assertIn('定制商品除外',result['answer'])
        self.assertNotIn('第4–6行',result['answer']);self.assertNotIn('程序核查结论',result['answer'])
        self.assertEqual(result['facts'][0]['result']['citation_index']['DOC:2']['text'],'未穿且吊牌完整；定制除外。')
        self.assertEqual([e['id'] for e in result['skills']],['buyer_support'])
        self.assertIn('买家订单与适用政策核对',requests[0]['messages'][0]['content'])
        self.assertNotIn('# 只读订单金额核查',requests[0]['messages'][0]['content'])
        self.assertEqual([x['function']['name'] for x in requests[0]['tools']],['review_after_sales'])
        tool=json.loads(requests[1]['messages'][-1]['content']);self.assertNotIn('buyer_id',tool)
    def test_wrong_reference_preserves_raw_and_falls_back(self):
        fact=self.fact();body={'status':'completed','answer':'错误引用示例','decision':expected_decision(fact),'citation_ids':['OTHER:1']}
        result,_=self.execute(fact,body)
        self.assertFalse(result['support_validation']['passed']);self.assertIn('程序依据事实',result['answer'])
        self.assertIn('OTHER:1',json.dumps(result['trace']))
        self.assertNotIn('错误引用示例',result['answer'])
    def test_no_tool_answer_is_not_success(self):
        result,_=self.execute(calls=False);self.assertEqual(result['status'],'failed')
    def test_changed_decision_not_accepted(self):
        fact=self.fact();body={'status':'completed','answer':'商家承担','decision':{**expected_decision(fact),'shipping_payer':'merchant'},'citation_ids':['DOC:2']}
        result,_=self.execute(fact,body);self.assertIn('decision_mismatch',result['support_validation']['errors'])
    def test_unsupported_approval_is_not_success_explanation(self):
        body={'status':'completed','answer':'已经批准退货','decision':expected_decision(self.fact()),'citation_ids':['DOC:2']}
        self.assertIn('unsupported_execution_claim',validate_support_answer(body,self.fact()))
    def test_required_clarification_status_cannot_be_marked_completed(self):
        fact=self.fact('clarification');body={'status':'completed','answer':'完成','decision':expected_decision(fact),'citation_ids':['DOC:2']}
        result,_=self.execute(fact,body);self.assertEqual(result['status'],'clarification')

    def test_correct_json_with_opposite_body_is_rejected(self):
        fact=self.fact();body={'status':'completed','answer':'不能退货，运费由商家承担。','decision':expected_decision(fact),'citation_ids':['DOC:2']}
        result,_=self.execute(fact,body)
        self.assertFalse(result['support_validation']['passed'])
        self.assertIn('eligibility_text_contradiction',result['support_validation']['errors'])

    def test_scope_citation_cannot_replace_required_clause(self):
        fact=self.fact();fact['required_citation_ids']=['RETURN:1'];fact['citation_index']['RETURN:1']={'text':'条件'}
        body={'status':'completed','answer':'条件性可退','decision':expected_decision(fact),'citation_ids':['DOC:2']}
        self.assertIn('required_clause_citations_missing',validate_support_answer(body,fact))

    def body(self, fact, answer):
        return {'status':'clarification' if fact['status']=='clarification' else 'completed',
                'answer':answer,'decision':expected_decision(fact),'citation_ids':list(fact['citation_index'])}

    def dated_fact(self):
        fact = self.fact()
        fact.update(as_of='2026-11-18T13:00:00+00:00',
                    trusted_facts={'delivered_at':'2026-11-16T02:00:00+00:00'},
                    return_window={'starts_at':'2026-11-16T02:00:00+00:00',
                                   'before':'2026-11-30T02:00:00+00:00'})
        return fact

    def test_wrong_timezone_relabel_is_rejected_and_raw_output_kept(self):
        fact = self.dated_fact()
        answer = '按本次记录，退货申请即截止2026-11-30T02:00:00+08:00之前，不含该时刻。'
        got, _ = self.execute(fact, self.body(fact, answer))
        self.assertIn('return_deadline_date_mismatch', got['support_validation']['errors'])
        self.assertIn('程序依据事实', got['answer'])
        self.assertNotIn('2026-11-30T02:00:00+08:00', got['answer'])
        self.assertIn(answer, json.dumps(got['trace'], ensure_ascii=False))
        self.assertEqual(got['facts'][0]['result']['return_window']['before'], fact['return_window']['before'])

    def test_same_deadline_in_utc_local_iso_and_chinese_is_allowed(self):
        fact = self.dated_fact()
        for stamp in ('2026-11-30T02:00:00+00:00', '2026-11-30T02:00:00Z',
                      '2026-11-30T10:00:00+08:00', '2026-11-30 10:00',
                      '2026年11月30日10点', '2026年11月30日10时00分',
                      '2026-11-30 02:00（UTC）', '2026年11月30日02:00 UTC',
                      '2026-11-30 10:00（UTC+08:00）'):
            with self.subTest(stamp=stamp):
                answer = '退货截止为' + stamp + '，不含该时刻。'
                self.assertEqual(validate_support_answer(self.body(fact, answer), fact), [])

    def test_date_guard_uses_actual_tool_values_instead_of_fixed_fixture(self):
        fact = self.dated_fact()
        fact['return_window']['before'] = '2027-02-08T19:15:00+00:00'
        good = '退货截止2027-02-09T03:15:00+08:00。'
        bad = '退货截止2027-02-08T19:15:00+08:00。'
        self.assertEqual(validate_support_answer(self.body(fact, good), fact), [])
        self.assertIn('return_deadline_date_mismatch', validate_support_answer(self.body(fact, bad), fact))

    def test_delivery_and_assessment_dates_are_checked_against_own_fields(self):
        fact = self.dated_fact()
        for answer, error in (
            ('收货时间2026-11-16T02:00:00+08:00。', 'delivery_date_mismatch'),
            ('判断时间2026-11-18T13:00:00+08:00。', 'assessment_date_mismatch'),
            ('在2026-11-18T13:00:00+08:00时点，核对本单。', 'assessment_date_mismatch'),
            ('收货时间2026年11月17日10点。', 'delivery_date_mismatch'),
        ):
            with self.subTest(answer=answer):
                self.assertIn(error, validate_support_answer(self.body(fact, answer), fact))
        answer = ('收货时间2026年11月16日10点，判断时间2026-11-18T21:00:00+08:00；'
                  '收货后336小时，即截止2026-11-30T10:00:00+08:00之前。')
        self.assertEqual(validate_support_answer(self.body(fact, answer), fact), [])

    def test_delivery_date_without_clock_only_compares_calendar_date(self):
        fact = self.dated_fact()
        for answer in ('收货日期2026-11-16。', '收货日期2026年11月16日。'):
            self.assertEqual(validate_support_answer(self.body(fact, answer), fact), [])
        wrong = '收货日期2026年11月17日。'
        self.assertIn('delivery_date_mismatch', validate_support_answer(self.body(fact, wrong), fact))

    def test_bare_deadline_does_not_silently_expand_to_whole_day(self):
        fact = self.dated_fact()
        for answer in ('退货截止2026-11-30。', '退货截止日期2026年11月30日。'):
            self.assertIn('return_deadline_time_missing', validate_support_answer(self.body(fact, answer), fact))
        wrong = '退货截止2026-12-01。'
        self.assertIn('return_deadline_date_mismatch', validate_support_answer(self.body(fact, wrong), fact))

    def test_missing_delivery_or_window_does_not_allow_fabricated_exact_date(self):
        fact = self.dated_fact()
        fact['trusted_facts'] = {}
        fact['return_window'] = {}
        for answer, error in (('收货时间2026-11-16T10:00:00+08:00。', 'delivery_date_unavailable'),
                              ('退货截止2026-11-30T10:00:00+08:00。', 'return_deadline_date_unavailable')):
            self.assertIn(error, validate_support_answer(self.body(fact, answer), fact))

    def test_invalid_explicit_date_is_not_accepted(self):
        fact = self.dated_fact()
        for answer in ('退货截止2026-11-31T10:00:00+08:00。', '退货截止2026年11月30日25点。'):
            self.assertIn('return_deadline_date_invalid', validate_support_answer(self.body(fact, answer), fact))

    def test_unrelated_dates_and_explicit_hypotheses_are_not_actual_time_claims(self):
        fact = self.dated_fact()
        for answer in ('政策版本生效2026-11-01，按支付时选择。',
                       '如果收货时间2026-11-20T10:00:00+08:00，仍须核对真实记录。',
                       '请核对收货时间是否2026-11-20T10:00:00+08:00。'):
            self.assertEqual(validate_support_answer(self.body(fact, answer), fact), [])

    def test_old_policy_false_positive_body_falls_back_and_keeps_raw(self):
        fact=self.fact();fact.update(eligibility='ineligible',policy_version='2026-10-v1')
        answer='按11月新规享受14天退货期，现在仍能退货。'
        got,_=self.execute(fact,self.body(fact,answer))
        self.assertFalse(got['support_validation']['passed'])
        self.assertIn('eligibility_text_contradiction',got['support_validation']['errors'])
        self.assertNotIn(answer,got['answer']);self.assertIn(answer,json.dumps(got['trace'],ensure_ascii=False))
        self.assertIn('2026-10-v1',got['answer'])

    def test_missing_delivery_does_not_accept_fabricated_backend_receipt(self):
        fact=self.fact('clarification');fact.update(eligibility='undetermined',missing=['delivered_at'])
        for answer in ('后台显示昨天已签收，商品符合条件，可以退货。','系统已确认已经收货。'):
            got,_=self.execute(fact,self.body(fact,answer))
            self.assertEqual(got['status'],'clarification')
            self.assertIn('unavailable_delivery_asserted',got['support_validation']['errors'])
            self.assertNotIn(answer,got['answer'])

    def test_self_report_is_not_unconditional_approval(self):
        fact=self.fact();answer='您的商品已经符合全部退货条件，直接寄回即可。'
        got,_=self.execute(fact,self.body(fact,answer))
        self.assertIn('self_report_upgraded',got['support_validation']['errors'])
        self.assertNotIn(answer,got['answer'])

    def test_no_authorized_order_does_not_accept_fabricated_amount(self):
        fact=self.fact('not_found');fact.update(eligibility='undetermined',citation_index={},shipping={'payer':None})
        answer='已查到订单，支付125.00元，退货运费由买家承担。'
        got,_=self.execute(fact,self.body(fact,answer))
        self.assertIn('unavailable_order_asserted',got['support_validation']['errors'])
        self.assertNotIn('125.00元',got['answer'])

    def test_negation_condition_and_quoted_rule_are_not_actual_positive_decisions(self):
        fact=self.fact('clarification');fact.update(eligibility='undetermined',missing=['delivered_at'])
        for answer in ('不能确认已经签收，因此不能认定可以退货。',
                       '如果后台确认已收货，且其他条件满足，才可以退货。',
                       '若后台已签收，才可以申请退货。',
                       '需核对后台是否已经签收，尚未确认能否退货。',
                       '不能确认“后台显示昨天已签收，可以退货”这一说法。'):
            self.assertEqual(validate_support_answer(self.body(fact,answer),fact),[],answer)
        eligible=self.fact()
        for answer in ('不代表已经批准退货，依未核实自述暂满足条件。',
                       '政策原文：“定制商品不能退货”；本单仍需核验。',
                       '如果存在商家责任，才由商家承担运费；当前按尺码自述由买家承担。'):
            self.assertEqual(validate_support_answer(self.body(eligible,answer),eligible),[],answer)

    def test_conditional_prefix_does_not_hide_later_direct_reversal(self):
        fact=self.fact();fact['eligibility']='ineligible'
        answer='如果满足条件，可以退货；但本订单现在仍能退货。'
        self.assertIn('eligibility_text_contradiction',validate_support_answer(self.body(fact,answer),fact))

    def test_inline_complete_reference_must_belong_to_current_sources(self):
        fact=self.fact()
        wrong='依自述暂满足条件。[OTHER_STORE:POLICY-999]'
        got,_=self.execute(fact,self.body(fact,wrong))
        self.assertIn('invalid_inline_citation',got['support_validation']['errors'])
        self.assertNotIn('OTHER_STORE:POLICY-999',got['answer'])
        for valid in ('依自述暂满足条件。[DOC:2]', '运费条款见[shipping_cost]，当前按尺码自述由买家承担。'):
            self.assertEqual(validate_support_answer(self.body(fact,valid),fact),[])

    def test_short_delivery_preserves_exception_missing_and_shipping_uncertainty(self):
        fact=self.fact('clarification');fact.update(eligibility='undetermined',missing=['delivered_at'])
        fact['shipping']={'payer':None,'status':'needs_verified_responsibility'}
        got,_=self.execute(fact,self.body(fact,'尚不能确定退货资格，请先核查后台。'))
        for phrase in ('336小时','截止时刻不含','未穿着','吊牌完好','定制商品除外','后台收货时间','运费承担未确定','自述未核实'):
            self.assertIn(phrase,got['answer'])
        self.assertEqual(got['facts'][0]['result'],{k:v for k,v in fact.items() if k!='buyer_id'})

    def test_rejected_answer_no_long_original_quote_or_duplicate_fixed_summary(self):
        fact=self.fact();answer='已经批准退货'
        got,_=self.execute(fact,self.body(fact,answer))
        self.assertIn('程序依据事实',got['answer'])
        self.assertEqual(got['answer'].count('政策v2'),1)
        self.assertNotIn('政策原文依据',got['answer'])
        self.assertIn(answer,json.dumps(got['trace'],ensure_ascii=False))

    def test_missing_trusted_skill_stops_before_model(self):
        from unittest.mock import patch
        with patch('agent_runtime.load_skill',side_effect=ValueError('missing')):
            got,requests=self.execute()
        self.assertEqual(got['status'],'failed');self.assertEqual(requests,[])
        self.assertEqual(got['usage']['model_requests'],0)

    def test_parallel_negation_is_not_an_execution_promise_but_turning_positive_is(self):
        fact=self.fact()
        for answer in ('你的自述尚未经后台核实，这不构成退货批准或已受理。',
                       '这不构成已批准或已退款或已受理。',
                       '这不意味着已受理。'):
            self.assertNotIn('unsupported_execution_claim',validate_support_answer(self.body(fact,answer),fact),answer)
        for answer in ('不构成退货批准，但已受理。',
                       '不构成退货批准，但是已受理。',
                       '不构成退货批准或已受理，但已经退款。',
                       '不构成退货批准或已受理但是已经退款。',
                       '已受理。'):
            self.assertIn('unsupported_execution_claim',validate_support_answer(self.body(fact,answer),fact),answer)

    def test_invalid_time_claim_followup_does_not_start_model_or_tool(self):
        now=datetime(2026,11,18,8,tzinfo=timezone.utc)
        old=reconcile_conditions('O001能退货吗，判断时间2026-11-20T00:00:00+08:00',None,now,'first')
        state=reconcile_conditions('未穿着，吊牌完好',old,now,'followup')
        def forbidden(*args,**kwargs):
            self.fail('unresolved assessment time must stop before model or tools')
        result=run_agent(self.cfg(),None,state['original_question'],'B001',[],lambda:False,
                        condition_state=state,transport=httpx.MockTransport(forbidden),tool_handlers={'review_after_sales':forbidden})
        self.assertEqual(result['status'],'clarification')
        self.assertIn('判断时间不合法',result['answer'])
        self.assertEqual(result['usage']['model_requests'],0)
        self.assertEqual(result['usage']['tool_calls'],0)
