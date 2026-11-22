"""店铺规则查阅的条件；与具体订单资格判断分开，不自行选择订单。"""
from copy import deepcopy
from datetime import datetime, timedelta
import re


def has_order_reference(text):
    return bool(re.search(r'(?<![A-Za-z0-9_])O(?:\d[A-Za-z0-9_]*|_[A-Za-z0-9_]+)(?![A-Za-z0-9_])|#\d+|(?:这|那|本|我的)笔?订单|我的这单|我这单|这单|那单|本单|订单号|订单编号', text, re.I))


def uses_general_policy(message, previous):
    from support_conditions import _request_text
    text = _request_text(message)
    from catalog_conditions import is_catalog_policy_followup
    # “那退货政策是什么”沿用刚才推荐的商品范围，不能切回默认店铺政策。
    if is_catalog_policy_followup(message, previous, routing=True):
        return False
    if has_order_reference(text) or re.search(r'核查订单|列出.*订单|查询订单|退款截止|查询政策|演示售后规则|净额|支付金额|汇总|推荐|选购|挑选|买什么', text):
        return False
    topic = re.search(r'退货|运费|政策|条款|换货|保修|维修|赔偿|配送|发货', text)
    general = re.search(r'本店|店铺|商店|一般|通用|规则|政策|条款|退货条件', text)
    if topic and general:
        return True
    return bool(previous and previous.get('kind') == 'general_policy')


def reconcile_general_policy(message, previous, now, turn_id):
    from condition_state import _aware, _ISO, _CHINESE, _datetime_text, _business_condition_text
    from support_conditions import _self_report_text, _requested_matches
    now = _aware(now)
    text = _self_report_text(_business_condition_text(message))
    old = deepcopy(previous or {})
    switched = bool(old and old.get('kind') != 'general_policy')
    expired = bool(old and old.get('expires_at') and now >= datetime.fromisoformat(old['expires_at']))
    reset = bool(re.search(r'重置|清空(?:查询|条件|会话)?', text))
    revision = old.get('revision', 0) + 1
    confirmed = deepcopy(old.get('confirmed', {})) if not (switched or expired or reset) else {}
    issues, changed = [], []
    def put(field, value, match, rule):
        if confirmed.get(field, {}).get('value') != value:
            changed.append(field)
        confirmed[field] = {'value': value, 'source': {'turn_id': turn_id, 'quote': message[match.start():match.end()] if match else None,
                            'span': list(match.span()) if match else None, 'rule': rule, 'revision': revision}}
    categories = []
    for pattern, category in ((r'普通鞋类|鞋子|鞋类|鞋', 'ordinary_shoes'), (r'T恤|T-shirt|tshirt', 't_shirt'),
                              (r'裤子', 'trousers'), (r'手机', 'phone'), (r'食品', 'food')):
        match = re.search(pattern, text, re.I)
        if match:
            categories.append((category, match))
    if len(categories) > 1:
        confirmed.pop('category', None); issues.append('conflicting_categories')
    elif categories:
        put('category', *categories[0], 'explicit_product_category')
    dates = []
    for pattern in (_ISO, _CHINESE):
        for match in pattern.finditer(text):
            if re.search(r'政策|规则|条款', text) or old.get('kind') == 'general_policy':
                try:
                    raw = match[0]
                    day = re.fullmatch(r'(\d{4})[-年](\d{1,2})[-月](\d{1,2})[日号]?', raw)
                    if day:
                        value = datetime(int(day[1]), int(day[2]), int(day[3]), tzinfo=now.tzinfo).replace(hour=0, minute=0, second=0).isoformat()
                        rule = 'explicit_policy_date_start'
                    else:
                        value, rule = _datetime_text(raw)
                    dates.append((value, match, rule))
                except ValueError:
                    issues.append('invalid_policy_time')
    # 日期不带时刻时按该日北京时间零点；这是查询口径，不是订单支付时间。
    if not dates:
        match = re.search(r'(?:政策日期|规则日期)\s*[:：]?\s*(\d{4}-\d{2}-\d{2})(?![T\d])', text)
        if match:
            try:
                value = datetime.fromisoformat(match[1]+'T00:00:00+08:00').isoformat()
                dates.append((value, match, 'explicit_policy_date_start'))
            except ValueError:
                issues.append('invalid_policy_time')
        elif re.search(r'政策日期|规则日期', text):
            issues.append('invalid_policy_time')
    if len({x[0] for x in dates}) > 1:
        issues.append('conflicting_policy_time')
    elif dates and not issues:
        put('policy_at', *dates[0])
    if any('policy_time' in x for x in issues):
        confirmed.pop('policy_at', None)
    prior_issues = old.get('issues', []) if not (switched or expired or reset) else []
    if not dates:
        issues.extend(x for x in prior_issues if 'policy_time' in x)
    topics = []
    for word, topic in (('退货', 'eligibility'), ('运费', 'shipping'), ('换货', 'exchange'), ('保修', 'warranty'),
                        ('维修','repair'), ('赔偿','compensation'), ('配送','delivery'), ('发货','delivery'), ('到账','arrival')):
        if any(_requested_matches(re.compile(word), text)):
            topics.append(topic)
    topics = list(dict.fromkeys(topics)) or (old.get('topics') if not(switched or expired or reset) else None) or ['eligibility','shipping']
    defaults = {} if 'policy_at' in confirmed or issues else {'policy_at': {'value': now.isoformat(), 'source':
        {'turn_id':turn_id,'quote':None,'span':None,'rule':'program_policy_query_time','revision':revision}}}
    need = bool(issues or reset)
    return dict(schema_version=1,kind='general_policy',revision=revision,turn_id=turn_id,business_timezone='Asia/Shanghai',
        original_question=message,updated_at=now.isoformat(),expires_at=(now+timedelta(minutes=30)).isoformat(),
        confirmed=confirmed,defaults=defaults,topics=topics,missing=[],issues=list(dict.fromkeys(issues)),changed=changed,
        blocked_fields=[],active=True,ready=not need,needs_clarification=need,reset=reset,expired=expired,switched=switched,new_query=False,
        allowed_tools=[] if need else ['lookup_general_policy'],clarification=(
            '已清空政策查询，请说明店铺政策主题和商品类别。' if reset else
            '请明确一种商品类别、一个有效政策查询日期；这不是本单退货资格判断。' if need else ''))
