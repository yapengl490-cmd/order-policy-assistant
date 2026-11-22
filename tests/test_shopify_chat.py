"""Offline platform chat routing, state, authorization gates and deterministic rendering."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from shopify_chat import (ShopifyChatError, uses_shopify, reconcile_shopify,
                         make_shopify_handler, render_shopify_fact)
from shopify_connection import ShopifySettings, ShopifyConnectionError

NOW=datetime(2026,11,20,7,tzinfo=timezone.utc)
SHOP='offline-demo.myshopify.com'


def fact():
    return {'status':'ok','platform':'shopify','shop_domain':SHOP,'order_id':'gid://shopify/Order/1001','order_name':'#1001',
            'currency':'USD','minor_unit_digits':2,'paid_amount_minor':10000,'refund_amount_minor':3000,'net_amount_minor':7000,
            'refund_before':'2026-11-20T07:00:00+00:00','successful_payments':[{'transaction_id':'gid://shopify/OrderTransaction/1',
                'kind':'CAPTURE','status':'SUCCESS','processed_at':'2026-11-20T06:15:00+00:00','currency':'USD','amount_minor':10000}],
            'successful_refunds':[{'transaction_id':'gid://shopify/OrderTransaction/2','kind':'REFUND','status':'SUCCESS',
                'processed_at':'2026-11-20T06:30:00+00:00','currency':'USD','amount_minor':3000,'included_before_cutoff':True}]}


class ShopifyChatConditionsTests(unittest.TestCase):
    def state(self,text,old=None,now=NOW): return reconcile_shopify(text,old,now,'turn')

    def test_explicit_platform_name_hash_and_gid_route_without_local_orders(self):
        for text in ('Shopify退货政策','查Shopify订单金额','查询#1001退款金额','#1002', 'gid://shopify/Order/19036071362781'):
            with self.subTest(text=text): self.assertTrue(uses_shopify(text))
        for text in ('查O001退款金额','S001普通鞋类退货政策','给我推荐鞋子','“Shopify #1001”只是例子'):
            self.assertFalse(uses_shopify(text))
        old=self.state('Shopify #1001退款金额')
        self.assertTrue(uses_shopify('截止2026-11-20T14:00:00+08:00',old))
        self.assertFalse(uses_shopify('换成O001',old))

    def test_default_cutoff_is_fixed_with_source_and_not_a_confirmed_user_time(self):
        state=self.state('Shopify #1001退款金额')
        self.assertTrue(state['ready']);self.assertEqual(state['kind'],'shopify_order')
        self.assertEqual(state['allowed_tools'],['shopify_check_order'])
        self.assertNotIn('refund_before',state['confirmed'])
        default=state['defaults']['refund_before']
        self.assertEqual(default['value'],'2026-11-20T15:00:00+08:00')
        self.assertEqual(default['source']['rule'],'program_task_time')
        self.assertIsNone(default['source']['quote'])
        follow=self.state('继续',state,NOW+timedelta(minutes=1))
        self.assertEqual(follow['defaults']['refund_before'],default)

    def test_explicit_time_and_order_source_retain_original_spans(self):
        text='请查询Shopify #1002，退款截止2026-11-20T14:00:00+08:00'
        state=self.state(text)
        self.assertTrue(state['ready']);self.assertEqual(state['defaults'],{})
        for item in state['confirmed'].values():
            a,b=item['source']['span'];self.assertEqual(text[a:b],item['source']['quote'])
        text='查Shopify #1001截至2026-11-20T14:30:00+08:00的支付金额、退款金额和净额'
        self.assertTrue(self.state(text)['ready'])

    def test_missing_order_is_clarified_then_same_session_fills_it(self):
        first=self.state('Shopify退款金额，截止2026-11-20T14:00:00+08:00')
        self.assertEqual(first['missing'],['order_id'])
        self.assertFalse(first['ready'])
        second=self.state('#1002',first)
        self.assertTrue(second['ready']);self.assertEqual(second['confirmed']['refund_before'],first['confirmed']['refund_before'])

    def test_invalid_time_does_not_silently_default_and_later_date_can_correct(self):
        for text in ('Shopify #1001截止昨天','Shopify #1001截止2026-11-20',
                     'Shopify #1001截止2026-02-30T00:00:00Z','Shopify #1001截止2026-11-21T00:00:00Z'):
            state=self.state(text)
            self.assertIn('invalid_refund_before',state['issues']);self.assertEqual(state['defaults'],{})
            again=self.state('继续',state)
            self.assertFalse(again['ready']);self.assertEqual(again['defaults'],{})
            corrected=self.state('2026-11-20T14:00:00+08:00',again)
            self.assertTrue(corrected['ready'])

    def test_valid_time_cannot_hide_second_unparsed_or_conflicting_cutoff(self):
        for text in ('Shopify #1001截止2026-11-20T14:00:00+08:00，截止改成最近一次',
                     'Shopify #1001截止2026-11-20T14:00:00+08:00，截止2026-11-20T13:00:00+08:00'):
            state=self.state(text)
            self.assertFalse(state['ready']);self.assertNotIn('refund_before',state['confirmed']);self.assertEqual(state['defaults'],{})

    def test_order_creation_or_payment_date_is_not_misused_as_refund_cutoff(self):
        for text in ('Shopify #1001创建时间2026-11-20T06:00:00Z', 'Shopify #1001支付时间2026-11-20T06:00:00Z'):
            state=self.state(text)
            self.assertFalse(state['ready']);self.assertNotIn('refund_before',state['confirmed']);self.assertEqual(state['defaults'],{})

    def test_switch_reset_and_expiry_do_not_transfer_order_cutoff(self):
        first=self.state('Shopify #1001截止2026-11-20T14:00:00+08:00')
        switch=self.state('#1002',first)
        self.assertTrue(switch['new_query']);self.assertNotIn('refund_before',switch['confirmed'])
        expired=self.state('继续',first,NOW+timedelta(minutes=30))
        self.assertTrue(expired['expired']);self.assertFalse(expired['ready']);self.assertEqual(expired['missing'],['order_id'])
        reset=self.state('重置查询',first)
        self.assertFalse(reset['ready']);self.assertTrue(reset['reset']);self.assertNotIn('order_id',reset['confirmed'])

    def test_multiple_or_mixed_orders_are_not_routed_to_old_amount_tool(self):
        for text in ('Shopify #1001和#1002退款金额','Shopify #1001和O001退款金额'):
            state=self.state(text)
            self.assertFalse(state['ready']);self.assertEqual(state['allowed_tools'],[])

    def test_reuse_cutoff_requires_valid_history_and_does_not_nest_origin(self):
        first=self.state('Shopify #1001退款金额')
        state=self.state('退款截止沿用刚才',first)
        self.assertTrue(state['ready'])
        self.assertEqual(state['confirmed']['refund_before']['value'],first['defaults']['refund_before']['value'])
        for _ in range(4): state=self.state('退款截止保持不变',state)
        self.assertNotIn('origin',state['confirmed']['refund_before']['source']['origin'])
        for text,old in (('Shopify #1001，退款截止沿用刚才',None),('#1002退款截止沿用刚才',first),('退款截止沿用刚才？',first)):
            invalid=self.state(text,old)
            self.assertFalse(invalid['ready']);self.assertEqual(invalid['defaults'],{})

    def test_reference_conflict_or_previous_time_issue_cannot_be_cleared(self):
        first=self.state('Shopify #1001截止2026-11-20T14:00:00+08:00')
        state=self.state('退款截止沿用刚才；截止2026-11-20T13:00:00+08:00',first)
        self.assertIn('conflicting_refund_before',state['issues'])
        bad=self.state('Shopify #1001截止昨天')
        state=self.state('退款截止沿用刚才',bad)
        self.assertIn('invalid_refund_before',state['issues']);self.assertEqual(state['defaults'],{})

    def test_current_policy_is_separate_and_never_applies_s001_or_payment_history(self):
        current=self.state('Shopify当前退货政策')
        self.assertTrue(current['ready']);self.assertEqual(current['kind'],'shopify_policy')
        self.assertEqual(current['allowed_tools'],['shopify_current_policy'])
        for text in ('Shopify历史退货政策','Shopify订单支付时的政策','Shopify政策截止2026-11-19T00:00:00Z'):
            state=self.state(text)
            self.assertFalse(state['ready']);self.assertIn('historical_platform_policy_not_available',state['issues'])
        unsupported=self.state('Shopify #1001能退货吗')
        self.assertFalse(unsupported['ready']);self.assertIn('platform_after_sales_or_arrival_not_supported',unsupported['issues'])


class ShopifyChatHandlerTests(unittest.TestCase):
    def state(self,policy=False): return reconcile_shopify('Shopify当前退款政策' if policy else 'Shopify #1001退款金额',None,NOW,'turn')
    def factory(self,**kwargs):
        return make_shopify_handler(self.state(),access_role='merchant',platform_context={'shop_domain':SHOP,'allow_network':True},**kwargs)

    def test_missing_binding_buyer_and_disabled_rejected_before_credentials(self):
        for role,context in (('buyer',{'shop_domain':SHOP,'allow_network':True}),('merchant',None),
                             ('merchant',{'shop_domain':SHOP}),('merchant',{'shop_domain':SHOP,'allow_network':'true'}),
                             ('merchant',{'shop_domain':'https://evil.example','allow_network':True})):
            with patch('shopify_chat.ShopifySettings.load',side_effect=AssertionError('must not read credentials')):
                with self.assertRaises(ShopifyChatError): make_shopify_handler(self.state(),access_role=role,platform_context=context)

    def test_factory_does_no_io_and_handler_is_zero_argument_server_injection(self):
        with patch('shopify_chat.ShopifySettings.load') as load, patch('shopify_chat.ShopifyReadClient') as cls:
            handler=self.factory(settings_path=Path('/offline/unused.json'))
            load.assert_not_called();cls.assert_not_called()
            self.assertEqual(len(inspect.signature(handler).parameters),0)
            cls.return_value.read_order.return_value=fact()
            result=handler()
            load.assert_called_once_with(Path('/offline/unused.json'))
            cls.return_value.read_order.assert_called_once_with('#1001',refund_before='2026-11-20T15:00:00+08:00')
            self.assertEqual(result['net_amount_minor'],7000)
            self.assertEqual(set(result['citation_index']),{'SHOPIFY-ORDER:1001','SHOPIFY-TX:1','SHOPIFY-TX:2'})
            self.assertEqual(len(result['source_snapshot_sha256']),64)
            with self.assertRaises(TypeError): handler(order_id='#1002')

    def test_handler_uses_state_snapshot_and_does_not_follow_later_mutation(self):
        state=self.state()
        with patch('shopify_chat.ShopifySettings.load'),patch('shopify_chat.ShopifyReadClient') as cls:
            cls.return_value.read_order.return_value=fact()
            handler=make_shopify_handler(state,access_role='merchant',platform_context={'shop_domain':SHOP,'allow_network':True})
            state['confirmed']['order_id']['value']='#9999'
            handler();self.assertEqual(cls.return_value.read_order.call_args.args,('#1001',))

    def test_invalid_state_rejected_before_io(self):
        state=self.state();state['confirmed']['order_id']['value']='O001'
        with patch('shopify_chat.ShopifySettings.load',side_effect=AssertionError('unexpected read')):
            with self.assertRaises(ShopifyChatError): make_shopify_handler(state,access_role='merchant',platform_context={'shop_domain':SHOP,'allow_network':True})

    def test_failed_credentials_returns_failure_without_secret_or_fake_not_found(self):
        with patch('shopify_chat.ShopifySettings.load',side_effect=ShopifyConnectionError('DO_NOT_ECHO_SECRET')):
            result=self.factory()()
        self.assertEqual(result['status'],'failed');self.assertNotIn('DO_NOT_ECHO_SECRET',json.dumps(result))
        self.assertEqual(result['citation_index'],{})
        self.assertIn('失败',render_shopify_fact(result))

    def test_policy_handler_only_reads_current_policy_and_explains_missing_source(self):
        with patch('shopify_chat.ShopifySettings.load'),patch('shopify_chat.ShopifyReadClient') as cls:
            cls.return_value.read_current_policies.return_value={'status':'insufficient_evidence','policies':[],'historical_applicability':'unknown'}
            handler=make_shopify_handler(self.state(True),access_role='merchant',platform_context={'shop_domain':SHOP,'allow_network':True})
            result=handler()
            cls.return_value.read_order.assert_not_called();cls.return_value.read_current_policies.assert_called_once_with()
        answer=render_shopify_fact(result)
        self.assertIn('没有读到',answer);self.assertIn('不能用隐私政策或S001',answer)

    def test_fixed_render_uses_currency_minor_timestamps_and_transaction_references(self):
        answer=render_shopify_fact(fact())
        for item in ('100.00 USD','30.00 USD','70.00 USD','SHOPIFY-ORDER:1001','SHOPIFY-TX:1','SHOPIFY-TX:2','2026-11-20T06:15:00+00:00'):
            self.assertIn(item,answer)
        self.assertNotIn('人民币',answer);self.assertNotIn('元',answer)
        self.assertIn('不证明真实到账',answer)
        self.assertIn('不证明订单在平台不存在',render_shopify_fact({'status':'not_found'}))

    def test_negative_minor_amount_renders_sign_and_absolute_value(self):
        # Defensive rendering only: live normalization still rejects refunds > receipts.
        value = fact(); value['net_amount_minor'] = -50
        answer = render_shopify_fact(value)
        self.assertIn('净额-0.50 USD', answer)
        self.assertNotIn('-1.50 USD', answer)
        value['net_amount_minor'] = -150
        self.assertIn('净额-1.50 USD', render_shopify_fact(value))

    def test_remaining_budget_is_read_at_handler_invocation_not_factory_creation(self):
        remaining = [12.0]
        with patch('shopify_chat.ShopifySettings.load'), patch('shopify_chat.ShopifyReadClient') as cls:
            cls.return_value.read_order.return_value = fact()
            callback = unittest.mock.Mock(side_effect=lambda: remaining[0])
            handler = self.factory(remaining_seconds=callback)
            callback.assert_not_called()
            remaining[0] = 2.5
            handler()
            self.assertEqual(cls.call_args.kwargs['timeout_seconds'], 2.5)

    def test_exhausted_or_invalid_budget_does_not_load_credentials(self):
        for remaining in (0, -1, float('nan'), float('inf'), True):
            with patch('shopify_chat.ShopifySettings.load', side_effect=AssertionError('unexpected credential read')):
                result = self.factory(remaining_seconds=lambda: remaining)()
                self.assertEqual(result['status'], 'failed')
        with self.assertRaises(ShopifyChatError): self.factory(remaining_seconds=2.0)
        remaining = [1.0]
        def delayed_load(path): remaining[0] = 0; return None
        with patch('shopify_chat.ShopifySettings.load', side_effect=delayed_load), patch('shopify_chat.ShopifyReadClient') as cls:
            self.assertEqual(self.factory(remaining_seconds=lambda: remaining[0])()['status'], 'failed')
            cls.assert_not_called()


if __name__=='__main__': unittest.main()
