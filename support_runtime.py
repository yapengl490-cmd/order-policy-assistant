"""售后工具连接：订单授权、政策检索、确定性判断及可追溯引用。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from copy import deepcopy
import re
from zoneinfo import ZoneInfo


def review_after_sales(engine, order_id, *, buyer_id=None, shop_id=None, state, should_cancel=lambda:False):
    from buyer_support_service import get_support_context
    if should_cancel():
        raise InterruptedError('cancelled_before_support')
    context = get_support_context(engine, order_id, buyer_id=buyer_id, shop_id=shop_id)
    return review_support_context(engine, context, state=state, should_cancel=should_cancel)


def review_support_context(engine, context, *, state, should_cancel=lambda:False, keyword_only=False):
    """接收程序已授权并冻结的订单事实；协作路径不重新读取或让模型改归属。"""
    from buyer_support_service import evaluate_buyer_support
    from policy_kb import select_policy, search_policy_chunks
    from embedding_client import EmbeddingClient, EmbeddingSettings
    if should_cancel():
        raise InterruptedError('cancelled_before_support')
    context = deepcopy(context)
    claims = {key:item['value'] for key,item in state.get('claims',{}).items()}
    now = datetime.fromisoformat((state['confirmed'].get('assessment_at') or state['defaults']['assessment_at'])['value'])
    policy = {'status':'insufficient_evidence'}
    retrieval = {'status':'not_run','reason':'no_authorized_order_context'}
    facts, order = context.get('trusted_facts',{}), context.get('order',{}) or {}
    if context.get('status') == 'ok' and order.get('shop_id') and facts.get('product_category'):
        scope = dict(store_id=order['shop_id'], category=facts['product_category'], paid_at=datetime.fromisoformat(order['paid_at']))
        policy = select_policy(engine, **scope)
        if policy.get('status') == 'ok':
            if should_cancel():
                raise InterruptedError('cancelled_before_retrieval')
            # 配置决定是否启用真实嵌入；未配置时明确关键词基线，不伪装向量结果。
            config = EmbeddingSettings.load(root=Path(__file__).resolve().parent)
            use_embedding = not keyword_only and bool(getattr(config, 'api_key', ''))
            embedder = EmbeddingClient(config) if use_embedding else None
            retrieval = search_policy_chunks(engine, state['original_question'][:1000], **scope,
                         mode='hybrid' if use_embedding else 'keyword', embedder=embedder, limit=4)
            # 完整对照保存在离线评测；运行答复不重复塞入三份同一原文。
            for rank_key in ('keyword_results', 'vector_results'):
                if rank_key in retrieval:
                    retrieval[rank_key] = [{'chunk_id': x['chunk_id'], 'score': x['score']} for x in retrieval[rank_key]]
            if should_cancel():
                raise InterruptedError('cancelled_after_retrieval')
    result = evaluate_buyer_support(context, policy, claims=claims, now=now, topics=state.get('topics'))
    # 所有必须适用的条款由规则判断引用；语义召回辅助解释，不能漏召回例外后放宽规则。
    # 仅保留已经匹配到本单的规则，用于简短展示必要条件；不加载咨询时的新政策。
    if result.get('policy_version'):
        result['applicable_rules'] = deepcopy(policy.get('rules', {}))
    result['retrieval'] = retrieval
    result['rule_evidence_mode'] = 'mandatory_clauses_plus_retrieved_context'
    scope = context.get('scope', {})
    scope_key = 'buyer_id' if scope.get('role') == 'buyer' else 'shop_id'
    result[scope_key] = scope.get(scope_key)
    result['decision'] = expected_decision(result)
    result['citation_index'] = citation_index(result)
    required_sections = set()
    if result.get('citations'):
        if 'eligibility' in state.get('topics', []): required_sections.add('return_eligibility')
        if 'shipping' in state.get('topics', []) and result.get('shipping',{}).get('status') != 'not_applicable': required_sections.add('shipping_cost')
    result['required_citation_ids'] = [key for key, item in result['citation_index'].items() if item.get('section') in required_sections]
    return result


def expected_decision(result):
    return {'eligibility':result.get('eligibility','undetermined'),
            'shipping_payer':result.get('shipping',{}).get('payer'),
            'policy_version':result.get('policy_version'), 'status':result.get('status')}


def citation_index(result):
    output = {}
    for rule, source in result.get('citations',{}).items():
        if isinstance(source,dict) and source.get('chunk_id'):
            key=source['chunk_id']; output[key]={**deepcopy(source),'supports':[]}
    for rule, source in result.get('citations',{}).items():
        if isinstance(source,dict) and source.get('chunk_id') in output:
            output[source['chunk_id']]['supports'].append(rule)
    return output


def _asserts(pattern, answer):
    """仅筛明确断言；引述、条件假设和待核说法不当作本单已确定结论。"""
    plain = re.sub(r'“[^”]*”|「[^」]*」|"[^"\n]*"|‘[^’]*’', '', answer)
    for sentence in re.split(r'[。！？!?；;\n]', plain):
        conditional = False
        for clause in re.split(r'[，,]', sentence):
            # 条件可在前半句，后半句“才可以退货”仍属假设。
            if re.search(r'如果|假如|假设|只要|仅当|只有|(?:^|：)若|待.+(?:核实|确认)', clause):
                conditional = True
            if re.match(r'\s*(?:但|不过|然而)', clause):
                conditional = False
            for match in re.finditer(pattern, clause):
                before = clause[:match.start()]
                # 只识别完整的并列否定链，不把逗号后的转折或新增正面承诺一起豁免。
                negated_chain = r'(?:不构成|不意味着)(?:(?:(?:退货|退款|售后)?批准|已经?(?:批准|退款|受理))\s*(?:或|和|及|、)\s*)*$'
                if re.search(negated_chain, before):
                    continue
                if conditional or re.search(r'不能(?:确认|确定|证明|认定|据此)|无法|尚未|未(?:经|被)?(?:确认|核实|证实)|'
                                            r'不(?:代表|表示|等于)|不是说|不能说|是否|(?:需|待|请)(?:先|进一步)?(?:核对|核实|确认)', before):
                    continue
                if re.search(r'(?:不|未|并非|不是|不能|不可)$', before):
                    continue
                return True
    return False


def _support_date_errors(answer, result):
    """有限核对正文中的明确时间陈述；时区换写必须仍是同一时刻。"""
    from condition_state import BUSINESS_TIMEZONE, _ISO, _CHINESE, _datetime_text

    expected = {
        'return_deadline': result.get('return_window', {}).get('before'),
        'delivery': result.get('trusted_facts', {}).get('delivered_at'),
        'assessment': result.get('as_of'),
    }
    labels = (
        ('return_deadline', re.compile(r'截止|截至|before', re.I)),
        ('delivery', re.compile(r'收货|签收|delivered_at|starts_at', re.I)),
        ('assessment', re.compile(r'判断|核查时|评估时|as_of', re.I)),
    )
    errors = []
    matches = sorted([*_ISO.finditer(answer), *_CHINESE.finditer(answer)], key=lambda m: m.start())
    previous_end = 0
    for match in matches:
        # 只在当前短句绑定字段，避免把上一时间的含义套到下一时间。
        prefix = re.split(r'[，,；;。！？!?\n]', answer[previous_end:match.start()])[-1][-50:]
        suffix = answer[match.end():match.end()+25]
        previous_end = match.end()
        candidates = [(m.end(), key) for key, pattern in labels for m in pattern.finditer(prefix)]
        key = max(candidates)[1] if candidates else None
        if key is None and re.match(r'\s*(?:收货|签收)', suffix):
            key = 'delivery'
        if key is None and re.match(r'\s*(?:时点|时刻)', suffix):
            key = 'assessment'
        if key is None or re.search(r'如果|假如|假设|不能确认|无法确认|是否|待核|请(?:先)?(?:核对|确认)', prefix):
            continue
        fact = expected[key]
        if not fact:
            errors.append(key + '_date_unavailable')
            continue
        try:
            trusted = datetime.fromisoformat(fact.replace('Z', '+00:00'))
            if trusted.tzinfo is None:
                raise ValueError('fact_without_timezone')
            raw = match[0]
            date_only = bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}|\d{4}年\d{1,2}月\d{1,2}[日号]', raw))
            # 沿用输入条件的北京时间默认值；临近的明确UTC标注另行处理。
            zone = ZoneInfo(BUSINESS_TIMEZONE)
            zone_pattern = r'(?:UTC)([+-]\d{1,2}(?::?\d{2})?)?|北京时间|中国标准时间'
            suffix_zone = re.match(r'\s*[（(]?\s*(' + zone_pattern + r')', suffix, re.I)
            zone_text = suffix_zone[1] if suffix_zone else prefix[-15:]
            explicit_zone = re.search(zone_pattern, zone_text, re.I)
            if explicit_zone and explicit_zone[0].upper().startswith('UTC'):
                offset = explicit_zone[1] or '+00:00'
                sign = -1 if offset.startswith('-') else 1
                parts = offset[1:].replace(':', '')
                hours, minutes = (int(parts[:-2]), int(parts[-2:])) if len(parts) > 2 else (int(parts), 0)
                if hours > 23 or minutes > 59:
                    raise ValueError('invalid_timezone')
                zone = timezone(sign * timedelta(hours=hours, minutes=minutes))
            if date_only:
                parts = re.findall(r'\d+', raw)
                stated_date = datetime(*map(int, parts)).date()
                if stated_date != trusted.astimezone(zone).date():
                    errors.append(key + '_date_mismatch')
                elif key == 'return_deadline':
                    # 截止精确到时刻，不能把“某日”暗补为零点或当天结束。
                    errors.append('return_deadline_time_missing')
                continue
            normalized, _ = _datetime_text(raw)
            stated = datetime.fromisoformat(normalized)
            if explicit_zone and not re.search(r'Z$|[+-]\d{2}:\d{2}$', raw):
                stated = stated.replace(tzinfo=zone)
            if stated != trusted:
                errors.append(key + '_date_mismatch')
        except (TypeError, ValueError):
            errors.append(key + '_date_invalid')
    return errors


def validate_support_answer(body, result):
    """检查结构化决定、引用归属及显然越权承诺；不宣称完整语义正确性。"""
    errors=[]
    if body.get('decision') != expected_decision(result):
        errors.append('decision_mismatch')
    refs=body.get('citation_ids')
    available=result.get('citation_index') or citation_index(result)
    if not isinstance(refs,list) or any(not isinstance(x,str) or x not in available for x in refs) or len(set(refs))!=len(refs):
        errors.append('invalid_citations')
    elif available and not refs:
        errors.append('missing_citations')
    elif not set(result.get('required_citation_ids',[])).issubset(refs):
        errors.append('required_clause_citations_missing')
    answer=body.get('answer','')
    if not isinstance(answer,str) or not 1<=len(answer)<=1200:
        errors.append('invalid_answer')
    elif _asserts(r'已经?(?:批准|退款|受理)|保证(?:退货|退款)|审核已通过|已为你(?:申请|退款)|本次(?:直接|可)?批准退货|不需要再核验|无需核验|所有费用商家负责',answer):
        errors.append('unsupported_execution_claim')
    if isinstance(answer,str):
        errors.extend(_support_date_errors(answer, result))
        # 只拦明确可识别的反向结论，否定/待核话语不作为肯定承担。
        payer=result.get('shipping',{}).get('payer')
        if payer=='buyer' and _asserts(r'(?:运费|邮费).{0,4}(?:由)?商家(?:承担|支付|出)',answer):
            errors.append('shipping_text_contradiction')
        if payer!='merchant' and _asserts(r'(?:确定|应当|应|需要|由)商家(?:来)?承担(?:退货)?运费',answer):
            errors.append('shipping_text_contradiction')
        if result.get('eligibility')=='eligible_with_self_report' and _asserts(r'(?:不能|不可|不可以|不符合).{0,3}(?:退货|申请退货)',answer):
            errors.append('eligibility_text_contradiction')
        positive_return = r'(?:可以|能够|仍能|能直接|能|可直接)(?:申请)?退货|符合(?:全部)?退货条件'
        if result.get('eligibility') in {'ineligible', 'undetermined'} and _asserts(positive_return, answer):
            errors.append('eligibility_text_contradiction')
        if result.get('eligibility') == 'eligible_with_self_report' and _asserts(
                r'(?:已经|已|确定|完全)符合(?:全部)?退货条件|直接寄回即可|无条件(?:申请)?退货', answer):
            errors.append('self_report_upgraded')
        if ('delivered_at' in result.get('missing', []) or result.get('status') == 'not_found') and _asserts(
                r'(?:后台|系统|记录)(?:显示|已确认).{0,12}(?:已签收|已收货)|(?:已|已经)(?:于.{0,15})?(?:签收|收货)', answer):
            errors.append('unavailable_delivery_asserted')
        if result.get('status') == 'not_found' and _asserts(r'已查到(?:该)?订单|支付(?:金额)?(?:为|是)?\s*\d+(?:\.\d+)?\s*元', answer):
            errors.append('unavailable_order_asserted')
        # 正文引用也要属于本轮原文；不能另写不存在的编号再附一组正确引用。
        inline_refs = re.findall(r'\[([A-Za-z0-9][A-Za-z0-9_.-]{0,79}:[A-Za-z0-9_.:-]{1,80})\]', answer)
        if any(ref not in available for ref in inline_refs):
            errors.append('invalid_inline_citation')
    if result.get('status')=='clarification' and body.get('status')!='clarification':
        errors.append('clarification_status_mismatch')
    if result.get('status')!='clarification' and body.get('status')!='completed':
        errors.append('completion_status_mismatch')
    return list(dict.fromkeys(errors))


def factual_support_answer(result, *, compact=False):
    """模型输出未通过时显式降级；原始输出仍保留在日志，不悄悄改写。"""
    labels={'eligible_with_self_report':'按订单记录和你的自述，暂满足该演示政策的退货条件，仍需售后核验',
            'ineligible':'本次未满足该演示政策的退货条件','undetermined':'目前不能确定是否满足退货条件','not_requested':'本次只核对运费条款，尚未判断退货资格'}
    if compact:
        if result.get('status') in {'not_found', 'data_error', 'support_data_unavailable', 'insufficient_evidence'}:
            return result.get('message') or '本次证据不足，不能确定售后结论。'
        return labels.get(result.get('eligibility'), '目前证据不足') + '。'
    parts=[result.get('message',''),labels.get(result.get('eligibility'),'目前证据不足')+'。']
    missing={'is_unworn':'是否未穿过','tags_intact':'吊牌是否完好','reason':'退货原因',
             'delivered_at':'后台收货时间','product_category':'商品类别','is_custom':'是否定制'}
    if result.get('missing'):
        parts.append('还缺：'+'、'.join(missing.get(x,x) for x in result['missing'])+'。')
    payer=result.get('shipping',{}).get('payer')
    if payer:
        parts.append('按当前自述原因，运费由'+{'buyer':'买家','merchant':'商家'}[payer]+'承担。')
    elif result.get('shipping',{}).get('status') not in {'not_requested','not_applicable'}:
        parts.append('运费承担尚不能确定。')
    if result.get('policy_version'):
        parts.append('政策按订单支付时选择，版本：'+str(result['policy_version'])+'。')
    parts.append('这是合成资料演示，未批准退货或执行退款。')
    return ''.join(parts)


def with_source_quotes(answer, result, refs=None):
    index=result.get('citation_index') or citation_index(result)
    selected=list(index) if refs is None else refs
    if not selected:
        return answer
    lines=['\n\n政策原文依据（合成演示）：']
    for ref in selected:
        item=index[ref]
        lines.append(f"[{ref}] {item.get('source_path', item.get('document_id',''))} 第{item.get('line_start','?')}–{item.get('line_end','?')}行：{item.get('text','')}")
    return answer+'\n'.join(lines)


def concise_support_answer(answer, result, refs=None):
    """模型短解释与规则保护信息分工，原文只留在facts供展开/导出，不截断正文。"""
    notes = []
    rules = result.get('applicable_rules', {})
    topics = result.get('requested_topics', [])
    if result.get('policy_version'):
        notes.append('按支付时政策' + str(result['policy_version']))
    if 'eligibility' in topics and rules:
        hours = rules.get('return_window_hours')
        if type(hours) is int:
            window = f'收货起{hours}小时内（截止时刻不含）'
            notes.append(window)
        if rules.get('requires_unworn'): notes.append('须未穿着')
        if rules.get('requires_tags'): notes.append('吊牌完好')
        if rules.get('custom_excluded'): notes.append('定制商品除外')
    missing_names = {'is_unworn':'是否未穿着', 'tags_intact':'吊牌是否完好', 'reason':'退货原因',
                     'delivered_at':'后台收货时间', 'product_category':'商品类别', 'is_custom':'后台定制状态', 'shop_id':'店铺记录'}
    if result.get('missing'):
        notes.append('待补：' + '、'.join(missing_names.get(x,x) for x in result['missing']))
    failed_names = {'custom_product_excluded':'定制商品不适用', 'return_window_expired':'已超过退货时限',
                    'user_reports_worn':'自述已穿着', 'user_reports_tags_missing':'自述吊牌不完整', 'category_not_covered':'品类不在适用范围'}
    if result.get('failed_conditions'):
        notes.append('本单未满足：' + '、'.join(failed_names.get(x,x) for x in result['failed_conditions']))
    shipping = result.get('shipping', {})
    if 'shipping' in topics and shipping.get('status') not in {'not_requested','not_applicable'}:
        if shipping.get('payer') == 'buyer': notes.append('按自述原因，退货运费由买家承担')
        elif shipping.get('payer') == 'merchant': notes.append('运费由商家承担（需经核实责任依据）')
        else: notes.append('运费承担未确定，商家责任须有核实依据')
    notes.append('仅合成政策核对；自述未核实，未批准或执行退货退款')
    if result.get('eligibility') == 'ineligible': notes.append('不否定其他未接入的售后权利')
    index = result.get('citation_index') or citation_index(result)
    selected = list(index) if refs is None else refs
    references = ' '.join('['+ref+']' for ref in selected if ref in index)
    # 不尝试按关键词删模型句子，避免删掉否定、例外或尚未知的事实。
    return answer.rstrip() + '\n\n核对条件：' + '；'.join(notes) + '。' + ('\n依据：'+references if references else '')
