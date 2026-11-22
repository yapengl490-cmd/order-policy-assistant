"""Synthetic responses only; MockTransport cannot send a real request."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch
import httpx

from shopify_connection import API_VERSION, ShopifySettings, ShopifyConnectionError, REQUIRED_SCOPES
from shopify_orders import (normalize_order, normalize_current_policies, ShopifyOrderError,
                            ShopifyReadClient, ORDER_QUERY, NAME_QUERY, POLICY_QUERY)

SHOP = 'offline-demo.myshopify.com'
STAMP = '2026-11-20T07:00:00Z'
NOW = datetime(2026, 11, 20, 7, tzinfo=timezone.utc)
TOKEN = 'offline_TOKEN_DO_NOT_LEAK_12345'
SECRET = 'offline_SECRET_DO_NOT_LEAK_12345'


def tx(number, kind='CAPTURE', status='SUCCESS', amount='100.00', stamp='2026-11-20T06:15:00Z'):
    return {'id': f'gid://shopify/OrderTransaction/{number}', 'kind': kind, 'status': status, 'test': True,
            'processedAt': stamp, 'amountSet': {'shopMoney': {'amount': amount, 'currencyCode': 'USD'}}}


def order():
    return {'id': 'gid://shopify/Order/1001', 'name': '#1001', 'test': True,
            'createdAt': '2026-11-20T06:14:53Z', 'displayFinancialStatus': 'PAID',
            'totalPriceSet': {'shopMoney': {'amount': '100.0', 'currencyCode': 'USD'}},
            'totalRefundedSet': {'shopMoney': {'amount': '0.0', 'currencyCode': 'USD'}},
            'transactionsCount': {'count': 2, 'precision': 'EXACT'},
            'transactions': [tx(1, 'AUTHORIZATION'), tx(2)]}


def policy():
    return {'myshopifyDomain': SHOP, 'shopPolicies': [{'id': 'gid://shopify/ShopPolicy/1',
            'type': 'REFUND_POLICY', 'title': 'Test policy', 'body': '<p>Demonstration only.</p>',
            'createdAt': '2026-11-01T00:00:00Z', 'updatedAt': '2026-11-20T06:00:00Z'}]}


class NormalizeTests(unittest.TestCase):
    def normalize(self, raw, **kwargs):
        return normalize_order(raw, shop_domain=SHOP, observed_at=STAMP, **kwargs)

    def test_capture_and_authorization_are_not_double_counted(self):
        raw = order(); saved = deepcopy(raw)
        result = self.normalize(raw)
        self.assertEqual((result['paid_amount_minor'], result['refund_amount_minor'], result['net_amount_minor']), (10000, 0, 10000))
        self.assertEqual(result['currency'], 'USD')
        self.assertEqual(result['successful_payments'][0]['processed_at'], '2026-11-20T06:15:00+00:00')
        self.assertNotEqual(result['created_at'], result['successful_payments'][0]['processed_at'])
        self.assertNotIn('buyer_id', result); self.assertNotIn('paid_amount_fen', result)
        self.assertEqual(result['customer_identity_binding'], 'not_established')
        self.assertEqual(raw, saved)

    def test_success_refund_partial_cutoff_is_exclusive_and_timezone_aware(self):
        raw = order()
        raw['transactions'] += [tx(3, 'REFUND', amount='30.00', stamp='2026-11-20T06:30:00Z'),
                                tx(4, 'REFUND', 'PENDING', '20.00', None)]
        raw['transactionsCount']['count'] = 4
        full = self.normalize(raw)
        self.assertEqual((full['refund_amount_minor'], full['net_amount_minor']), (3000, 7000))
        boundary = self.normalize(raw, refund_before='2026-11-20T14:30:00+08:00')
        self.assertEqual(boundary['refund_amount_minor'], 0)
        self.assertFalse(boundary['successful_refunds'][0]['included_before_cutoff'])

    def test_void_failed_pending_and_suggested_refund_are_not_money(self):
        raw = order()
        raw['transactions'] = [tx(1, 'AUTHORIZATION'), tx(2, 'VOID'), tx(3, 'CAPTURE', 'FAILURE'),
                               tx(4, 'SALE', 'PENDING'), tx(5, 'REFUND', 'ERROR'), tx(6, 'SUGGESTED_REFUND')]
        raw['transactionsCount']['count'] = len(raw['transactions'])
        result = self.normalize(raw)
        self.assertEqual(result['paid_amount_minor'], 0); self.assertEqual(result['refund_amount_minor'], 0)
        self.assertEqual(len(result['excluded_transactions']), 6)

    def test_sale_and_multiple_captures_use_real_individual_times(self):
        raw = order(); raw['transactions'] = [tx(1, 'SALE', amount='40.00'), tx(2, amount='60.00', stamp='2026-11-20T06:20:00Z')]
        result = self.normalize(raw)
        self.assertEqual(result['paid_amount_minor'], 10000)
        self.assertEqual(len(result['successful_payments']), 2)
        self.assertNotIn('paid_at', result)

    def test_platform_refunded_total_or_financial_status_does_not_prove_success(self):
        raw = order(); raw['displayFinancialStatus'] = 'REFUNDED'
        raw['totalRefundedSet']['shopMoney']['amount'] = '30.00'
        self.assertEqual(self.normalize(raw)['refund_amount_minor'], 0)

    def test_incomplete_nonexact_and_duplicate_transactions_rejected(self):
        for mutate in (lambda r: r['transactionsCount'].update(count=3),
                       lambda r: r['transactionsCount'].update(precision='AT_LEAST'),
                       lambda r: r['transactionsCount'].update(count=True),
                       lambda r: r['transactions'][1].update(id=r['transactions'][0]['id'])):
            raw = order(); mutate(raw)
            with self.assertRaises(ShopifyOrderError): self.normalize(raw)

    def test_currency_mismatch_unsupported_and_fractional_minor_units_rejected(self):
        for value in ('EUR', 'JPY', 'CNY'):
            raw = order(); raw['transactions'][1]['amountSet']['shopMoney']['currencyCode'] = value
            with self.assertRaises(ShopifyOrderError): self.normalize(raw)
        for amount in ('0.001', '-1', 'NaN', 'Infinity', '1e2', 100, '999999999999999.99'):
            raw = order(); raw['transactions'][1]['amountSet']['shopMoney']['amount'] = amount
            with self.assertRaises(ShopifyOrderError): self.normalize(raw)
        raw = order(); raw['transactions'][1]['amountSet']['shopMoney']['amount'] = '0.01'
        self.assertEqual(self.normalize(raw)['paid_amount_minor'], 1)

    def test_missing_naive_or_future_successful_processed_time_rejected(self):
        for stamp in (None, '2026-11-20T06:15:00', '2026-11-21T00:00:00Z', 'garbage'):
            raw = order(); raw['transactions'][1]['processedAt'] = stamp
            with self.assertRaises(ShopifyOrderError): self.normalize(raw)
        raw = order(); raw['transactions'][0]['processedAt'] = '2026-11-21T00:00:00Z'
        with self.assertRaises(ShopifyOrderError): self.normalize(raw)
        with self.assertRaises(ShopifyOrderError): self.normalize(order(), refund_before='2026-11-21T00:00:00Z')

    def test_cutoff_before_or_at_any_successful_receipt_cannot_produce_net(self):
        raw = order()
        for cutoff in ('2026-11-20T06:14:59Z', '2026-11-20T06:15:00Z'):
            with self.assertRaises(ShopifyOrderError): self.normalize(raw, refund_before=cutoff)
        self.assertEqual(self.normalize(raw, refund_before='2026-11-20T06:15:01Z')['net_amount_minor'], 10000)
        raw['transactions'] = [tx(1, amount='40.00'), tx(2, amount='60.00', stamp='2026-11-20T06:20:00Z')]
        with self.assertRaises(ShopifyOrderError): self.normalize(raw, refund_before='2026-11-20T06:18:00Z')

    def test_non_test_records_and_refund_exceeding_receipts_rejected(self):
        raw = order(); raw['test'] = False
        with self.assertRaises(ShopifyOrderError): self.normalize(raw)
        raw = order(); raw['transactions'][0]['test'] = False
        with self.assertRaises(ShopifyOrderError): self.normalize(raw)
        raw = order(); raw['transactions'] += [tx(3, 'REFUND', amount='101.00')]; raw['transactionsCount']['count'] = 3
        before = deepcopy(raw)
        with self.assertRaisesRegex(ShopifyOrderError, '成功退款超过.*未计算净额'): self.normalize(raw)
        self.assertEqual(raw, before)  # No clamping or rewriting the anomalous source.

    def test_unknown_cash_change_and_malformed_response_rejected(self):
        for value in ('CHANGE', 'UNKNOWN_NEW_KIND'):
            raw = order(); raw['transactions'][1]['kind'] = value
            with self.assertRaises(ShopifyOrderError): self.normalize(raw)
        for raw in ({}, None, {'test': True}, {**order(), 'transactions': [None, None]}):
            with self.assertRaises(ShopifyOrderError): self.normalize(raw)

    def test_current_policy_preserves_source_but_does_not_invent_historical_effectiveness(self):
        result = normalize_current_policies(policy(), shop_domain=SHOP, observed_at=STAMP)
        self.assertEqual(result['historical_applicability'], 'unknown')
        self.assertEqual(result['selection_basis'], 'current_store_snapshot')
        self.assertNotIn('effective_from', result['policies'][0])
        self.assertTrue(result['policies'][0]['untrusted_source_text'])
        self.assertEqual(result['policies'][0]['body_html'], policy()['shopPolicies'][0]['body'])

    def test_privacy_policy_is_not_a_refund_policy_and_missing_is_unknown(self):
        raw = policy(); raw['shopPolicies'][0]['type'] = 'PRIVACY_POLICY'
        self.assertEqual(normalize_current_policies(raw, shop_domain=SHOP, observed_at=STAMP)['status'], 'insufficient_evidence')
        raw = policy(); raw['shopPolicies'][0]['body'] = ' '
        self.assertEqual(normalize_current_policies(raw, shop_domain=SHOP, observed_at=STAMP)['policies'], [])
        raw = policy(); raw['myshopifyDomain'] = 'other.myshopify.com'
        with self.assertRaises(ShopifyOrderError): normalize_current_policies(raw, shop_domain=SHOP, observed_at=STAMP)


class ReadClientTests(unittest.TestCase):
    def client(self, *, body=None, meta_change=None, response_headers=None, response_status=200, token_status=200,
               token_headers=None, token_body=None, timeout_seconds=15, after_request=None):
        self.requests = []
        meta = {'shop': {'myshopifyDomain': SHOP, 'plan': {'partnerDevelopment': True},
                        'currencyCode': 'USD', 'ianaTimezone': 'America/New_York'},
                'currentAppInstallation': {'accessScopes': [{'handle': x} for x in REQUIRED_SCOPES]}}
        if meta_change: meta_change(meta)
        def send(request):
            self.requests.append(request)
            self.assertEqual(request.url.host, SHOP)
            if after_request: after_request()
            if request.url.path == '/admin/oauth/access_token':
                return httpx.Response(token_status, headers=token_headers, json=token_body or {'access_token': TOKEN})
            query = json.loads(request.content)['query']
            headers = {'X-Shopify-API-Version': API_VERSION} if response_headers is None else response_headers
            if len(self.requests) == 2:
                return httpx.Response(200, headers=headers, json={'data': meta})
            output = {'data': {'order': order()}} if body is None else body
            return httpx.Response(response_status, headers=headers, json=output)
        cfg = ShopifySettings(SHOP, 'offline_client_id_123', SECRET)
        return ShopifyReadClient(cfg, authorized_shop_domain=SHOP, access_role='merchant',
                                 transport=httpx.MockTransport(send), clock=lambda: NOW, timeout_seconds=timeout_seconds)

    def assert_safe_error(self, callback):
        with self.assertRaises(ShopifyConnectionError) as caught: callback()
        for secret in (SECRET, TOKEN): self.assertNotIn(secret, str(caught.exception))

    def test_explicit_gated_client_uses_three_fixed_read_requests(self):
        result = self.client().read_order('gid://shopify/Order/1001')
        self.assertEqual(result['net_amount_minor'], 10000)
        self.assertEqual(len(self.requests), 3)
        body = json.loads(self.requests[-1].content)
        self.assertEqual(body, {'query': ORDER_QUERY, 'variables': {'id': 'gid://shopify/Order/1001'}})
        for field in ('customer', 'email', 'address', 'paymentDetails', 'mutation'):
            self.assertNotIn(field, ORDER_QUERY)
        self.assertNotIn(TOKEN, json.dumps(result)); self.assertNotIn(SECRET, json.dumps(result))

    def test_default_disabled_and_buyer_or_other_store_binding_rejected(self):
        cfg = ShopifySettings(SHOP, 'offline_client_id_123', SECRET)
        client = ShopifyReadClient(cfg, authorized_shop_domain=SHOP, access_role='merchant')
        self.assert_safe_error(lambda: client.read_order('gid://shopify/Order/1001'))
        for role, domain in (('buyer', SHOP), ('merchant', 'other.myshopify.com')):
            self.assert_safe_error(lambda: ShopifyReadClient(cfg, authorized_shop_domain=domain, access_role=role))

    def test_order_name_is_exact_and_ambiguous_or_partial_search_rejected(self):
        body = {'data': {'orders': {'nodes': [order()], 'pageInfo': {'hasNextPage': False}}}}
        self.assertEqual(self.client(body=body).read_order('#1001')['order_name'], '#1001')
        self.assertEqual(json.loads(self.requests[-1].content)['variables'], {'query': 'test:true AND name:#1001'})
        for nodes, more in (([order(), order()], False), ([order()], True), ([{**order(), 'name': '#10010'}], False)):
            bad = {'data': {'orders': {'nodes': nodes, 'pageInfo': {'hasNextPage': more}}}}
            self.assert_safe_error(lambda: self.client(body=bad).read_order('#1001'))

    def test_bad_tool_identifiers_rejected_before_authentication(self):
        client = self.client()
        for value in ('#1001 OR test:false', 'https://evil', 'gid://shopify/Customer/1', '1001', None):
            self.assert_safe_error(lambda: client.read_order(value))
        self.assertEqual(self.requests, [])

    def test_not_found_does_not_leak_other_store_or_claim_nonexistence(self):
        result = self.client(body={'data': {'order': None}}).read_order('gid://shopify/Order/1001')
        self.assertEqual(result['status'], 'not_found')
        self.assertNotIn('paid_amount_minor', result)
        self.assertIn('可读日期范围', result['notice'])

    def test_meta_store_plan_permissions_checked_before_business_query(self):
        for mutate in (lambda m: m['shop'].update(myshopifyDomain='other.myshopify.com'),
                       lambda m: m['shop']['plan'].update(partnerDevelopment=False),
                       lambda m: m['currentAppInstallation'].update(accessScopes=[{'handle':'read_orders'}]),
                       lambda m: m['currentAppInstallation']['accessScopes'].append({'handle':'write_orders'})):
            client = self.client(meta_change=mutate)
            self.assert_safe_error(lambda: client.read_order('gid://shopify/Order/1001'))
            self.assertEqual(len(self.requests), 2)

    def test_redirect_http_error_version_error_and_graphql_partial_rejected(self):
        for kwargs in ({'token_status':302,'token_headers':{'Location':'https://evil.example'}},
                       {'response_status':403}, {'response_headers':{}},
                       {'response_headers':{'X-Shopify-API-Version':'2026-07'}},
                       {'body':{'data':{'order':order()},'errors':[{'message':SECRET+TOKEN}]}}):
            client = self.client(**kwargs)
            self.assert_safe_error(lambda: client.read_order('gid://shopify/Order/1001'))
            self.assertLessEqual(len(self.requests), 3)

    def test_response_order_must_match_requested_gid(self):
        self.assert_safe_error(lambda: self.client(body={'data':{'order':{**order(),'id':'gid://shopify/Order/9999'}}}).read_order('gid://shopify/Order/1001'))

    def test_malformed_order_and_timezone_are_safe_errors(self):
        self.assert_safe_error(lambda: self.client(body={'data': {'order': 'unexpected'}}).read_order('gid://shopify/Order/1001'))
        self.assert_safe_error(lambda: self.client(meta_change=lambda m: m['shop'].update(ianaTimezone='/not-a-zone')).read_order('gid://shopify/Order/1001'))

    def test_current_policy_uses_fixed_read_query(self):
        result = self.client(body={'data': {'shop': policy()}}).read_current_policies()
        self.assertEqual(result['historical_applicability'], 'unknown')
        self.assertEqual(json.loads(self.requests[-1].content)['query'], POLICY_QUERY)
        self.assertEqual(len(self.requests), 3)

    def test_timeout_budget_must_be_positive_finite_number(self):
        for value in (0, -1, True, None, '5', float('inf'), float('nan')):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ShopifyOrderError, '正有限秒数'):
                    self.client(timeout_seconds=value)

    def test_three_requests_share_one_deadline_and_decreasing_timeouts(self):
        clock = [100.0]
        def advance(): clock[0] += 1.0
        client = self.client(timeout_seconds=5, after_request=advance)
        with patch('shopify_orders.time.monotonic', side_effect=lambda: clock[0]):
            result = client.read_order('gid://shopify/Order/1001')
        self.assertEqual(result['status'], 'ok')
        for request, expected in zip(self.requests, (5.0, 4.0, 3.0)):
            self.assertEqual(set(request.extensions['timeout'].values()), {expected})

    def test_expired_shared_deadline_stops_next_request_and_rejects_late_result(self):
        for total, expected_requests in ((1, 1), (3, 2), (5, 3)):
            clock = [100.0]
            def advance(): clock[0] += 2.0
            client = self.client(timeout_seconds=total, after_request=advance)
            with patch('shopify_orders.time.monotonic', side_effect=lambda: clock[0]):
                with self.assertRaisesRegex(ShopifyOrderError, '总时间预算'):
                    client.read_order('gid://shopify/Order/1001')
            self.assertEqual(len(self.requests), expected_requests)
            for index, request in enumerate(self.requests):
                self.assertEqual(set(request.extensions['timeout'].values()), {float(total - 2 * index)})


if __name__ == '__main__': unittest.main()
