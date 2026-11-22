"""Shopify chat conditions and zero-argument tool adapter, separate from local O-orders.

The runtime must call make_shopify_handler before its first model request. That
factory checks the server binding without reading credentials or making I/O.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import re

from display_preferences import answer_style, format_minor_amount
from shopify_connection import ShopifySettings, ShopifyConnectionError, normalize_shop_domain
from shopify_orders import ShopifyReadClient

KINDS = {'shopify_order', 'shopify_policy'}
ORDER = re.compile(r'gid://shopify/Order/[1-9][0-9]{0,24}(?![0-9A-Za-z_])|(?<![A-Za-z0-9_])#[1-9][0-9]{0,17}(?![0-9A-Za-z_])')
PLATFORM = re.compile(r'(?<![A-Za-z0-9_])shopify(?![A-Za-z0-9_])', re.I)
LOCAL_ORDER = re.compile(r'(?<![A-Za-z0-9_])(?:O\d[A-Za-z0-9_-]*|O_[A-Za-z0-9_]+)(?![A-Za-z0-9_])')
POLICY = re.compile(r'政策|条款|规则')
TIME_LABEL = re.compile(r'退款(?:截止|截至)|截止(?:时间|时刻)?|截至(?:时间|时刻)?')
TIME_HINT = re.compile(r'判断时间|时间改|最近一次|昨天|今天|明天|\d{1,2}月\d{1,2}[日号]|\d{4}-\d{2}-\d{2}')
TOOLS = {'shopify_order': 'shopify_check_order', 'shopify_policy': 'shopify_current_policy'}
TIME_ISSUES = {'invalid_refund_before', 'conflicting_refund_before', 'missing_previous_cutoff'}
REFERENCE = re.compile(r'(?:退款截止|截止时间|截止时刻)\s*(?:沿用刚才|保持不变)(?=\s*(?:[，,；;。！？!?\n]|$))')


class ShopifyChatError(ValueError):
    """Safe pre-model refusal; no credentials have been loaded."""


def _text(message):
    from condition_state import _business_condition_text
    safe = list(_business_condition_text(message))
    for pattern in (r'```[\s\S]*?(?:```|$)', r'“[^”]*”|「[^」]*」|‘[^’]*’|"[^"\n]*"', r"'[^'\n]*'"):
        for match in re.finditer(pattern, message):
            safe[match.start():match.end()] = ' ' * len(match[0])
    return ''.join(safe)


def uses_shopify(message, previous=None):
    if not isinstance(message, str):
        return False
    text = _text(message)
    if PLATFORM.search(text) or ORDER.search(text):
        return True
    if previous and previous.get('kind') in KINDS:
        # Explicit local order or another business opens its own condition state.
        return not (LOCAL_ORDER.search(text) or re.search(r'推荐|选品|商品预算|合成店铺|S001|普通鞋类', text))
    return False


def reconcile_shopify(message, previous, now, turn_id):
    from condition_state import _aware, _ISO, _CHINESE, _datetime_text
    now = _aware(now)
    if not isinstance(message, str) or not 1 <= len(message.strip()) <= 4000:
        raise ValueError('问题须为1—4000字符')
    if not isinstance(turn_id, str) or not 1 <= len(turn_id) <= 128:
        raise ValueError('turn_id无效')
    text = _text(message)
    matches = list(ORDER.finditer(text))
    ids = {m[0] for m in matches}
    old = deepcopy(previous or {})
    revision = old.get('revision', 0) + 1
    expired = False
    if old:
        try:
            expired = now >= datetime.fromisoformat(old['expires_at'])
        except (KeyError, ValueError, TypeError):
            expired = True
    reset = bool(re.search(r'重置|清空(?:查询|条件|会话)', text))
    old_kind = old.get('kind')
    kind = 'shopify_order' if ids else ('shopify_policy' if POLICY.search(text) else old_kind if old_kind in KINDS else 'shopify_order')
    switched = bool(old and old_kind != kind)
    previous_id = old.get('confirmed', {}).get('order_id', {}).get('value')
    new_query = bool(len(ids) == 1 and previous_id and next(iter(ids)) != previous_id)
    pending = [x for x in old.get('issues', []) if x in TIME_ISSUES]
    if switched or reset or new_query:
        pending = []
    if expired or switched or reset or new_query:
        old = {}
    confirmed, defaults = deepcopy(old.get('confirmed', {})), deepcopy(old.get('defaults', {}))
    issues, changed = [], []
    def put(field, value, start, end, rule):
        before = confirmed.get(field, {}).get('value')
        confirmed[field] = {'value': value, 'source': {'turn_id':turn_id, 'quote':message[start:end],
            'span':[start,end], 'rule':rule, 'revision':revision}}
        defaults.pop(field, None)
        if before != value: changed.append({'field':field, 'before':before, 'after':value})
    if LOCAL_ORDER.search(text):
        issues.append('mixed_local_and_platform_orders')
    if len(ids) > 1:
        confirmed.clear(); defaults.clear(); issues.append('one_platform_order_at_a_time')
    elif ids:
        m = matches[0]; put('order_id',m[0],m.start(),m.end(),'explicit_shopify_order')
    if kind == 'shopify_order':
        if re.search(r'能退|可退|退货|资格|运费|换货|维修|赔偿|到账', text):
            issues.append('platform_after_sales_or_arrival_not_supported')
        references = [m for m in REFERENCE.finditer(text)
            if not re.search(r'不是|并非|不要|不用|是否', re.split(r'[，,；;。！？!?\n]', text[:m.start()])[-1])
            and not re.match(r'\s*[？?]', text[m.end():])]
        time_mentions = bool(TIME_LABEL.search(text) or TIME_HINT.search(text))
        values, parsed_spans = [], []
        for pattern in (_ISO, _CHINESE):
            for m in pattern.finditer(text):
                parsed_spans.append(m.span())
                prefix = re.split(r'[，,；;。！？!?\n]', text[:m.start()])[-1]
                if not TIME_LABEL.search(prefix) and not (old.get('kind')=='shopify_order' and text.strip()==m[0]):
                    issues.append('invalid_refund_before')
                    continue
                try:
                    value, rule = _datetime_text(m[0])
                    if datetime.fromisoformat(value) > now: raise ValueError('future')
                    values.append((value,m,rule))
                except ValueError:
                    issues.append('invalid_refund_before')
        # A second unsupported cutoff expression cannot hide beside a valid timestamp.
        remainder = list(text)
        for start,end in parsed_spans:
            remainder[start:end] = ' ' * (end-start)
        remainder = ''.join(remainder)
        for match in TIME_LABEL.finditer(remainder):
            tail = re.split(r'[，,；;。！？!?\n]', remainder[match.end():])[0].strip(' ：:=')
            amount_tail = r'(?:的?(?:支付金额|退款金额|净额|净支出|金额)(?:[、和及]*(?:支付金额|退款金额|净额|净支出|金额))*)?'
            if tail and not re.fullmatch(r'(?:改为|改成|为|是|到)?\s*' + amount_tail + r'|(?:沿用刚才|保持不变)', tail):
                issues.append('invalid_refund_before')
        if references:
            prior = old.get('confirmed', {}).get('refund_before') or old.get('defaults', {}).get('refund_before')
            if not (old.get('active') and previous_id and previous_id == confirmed.get('order_id',{}).get('value') and prior):
                issues.append('missing_previous_cutoff')
            else:
                try:
                    value = prior['value']; parsed = datetime.fromisoformat(value)
                    if parsed.tzinfo is None or parsed > now: raise ValueError
                    if any(datetime.fromisoformat(v) != parsed for v,_,_ in values):
                        issues.append('conflicting_refund_before')
                    elif not pending and not any(x in TIME_ISSUES for x in issues):
                        m = references[0]; put('refund_before',value,m.start(),m.end(),'reuse_previous_platform_cutoff')
                        source=prior['source']; origin=source.get('origin',source)
                        confirmed['refund_before']['source']['origin']={k:deepcopy(origin[k]) for k in ('turn_id','quote','span','rule','revision') if k in origin}
                except (KeyError, ValueError, TypeError):
                    issues.append('invalid_refund_before')
        elif len({datetime.fromisoformat(v) for v,_,_ in values}) > 1:
            issues.append('conflicting_refund_before')
        elif values and not any(x in TIME_ISSUES for x in issues):
            value,m,rule = values[0];put('refund_before',value,m.start(),m.end(),rule);pending=[]
        elif time_mentions:
            issues.append('invalid_refund_before')
        time_issues=list(dict.fromkeys(pending+[x for x in issues if x in TIME_ISSUES]))
        issues.extend(time_issues)
        if time_issues:
            confirmed.pop('refund_before',None);defaults.pop('refund_before',None)
        elif 'refund_before' not in confirmed and 'refund_before' not in defaults:
            defaults['refund_before']={'value':now.isoformat(),'source':{'turn_id':turn_id,'quote':None,'span':None,'rule':'program_task_time','revision':revision}}
        missing=[] if 'order_id' in confirmed else ['order_id']
    else:
        confirmed.clear();defaults.clear();missing=[]
        if TIME_LABEL.search(text) or TIME_HINT.search(text) or re.search(r'支付时|历史|旧版|当时',text):
            issues.append('historical_platform_policy_not_available')
    issues=list(dict.fromkeys(issues))
    ready=not (missing or issues or reset)
    if 'mixed_local_and_platform_orders' in issues:
        clarification='请分开查询Shopify平台订单与本地O编号订单；两种来源不能混算。'
    elif 'platform_after_sales_or_arrival_not_supported' in issues:
        clarification='当前平台工具只核测试订单成功收款、成功退款及净额；尚不能核本单退货资格、运费或真实到账。当前店铺政策也不证明支付时适用版本。'
    elif 'historical_platform_policy_not_available' in issues:
        clarification='当前只能读取Shopify店铺现行政策快照，尚无历史版本证据，不能核对过去或订单支付时的政策。'
    elif issues:
        clarification='请只给一个Shopify订单及明确有效退款截止，例如“Shopify #1001，退款截止2026-11-20T14:30:00+08:00”；截止必须含时刻且不晚于现在。'
    elif missing or reset:
        clarification='请给出Shopify测试订单编号，例如#1001，或平台Order GID。'
    else:
        clarification=''
    return {'schema_version':1,'revision':revision,'turn_id':turn_id,'kind':kind,'business_timezone':'Asia/Shanghai',
            'updated_at':now.isoformat(),'expires_at':(now+timedelta(minutes=30)).isoformat(),'original_question':message,
            'confirmed':confirmed,'defaults':defaults,'missing':missing,'issues':issues,'changed':changed,'blocked_fields':[],
            'reset':reset,'expired':expired,'switched':switched,'new_query':new_query,'active':True,'ready':ready,
            'needs_clarification':not ready,'allowed_tools':[TOOLS[kind]] if ready else [],'clarification':clarification}


def _attach_citations(fact):
    value=deepcopy(fact)
    digest=hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    index={}
    if value.get('status')=='ok' and 'order_id' in value:
        index['SHOPIFY-ORDER:'+value['order_id'].rsplit('/',1)[-1]]={'document_id':value['order_id'],'source_sha256':digest,
            'text':json.dumps({**{k:value[k] for k in ('currency','paid_amount_minor','refund_amount_minor','net_amount_minor','refund_before','shop_domain','observed_at') if k in value}, 'excluded_transaction_count':len(value.get('excluded_transactions',[]))},ensure_ascii=False),'pointer':'/'}
        for field in ('successful_payments','successful_refunds','excluded_transactions'):
            for i,item in enumerate(value.get(field,[])):
                index['SHOPIFY-TX:'+item['transaction_id'].rsplit('/',1)[-1]]={'document_id':item['transaction_id'],
                    'source_sha256':digest,'text':json.dumps(item,ensure_ascii=False),'pointer':f'/{field}/{i}'}
    for item in value.get('policies',[]):
        index[item['citation_id']]={'document_id':item['source_id'],'source_sha256':item['body_sha256'],
            'text':item['body_html'],'updated_at':item['updated_at'],'untrusted_source_text':True}
    value['citation_index']=index;value['source_snapshot_sha256']=digest
    return value


def make_shopify_handler(state, *, access_role, platform_context, settings_path=None, transport=None, remaining_seconds=None):
    """Call BEFORE model I/O. Context must be server-owned, never model parameters."""
    if access_role!='merchant' or not isinstance(platform_context,dict):
        raise ShopifyChatError('尚无授权商家平台绑定；本地买家身份不能代替Shopify客户身份。')
    try: domain=normalize_shop_domain(platform_context.get('shop_domain'))
    except ShopifyConnectionError:
        raise ShopifyChatError('尚未配置有效的平台店铺授权绑定。') from None
    if platform_context.get('allow_network') is not True:
        raise ShopifyChatError('平台只读联网尚未启用；未读取凭据或调用模型/平台。')
    if remaining_seconds is not None and not callable(remaining_seconds):
        raise ShopifyChatError('任务剩余时间必须由服务器预算回调提供。')
    if not isinstance(state,dict):
        raise ShopifyChatError('平台查询条件状态无效，未读取凭据。')
    state=deepcopy(state);kind=state.get('kind')
    if state.get('schema_version')!=1 or kind not in KINDS or not state.get('ready') or state.get('allowed_tools')!=[TOOLS[kind]]:
        raise ShopifyChatError('平台查询条件尚未确认，不能执行工具。')
    order_id=cutoff=None
    if kind=='shopify_order':
        order_id=state.get('confirmed',{}).get('order_id',{}).get('value')
        selected=state.get('confirmed',{}).get('refund_before') or state.get('defaults',{}).get('refund_before')
        try:
            cutoff=selected['value']; parsed=datetime.fromisoformat(cutoff)
            if not isinstance(order_id,str) or not ORDER.fullmatch(order_id) or parsed.tzinfo is None or parsed>datetime.fromisoformat(state['updated_at']):
                raise ValueError
        except (KeyError,TypeError,ValueError):
            raise ShopifyChatError('平台订单或截止状态无效，未读取凭据。') from None
    path=Path(settings_path) if settings_path is not None else Path(__file__).parent/'local/shopify.json'
    def current_timeout():
        timeout = remaining_seconds() if remaining_seconds is not None else 15.0
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or timeout <= 0:
            raise ShopifyConnectionError('任务剩余时间不足；未发送请求。')
        return min(15.0,timeout)
    def handler():
        try:
            timeout = current_timeout()
            settings=ShopifySettings.load(path)
            if remaining_seconds is not None:
                timeout = current_timeout()  # Account for time spent reading local configuration.
            client=ShopifyReadClient(settings,authorized_shop_domain=domain,access_role='merchant',allow_network=True,
                                     transport=transport,timeout_seconds=timeout)
            fact=client.read_order(order_id,refund_before=cutoff) if kind=='shopify_order' else client.read_current_policies()
            return _attach_citations(fact)
        except ShopifyConnectionError:
            return _attach_citations({'status':'failed','platform':'shopify','notice':'平台只读查询失败或配置不匹配；没有把失败当作不存在订单或政策，未输出凭据。'})
    return handler


def render_shopify_fact(fact, display=None):
    """Render a frozen platform fact; display preferences never alter its money."""
    style = answer_style(display)
    if fact.get('status')=='failed':
        return 'Shopify只读查询失败，未得到可核实结果；未把失败当作没有订单或政策。'
    if fact.get('status')=='not_found':
        return '当前授权Shopify店铺及可读日期范围内未找到该测试订单；这不证明订单在平台不存在。'
    if 'policies' in fact:
        if not fact['policies']:
            return '当前Shopify店铺没有读到非空退款政策；不能用隐私政策或S001合成鞋类政策代替，也无法判断本单可退。'
        lines=['已读取Shopify店铺当前退款政策快照，原文与摘要保留在依据中：']
        for item in fact['policies']:
            lines.append(f"- {item['title']}，最后修改 {item['updated_at']}。[{item['citation_id']}]")
        if style == 'detailed':
            if fact.get('shop_domain'):
                lines.append(f"查询范围：{fact['shop_domain']}。")
            if fact.get('observed_at'):
                lines.append(f"平台观察时刻：{fact['observed_at']}。")
            lines.append('依据中的原文是本次读取的当前政策快照；没有历史版本时，不回填订单支付时政策。')
        lines.append('创建/修改时间不证明订单支付时适用的政策版本；本单退货资格仍未知，不构成正式售后承诺。')
        return '\n'.join(lines)
    if fact.get('status')!='ok' or 'order_id' not in fact:
        return '平台资料不足，尚未形成可核实的订单金额结论。'
    digits=fact['minor_unit_digits'];currency=fact['currency']
    def money(field):
        return format_minor_amount(fact[field], currency, digits, display)
    key='SHOPIFY-ORDER:'+fact['order_id'].rsplit('/',1)[-1]
    lines=[f"Shopify测试订单{fact['order_name']}：成功收款{money('paid_amount_minor')}，截止前成功退款{money('refund_amount_minor')}，净额{money('net_amount_minor')}。[{key}]",
           f"退款截止{fact['refund_before']}，不含该时刻；全部计入收款亦须早于截止。"]
    for tx in fact.get('successful_payments',[])+fact.get('successful_refunds',[]):
        label='成功退款' if tx['kind']=='REFUND' else '成功收款'
        suffix='（不早于截止，本次未计入）' if tx.get('included_before_cutoff') is False else ''
        reference=f"[SHOPIFY-TX:{tx['transaction_id'].rsplit('/',1)[-1]}]"
        if style == 'standard':
            lines.append(f"{label}处理时间{tx['processed_at']}{suffix}。{reference}")
        else:
            amount=format_minor_amount(tx['amount_minor'],tx['currency'],digits,display)
            if style == 'brief':
                lines.append(f"{label}{amount}{suffix}。{reference}")
            else:
                lines.append(f"{label}{amount}，类型{tx['kind']}，状态{tx['status']}，处理时间{tx['processed_at']}{suffix}。{reference}")
    if style == 'detailed':
        lines.append(f"计算依据：截止前成功收款{money('paid_amount_minor')} − 截止前成功退款{money('refund_amount_minor')} = 净额{money('net_amount_minor')}。[{key}]")
        if fact.get('observed_at'):
            lines.append(f"平台观察时刻：{fact['observed_at']}；查询范围：{fact.get('shop_domain','当前授权店铺')}。[{key}]")
        for tx in fact.get('excluded_transactions',[]):
            tx_key='SHOPIFY-TX:'+tx['transaction_id'].rsplit('/',1)[-1]
            source=(f'[{tx_key}]' if tx_key in fact.get('citation_index',{})
                    else '（详见完整工具依据中的excluded_transactions）')
            lines.append(f"未计入交易{tx['transaction_id'].rsplit('/',1)[-1]}：类型{tx['kind']}、状态{tx['status']}；不属于成功收款或成功退款。{source}")
    elif style == 'brief' and fact.get('excluded_transactions'):
        lines.append(f"另有{len(fact['excluded_transactions'])}笔非成功收款/退款交易未计入，明细保留在依据中。[{key}]")
    lines.append('依据为开发店测试交易；授权、待处理交易不计成功收退款，不证明真实到账、买家身份绑定或退货资格。')
    return '\n'.join(lines)
