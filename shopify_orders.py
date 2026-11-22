"""Read-only development-store adapter, separate from the legacy CNY/fen schema.

No credentials are loaded, requests made, or database writes performed on import.
The caller supplies a server-authorized merchant/store binding and opts into live I/O.
"""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import math
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from shopify_connection import (API_VERSION, CONNECTION_QUERY, REQUIRED_SCOPES,
    ShopifySettings, ShopifyConnectionError, normalize_shop_domain, _response_json)

MINOR_DIGITS = {'USD': 2, 'CNY': 2}  # Explicit initial coverage; never infer another currency's exponent.
ORDER_FIELDS = '''id name test createdAt displayFinancialStatus
  totalPriceSet { shopMoney { amount currencyCode } }
  totalRefundedSet { shopMoney { amount currencyCode } }
  transactionsCount { count precision }
  transactions(first: 250) {
    id kind status test processedAt
    amountSet { shopMoney { amount currencyCode } }
  }'''
ORDER_QUERY = 'query ReadDevelopmentOrder($id: ID!) { order(id: $id) { ' + ORDER_FIELDS + ' } }'
NAME_QUERY = 'query FindDevelopmentOrder($query: String!) { orders(first: 2, query: $query) { nodes { ' + ORDER_FIELDS + ' } pageInfo { hasNextPage } } }'
POLICY_QUERY = '''query ReadCurrentDevelopmentPolicies {
  shop { myshopifyDomain shopPolicies { id type title body createdAt updatedAt } }
}'''


class ShopifyOrderError(ShopifyConnectionError):
    """Predefined errors only: upstream text and credentials must not be echoed."""


def _time(value):
    try:
        if not isinstance(value, str) or not 20 <= len(value) <= 40:
            raise ValueError
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise ShopifyOrderError('时间缺失或未含有效时区；不能用订单创建时间替代交易处理时间。') from None


def _gid(value, kind):
    if not isinstance(value, str) or not re.fullmatch(r'gid://shopify/' + kind + r'/[1-9][0-9]{0,24}', value):
        raise ShopifyOrderError('平台记录标识无效。')
    return value


def _money(raw, expected_currency=None):
    if not isinstance(raw, dict):
        raise ShopifyOrderError('平台金额字段不完整。')
    currency, amount = raw.get('currencyCode'), raw.get('amount')
    if currency not in MINOR_DIGITS or expected_currency is not None and currency != expected_currency:
        raise ShopifyOrderError('币种不一致或最小货币单位尚未支持；未混算或转换为人民币。')
    if not isinstance(amount, str) or not re.fullmatch(r'[0-9]{1,15}(?:\.[0-9]{1,6})?', amount):
        raise ShopifyOrderError('平台金额须为非负十进制字符串。')
    minor = Decimal(amount) * (10 ** MINOR_DIGITS[currency])
    if minor != minor.to_integral_value() or minor > 9_000_000_000_000_000:
        raise ShopifyOrderError('平台金额精度或范围超出本适配器支持范围；未四舍五入。')
    return int(minor), currency


def normalize_order(raw, *, shop_domain, observed_at, refund_before=None):
    """Complete transaction snapshot -> integer minor amounts; no buyer identity inference.

    Paid = successful SALE/CAPTURE observed now, all strictly before refund_before.
    Refunded = successful REFUND with processedAt < refund_before (defaults to observed_at). Authorization/void/pending
    are not receipts/refunds; no inference from display status or refund objects.
    """
    shop_domain = normalize_shop_domain(shop_domain)
    observed = _time(observed_at)
    cutoff = _time(refund_before) if refund_before is not None else observed
    if cutoff > observed:
        raise ShopifyOrderError('退款截止不能晚于本次平台观察时刻。')
    try:
        if not isinstance(raw, dict) or raw.get('test') is not True:
            raise ShopifyOrderError('仅接受开发店测试订单。')
        order_id = _gid(raw['id'], 'Order')
        name = raw['name']
        if not isinstance(name, str) or not 1 <= len(name) <= 128:
            raise ShopifyOrderError('订单名称不完整。')
        created = _time(raw['createdAt'])
        if created > observed:
            raise ShopifyOrderError('订单创建时间晚于观察时间。')
        order_total, currency = _money(raw['totalPriceSet']['shopMoney'])
        platform_refunded, _ = _money(raw['totalRefundedSet']['shopMoney'], currency)
        txs, count = raw['transactions'], raw['transactionsCount']
        if (not isinstance(txs, list) or len(txs) > 250 or not isinstance(count, dict)
                or count.get('precision') != 'EXACT' or type(count.get('count')) is not int
                or count['count'] != len(txs)):
            raise ShopifyOrderError('交易列表不完整或计数不是精确值；停止计算完整金额。')
        seen, payments, refunds, excluded = set(), [], [], []
        for tx in txs:
            txid = _gid(tx['id'], 'OrderTransaction')
            if txid in seen:
                raise ShopifyOrderError('平台交易标识重复；停止计算以避免重复计款。')
            seen.add(txid)
            if tx.get('test') is not True:
                raise ShopifyOrderError('包含非测试交易；未计算。')
            amount, _ = _money(tx['amountSet']['shopMoney'], currency)
            kind, status = tx['kind'], tx['status']
            if status not in {'SUCCESS', 'PENDING', 'AWAITING_RESPONSE', 'ERROR', 'FAILURE', 'UNKNOWN'}:
                raise ShopifyOrderError('交易状态不在已核实的接口范围。')
            if kind not in {'SALE', 'CAPTURE', 'REFUND', 'AUTHORIZATION', 'EMV_AUTHORIZATION', 'VOID', 'SUGGESTED_REFUND', 'CHANGE'}:
                raise ShopifyOrderError('交易类型不在已核实的接口范围。')
            if kind == 'CHANGE' and status == 'SUCCESS':
                raise ShopifyOrderError('当前适配器尚未支持现金找零交易口径，未计算净额。')
            processed = _time(tx['processedAt']) if tx.get('processedAt') is not None else None
            if processed is not None and processed > observed:
                raise ShopifyOrderError('交易处理时间晚于观察时间；未计算。')
            if status != 'SUCCESS' or kind not in {'SALE', 'CAPTURE', 'REFUND'}:
                excluded.append({'transaction_id': txid, 'kind': kind, 'status': status,
                                 'reason': 'not_successful_receipt_or_refund'})
                continue
            if processed is None:
                raise ShopifyOrderError('成功收款或退款缺少交易处理时间；未计算。')
            item = {'transaction_id': txid, 'kind': kind, 'status': status, 'amount_minor': amount,
                    'currency': currency, 'processed_at': processed.isoformat()}
            if kind == 'REFUND':
                item['included_before_cutoff'] = processed < cutoff
                refunds.append(item)
            else:
                payments.append(item)
        paid = sum(tx['amount_minor'] for tx in payments)
        if any(_time(tx['processed_at']) >= cutoff for tx in payments):
            raise ShopifyOrderError('成功收款时间不早于本次截止时刻；未计算净额，请选全部收款完成后的截止时刻。')
        all_refunds = sum(tx['amount_minor'] for tx in refunds)
        if all_refunds > paid:
            raise ShopifyOrderError('成功退款超过已观察的成功收款；未计算净额，交易口径需人工核对。')
        refunded = sum(tx['amount_minor'] for tx in refunds if tx['included_before_cutoff'])
        return {'status': 'ok', 'platform': 'shopify', 'scope': 'authorized_development_store',
                'shop_domain': shop_domain, 'order_id': order_id, 'order_name': name, 'test': True,
                'observed_at': observed.isoformat(), 'created_at': created.isoformat(),
                'currency': currency, 'minor_unit_digits': MINOR_DIGITS[currency],
                'paid_amount_minor': paid, 'refund_amount_minor': refunded, 'net_amount_minor': paid - refunded,
                'refund_before': cutoff.isoformat(), 'refund_boundary': 'exclusive',
                'payment_basis': 'all_observed_successful_SALE_CAPTURE_strictly_before_cutoff',
                'payment_state': 'successful_receipts_observed' if payments else 'no_successful_receipts_observed',
                'order_total_minor': order_total, 'platform_refunded_total_minor': platform_refunded,
                'platform_refunded_total_is_not_success_evidence': True,
                'successful_payments': payments, 'successful_refunds': refunds,
                'excluded_transactions': excluded, 'transaction_list_complete': True,
                'customer_identity_binding': 'not_established', 'policy_at_payment': 'unknown',
                'notice': '开发店测试交易；成功收款/退款不代表真实资金到账、退货资格或买家身份归属。'}
    except (KeyError, TypeError, AttributeError):
        raise ShopifyOrderError('平台订单结构不完整，未输出部分金额。') from None


def normalize_current_policies(raw, *, shop_domain, observed_at):
    """Read current refund policy only; created/updated timestamps are not an effective history."""
    shop_domain = normalize_shop_domain(shop_domain)
    observed = _time(observed_at)
    try:
        if raw['myshopifyDomain'] != shop_domain or not isinstance(raw['shopPolicies'], list):
            raise ShopifyOrderError('当前政策不属于授权店铺或结构无效。')
        policies = []
        for policy in raw['shopPolicies']:
            if policy['type'] != 'REFUND_POLICY':
                continue
            body = policy['body']
            if not isinstance(body, str) or len(body) > 65536:
                raise ShopifyOrderError('政策正文超出本次有限读取范围。')
            if not body.strip():
                continue
            created, updated = _time(policy['createdAt']), _time(policy['updatedAt'])
            if not created <= updated <= observed:
                raise ShopifyOrderError('政策修改时间与观察时间不一致。')
            if not isinstance(policy['id'], str) or not isinstance(policy['title'], str):
                raise ShopifyOrderError('政策标识或标题不完整。')
            digest = hashlib.sha256(body.encode()).hexdigest()
            policies.append({'source_id': policy['id'], 'title': policy['title'], 'type': policy['type'],
                             'body_html': body, 'body_sha256': digest,
                             'citation_id': 'SHOPIFY-REFUND-' + digest[:20],
                             'created_at': created.isoformat(), 'updated_at': updated.isoformat(),
                             'untrusted_source_text': True})
        return {'status': 'ok' if policies else 'insufficient_evidence', 'platform': 'shopify',
                'shop_domain': shop_domain, 'observed_at': observed.isoformat(), 'policies': policies,
                'selection_basis': 'current_store_snapshot', 'historical_applicability': 'unknown',
                'notice': '仅当前店铺政策快照；创建/更新时间不能证明订单支付时适用版本，不能据此判定本单售后资格。'}
    except (KeyError, TypeError, AttributeError):
        raise ShopifyOrderError('平台政策结构不完整。') from None


class ShopifyReadClient:
    """Server-owned capability. Never expose constructor fields as model/tool arguments."""
    def __init__(self, settings: ShopifySettings, *, authorized_shop_domain, access_role,
                 allow_network=False, transport=None, clock=None, timeout_seconds=15):
        if access_role != 'merchant' or normalize_shop_domain(authorized_shop_domain) != settings.shop_domain:
            raise ShopifyOrderError('缺少授权商家店铺绑定；本地买家身份不能代替平台客户认证。')
        if type(allow_network) is not bool or not allow_network and transport is not None and not isinstance(transport, httpx.MockTransport):
            raise ShopifyOrderError('未允许真实网络读取。')
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ShopifyOrderError('平台读取时间预算必须是正有限秒数。')
        self.settings, self.allow_network, self.transport = settings, allow_network, transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.timeout_seconds = float(timeout_seconds)

    def _fetch(self, query, variables=None):
        if not self.allow_network and not isinstance(self.transport, httpx.MockTransport):
            raise ShopifyOrderError('真实平台读取尚未启用；未读取凭据或发送请求。')
        origin = 'https://' + self.settings.shop_domain
        deadline = time.monotonic() + self.timeout_seconds
        def remaining_timeout():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ShopifyOrderError('平台只读查询已达到本次总时间预算；停止后续请求。')
            return min(15.0, remaining)
        try:
            with httpx.Client(timeout=min(15.0, self.timeout_seconds), follow_redirects=False, trust_env=False, transport=self.transport) as client:
                token_data = _response_json(client.post(origin + '/admin/oauth/access_token', data={
                    'grant_type': 'client_credentials', 'client_id': self.settings.client_id,
                    'client_secret': self.settings.client_secret}, timeout=remaining_timeout()), '应用认证')
                remaining_timeout()
                token = token_data.get('access_token')
                if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,2048}', token):
                    raise ShopifyOrderError('认证未返回可用令牌。')
                def graphql(document, values=None):
                    response = client.post(origin + f'/admin/api/{API_VERSION}/graphql.json',
                        headers={'X-Shopify-Access-Token': token}, json={'query': document, 'variables': values or {}},
                        timeout=remaining_timeout())
                    remaining_timeout()
                    body = _response_json(response, '平台只读查询')
                    if response.headers.get('X-Shopify-API-Version') != API_VERSION or body.get('errors'):
                        raise ShopifyOrderError('接口版本或查询权限不符合预期；未接受部分结果。')
                    if not isinstance(body.get('data'), dict):
                        raise ShopifyOrderError('接口未返回完整查询数据。')
                    return body['data']
                meta = graphql(CONNECTION_QUERY)
                shop = meta['shop']
                if (shop['myshopifyDomain'] != self.settings.shop_domain or shop['plan']['partnerDevelopment'] is not True
                        or {item['handle'] for item in meta['currentAppInstallation']['accessScopes']} != REQUIRED_SCOPES):
                    raise ShopifyOrderError('开发店与两项最小读取权限未通过；未读取业务数据。')
                if shop['currencyCode'] not in MINOR_DIGITS:
                    raise ShopifyOrderError('店铺币种的最小货币单位尚未支持。')
                ZoneInfo(shop['ianaTimezone'])
                data = graphql(query, variables)
            observed = self.clock()
            if not isinstance(observed, datetime) or observed.tzinfo is None:
                raise ShopifyOrderError('观察时钟必须包含时区。')
            return data, shop, observed.isoformat()
        except httpx.HTTPError:
            raise ShopifyOrderError('平台读取失败或超时；未输出请求或凭据，未自动重试。') from None
        except ShopifyConnectionError:
            raise
        except (KeyError, TypeError, AttributeError, ValueError, ZoneInfoNotFoundError):
            raise ShopifyOrderError('平台返回结构不符合本次只读契约。') from None

    def read_order(self, order_id, *, refund_before=None):
        if refund_before is not None:
            _time(refund_before)  # Reject malformed tool arguments before any I/O.
        if isinstance(order_id, str) and re.fullmatch(r'#[1-9][0-9]{0,17}', order_id):
            data, shop, observed = self._fetch(NAME_QUERY, {'query': 'test:true AND name:' + order_id})
            try:
                connection = data['orders']
                if (not isinstance(connection['nodes'], list) or connection['pageInfo']['hasNextPage'] is not False
                        or len(connection['nodes']) > 1):
                    raise ShopifyOrderError('订单名称查询不唯一或列表不完整，请使用平台订单标识。')
                raw = connection['nodes'][0] if connection['nodes'] else None
                if raw is not None and (not isinstance(raw, dict) or raw.get('name') != order_id):
                    raise ShopifyOrderError('返回订单名称与请求不一致。')
            except (KeyError, TypeError, AttributeError):
                raise ShopifyOrderError('平台订单列表结构不完整。') from None
        else:
            _gid(order_id, 'Order')
            data, shop, observed = self._fetch(ORDER_QUERY, {'id': order_id})
            if 'order' not in data:
                raise ShopifyOrderError('平台订单查询结构不完整。')
            raw = data['order']
            if raw is not None and (not isinstance(raw, dict) or raw.get('id') != order_id):
                raise ShopifyOrderError('返回订单标识与请求不一致。')
        if raw is None:
            return {'status': 'not_found', 'platform': 'shopify', 'shop_domain': self.settings.shop_domain,
                    'observed_at': observed, 'notice': '当前授权店铺与可读日期范围内未找到该测试订单；不证明订单不存在。'}
        normalized = normalize_order(raw, shop_domain=self.settings.shop_domain, observed_at=observed, refund_before=refund_before)
        if normalized['currency'] != shop['currencyCode']:
            raise ShopifyOrderError('订单和店铺当前币种不一致，未混算。')
        normalized['shop_timezone'] = shop['ianaTimezone']
        return normalized

    def read_current_policies(self):
        data, _, observed = self._fetch(POLICY_QUERY)
        return normalize_current_policies(data.get('shop'), shop_domain=self.settings.shop_domain, observed_at=observed)
