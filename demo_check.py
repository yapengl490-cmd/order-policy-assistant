"""在独立演示容器内核对真实数据库、内部协议和HTTP接口；无模型/嵌入调用。

这些是固定合成样本的开发回归，不是模型质量独立评测。
"""
from datetime import datetime
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent


def run_checks():
    if (os.environ.get('DATUS_DEPLOYMENT') != 'container'
            or os.environ.get('DB_HOST') != 'db' or os.environ.get('MODEL_ENABLED') != '0'):
        raise ValueError('仅允许模型关闭的独立演示容器')
    from database import get_engine
    from buyer_support_service import check_buyer_support, get_support_context
    from policy_kb import search_policy_chunks
    from refund_investigation import investigate_refund_increase
    from general_policy import lookup_general_policy, render_general_policy
    from catalog_recommendations import CatalogScope, CatalogSnapshot, recommend
    from catalog_policy import check_catalog_policy
    from mcp_order_client import OrderProtocolClient
    from sqlalchemy import text
    expected = json.loads((ROOT/'samples/support/expected.json').read_text())
    now = datetime.fromisoformat(expected['now'])
    engine = get_engine()
    checks = {}
    try:
        cases = {}
        for identity in ('O_SUPPORT_OK', 'O001', 'O_SUPPORT_UNKNOWN', 'O_SUPPORT_CUSTOM'):
            value = check_buyer_support(engine, identity, buyer_id='B001', claims=expected['r06']['claims'], now=now)
            cases[identity] = value
            checks['support_'+identity] = all(
                (value['shipping']['payer'] if key == 'payer' else value[key]) == target
                for key, target in expected['r06'][identity].items())
        checks['old_and_current_policy_versions'] = (cases['O001']['policy_version'] == '2026-10-v1'
            and cases['O_SUPPORT_OK']['policy_version'] == '2026-11-v1'
            and bool(cases['O_SUPPORT_OK']['citations']))
        denied = get_support_context(engine, 'O_SUPPORT_OK', buyer_id='B404')
        checks['unauthorized_buyer_has_no_order'] = denied['status'] == 'not_found' and 'order' not in denied
        search = search_policy_chunks(engine, '尺码不合适，退货运费谁承担', store_id='S001',
            category='ordinary_shoes', paid_at=datetime.fromisoformat('2026-11-11T10:00:00+08:00'))
        checks['keyword_retrieval_without_embedding'] = search['status'] == 'ok' and search['stats']['embedding_requests'] == 0
        conditions = expected['r05']['conditions']
        times = [datetime.fromisoformat(conditions[key]) for key in
                 ('first_from', 'first_before', 'second_from', 'second_before', 'refund_before')]
        investigation = investigate_refund_increase(engine, {'role':'merchant', 'shop_id':'S001'}, *times, observed_at=now)
        checks['refund_investigation_fixed_facts'] = (investigation['status'] == 'ok'
            and investigation['total_refund_delta_fen'] == 3000 and investigation['verified_size_delta_fen'] == 2000
            and investigation['causality_verified'] is False
            and all(investigation['comparison'][key] == expected['r05'][key] for key in ('first', 'second')))
        with engine.connect() as connection:
            counts = {table: connection.scalar(text('SELECT count(*) FROM public.'+table)) for table in
                ('buyers','orders','refunds','policy_documents','policy_chunks','order_support_facts','refund_reason_records')}
            checks['policy_documents_and_whole_clause_chunks'] = counts['policy_documents'] == 3 and counts['policy_chunks'] == 9
            checks['readonly_database_role'] = all(not connection.scalar(text(
                "SELECT has_table_privilege(current_user, :name, 'INSERT,UPDATE,DELETE')"), {'name':table})
                for table in ('orders','refunds','policy_documents','policy_chunks','order_support_facts'))
        policy_at = datetime.fromisoformat('2026-11-20T12:00:00+08:00')
        general = lookup_general_policy(engine, store_id='S001', category='ordinary_shoes', policy_at=policy_at)
        checks['general_policy_without_order'] = (general['status'] == 'ok'
            and general['order_eligibility'] == 'not_evaluated' and general['policy_version'] == '2026-11-v1'
            and '定制商品不适用' in render_general_policy(general, {'answer_style':'brief'}))
        catalog = CatalogSnapshot.load(ROOT/'samples/catalog_policy_demo.json')
        products = recommend(catalog, {'category':'t_shirt', 'currency':'USD', 'budget_max_minor':3000,
            'material':'cotton', 'size':'M', 'require_returnable':True},
            scope=CatalogScope(frozenset({'DEMO-SHOP-A'}), 'US', 'demo_storefront'),
            as_of=datetime.fromisoformat(catalog.data()['snapshot_at']),
            policy_lookup=lambda **scope: check_catalog_policy(engine, **scope))
        checks['synthetic_catalog_linked_to_policy'] = (products['status'] == 'matched'
            and [item['variant_id'] for item in products['candidates']] == ['V-A']
            and products['candidates'][0]['price']['amount_minor'] == 2500
            and products['candidates'][0]['price']['currency'] == 'USD'
            and bool(products.get('return_policies')))
        order = OrderProtocolClient('buyer:B001').check_order(order_id='O001', refund_before='2026-11-03T00:00:00+08:00')
        checks['real_internal_protocol_order'] = order['status'] == 'ok' and order['net_amount_fen'] == 8500
        token = json.loads((ROOT/'local/access.json').read_text())['accounts'][0]['token']
        request = Request('http://127.0.0.1:18080/api/orders/O001?refund_before=2026-11-03T00%3A00%3A00%2B08%3A00',
                          headers={'Authorization': 'Bearer '+token, 'X-Access-Mode': 'buyer'})
        with urlopen(request, timeout=10) as response:
            api_order = json.load(response)
        checks['authenticated_http_order'] = api_order['status'] == 'ok' and api_order['net_amount_fen'] == 8500
        with urlopen('http://127.0.0.1:18080/health', timeout=5) as response:
            health = json.load(response)
        checks['model_disabled'] = health.get('model_enabled') is False
        return {'status':'passed' if all(checks.values()) else 'failed', 'checks':checks, 'counts':counts,
                'model_requests':0, 'embedding_requests':0,
                'example':{'order_id':'O001','paid_amount_fen':12500,'refund_amount_fen':4000,'net_amount_fen':8500},
                'scope':'synthetic_database_protocol_http_regression_not_model_evaluation'}
    finally:
        engine.dispose()


if __name__ == '__main__':
    try:
        report = run_checks()
    except Exception as error:
        # 异常正文可能包含连接信息，公开输出只保留类别。
        report = {'status':'failed','error_type':type(error).__name__}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report['status'] == 'passed' else 1)
