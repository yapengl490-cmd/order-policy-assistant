"""Bounded Chinese catalog chat conditions with original-text evidence.

This parser handles a small documented shopping vocabulary, not general natural
language. No network, authentication, clock reads, model calls or persistence.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re

from catalog_recommendations import CURRENCY_MINOR_UNITS

FIELDS = {"category", "currency", "budget_max_minor", "material", "cotton_percentage", "size", "require_returnable"}
REQUIRED = ("category", "currency", "budget_max_minor")
TOOL_NAME = "recommend_products"
_CURRENCY = re.compile(r"美元|美金|人民币|日元|欧元|英镑|科威特第纳尔|(?<![A-Za-z])(?:USD|CNY|RMB|JPY|EUR|GBP|KWD)(?![A-Za-z])|元", re.I)
_CURRENCY_VALUES = {"美元": "USD", "美金": "USD", "人民币": "CNY", "元": "CNY", "RMB": "CNY", "日元": "JPY", "欧元": "EUR", "英镑": "GBP", "科威特第纳尔": "KWD"}
_CATEGORY = re.compile(r"T\s*恤|T[- ]?shirt|t_shirt", re.I)
_MATERIAL = re.compile(r"100\s*[%％]\s*(?:纯)?棉|纯棉|全棉|棉质|cotton|棉|聚酯纤维|涤纶|polyester", re.I)
_SIZE = re.compile(r"(?<![A-Za-z])(?:XXXL|XXL|XXS|XL|XS|S|M|L)(?![A-Za-z])(?:\s*[码号])?", re.I)
_NUM = re.compile(r"(?<![\d.])-?\d+(?:\.\d+)?(?![\d.])")
_CHANGE = re.compile(r"改成|改为|改到|更改|修改|换成|调整为")
_NEW = re.compile(r"重新推荐|重新选|换个需求|新推荐|重新开始")
_RESET = re.compile(r"重置|清空(?:当前|本次|所有)?(?:查询|条件|会话)")
_ORDER = re.compile(r"订单|(?<![A-Za-z0-9_])O(?:\d[A-Za-z0-9_-]*|_[A-Za-z0-9_]+)(?![A-Za-z0-9_])|支付月份|退款截止|净支出|净销售额|汇总|统计", re.I)
_SCOPE = re.compile(r"shop_id|shop_ids|buyer_id|account_id|店铺编号|跨店|另一家店|所有店铺|市场改|渠道改", re.I)
_RETURN = re.compile(r"要能退|要求能退|必须能退|可退货|支持退货|能退的|能退吗|能不能退|是否能退|退货政策|无理由退货|可以退|退换货|可退(?:的|吗)?")
_RETURN_CANCEL = re.compile(r"不要求(?:能|可)?退(?:货)?|取消退货要求|不用考虑退货|不限制(?:是否)?能退|不要求退货")
_ADDITIVE_PREFIX = re.compile(r"\s*(?:(?:另外|此外|同时|而且|并且|还|也|再)\s*)+(?:(?:我|希望|想|需要|要求|要|必须|一定)\s*)*")
# 只接住完整的购买前政策追问；不把任意新要求或显式订单吞进旧推荐条件。
_POLICY_FOLLOWUP = re.compile(r"\s*(?:那|请问|请|想问)?\s*(?:(?:这款|这些|刚才推荐的|推荐的)(?:商品|T恤)?(?:的)?)?\s*(?:退货运费(?:由)?谁承担|退货政策是什么|退货条件是什么|退货规则是什么)[\s？?。！!，,；;]*", re.I)
_UNSUPPORTED = {
    "material": re.compile(r"真丝|羊毛|亚麻|混纺|含棉|粘纤|纤维素"),
    "category": re.compile(r"衬衫|裤子|裙子|运动鞋|鞋子|卫衣|外套"),
    "size": re.compile(r"\d+\s*[码号]|均码|尺码\s*\d+"),
    "color": re.compile(r"红色|蓝色|黑色|白色|绿色|黄色|紫色|颜色"),
    "brand": re.compile(r"品牌|耐克|阿迪|优衣库|Nike|Adidas|Uniqlo", re.I),
    "cut": re.compile(r"长袖|短袖|宽松|修身|圆领|V领|有口袋|带口袋"),
    "properties": re.compile(r"防晒|透气|抗菌|防水|不起球|不掉色|敏感肌|夏天|冬天|机洗|手洗"),
    "availability_time": re.compile(r"今天(?:的)?(?:库存|可售)|今天还(?:有货|能买)|现在的库存|当前库存|实时库存"),
    "total_price": re.compile(r"包邮|免运费|含运费|含税|总价|优惠|折扣|满减"),
}


def _request_text(message):
    from condition_state import _business_condition_text
    # 展示偏好不参与预算币种/规格解析；等长遮蔽保留原文证据的位置。
    clean = list(_business_condition_text(message))
    for pattern in (r"```[\s\S]*?(?:```|$)", r'“[^”]*”|「[^」]*」|"[^"\n]*"',
                    r"(?:如果|假设|假如|比如|例如|他说|她说|手册说)[^，,。；;\n]*"):
        for match in re.finditer(pattern, message):
            clean[match.start():match.end()] = " " * len(match[0])
    return "".join(clean)


def is_catalog_policy_followup(message, previous=None, *, routing=False):
    """原推荐会话的政策追问；路由可保留附加要求，解析仍须逐项核对。"""
    if not (isinstance(message,str) and previous and previous.get('kind')=='catalog'):
        return False
    text=_request_text(message)
    if _POLICY_FOLLOWUP.fullmatch(text):
        return True
    match=_POLICY_FOLLOWUP.match(text) if routing else None
    # 后面另有业务条件时交回原推荐解析器澄清，不误切泛政策而漏掉条件。
    return bool(match and re.search(r'[，,；;。！？!?]\s*$',match[0]))


def uses_catalog(message, previous=None):
    """Explicit order queries leave catalog mode, even after shopping history."""
    if not isinstance(message, str):
        return False
    text = _request_text(message)
    if _ORDER.search(text) or re.search(r"不要推荐|不推荐|不买了|取消推荐", text):
        return False
    if re.search(r"推荐|选购|买(?:一件|个|件)?(?:商品|衣服|T恤)|商品导购", text, re.I):
        return True
    if _CATEGORY.search(text) and (re.search(r"预算|棉|码|美元|人民币|USD|CNY", text, re.I)
                                   or _RETURN.search(text) or _RETURN_CANCEL.search(text)):
        return True
    if previous and previous.get("kind") == "catalog":
        return bool(is_catalog_policy_followup(message,previous,routing=True) or _CATEGORY.search(text) or _MATERIAL.search(text) or _SIZE.search(text)
                    or _CURRENCY.search(text) or _RETURN.search(text) or _RETURN_CANCEL.search(text)
                    or _CHANGE.search(text) or _RESET.search(text) or _NEW.search(text)
                    or _SCOPE.search(text)
                    or re.search(r"预算|价格|以内|以下|不超过|取消.*要求|不限制|需要|要求|我要|想要|^要|^继续$", text)
                    or any(pattern.search(text) for pattern in _UNSUPPORTED.values()))
    return False


def _aware(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must have a timezone")
    try:
        return now.astimezone(timezone.utc)
    except OverflowError:
        raise ValueError("now exceeds supported UTC range") from None


def reconcile_catalog(message, previous, now, turn_id):
    """Keep only same-session server state; every new value keeps its real quote."""
    now = _aware(now)
    if not isinstance(message, str) or not 1 <= len(message.strip()) <= 4000:
        raise ValueError("message must contain 1..4000 characters")
    if not isinstance(turn_id, str) or not 1 <= len(turn_id) <= 128:
        raise ValueError("invalid turn_id")
    text = _request_text(message)
    prior = deepcopy(previous or {})
    same = prior.get("kind") == "catalog" and prior.get("schema_version") == 1
    expired = False
    if same:
        try:
            expired = now >= _aware(datetime.fromisoformat(prior["expires_at"]))
        except (ValueError, TypeError, KeyError):
            expired = True
    reset, new_query = bool(_RESET.search(text)), bool(_NEW.search(text))
    switched = bool(prior and not same)
    keep = same and not (expired or reset or new_query)
    policy_followup = bool(keep and _POLICY_FOLLOWUP.fullmatch(text))
    old = prior if keep else {}
    revision = prior.get("revision", 0) + 1
    confirmed = deepcopy(old.get("confirmed", {}))
    confirmed = {key: value for key, value in confirmed.items() if key in FIELDS}
    issues = deepcopy(old.get("field_issues", {}))
    pending_budget = deepcopy(old.get("pending_budget"))
    changed, touched = [], []

    def source(match, rule):
        return {"turn_id": turn_id, "quote": message[match.start():match.end()],
                "span": [match.start(), match.end()], "rule": rule, "revision": revision}

    def clear_issue(field):
        issues.pop(field, None)

    def reject(field, reason):
        confirmed.pop(field, None)
        issues[field] = [reason]
        if field not in touched:
            touched.append(field)

    def assign(field, value, evidence):
        before = confirmed.get(field, {}).get("value")
        confirmed[field] = {"value": value, "source": evidence}
        clear_issue(field)
        touched.append(field)
        if before != value:
            changed.append({"field": field, "before": before, "after": value})

    def choose(field, matches):
        if not matches:
            return None
        # 明确“改成/改为”只采用其后的新值；普通并列冲突必须澄清。
        correction = list(_CHANGE.finditer(text))
        if correction:
            after = [entry for entry in matches if entry[0].start() >= correction[-1].end()]
            if after:
                matches = after
        if len({value for _, value in matches}) > 1:
            reject(field, "conflicting_" + field)
            return None
        match, value = matches[-1]
        prefix = re.split(r"[，,；;。\n]", text[:match.start()])[-1]
        if field in {"budget_max_minor", "currency"}:
            prefix = prefix.replace("不要超过", "不超过")
        if re.search(r"不是|并非|不要|不想|不确定|不知道|是否|能否", prefix):
            reject(field, "negated_or_uncertain_" + field)
            return None
        return match, value

    # 店铺范围属于服务端授权；聊天编号不能改变目录可见性。
    if _SCOPE.search(text):
        reject("scope", "scope_change_not_allowed")

    category = choose("category", [(m, "t_shirt") for m in _CATEGORY.finditer(text)])
    if category:
        assign("category", category[1], source(category[0], "explicit_t_shirt"))
    currency = choose("currency", [(m, _CURRENCY_VALUES.get(m[0], _CURRENCY_VALUES.get(m[0].upper(), m[0].upper())))
                                   for m in _CURRENCY.finditer(text)])
    previous_currency = confirmed.get("currency", {}).get("value")
    if currency:
        assign("currency", currency[1], source(currency[0], "explicit_currency"))
    if re.search(r"[$¥￥]", text) and not currency:
        reject("currency", "ambiguous_currency_symbol")

    materials = []
    for match in _MATERIAL.finditer(text):
        materials.append((match, "polyester" if re.fullmatch(r"聚酯纤维|涤纶|polyester", match[0], re.I) else "cotton"))
    material = choose("material", materials)
    if material:
        assign("material", material[1], source(material[0], "explicit_material"))
        confirmed.pop("cotton_percentage", None)
        clear_issue("cotton_percentage")
        if re.search(r"纯棉|全棉|100\s*[%％]", material[0][0]):
            assign("cotton_percentage", 100, source(material[0], "explicit_pure_cotton"))
    size = choose("size", [(m, re.sub(r"\s*[码号]$", "", m[0]).upper()) for m in _SIZE.finditer(text)])
    if size:
        assign("size", size[1], source(size[0], "explicit_letter_size"))

    # 问已有候选的政策不等于新增“必须能退”的筛选条件。
    returned = [] if policy_followup else list(_RETURN.finditer(text))
    cancelled_return = list(_RETURN_CANCEL.finditer(text))
    if cancelled_return:
        prefix = re.split(r"[，,；;。\n]", text[:cancelled_return[-1].start()])[-1]
        if re.search(r"不是|并非|不能说", prefix):
            reject("require_returnable", "uncertain_return_requirement_cancel")
        else:
            assign("require_returnable", False, source(cancelled_return[-1], "explicit_return_requirement_cancel"))
    elif returned:
        assign("require_returnable", True, source(returned[-1], "explicit_return_requirement"))

    budget_matches = []
    for match in _NUM.finditer(text):
        prefix = re.split(r"[，,；;。\n]", text[:match.start()])[-1]
        suffix = text[match.end():]
        if re.match(r"\s*[%％码号]", suffix):
            continue
        label = re.search(r"预算|价格|上限|最多|不要超过|不超过|不高于|低于|不到|少于|小于", prefix)
        currency_adjacent = _CURRENCY.match(suffix.lstrip()) or re.search("(?:" + _CURRENCY.pattern + r")\s*$", prefix, re.I)
        if label or currency_adjacent:
            exclusive = bool(re.search(r"(?:低于|不到|少于|小于)\s*$", prefix))
            budget_matches.append((match, (match[0], exclusive)))
    budget = choose("budget_max_minor", budget_matches)
    if budget:
        pending_budget = {"number": budget[1][0], "exclusive": budget[1][1], "source": source(budget[0], "explicit_budget_number")}
        clear_issue("budget_max_minor")
    elif budget_matches:
        pending_budget = None
    elif currency and previous_currency and currency[1] != previous_currency:
        pending_budget = None
        reject("budget_max_minor", "budget_requires_reconfirmation_on_currency_change")

    if re.search(r"\d+(?:\.\d+)?\s*(?:-|到|至|~|—)\s*\d+|至少|不低于|大于|左右|大概|上下|不限预算", text):
        pending_budget = None
        reject("budget_max_minor", "unsupported_budget_range_or_bound")
    if pending_budget and "currency" in confirmed and "budget_max_minor" not in issues:
        try:
            amount = Decimal(pending_budget["number"])
            exponent = CURRENCY_MINOR_UNITS[confirmed["currency"]["value"]]
            minor = amount * (10 ** exponent)
            if amount < 0 or minor != minor.to_integral_value():
                raise ValueError("invalid budget precision")
            value = int(minor) - int(pending_budget["exclusive"])
            if value < 0:
                raise ValueError("negative upper bound")
            evidence = deepcopy(pending_budget["source"])
            evidence["rule"] = "budget_minor_units_with_explicit_currency"
            evidence["currency_source_turn"] = confirmed["currency"]["source"]["turn_id"]
            assign("budget_max_minor", value, evidence)
        except (InvalidOperation, KeyError, ValueError):
            pending_budget = None
            reject("budget_max_minor", "invalid_budget_precision_or_value")

    cancelled_unsupported = []
    for field, pattern in _UNSUPPORTED.items():
        cancellation = re.search(r"(?:取消|不限制|不要求)(?:的)?(?:" + pattern.pattern + r")(?:要求)?", text, pattern.flags)
        if cancellation:
            clear_issue(field)
            cancelled_unsupported.append(cancellation)
            continue
        if pattern.search(text):
            reject(field, "unsupported_" + field)
    if re.search(r"(?<!\d)(?!100(?:\D|$))\d+(?:\.\d+)?\s*[%％]\s*棉", text):
        reject("cotton_percentage", "unsupported_cotton_composition")
    if re.search(r"取消(?:其他|额外)要求", text):
        issues = {key: value for key, value in issues.items() if key in FIELDS or key == "scope"}
    # 未覆盖的明确“要/需要/要求”子句不能靠忽略文本变成匹配成功。
    for clause in re.finditer(r"[^，,；;。\n]+", text):
        if re.search(r"需要|要求|想要|我要|(?:^|\s)要", clause[0]) and not any(
            pattern.search(clause[0]) for pattern in (_CATEGORY, _MATERIAL, _SIZE, _CURRENCY, _RETURN, _RETURN_CANCEL)
        ) and not re.search(r"预算|价格|推荐|取消|不限制", clause[0]) and not any(
            pattern.search(clause[0]) for pattern in _UNSUPPORTED.values()
        ):
            reject("other", "unsupported_requirement")

    # 当前只实现这些词汇；剩余实质描述保守澄清，不能静默丢掉新硬条件。
    if not issues:
        remainder = list(text)
        if policy_followup:
            remainder = [' '] * len(text)
        recognized_starts = []
        for pattern in (_CATEGORY, _MATERIAL, _SIZE, _CURRENCY, _RETURN, _RETURN_CANCEL, _RESET, _NEW):
            for match in pattern.finditer(text):
                remainder[match.start():match.end()] = " " * len(match[0])
                recognized_starts.append(match.start())
        for match, _ in budget_matches:
            remainder[match.start():match.end()] = " " * len(match[0])
            recognized_starts.append(match.start())
        for match in cancelled_unsupported:
            remainder[match.start():match.end()] = " " * len(match[0])
        # 只把已识别要求之前的句首连接结构当语言衔接，不全局删除“还/另外”。
        # 例如“还要求能退且带帽子”只移除句首“还”，未知的“带帽子”仍须澄清。
        for clause in re.finditer(r"[^，,；;。\n]+", text):
            starts = [start for start in recognized_starts if clause.start() <= start < clause.end()]
            if starts:
                first = min(starts)
                if _ADDITIVE_PREFIX.fullmatch(text[clause.start():first]):
                    remainder[clause.start():first] = " " * (first - clause.start())
        residual = "".join(remainder)
        fillers = r"其他条件不变|其他不变|保持其他条件|保持不变|沿用其他条件|取消其他要求|取消额外要求|不要超过|不超过|不高于|低于|不到|少于|小于|以内|以下|最多|上限|预算|价格|币种|货币|材质|面料|尺码|大小|改成|改为|改到|更改|修改|换成|调整为|补充|推荐|选购|给我|帮我|请|想要|需要|要求|我要|只要|一定|一件|一款|一个|衣服|商品|继续|那就|还是|就是|再|要|买|我|是|用|为|的|好|那|且|并且|和|不要|不是|从"
        residual = re.sub(fillers, "", residual)
        residual = re.sub(r"[\s，,。；;：:！？!?、（）()=\-]", "", residual)
        if residual:
            reject("other", "unsupported_requirement_text")

    missing = [field for field in REQUIRED if field not in confirmed]
    issue_list = list(dict.fromkeys(code for codes in issues.values() for code in codes))
    ready = not missing and not issue_list
    names = {"category": "商品品类（目前支持T恤）", "currency": "币种（例如美元USD或人民币CNY）", "budget_max_minor": "单件商品标价预算上限"}
    clarification = ""
    if missing:
        clarification = "还需要明确" + "、".join(names[field] for field in missing) + "。"
    if issue_list:
        clarification += "当前条件存在冲突、未确认或暂不支持的要求，请明确更正；未忽略这些条件执行推荐。"
    if clarification:
        clarification += "例如：推荐M码纯棉T恤，预算不超过30美元。"
    return {
        "schema_version": 1, "kind": "catalog", "revision": revision, "turn_id": turn_id,
        "business_timezone": "UTC", "updated_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "confirmed": confirmed, "defaults": {}, "pending_budget": pending_budget,
        "missing": missing, "issues": issue_list, "field_issues": issues,
        "ready": ready, "active": True, "needs_clarification": not ready,
        "allowed_tools": [TOOL_NAME] if ready else [], "clarification": clarification,
        "changed": changed, "last_updated_fields": list(dict.fromkeys(touched)),
        "reset": reset, "expired": expired, "switched": switched, "new_query": new_query,
        "source_message": message,
        "policy_followup": policy_followup,
    }


def catalog_requirements(state):
    """Only server-confirmed conditions cross into the catalog service."""
    if (not isinstance(state, dict) or state.get("schema_version") != 1 or state.get("kind") != "catalog"
            or not state.get("ready") or state.get("issues") or state.get("missing")):
        raise ValueError("catalog conditions are not ready")
    result = {field: deepcopy(item["value"]) for field, item in state.get("confirmed", {}).items() if field in FIELDS}
    if not all(field in result for field in REQUIRED):
        raise ValueError("missing confirmed catalog requirements")
    return result


def validate_catalog_arguments(arguments, state):
    """The model may invoke the tool, but cannot supply scope or rewrite needs."""
    if type(arguments) is not dict or arguments:
        raise ValueError("recommend_products accepts only an empty argument object")
    catalog_requirements(state)
    return {}
