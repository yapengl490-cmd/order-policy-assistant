"""程序维护的查询条件及原文依据；不使用模型声称的 source_span。

只支持明确订单号、带年份的月份、ISO/中文年月日时间及有唯一目标的修改。
未写时区的输入使用公开的 Asia/Shanghai 业务时区；日期边界必须写明时刻。
previous 必须来自按身份/角色/会话隔离的服务器存储，不能接受模型或请求体状态。
本模块不访问文件、时钟、网络或数据库；调用方传入带时区 now 和唯一 turn_id。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo

BUSINESS_TIMEZONE = "Asia/Shanghai"
TIME_FIELDS = ("paid_from", "paid_before", "refund_before")
KINDS = {"unknown", "order", "summary", "list", "policy"}
FIELDS = {"order_id", *TIME_FIELDS, "policy_as_of"}
_ISO = re.compile(r"(?<![\dA-Za-z])\d{4}-\d{2}-\d{2}(?:\s*零点|[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)?(?!\d)")
_CHINESE = re.compile(r"(?<!\d)\d{4}年\d{1,2}月\d{1,2}[日号](?:\s*(?:零点|\d{1,2}:\d{2}(?::\d{2})?|\d{1,2}[点时](?:\d{1,2}分?)?))?")
_PARTIAL_CHINESE = re.compile(r"\d{1,2}月\d{1,2}[日号](?:\s*(?:零点|\d{1,2}:\d{2}(?::\d{2})?|\d{1,2}[点时](?:\d{1,2}分?)?))?")
_MONTH = re.compile(r"(?<!\d)(\d{4})(?:年(\d{1,2})月|-(\d{2}))(?![\d-])")
_EXPLICIT_ORDER = re.compile(r"(?:订单(?:编号|号|ID)?|order_id)[\s\"':：=]*(?P<id>[A-Za-z0-9][A-Za-z0-9_.-]{0,127})", re.I)
_DEMO_ORDER = re.compile(r"(?<![A-Za-z0-9_])(?:O\d[A-Za-z0-9_-]*|O_[A-Za-z0-9_]+)(?![A-Za-z0-9_])")
_LABELS = (
    ("policy_as_of", re.compile(r"policy_as_of|as_of|政策(?:日期|截至|截止|生效)|条款日期", re.I)),
    ("paid_from", re.compile(r"paid_from|支付(?:开始|起始|起点)|开始(?:时间|日期)", re.I)),
    ("paid_before", re.compile(r"paid_before|支付(?:结束|截止|终点)|结束(?:时间|日期)", re.I)),
    ("refund_before", re.compile(r"refund_before|退款(?:截止|截至)|(?:截止|截至)(?:时间|时刻)?", re.I)),
)


class ConditionValidationError(ValueError):
    """由调用方作为可解释的参数拒绝处理；不应继续执行工具。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now必须为带时区datetime")
    return value.astimezone(ZoneInfo(BUSINESS_TIMEZONE))


def _datetime_text(text: str) -> tuple[str, str]:
    """返回规范化时间和可核查解析规则；没有时刻不能暗补一天起/止。"""
    midnight = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*零点", text)
    if midnight:
        # 明确写出零点才能补00:00；裸日期仍须澄清，原文位置保持不变。
        value = datetime.combine(date.fromisoformat(midnight[1]), datetime.min.time(),
                                 tzinfo=ZoneInfo(BUSINESS_TIMEZONE))
        return value.isoformat(), "iso_date_explicit_midnight_business_timezone"
    if "年" not in text:
        if "T" not in text and " " not in text:
            raise ValueError("date_without_time")
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo(BUSINESS_TIMEZONE))
            return value.isoformat(), "iso_time_business_timezone"
        return value.isoformat(), "iso_time_explicit_timezone"
    match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})[日号]\s*(.+)?", text)
    if not match or not match[4]:
        raise ValueError("date_without_time")
    clock = match[4]
    if clock == "零点":
        hour, minute, second = 0, 0, 0
    elif ":" in clock:
        parts = [int(piece) for piece in clock.split(":")]
        hour, minute = parts[:2]
        second = parts[2] if len(parts) == 3 else 0
    else:
        time_match = re.fullmatch(r"(\d{1,2})[点时](?:(\d{1,2})分?)?", clock)
        if not time_match:
            raise ValueError("unsupported_time")
        hour, minute, second = int(time_match[1]), int(time_match[2] or 0), 0
    value = datetime(int(match[1]), int(match[2]), int(match[3]), hour, minute, second,
                     tzinfo=ZoneInfo(BUSINESS_TIMEZONE))
    return value.isoformat(), "chinese_time_business_timezone"


def _label_before(message: str, start: int) -> str | None:
    prefix = re.split(r"[，,；;。\n]", message[max(0, start - 55):start])[-1]
    labels = [(match.end(), len(match[0]), field) for field, pattern in _LABELS
              for match in pattern.finditer(prefix)]
    # “支付截止”与“截止”末尾相同，选择更具体的完整标签。
    return max(labels)[2] if labels else None


def _clarification(state: dict) -> str:
    if not state["needs_clarification"]:
        return ""
    names = {"order_id": "订单编号", "paid_from": "支付开始时刻", "paid_before": "支付结束时刻",
             "refund_before": "退款截止时刻", "policy_as_of": "政策适用日期"}
    missing = "、".join(names.get(field, field) for field in state["missing"])
    head = f"还需要明确{missing}。" if missing else "本次条件存在歧义，未执行金额查询。"
    return (head + "可写“订单号O001，退款截止2026-11-03T00:00:00+08:00”；"
            "汇总可写“支付月份2026年10月，退款截止2026年11月3日零点”。"
            "未标时区按北京时间，开始含边界、结束/退款截止不含边界；修改时请指出具体字段。")


def _business_condition_text(message: str) -> str:
    """等长遮蔽完整的展示偏好子句，避免把“以后/改为”误认成业务时间或修改。

    这里只区分业务解析范围，不提取、授权或保存偏好。原文仍交模型及独立记忆校验。
    不做关键词删除；包含日期、订单条件或不认识的表达时整个子句保留，继续严格校验。
    """
    prefix = r"(?:(?:请|以后|今后|往后|之后|后续|默认|长期|一直|每次|这次|本次|都)\s*){0,4}"
    change = r"(?:改成|改为|调整为|设置为|修改为)?"
    style = (
        r"(?:把|将)?(?:(?:回答|答复|回复|解释|说明)(?:风格|方式|详略)?)?"
        + change
        + r"(?:简短|简洁|简明|详细|正常|标准)(?:一点|一些|些)?"
          r"(?:(?:且|并且)?(?:准确|清楚))?(?:的|地)?"
          r"(?:回答|答复|回复|解释|说明|展示|输出)?(?:问题)?"
    )
    amount = (
        r"(?:把|将)?(?:金额(?:格式|单位)?|价格)?"
        r"(?:用|以|按|改成|改为|显示为|展示为)(?:元|分)(?:为单位)?"
        r"(?:显示|展示|输出|回答|表示)?"
    )
    precision = r"(?:金额)?(?:保留两位小数|精确到分)(?:显示|展示|输出|回答)?"
    preference = re.compile(prefix + r"(?:" + style + "|" + amount + "|" + precision + r")")
    result = list(message)
    for clause in re.finditer(r"[^，,；;。！？!?\n]+", message):
        if preference.fullmatch(clause[0].strip()):
            result[clause.start():clause.end()] = " " * (clause.end() - clause.start())
    return "".join(result)


def _month_relation(message: str, match: re.Match) -> str:
    """月份须关联支付订单；明确退款发生月不能换成支付批次口径。"""
    before = re.split(r"[，,；;。！？!?\n]", message[:match.start()])[-1]
    after = re.split(r"[，,；;。！？!?\n]", message[match.end():])[0]
    qualifier = r"(?:本店|全店|本人|我的|我|该店|已|成功|已成功|所有|全部|的|份|内|期间|\s)*"
    if (re.search(r"退款(?:发生|成功|月份|月|区间|期间|时间|日期|截止|截至)*(?:为|是|改成|改为)?\s*$", before)
            or re.match(qualifier + r"(?:发生的)?退款", after)):
        return "refund"
    if (re.search(r"(?:支付|付款)(?:月份|月|区间|期间|时间|日期)?(?:改成|改为|为|是)?\s*$", before)
            or re.match(qualifier + r"(?:支付|付款)", after)):
        return "payment"
    return "unspecified"


def _condition_reference_clause(message: str, clause, start: int, end: int) -> bool:
    """有限识别“解释边界/保留条件”的提及，不豁免真实修改或不认识的句式。"""
    text = (message[clause.start():start] + "<field>" + message[end:clause.end()]).strip()
    prefix = r"(?:(?:请|并|同时|仍|还|继续|再|本次|这次|要|应)\s*)*"
    retain = (prefix + r"(?:保留|沿用|保持)(?:已确认的|原有的|原来的|当前的)?<field>(?:不变)?"
              r"(?:[、及和与](?:数据依据|必要限制|引用|证据|来源|口径))*")
    explain = prefix + r"(?:(?:说明|解释|讲清)(?:一下)?)?"
    question = (r"<field>(?:是否|是不是|含不含)(?:包含|包括|计入|含)?"
                r"(?:该时刻|这个时刻|边界|上界|截止时刻)?(?:的)?(?:规则|含义|口径)?")
    reverse_question = r"(?:是否|是不是)(?:包含|包括|计入|含)<field>"
    return bool(re.fullmatch(retain, text) or re.fullmatch(explain + "(?:" + question + "|" + reverse_question + ")", text))


def _reconcile_order_conditions(message: str, previous: dict | None, now: datetime, turn_id: str) -> dict:
    """从真实用户文字按有限规则更新条件；未知/冲突保留问题，不能借旧值执行。

    confirmed每项保存value及source{turn_id,quote,span,rule,revision}。
    defaults单列单笔程序当前时间，不能被模型自报值覆盖；changed逐项给出前后值。
    """
    now = _aware(now)
    if not isinstance(message, str) or not message.strip() or len(message) > 4000:
        raise ValueError("message须为1—4000字符")
    if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 128:
        raise ValueError("turn_id须为1—128字符")
    original_message = message
    message = _business_condition_text(message)
    old = deepcopy(previous) if previous else {}
    if old and (old.get("schema_version") != 1 or old.get("kind") not in KINDS):
        raise ValueError("不支持的服务器条件状态")
    expired = False
    if old:
        try:
            expiry = datetime.fromisoformat(old["expires_at"])
            expired = expiry.tzinfo is None or now >= expiry
        except (KeyError, TypeError, ValueError):
            expired = True
    reset = bool(re.search(r"重置|清空(?:当前|本次|所有)?(?:查询|条件|会话)", message))
    if expired or reset:
        old = {}
    old_kind = old.get("kind", "unknown")
    order_matches = [(m.start("id"), m.end("id"), m["id"]) for m in _EXPLICIT_ORDER.finditer(message)]
    order_matches += [(m.start(), m.end(), m[0]) for m in _DEMO_ORDER.finditer(message)]
    order_matches = list(dict.fromkeys(order_matches))
    # 裸数字后的“年/月/日”不能被“订单2026年...”误当编号。
    order_matches = [m for m in order_matches if not (m[2].isdigit() and message[m[1]:m[1]+1] in "年月日")]
    tokens = sorted([(m.start(), m.end(), m[0]) for pattern in (_ISO, _CHINESE)
                     for m in pattern.finditer(message)])
    months = [m for m in _MONTH.finditer(message)
              if not any(start <= m.start() < end for start, end, _ in tokens)]
    payment_month = any(_month_relation(message, match) == "payment" for match in months)
    refund_month = any(_month_relation(message, match) == "refund" for match in months)
    payment_scope = bool(payment_month or re.search(r"支付(?:月份|区间|开始|起始)|paid_from|paid_before", message, re.I))
    aggregate_request = bool(re.search(r"汇总|统计|合计|总共", message))
    # “一笔订单的退款合计”仍是单笔核查；跨订单范围必须另有明确依据。
    summary_request = payment_scope or (aggregate_request and not order_matches)
    policy_request = bool(re.search(r"政策|条款|规则|售后|退款资格|退货资格|七天无理由|运费|到账", message))
    list_request = bool(re.search(r"列出|有哪些|订单列表|最近订单|选单", message))
    order_request = bool(order_matches or re.search(r"单笔|这笔|该订单|这个订单|查(?:询|看|核)?订单", message))
    # 订单号可只是政策资格问题的指代，不因此授权额外查询金额。
    amount_request = bool(re.search(r"金额|净额|净支出|净销售额|实付|支付额|退款额|退了多少|付了多少|花了多少", message))
    eligibility_request = bool(re.search(r"资格|七天无理由|能否.*退|是否.*退|能.*申请|可以.*退", message))
    if not eligibility_request:
        amount_request = amount_request or bool(re.search(r"核查|核对|查(?:询|看)?(?:订单|支付|退款)", message))
        amount_request = amount_request or any(re.search(r"(?:查询|查看|查)\s*(?:订单(?:编号|号)?)?\s*$", message[:start])
                                               for start, _, _ in order_matches)
    policy_only = policy_request and not (amount_request or summary_request or list_request)
    continuation = bool(re.search(r"改成|改为|改到|更改|修改|补充|截止|截至|支付结束|重新查|再查|沿用|保持|不变|refund_before|policy_as_of|as_of", message, re.I))
    if policy_only:
        kind = "policy"
    elif summary_request:
        kind = "summary"
    elif order_request:
        kind = "order"
    elif list_request:
        kind = "list"
    elif policy_request and not re.search(r"改成|改为|改到|更改|修改|补充", message):
        kind = "policy"
    elif continuation and old_kind != "unknown":
        kind = old_kind
    elif policy_request:
        kind = "policy"
    else:
        kind = old_kind
    active = bool(summary_request or order_request or list_request or policy_request or continuation or refund_month)
    switched = kind != old_kind and old_kind != "unknown"
    explicit_merge = bool(re.search(r"改成|改为|改到|改查|更改|修改|补充|重新查|再查|沿用|保持|不变", message))
    new_query = bool(old and not explicit_merge and (
        (summary_request and (aggregate_request or payment_month)) or
        (order_request and order_matches and re.search(r"查询|核查|检查|查(?:看)?", message))))
    keep = not reset and not expired and not switched and not new_query
    confirmed = deepcopy(old.get("confirmed", {})) if keep else {}
    confirmed = {field: value for field, value in confirmed.items() if field in FIELDS}
    revision = (previous or {}).get("revision", 0) + 1
    state = {"schema_version": 1, "revision": revision, "turn_id": turn_id, "kind": kind,
             "business_timezone": BUSINESS_TIMEZONE, "updated_at": now.isoformat(),
             "expires_at": (now + timedelta(minutes=30)).isoformat(),
             "original_question": old.get("original_question", original_message) if keep else original_message,
             "confirmed": confirmed, "defaults": {}, "missing": [], "issues": [], "changed": [],
             "blocked_fields": list(old.get("blocked_fields", [])) if keep else [],
             "reset": reset, "expired": expired, "switched": switched, "new_query": new_query, "active": active}
    issues = state["issues"]
    if order_matches and (payment_scope or (aggregate_request and re.search(r"(?:所有|全部|各笔|多笔|全店)(?:的)?订单", message))):
        issues.append("single_order_and_batch_scope_conflict")
    blocked = set(state["blocked_fields"])
    touched: dict[str, list[dict]] = {}

    def add(field, value, start, end, rule):
        touched.setdefault(field, []).append({"value": value, "source": {
            "turn_id": turn_id, "quote": original_message[start:end], "span": [start, end],
            "rule": rule, "revision": revision}})

    if len({m[2] for m in order_matches}) > 1:
        issues.append("multiple_order_ids")
        confirmed.pop("order_id", None)
    elif order_matches and kind == "order":
        start, end, identifier = order_matches[0]
        add("order_id", identifier, start, end, "explicit_order_id")

    if tokens and keep and old_kind != "unknown":
        active = state["active"] = True
    # 只识别同一句明确关联“支付”的中文起止范围；第二端省年份必须有同范围首端依据。
    range_spans = []
    for start, end, raw in tokens:
        if kind != "summary" or "年" not in raw:
            continue
        connector = re.match(r"\s*(?:到|至|—|–|~)\s*", message[end:])
        if not connector:
            continue
        right_start = end + connector.end()
        right = _CHINESE.match(message, right_start) or _PARTIAL_CHINESE.match(message, right_start)
        if right is None:
            continue
        before = re.split(r"[，,；;。\n]", message[:start])[-1]
        after = re.split(r"[，,；;。\n]", message[right.end():])[0]
        if "支付" not in before + after or re.search(r"退款(?:截止|截至|区间|范围)", before):
            continue
        right_raw = right[0]
        inherited_year = "年" not in right_raw
        if inherited_year:
            right_raw = raw.split("年", 1)[0] + "年" + right_raw
        range_spans.append((start, right.end()))
        try:
            if re.match(r"(?:半|\d+(?:分|秒)|[+-]\d)", message[right.end():]):
                raise ValueError("unsupported_range_clock_suffix")
            first_value, _ = _datetime_text(raw)
            second_value, _ = _datetime_text(right_raw)
            if datetime.fromisoformat(first_value) >= datetime.fromisoformat(second_value):
                raise ValueError("invalid_payment_range")
            rule = "explicit_payment_range_business_timezone" + (":end_year_from_same_range_start" if inherited_year else "")
            # 全范围引文包含首端年份，不能伪称省略年份本身是独立完整证据。
            add("paid_from", first_value, start, right.end(), rule)
            add("paid_before", second_value, start, right.end(), rule)
        except ValueError:
            issues.append("invalid_or_ambiguous_payment_range")
            for field in ("paid_from", "paid_before"):
                confirmed.pop(field, None)
                blocked.add(field)
    if months:
        if refund_month:
            issues.append("refund_month_is_not_payment_range")
        elif kind != "summary" or len(months) != 1:
            issues.append("ambiguous_payment_month")
        else:
            match = months[0]
            try:
                year, month = int(match[1]), int(match[2] or match[3])
                start = datetime(year, month, 1, tzinfo=ZoneInfo(BUSINESS_TIMEZONE))
                end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=start.tzinfo)
                for field, value in (("paid_from", start), ("paid_before", end)):
                    add(field, value.isoformat(), match.start(), match.end(), "explicit_month_range_business_timezone")
            except ValueError:
                issues.append("invalid_payment_month")

    pending_times = [field for field in old.get("missing", []) if field in TIME_FIELDS] if keep else []
    last_times = old.get("last_updated_fields", []) if keep else []
    last_times = [field for field in last_times if field in TIME_FIELDS]

    def target_for(start):
        label = _label_before(message, start)
        if label:
            return label, "explicit_field"
        if kind == "order":
            return "refund_before", "single_order_cutoff"
        if kind == "policy":
            return "policy_as_of", "policy_date"
        if len(tokens) == 1 and len(pending_times) == 1:
            return pending_times[0], "only_missing_field"
        if len(tokens) == 1 and re.search(r"改成|改为|改到", message) and len(last_times) == 1:
            return last_times[0], "previous_single_updated_field"
        return None, "ambiguous"

    for start, end, raw in tokens:
        if any(left <= start < right for left, right in range_spans):
            continue
        field, target_rule = target_for(start)
        if field is None:
            issues.append("ambiguous_date_target")
            continue
        if re.match(r"[+-]\d", message[end:]):
            issues.append("unsupported_iso_offset")
            blocked.add(field)
            confirmed.pop(field, None)
            continue
        if re.match(r"(?:半|\d+(?:分|秒))", message[end:]):
            issues.append("unsupported_clock_suffix")
            blocked.add(field)
            confirmed.pop(field, None)
            continue
        if kind == "summary" and field == "order_id":
            issues.append("unsupported_field")
            continue
        try:
            if field == "policy_as_of" and not re.search(r"[T :点时]", raw):
                parts = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})[日号]", raw)
                value = date(*map(int, parts.groups())).isoformat() if parts else date.fromisoformat(raw).isoformat()
                rule = "explicit_policy_date"
            else:
                value, rule = _datetime_text(raw)
                if field == "policy_as_of":
                    value = datetime.fromisoformat(value).astimezone(ZoneInfo(BUSINESS_TIMEZONE)).date().isoformat()
            add(field, value, start, end, rule + ":" + target_rule)
        except ValueError as exc:
            issues.append("date_without_time" if str(exc) == "date_without_time" else "invalid_datetime")
            confirmed.pop(field, None)
            blocked.add(field)

    # “截止现在”是用户明确请求程序捕获时钟；批量缺省绝不自动生成退款截止。
    for match in re.finditer(r"(?:退款)?(?:截止|截至)(?:到|为)?\s*(?:现在|当前时刻)", message):
        add("refund_before", now.isoformat(), match.start(), match.end(), "explicit_current_time_program_clock")
    for field, items in touched.items():
        if len({item["value"] for item in items}) != 1:
            issues.append("conflicting_" + field)
            confirmed.pop(field, None)
            blocked.add(field)
            continue
        item = items[0]
        before = confirmed.get(field, {}).get("value")
        confirmed[field] = item
        blocked.discard(field)
        state["changed"].append({"field": field, "before": before, "after": item["value"], "source": deepcopy(item["source"])})

    # 不理解的修改不能静默保留旧日期，也不把“今天/月底”等交给模型猜。
    if re.search(r"今天|昨天|明天|前天|后天|今[年月]|去[年月]|明年|本月|上个?月|下个?月|月底|月末|最近\d+天|\d+天前|\d+天后", message):
        issues.append("unsupported_relative_date")
    # 任何未消费的日期片段都不能被完整日期遮蔽；范围内省年份由上面显式处理。
    remainder = list(message)
    for start, end in ([(start, end) for start, end, _ in tokens] + range_spans + [(m.start(), m.end()) for m in months]):
        remainder[start:end] = " " * (end - start)
    if re.search(r"\d{1,2}月(?:\d{1,2}[日号])?", "".join(remainder)):
        issues.append("date_year_required")
    # 逐个字段提及核对其自己的原文值；成功解析订单号/另一个日期，不能替代本字段。
    # 同字段重复提及也逐处检查，避免后一个合法日期掩盖前一个不明确修改。
    unresolved_fields = set()
    unsupported_update = False
    reference_spans = []
    if not reset:
        for clause in re.finditer(r"[^，,；;。！？!?\n]+", message):
            labels = sorted((m.start(), m.end(), field) for field, pattern in _LABELS
                            for m in pattern.finditer(message, clause.start(), clause.end()))
            specific = []
            for start, end, field in sorted(labels, key=lambda item: (item[0], -item[1])):
                if not specific or start >= specific[-1][1]:
                    specific.append((start, end, field))
            for index, (start, end, field) in enumerate(specific):
                right = specific[index + 1][0] if index + 1 < len(specific) else clause.end()
                candidates = touched.get(field, [])
                grounded = any(item["source"]["span"][0] < right
                               and item["source"]["span"][1] > start for item in candidates)
                if not grounded and field in confirmed and _condition_reference_clause(message, clause, start, end):
                    reference_spans.append((clause.start(), clause.end()))
                    continue
                if not grounded or re.search(r"或(?:者)?|还是|任选|二选一", message[end:right]):
                    unresolved_fields.add(field)
                    issues.append("condition_value_required")
            # 没有明确字段名的修改也须在该修改之后找到值；不能借前一子句的订单号通过。
            changes = list(re.finditer(r"改成|改为|改到|更改|修改|补充", message[clause.start():clause.end()]))
            for index, change in enumerate(changes):
                left = clause.start() + change.end()
                right = clause.start() + changes[index + 1].start() if index + 1 < len(changes) else clause.end()
                if not any(item["source"]["span"][0] < right and item["source"]["span"][1] > left
                           for items in touched.values() for item in items):
                    unsupported_update = True
        if unsupported_update:
            issues.append("unsupported_condition_update")
    for field in unresolved_fields:
        confirmed.pop(field, None)
        blocked.add(field)
    if re.search(r"不要|别查|不查|取消查询|停止查询|不是.+而是", message):
        issues.append("negative_or_ambiguous_instruction")
    # 只接受北京时间/中国标准时间/Asia/Shanghai，或ISO时间内明确偏移。
    # 不用有限城市黑名单将“日本时间”等未知时区静默解释成北京时间。
    allowed_zone = r"(?:时区\s*[:：=]?\s*)?(?:Asia/Shanghai|北京时间|中国标准时间|中国时间)(?:时区)?"
    zone_text = re.sub(allowed_zone, "", message)
    unknown_zone = bool(re.search(r"UTC|GMT|[A-Za-z_]+/[A-Za-z_]+|(?<![A-Za-z0-9_])(?:CST|JST|EST|EDT|PST|PDT|CET|CEST|BST|HKT|SGT|IST|KST|AEST|AEDT)(?:(?![A-Za-z0-9_])|(?=\d{4}(?:年|-)))|[A-Za-z]{2,10}(?:时间|时区)|时区|标准时|夏令时|当地时|本地时", zone_text))
    ordinary_time_labels = ("截止", "截至", "支付", "退款", "开始", "结束", "当前", "指定", "查询", "更新", "同一", "这个", "什么", "有效")
    if tokens or months:
        if re.search(r"\b(?:time(?:\s*zone)?|timezone)\b", zone_text, re.I):
            unknown_zone = True
        for start, _, _ in tokens:
            prefix = re.sub(allowed_zone, "", message[:start])
            qualifier = re.search(r"([A-Za-z][A-Za-z_]*)\s*$", prefix)
            if qualifier and qualifier[1] not in FIELDS | {"as_of"}:
                unknown_zone = True
        for match in re.finditer(r"([\u4e00-\u9fff]{1,16})时间", zone_text):
            if not match[1].endswith(ordinary_time_labels):
                unknown_zone = True
        for match in re.finditer(r"[+-]\d{2}:\d{2}", message):
            if not any(start <= match.start() < end and "年" not in raw for start, end, raw in tokens):
                unknown_zone = True
    if unknown_zone:
        issues.append("unsupported_timezone_label_use_iso_offset")
    boundary_text = list(message)
    for start, end in reference_spans:
        boundary_text[start:end] = " " * (end - start)
    boundary_text = "".join(boundary_text)
    if re.search(r"上午|下午|晚上|中午|凌晨|全天|当天结束|含当天|包含当天", boundary_text):
        issues.append("unsupported_or_ambiguous_time_boundary")
    if re.search(r"之后|以后|含截止|含上界|包括截止|包含截止|不含开始|不包含开始|<=|>=", boundary_text):
        issues.append("unsupported_or_ambiguous_time_boundary")

    # 无法解析的日期更新不会在下一轮“再查”时悄悄回到旧值/默认当前时间。
    unsafe_update = any(issue in issues for issue in (
        "unsupported_relative_date", "date_year_required", "unsupported_condition_update",
        "condition_value_required", "unsupported_or_ambiguous_time_boundary", "unsupported_timezone_label_use_iso_offset"))
    if unsafe_update:
        targets = unresolved_fields or ({"refund_before"} if kind == "order" else
            {"policy_as_of"} if kind == "policy" else
            {pending_times[0]} if len(pending_times) == 1 else
            set(TIME_FIELDS) if unsupported_update and kind == "summary" else set())
        for target in targets:
            confirmed.pop(target, None)
            blocked.add(target)
    if keep and not touched and not issues and old.get("issues"):
        issues.extend(old["issues"])

    state["last_updated_fields"] = list(touched)
    if kind == "summary":
        confirmed.pop("order_id", None)
        state["missing"] = [field for field in TIME_FIELDS if field not in confirmed]
        if not state["missing"]:
            times = [datetime.fromisoformat(confirmed[field]["value"]) for field in TIME_FIELDS]
            if not times[0] < times[1] <= times[2]:
                issues.append("invalid_time_range")
    elif kind == "order":
        for field in ("paid_from", "paid_before"):
            if field in touched:
                issues.append("single_order_does_not_apply_payment_range")
            confirmed.pop(field, None)
        if "order_id" not in confirmed:
            state["missing"].append("order_id")
        if "refund_before" in blocked:
            state["missing"].append("refund_before")
        elif "refund_before" not in confirmed:
            state["defaults"]["refund_before"] = {"value": now.isoformat(), "source": {
                "turn_id": turn_id, "quote": None, "span": None, "rule": "program_clock_single_order_default", "revision": revision}}
    if "policy_as_of" in blocked and (kind == "policy" or policy_request):
        state["missing"].append("policy_as_of")
    state["blocked_fields"] = sorted(blocked)
    state["issues"] = list(dict.fromkeys(issues))
    state["ready"] = active and kind != "unknown" and not state["missing"] and not state["issues"]
    state["needs_clarification"] = active and bool(state["missing"] or state["issues"] or kind == "unknown")
    state["allowed_tools"] = []
    if state["ready"]:
        state["allowed_tools"] = ({"order": ["check_order"], "summary": ["summary_orders"],
                                   "policy": ["search_policies"], "list": ["list_orders"]})[kind]
        if policy_request and kind != "policy":
            state["allowed_tools"].append("search_policies")
    elif active and kind == "order" and state["missing"] == ["order_id"] and not state["issues"]:
        state["allowed_tools"] = ["list_orders"]
        if policy_request:
            state["allowed_tools"].append("search_policies")
    state["clarification"] = _clarification(state)
    return state


def reconcile_conditions(message: str, previous: dict | None, now: datetime, turn_id: str) -> dict:
    """按明确业务路由，售后自述与金额条件不互相污染。"""
    from support_conditions import uses_support, reconcile_support
    if uses_support(message, previous):
        return reconcile_support(message, previous, now, turn_id)
    was_support = bool(previous and previous.get("kind") == "support")
    result = _reconcile_order_conditions(message, None if was_support else previous, now, turn_id)
    if was_support:
        result["switched"] = True
        result["revision"] = previous.get("revision", 0) + 1
    return result


def validate_tool_arguments(name: str, arguments: dict, state: dict) -> dict:
    """验证工具参数与程序确认的条件一致，返回实际应执行的规范化参数。

    单笔省略截止时注入本轮固定默认/已确认截止；模型不得替换它。
    政策省略日期时注入本轮北京时间日期，避免跨午夜二次捕获时钟。
    """
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ConditionValidationError("missing_condition_state", "缺少服务器确认的查询条件")
    if name not in state.get("allowed_tools", []):
        raise ConditionValidationError("conditions_not_ready", state.get("clarification") or "该工具不属于本次明确查询")
    if not isinstance(arguments, dict):
        raise ConditionValidationError("invalid_arguments", "工具参数必须为对象")
    args = deepcopy(arguments)
    expected_keys = {"list_orders": {"limit"}, "check_order": {"order_id", "refund_before"},
                     "summary_orders": set(TIME_FIELDS), "search_policies": {"query", "as_of"}, "review_after_sales": {"order_id"}}[name]
    if set(args) - expected_keys:
        raise ConditionValidationError("unexpected_arguments", "工具包含未获准参数")
    confirmed = state["confirmed"]

    def check_time(field, expected):
        value = args.get(field)
        if value is not None:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None or parsed != datetime.fromisoformat(expected):
                    raise ValueError("mismatch")
            except (TypeError, AttributeError, ValueError):
                raise ConditionValidationError("condition_mismatch", f"{field}与用户已确认条件/程序默认不一致") from None
        args[field] = expected

    if name == "review_after_sales":
        if args.get("order_id") != confirmed.get("order_id", {}).get("value"):
            raise ConditionValidationError("condition_mismatch", "售后订单与已确认订单不一致")
        return args
    if name == "check_order":
        expected = confirmed["order_id"]["value"]
        if args.get("order_id") != expected:
            raise ConditionValidationError("condition_mismatch", "订单编号与用户明确选择不一致")
        cutoff = confirmed.get("refund_before") or state["defaults"]["refund_before"]
        check_time("refund_before", cutoff["value"])
    elif name == "summary_orders":
        for field in TIME_FIELDS:
            if field not in args or args[field] is None:
                raise ConditionValidationError("missing_arguments", "汇总必须提供三个已确认时间字段")
            check_time(field, confirmed[field]["value"])
    elif name == "search_policies":
        expected = confirmed.get("policy_as_of", {}).get("value", state["updated_at"][:10])
        if args.get("as_of") is not None and args["as_of"] != expected:
            raise ConditionValidationError("condition_mismatch", "政策日期没有用户明确依据")
        if not isinstance(args.get("query"), str) or not 1 <= len(args["query"]) <= 200:
            raise ConditionValidationError("invalid_arguments", "政策检索词须为1—200字符")
        args["as_of"] = expected
    else:
        if type(args.get("limit", 20)) is not int or not 1 <= args.get("limit", 20) <= 20:
            raise ConditionValidationError("invalid_arguments", "选单条数须为1—20整数")
    return args


def public_condition_state(state: dict) -> dict:
    """仅供页面解释条件；身份/凭证不属于本模块，也不进入此结果。"""
    keys = ("revision", "kind", "business_timezone", "confirmed", "defaults", "missing", "issues",
            "ready", "needs_clarification", "clarification", "changed", "reset", "expired", "switched", "new_query")
    return {**{key: deepcopy(state[key]) for key in keys},
            **({"claims": deepcopy(state.get("claims", {})), "topics": state.get("topics", []), "support_missing": state.get("support_missing", [])} if state.get("kind") == "support" else {})}
