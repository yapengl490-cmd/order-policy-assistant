"""售后问题的有限条件识别：自述有原句，不替代数据库事实，也不承接别单自述。"""
from copy import deepcopy
from datetime import datetime, timedelta
import re

INTENT = re.compile(r'退货|退回|退换|换货|运费|能不能退|能否退|可以退|能退吗')
FOLLOWUP = re.compile(r'没穿|未穿|穿过|吊牌|尺码|尺寸|太小|太大|不喜欢|不想要|质量|破损|发错|商家责任|再查|继续|补充|改为|改成')
OTHER_QUERY = re.compile(r'汇总|统计|退款截止|退款截至|净额|净支出|退款金额|支付金额|列出|订单列表')


# 本期只接入退货资格及运费条款；这些诉求没有对应资料/业务工具。
UNSUPPORTED = {
    'exchange': (re.compile(r'换货|退换货|换鞋|换码'), '换货'),
    'arrival': (re.compile(r'(?:什么时候|何时|多久|几天|多长时间).{0,10}(?:到账|入账|收到(?:退款|钱))'
                           r'|(?:到账|入账)(?:时间|时限|进度|状态)'
                           r'|(?:退款|钱).{0,8}(?:到账|入账)(?:了?吗|没有|了没|没)'
                           r'|(?:退款|钱)(?:什么时候|多久|几天).{0,6}(?:退回|收到)'), '真实资金到账时间或状态'),
    'compensation': (re.compile(r'赔偿|赔付|补偿'), '赔偿或赔付'),
    'repair': (re.compile(r'维修|保修'), '维修或保修'),
}


def _request_text(message):
    """只屏蔽明确引用/代码段，保持长度，避免把引用政策当当前业务诉求。"""
    clean = list(message)
    for pattern in (r'```[\s\S]*?(?:```|$)', r'“[^”]*”|「[^」]*」|"[^"\n]*"'):
        for match in re.finditer(pattern, message):
            clean[match.start():match.end()] = ' ' * len(match[0])
    return ''.join(clean)


def _requested_matches(pattern, text):
    """有限辨认明确撤回/否定请求；不把否定问句“不能换货吗”当撤回。"""
    for match in pattern.finditer(text):
        prefix = re.split(r'[，,；;。！？!?\n]', text[:match.start()])[-1]
        negated = re.search(r'(?:不问|不查|不用查|不需要|不考虑|不是要|不是问|不想|暂不|不要求|无需|不用|不要|不)'
                     r'(?:再|办理|了解|查询|询问|问|查|考虑|申请|处理|要求|判断|核查|核对|关于|的事|是否|能否|能不能|可以)*\s*$', prefix)
        if negated and not re.search(r'不是|并非|不一定|不能说', prefix[max(0,negated.start()-6):negated.start()]):
            continue
        yield match


def _unsupported_topics(text):
    return [topic for topic, (pattern, _) in UNSUPPORTED.items() if any(_requested_matches(pattern, text))]


def uses_support(message, previous):
    text = _request_text(message)
    if any(_requested_matches(INTENT, text)) or _unsupported_topics(text):
        return True
    if previous and previous.get('kind') == 'support' and not OTHER_QUERY.search(text):
        return True
    return False


def reconcile_support(message, previous, now, turn_id):
    from condition_state import _aware, _DEMO_ORDER, _EXPLICIT_ORDER, _ISO, _CHINESE, _datetime_text, _business_condition_text
    now = _aware(now)
    if not isinstance(message, str) or not 1 <= len(message.strip()) <= 4000:
        raise ValueError('问题须为1—4000字符')
    if not isinstance(turn_id, str) or not 1 <= len(turn_id) <= 128:
        raise ValueError('turn_id无效')
    text = _request_text(_business_condition_text(message))
    old = deepcopy(previous or {})
    # 未解决的时间问题是待澄清状态，不能靠补商品状态或等待过期悄悄改成当前时间。
    assessment_issue_codes = {'invalid_assessment_time', 'conflicting_assessment_time'}
    pending_assessment_issues = [x for x in old.get('issues', []) if x in assessment_issue_codes]
    prior_order_id = old.get('confirmed', {}).get('order_id', {}).get('value')
    expired = False
    if old:
        try:
            expired = now >= datetime.fromisoformat(old['expires_at'])
        except (KeyError, ValueError, TypeError):
            expired = True
    reset = bool(re.search(r'重置|清空(?:当前|本次|所有)?(?:查询|条件|会话)', text))
    switched = bool(old and old.get('kind') != 'support')
    revision = old.get('revision', 0) + 1
    if reset or switched:
        pending_assessment_issues = []
    if expired or reset or switched:
        old = {}
    matches = [(m.start('id'), m.end('id'), m['id']) for m in _EXPLICIT_ORDER.finditer(text)]
    matches += [(m.start(), m.end(), m[0]) for m in _DEMO_ORDER.finditer(text)]
    matches = list(dict.fromkeys(matches))
    matches = [m for m in matches if not (m[2].isdigit() and text[m[1]:m[1]+1] in '年月日')]
    ids = {m[2] for m in matches}
    previous_id = old.get('confirmed', {}).get('order_id', {}).get('value')
    new_query = bool(len(ids) == 1 and previous_id and next(iter(ids)) != previous_id)
    if len(ids) == 1 and prior_order_id and next(iter(ids)) != prior_order_id:
        pending_assessment_issues = []
    confirmed = deepcopy(old.get('confirmed', {})) if not new_query else {}
    claims = deepcopy(old.get('claims', {})) if not new_query else {}
    issues, changed = [], []
    def put(target, field, value, match, rule):
        before = target.get(field, {}).get('value')
        target[field] = {'value': value, 'source': {'turn_id': turn_id, 'quote': message[match[0]:match[1]],
                         'span': [match[0], match[1]], 'rule': rule, 'revision': revision}}
        if before != value:
            changed.append({'field': field, 'before': before, 'after': value})
    if len(ids) > 1:
        claims.clear()  # 多单歧义时旧自述失去唯一归属，选回其他订单不能继承。
        confirmed.pop('order_id', None)
        issues.append('one_order_at_a_time')
    elif ids:
        match = matches[0]
        put(confirmed, 'order_id', match[2], match, 'explicit_order_id')
    # 仅完整、无否定歧义的本轮自述。前后矛盾不猜，清掉旧值要求澄清。
    specs = {
        'is_unworn': [(r'没有穿过|还没穿|没穿过|未穿过|没穿|未穿|从未穿', True), (r'已经穿过|已穿过|穿过了|穿过', False)],
        'tags_intact': [(r'吊牌(?:还在|完好|完整|没拆|未拆|没有拆|保留着)', True), (r'吊牌(?:已经拆了|已经拆|已拆|拆了|不在|没了|丢了|不完好|破损)', False)],
        'reason': [(r'尺码不合适|尺寸不合适|尺码不合|太小|太大', 'size'), (r'不喜欢|不想要|个人喜好', 'preference'),
                   (r'质量问题|破损|发错(?:货)?|商家责任', 'merchant_responsibility')],
    }
    # 保持原文长度，使source.span能回到实际用户原句。
    safe = list(text)
    for pattern in (r'```[\s\S]*?(?:```|$)', r'“[^”]*”|「[^」]*」|"[^"\n]*"', r'(?:如果|假设|比如|例如|他说|她说|手册说)[^。；;\n]*'):
        for m in re.finditer(pattern, text):
            safe[m.start():m.end()] = ' ' * len(m[0])
    text_claims = ''.join(safe)
    for field, patterns in specs.items():
        found = []
        for pattern, value in patterns:
            for m in re.finditer(pattern, text_claims):
                # “没穿过”内的“穿过”不重复识别为已穿；“不是太小”等不是原因确认。
                if any(a <= m.start() and m.end() <= b for a,b,_ in found):
                    continue
                prefix = text_claims[max(0, m.start()-3):m.start()]
                suffix = text_claims[m.end():m.end()+3]
                if re.search(r'不是|并非|不确定|是否|没有|并无|无', prefix) or re.match(r'(?:吗|么|\?|？)', suffix):
                    continue
                found.append((m.start(), m.end(), value))
        values = {m[2] for m in found}
        if len(values) > 1:
            claims.pop(field, None)
            issues.append('conflicting_' + field)
        elif found:
            put(claims, field, found[0][2], found[0], 'explicit_user_claim_unverified')
        elif re.search(r'不确定|不知道|记不清', text_claims) and re.search({'is_unworn':'穿','tags_intact':'吊牌','reason':'原因'}[field], text_claims):
            claims.pop(field, None)
    if len(ids) > 1:
        claims.clear()  # 本轮多单自述也没有唯一归属，不能在后续选单时串用。
    # 查询时刻只接受显式“判断时间/按…时刻”，不能把支付/收货自述当后台事实。
    assessment_values, assessment_issues = [], []
    for pattern in (_ISO, _CHINESE):
        for m in pattern.finditer(text):
            prefix = text[max(0, m.start()-15):m.start()]
            if not re.search(r'判断(?:时间|时刻)|评估(?:时间|时刻)|按\s*$', prefix):
                continue
            try:
                value, rule = _datetime_text(m[0])
                if datetime.fromisoformat(value) > now:
                    raise ValueError('future')
                assessment_values.append((value, (m.start(),m.end()), rule))
            except ValueError:
                confirmed.pop('assessment_at', None)
                assessment_issues.append('invalid_assessment_time')
    if len({value for value, _, _ in assessment_values}) > 1:
        confirmed.pop('assessment_at', None)
        assessment_issues.append('conflicting_assessment_time')
    elif assessment_values and not assessment_issues:
        value, span, rule = assessment_values[0]
        put(confirmed, 'assessment_at', value, span, rule)
        pending_assessment_issues = []
    elif not assessment_values and re.search(r'判断(?:时间|时刻)|评估(?:时间|时刻)', text):
        assessment_issues.append('invalid_assessment_time')
    time_issues = list(dict.fromkeys(assessment_issues or pending_assessment_issues))
    if time_issues:
        confirmed.pop('assessment_at', None)
        issues.extend(time_issues)
    if re.search(r'(?:换成|采用|按照|按|用).{0,10}(?:最新|咨询时|新版本).{0,8}(?:政策|条款)|政策版本\s*=', text):
        issues.append('policy_version_is_selected_by_order_payment')
    unsupported = _unsupported_topics(text)
    explicit_supported_request = bool(re.search(
        r'(?:能否|能不能|可以|能|可否).{0,6}退(?:货)?|退货.{0,6}(?:资格|条件)'
        r'|运费.{0,8}(?:谁|承担|负责|出)|(?:只|仅).{0,8}(?:退货|运费)', text))
    # “继续/补吊牌”并未撤回上一轮未支持诉求，不能悄悄改答退货。
    if not unsupported and old.get('unsupported_topics') and not explicit_supported_request:
        unsupported = deepcopy(old['unsupported_topics'])
    if unsupported:
        issues.append('unsupported_support_topic')
    topics = []
    if any(_requested_matches(re.compile(r'退货|退回|能不能退|能否退|可以退|能退吗|退换|换货'), text)):
        topics.append('eligibility')
    if any(_requested_matches(re.compile(r'运费'), text)):
        topics.append('shipping')
    topics = topics or old.get('topics') or ['eligibility','shipping']
    missing = [] if 'order_id' in confirmed else ['order_id']
    defaults = {} if 'assessment_at' in confirmed or time_issues else {'assessment_at': {'value': now.isoformat(), 'source': {
        'turn_id':turn_id,'quote':None,'span':None,'rule':'program_task_time','revision':revision}}}
    need = bool(missing or issues or reset)
    clarification = (('当前尚未接入' + '、'.join(UNSUPPORTED[key][1] for key in unsupported)
                      + '的资料与处理能力，不能用退货条件代替回答。现在可核对订单是否满足演示退货条件、退货运费由谁承担；如需继续，请明确要核对这两项中的哪项。') if unsupported else
                     ('判断时间不合法：请明确给出一个不晚于当前时刻的有效时间，例如“判断时间2026-11-01T00:00:00+08:00”；补商品状态不会更改判断时间。' if 'invalid_assessment_time' in time_issues else
                      '判断时间有冲突：请只保留一个不晚于当前时刻的有效判断时间；补商品状态不会自动选定时间。') if time_issues else
                     '请给出要核对的一个订单编号。' if missing else
                     '商品状态自述有冲突或判断时间/政策版本要求不明确，请更正后再核对；政策仍按订单支付时版本选择。' if issues else
                     '本次售后查询已清空；请重新给出订单编号。' if reset else '')
    return dict(schema_version=1,revision=revision,turn_id=turn_id,kind='support',business_timezone='Asia/Shanghai',
                updated_at=now.isoformat(),expires_at=(now+timedelta(minutes=30)).isoformat(),
                original_question=message,confirmed=confirmed,defaults=defaults,claims=claims,topics=topics,unsupported_topics=unsupported,
                missing=missing,issues=issues,changed=changed,blocked_fields=[],reset=reset,expired=expired,
                switched=switched,new_query=new_query,active=True,ready=not need,needs_clarification=need,
                allowed_tools=[] if need else ['review_after_sales'],clarification=clarification)
