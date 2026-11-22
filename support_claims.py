"""售后第二版输出合同：模型选结论，程序核对依据并渲染可信文本。

目录只来自本次授权工具结果；模型不能提供结论正文、业务事实或来源类型。
旧任务及第一版输出函数不受影响。
"""
from datetime import datetime
from zoneinfo import ZoneInfo


def _refs(result, section, rules):
    from support_runtime import citation_index
    index = result.get('citation_index') or citation_index(result)
    # 同一章节不等于支持任意结论；逐条规则核对，缺任意一条就不生成该政策断言。
    selected = set()
    direct = result.get('citations') or {}
    for rule in rules:
        matches = []
        source = direct.get(rule) if direct else None
        if direct and not isinstance(source, dict):
            return []
        for ref, item in index.items():
            if (not isinstance(ref, str) or not isinstance(item, dict)
                    or item.get('section') != section
                    or not isinstance(item.get('supports'), list)
                    or rule not in item['supports']):
                continue
            if source is not None and source.get('chunk_id') != ref:
                continue
            matches.append(ref)
        if not matches:
            return []
        selected.update(matches)
    return sorted(selected)


def _beijing(value):
    """保留精确时刻和时区，不把UTC钟面数值直接换标签。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(ZoneInfo('Asia/Shanghai')).isoformat(sep=' ')
    except ValueError:
        return None


def build_support_claim_catalog(result):
    """返回id→可展示文本、准确引用、必选标志和程序确定的来源类型。"""
    catalog = {}

    def add(identity, text, refs=(), *, required=True, kind='program_state'):
        catalog[identity] = {'text': text, 'citation_ids': list(refs),
                             'required': required, 'kind': kind}

    status = result.get('status')
    if status == 'not_found':
        # 即便异常上游夹带了事实，也不在范围外结果中展示其金额、版本或条款。
        add('scope_result', '当前授权范围内未找到订单；不能区分订单不存在与无权访问，也不能据此认定没有支付或退款。')
        add('boundary', '仅合成演示，只读核查；未批准、受理或执行退货退款。')
        return catalog
    blocked = {'data_error': '订单记录的时间或数据不一致，当前不能判断售后结果。',
               'support_data_unavailable': '尚未取得售后补充记录，当前不能判断退货条件。'}
    if status in blocked:
        add('evidence_gap', blocked[status])
        add('boundary', '仅合成演示，只读核查；未批准、受理或执行退货退款。')
        return catalog
    if (result.get('policy_status') in {'withdrawn', 'corrected', 'superseded_without_valid_replacement', 'check_failed', 'invalid_policy', 'conflict', 'missing'}
            or result.get('policy_lifecycle_check', {}).get('status') in {'withdrawn', 'corrected', 'check_failed', 'invalid_policy', 'conflict', 'missing'}):
        # 只声明程序状态，不拿撤回条款给资格/运费结论背书。
        add('policy_source_unavailable', '订单事实已保留；政策已撤回、更正或有效性无法确认，暂停退货资格与运费判断，需重新取得适用依据。')
        add('boundary', '原冻结条款仅作历史审计；只读核查，未批准、受理或执行退货退款。')
        return catalog

    topics = result.get('requested_topics', [])
    rules = result.get('applicable_rules', {})
    facts = result.get('trusted_facts', {})
    claims = result.get('user_claims', {}).get('values', {})
    window = result.get('return_window', {})
    return_refs = _refs(result, 'return_eligibility',
                        ('return_window_hours', 'requires_unworn', 'requires_tags', 'custom_excluded'))
    shipping_refs = _refs(result, 'shipping_cost', ('shipping_by_reason',))
    scope_refs = _refs(result, 'scope', ('allowed_categories',))

    if 'eligibility' in topics:
        eligibility = result.get('eligibility', 'undetermined')
        labels = {'eligible_with_self_report': '本单暂满足该演示条款的退货条件，仍需售后核验。',
                  'ineligible': '本单未满足该演示条款的退货条件；不否定其他未接入的售后权利。',
                  'undetermined': '现有依据尚不能确定本单是否满足退货条件。'}
        text = labels.get(eligibility, labels['undetermined'])
        failed = {'custom_product_excluded': '订单记录为定制商品',
                  'return_window_expired': '按记录计算已超过申请时限',
                  'user_reports_worn': '用户自述已穿着',
                  'user_reports_tags_missing': '用户自述吊牌不完整',
                  'category_not_covered': '订单品类不在政策范围'}
        reasons = [failed[key] for key in result.get('failed_conditions', []) if key in failed]
        if reasons:
            text += '未满足项：' + '、'.join(reasons) + '。'
        refs = return_refs
        if 'category_not_covered' in result.get('failed_conditions', []):
            refs = sorted(set(refs + scope_refs))
        if eligibility in {'eligible_with_self_report', 'ineligible'} and not refs:
            text = '本轮缺少可核对的政策条款，不能确认该退货结论。'
        add('eligibility', text, refs, kind='rule_decision' if refs else 'program_state')

        if return_refs and rules:
            conditions = []
            version = result.get('policy_version')
            if version:
                conditions.append('按订单支付时适用版本' + str(version))
            hours = rules.get('return_window_hours')
            if type(hours) is int and hours > 0:
                conditions.append(f'收货起{hours}小时内申请，包含收货时刻、不含截止时刻')
            if rules.get('requires_unworn') is True:
                conditions.append('须未穿着')
            if rules.get('requires_tags') is True:
                conditions.append('吊牌完好')
            if conditions:
                add('policy_conditions', '政策条件：' + '；'.join(conditions) + '。', return_refs, kind='policy')
            if rules.get('custom_excluded') is True:
                add('custom_exception', '政策例外：定制商品不适用该退货条款。',
                    _refs(result, 'return_eligibility', ('custom_excluded',)), kind='policy_exception')

        times = []
        delivered = _beijing(facts.get('delivered_at'))
        deadline = _beijing(window.get('before'))
        assessed = _beijing(result.get('as_of'))
        if delivered:
            times.append('订单记录收货时间' + delivered)
        if deadline:
            times.append('程序计算退货截止' + deadline + '（不含）')
        if assessed:
            times.append('本轮判断时间' + assessed)
        if times:
            add('order_times', '；'.join(times) + '，均为北京时间。', kind='tool_facts')

    descriptions = []
    already_explained = set(result.get('failed_conditions', []))
    for field, true_text, false_text, failure in (
            ('is_unworn', '未穿着', '已穿着', 'user_reports_worn'),
            ('tags_intact', '吊牌完好', '吊牌不完整', 'user_reports_tags_missing')):
        if type(claims.get(field)) is bool:
            if claims[field] or failure not in already_explained:
                descriptions.append(true_text if claims[field] else false_text)
    reason_names = {'size': '尺码不合适', 'preference': '个人偏好',
                    'merchant_responsibility': '商家责任', 'other': '其他原因', 'unknown': '原因尚不明确'}
    if claims.get('reason') in reason_names:
        descriptions.append('原因是' + reason_names[claims['reason']])
    if descriptions:
        add('self_report', '用户自述（未核实）：' + '、'.join(descriptions) + '。', kind='user_claim')

    missing_names = {'is_unworn': '是否未穿着（用户自述）', 'tags_intact': '吊牌是否完好（用户自述）',
                     'reason': '退货原因（用户自述）', 'delivered_at': '收货时间（订单记录）',
                     'product_category': '商品类别（订单记录）', 'is_custom': '定制状态（订单记录）',
                     'shop_id': '店铺归属（订单记录）'}
    missing = [missing_names.get(key, '必要业务事实') for key in result.get('missing', [])]
    if missing:
        add('missing', '待补：' + '、'.join(dict.fromkeys(missing)) + '；不得把未知当成已满足。')

    shipping = result.get('shipping', {})
    if 'shipping' in topics and shipping.get('status') != 'not_applicable':
        payer = shipping.get('payer')
        if shipping_refs and payer in {'buyer', 'merchant'}:
            text = '按本次未核实退货原因及适用政策，运费由' + {'buyer': '买家', 'merchant': '商家'}[payer] + '承担；仍需售后核验。'
        elif shipping.get('status') == 'needs_verified_responsibility':
            text = '本次商家责任缺少已核实记录，运费承担尚不能确定，不能仅凭自述判定商家承担。'
        else:
            text = '当前依据不足，尚不能确定本单退货运费承担。'
        if 'eligibility' not in topics and shipping_refs and result.get('policy_version'):
            text = '按支付时适用版本' + str(result['policy_version']) + '：' + text
        add('shipping', text, shipping_refs, kind='rule_decision' if shipping_refs else 'program_state')
    elif shipping.get('status') == 'not_requested' and shipping_refs:
        mapping = rules.get('shipping_by_reason', {})
        supplements = []
        for reason, label in (('size', '尺码原因'), ('preference', '个人偏好')):
            if mapping.get(reason) in {'buyer', 'merchant'}:
                supplements.append(label + '由' + {'buyer': '买家', 'merchant': '商家'}[mapping[reason]] + '承担运费')
        if mapping.get('merchant_responsibility') == 'verified_record_required':
            supplements.append('商家责任须有已核实依据')
        if supplements:
            add('shipping_policy', '补充运费政策：' + '；'.join(supplements) + '。本次未判断本单运费。',
                shipping_refs, required=False, kind='policy_supplement')

    if not result.get('policy_version') and status == 'insufficient_evidence':
        add('evidence_gap', '未取得本单支付时适用的完整政策依据，不能套用其他版本补结论。')
    scope_note = '本次仅核对运费，不判断退货资格。' if 'shipping' in topics and 'eligibility' not in topics else ''
    add('boundary', scope_note + '仅合成政策核对；未批准、受理或执行退货退款。')
    return catalog


def validate_support_claims(body, result):
    """不接受自由正文旁路；逐项核对本次允许结论及其准确来源。"""
    from support_runtime import expected_decision
    if not isinstance(body, dict):
        return ['invalid_support_structure']
    errors = []
    if set(body) != {'schema_version', 'status', 'decision', 'claims'}:
        errors.append('unexpected_output_fields')
    if type(body.get('schema_version')) is not int or body['schema_version'] != 2:
        errors.append('invalid_schema_version')
    expected_status = 'clarification' if result.get('status') == 'clarification' else 'completed'
    if body.get('status') != expected_status:
        errors.append('status_mismatch')
    if body.get('decision') != expected_decision(result):
        errors.append('decision_mismatch')
    catalog = build_support_claim_catalog(result)
    selected = body.get('claims')
    if not isinstance(selected, list) or not selected or len(selected) > len(catalog):
        return errors + ['invalid_claims']
    seen = set()
    for item in selected:
        if not isinstance(item, dict) or set(item) != {'id', 'citation_ids'}:
            errors.append('invalid_claim_fields')
            continue
        identity = item.get('id')
        if not isinstance(identity, str) or identity not in catalog:
            errors.append('unsupported_claim')
            continue
        if identity in seen:
            errors.append('duplicate_claim:' + identity)
        seen.add(identity)
        refs = item.get('citation_ids')
        if (not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs)
                or len(set(refs)) != len(refs) or set(refs) != set(catalog[identity]['citation_ids'])):
            errors.append('wrong_citation:' + identity)
    errors.extend('missing_claim:' + identity for identity, item in catalog.items() if item['required'] and identity not in seen)
    return list(dict.fromkeys(errors))


def _render_selected(catalog, identities):
    lines = []
    for identity in identities:
        claim = catalog[identity]
        refs = ' '.join('[' + ref + ']' for ref in claim['citation_ids'])
        lines.append(claim['text'] + (' ' + refs if refs else ''))
    return '\n'.join(lines)


def render_support_claims(body, result):
    errors = validate_support_claims(body, result)
    if errors:
        raise ValueError('invalid_support_claims:' + ','.join(errors))
    catalog = build_support_claim_catalog(result)
    return _render_selected(catalog, [item['id'] for item in body['claims']])


def fallback_support_claims(result):
    """只输出必需可信结论；调用方另加明确的模型解释降级提示。"""
    catalog = build_support_claim_catalog(result)
    return _render_selected(catalog, [identity for identity, item in catalog.items() if item['required']])
