"""Read-only product-to-policy linkage at an explicitly supplied catalog time.

购买前筛选只确认此商品有适用条款且没有已知定制例外；不会批准未来退货。
调用方先按可信商品店铺/品类/规格过滤，再传入该范围和目录观察时刻。
政策继续使用原库及原文规则，不复制第二份规则，也不调用模型或嵌入。
"""
from copy import deepcopy
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError

from policy_documents import CATEGORY_LABELS, IDENTIFIER, aware_time, parse_policy_text
from policy_kb import select_policy


NOTICE = ("仅为合成目录所选时点的购买前政策说明；不是Shopify实时政策。"
          "实际售后须按订单支付时适用版本、收货时间及商品状态另行核验，未批准或执行退货退款。")


def _verified_document(bundle, store_id, category, at):
    """检查返回资料确实对应本次范围；最终规则与引用仍从同一完整原文重建。"""
    document = bundle['document']
    parsed = parse_policy_text(document['source_text'], source_path=document['source_path'])
    expected_document = {k: v for k, v in parsed.items() if k not in {'rules', 'rule_citations', 'chunks'}}
    if document != expected_document:
        raise ValueError('policy_document_mismatch')
    if (parsed['store_id'] != store_id or parsed['category'] != category
            or parsed['source_kind'] != 'synthetic_demo'
            or not aware_time(parsed['effective_from']) <= at < aware_time(parsed['effective_before'])
            or bundle.get('payment_version') != parsed['version']):
        raise ValueError('policy_scope_or_time_mismatch')
    expected_scope = {'store_id': store_id, 'category': category, 'paid_at': at.isoformat(),
                      'effective_from': parsed['effective_from'], 'effective_before': parsed['effective_before'],
                      'selection_basis': 'order_paid_at'}
    if bundle.get('applicability') != expected_scope:
        raise ValueError('policy_selection_mismatch')
    if any(bundle.get(key) != parsed[key] for key in ('rules', 'rule_citations', 'chunks')):
        raise ValueError('policy_rules_or_citations_mismatch')
    return parsed, expected_document


def check_catalog_policy(engine, *, store_id, category, is_custom, policy_at):
    """Return conditional/excluded/unknown with complete policy evidence.

    ``is_custom`` is a trusted catalog attribute, never a model inference or
    buyer claim. Unknown includes None and non-bools. ``policy_at`` is the
    application-supplied snapshot/query time, not a fictitious payment date.
    """
    result = {'status': 'unknown', 'reason': 'no_applicable_policy', 'message': '尚无适用政策依据。',
              'store_id': store_id, 'category': category, 'policy_at': None,
              'selection_basis': 'catalog_policy_at', 'policy_version': None, 'document_id': None,
              'document': None, 'rules': {}, 'citations': {}, 'citation_index': {}, 'claims': [],
              'decision_citation_ids': [], 'applicability': {}, 'notice': NOTICE,
              'order_eligibility': 'not_evaluated', 'refund_execution': False,
              'model_requests': 0, 'embedding_requests': 0}

    def unknown(reason, message):
        result.update(reason=reason, message=message)
        return result

    if not isinstance(store_id, str) or not IDENTIFIER.fullmatch(store_id):
        return unknown('invalid_store', '缺少可信商品店铺范围，未查询政策。')
    if not isinstance(category, str) or category not in CATEGORY_LABELS:
        return unknown('unsupported_category', '该商品品类尚未有受控政策支持，不借用其他品类条款。')
    try:
        if not isinstance(policy_at, datetime):
            raise ValueError('explicit_datetime_required')
        at = aware_time(policy_at, 'policy_at')
    except (ValueError, OverflowError):
        return unknown('invalid_policy_time', '缺少带时区的明确目录/查询时刻，未查询政策。')
    result['policy_at'] = at.isoformat()
    result['applicability'] = {'store_id': store_id, 'category': category, 'policy_at': at.isoformat(),
                               'selection_basis': 'catalog_policy_at'}
    try:
        # 复用既有区间选择器；输出另标catalog_policy_at，不把查询时刻伪装为订单支付时刻。
        bundle = select_policy(engine, store_id=store_id, category=category, paid_at=at)
    except SQLAlchemyError:
        return unknown('policy_storage_unavailable', '政策读取暂不可用，不能据此承诺可退。')
    if not isinstance(bundle, dict) or bundle.get('status') != 'ok':
        if isinstance(bundle, dict) and bundle.get('status') in {'withdrawn', 'superseded_without_valid_replacement'}:
            # 不填可用document/rules：商品事实可保留，撤回条款不能支持“要能退”的推荐。
            result.update(policy_status=bundle['status'], policy_lifecycle=deepcopy(bundle.get('lifecycle', {})))
            return unknown(bundle.get('reason', 'applicable_policy_withdrawn'),
                           '适用退货政策已撤回且当前没有可用更正版，不能确认满足可退要求；商品自身信息不因此删除。')
        reason = bundle.get('reason') if isinstance(bundle, dict) else None
        # 仅传递已知错误类别；不将外部文本当政策或暴露其他范围内容。
        reason = reason if isinstance(reason, str) and reason in {'no_applicable_policy', 'overlapping_policy_versions', 'stored_content_mismatch'} else 'invalid_policy'
        return unknown(reason, '尚无完整且唯一适用的政策依据，不能确认满足退货条件。')
    try:
        parsed, document = _verified_document(bundle, store_id, category, at)
    except (KeyError, TypeError, ValueError, OverflowError):
        return unknown('invalid_policy', '政策原文、范围或引用校验未通过，不能确认满足退货条件。')

    rules, citations = deepcopy(parsed['rules']), deepcopy(parsed['rule_citations'])
    index = {}
    for key, citation in citations.items():
        identity = citation['citation_id']
        index.setdefault(identity, {**deepcopy(citation), 'supports': []})['supports'].append(key)
    result.update(policy_version=parsed['version'], document_id=parsed['document_id'], document=deepcopy(document),
                  rules=rules, citations=citations, citation_index=index)
    result['applicability'].update(effective_from=parsed['effective_from'], effective_before=parsed['effective_before'])

    def claim(identity, text, rule):
        result['claims'].append({'id': identity, 'text': text, 'citation_ids': [citations[rule]['citation_id']]})

    claim('scope', f"店铺{store_id}的{CATEGORY_LABELS[category]}条款版本为{parsed['version']}；按目录/查询时刻{at.isoformat()}匹配，实际订单仍按支付时适用版本核验。", 'allowed_categories')
    claim('eligibility_policy', f"退货申请须在收货起{rules['return_window_hours']}小时内，含起点、不含截止；须未穿着且吊牌完好，定制商品不适用，自述还须核实。", 'return_window_hours')
    claim('shipping_policy', '尺码或个人偏好退货的寄回运费由买家承担；商家责任须有已核实的本次记录，否则责任与运费承担待核实。', 'shipping_by_reason')
    result['decision_citation_ids'] = [citations['custom_excluded']['citation_id']]
    if type(is_custom) is not bool:
        return unknown('unknown_custom_status', '已有适用条款，但商品是否定制尚未核实，暂不作为满足退货要求的候选。')
    if is_custom:
        result.update(status='excluded', reason='custom_product_excluded', message='商品已标记为定制，适用条款明确排除此类退货资格。')
    else:
        result.update(status='conditional', reason='applicable_return_policy', message='有适用退货条款，且目录已明确标记为非定制；购买后的具体退货仍须核验期限和商品状态。')
    return result
