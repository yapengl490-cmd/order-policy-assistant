"""校验模型提出的偏好候选；本模块不访问数据库，也不决定账号或权限。

模型负责把自然语言归纳成有限字段；程序核原文、作用范围和允许值。
自动写入只覆盖能核实的明确长期表达，复杂表达保留为待确认候选。
"""
from __future__ import annotations

import re

from preference_store import PreferenceValidationError, validate_values

_LONG_TERM = re.compile(r"以后|今后|往后|从今(?:天|往后)|接下来都|今后默认|后续都|长期|一直都|每次都|永远|记住(?:我的|这个|这条|我)?(?:偏好|习惯)|(?:默认|长期)(?:设置|偏好)")
_TEMPORARY = re.compile(r"这次|本次|这一回|这回|仅这回|仅本轮|只在本轮|这一次|临时|暂时|仅供本次")
_NO_SAVE = re.compile(r"(?:不要|别|不必|不用|无需|不需要|不能|禁止|不允许|莫)(?:再|帮我|给我|为我|自动|长期|永久|把.{0,20})?\s*(?:记住|记忆|保存|存储|记录|记下)|(?:不要|别).{0,15}(?:存入|写入).{0,8}(?:记忆|偏好)")
_THIRD_PARTY = re.compile(r"(?:他|她|别人|其他人|对方|客户|客服|顾客|用户|朋友|同事|老板|模型|助手|系统)(?:曾|刚|以前|之前|总是|经常|的)?(?:说|表示|要求|希望|喜欢|偏好|习惯)|(?:替|帮|为)(?:别人|他人|客户|另一.{0,8})(?:记住|保存|设置)")
_EXTERNAL = re.compile(r"(?:政策|手册|规则|原文|文档|网页|资料)(?:中|里|要求|写着|写道|说|规定|内容|如下|是[：:]|[：:])|转述|翻译|(?:解释|分析).{0,8}(?:句子|这句话)")
_HYPOTHETICAL = re.compile(r"假如|假设|假定|举例|例如|比如|示例|测试句|测试用例|测试文本")
_CONDITIONAL = re.compile(r"如果|除非|仅当|只有.{0,20}才|必要时|的时候|情况下|当.{1,20}时")
_NEGATED = re.compile(r"不要|不想|不喜欢|不用|不是|不再|不能|无需|别|不超过|不许|非")
_ROLE_SCOPE = re.compile(r"买家|卖家|商家|店主|店铺角色|(?:用户|账号|账户)(?:编号|ID|id)?\s*[：:]?\s*[A-Za-z0-9]|buyer_id|shop_id|account_id|user_id")
_QUESTION_OR_DEFERRED = re.compile(r"[？?]|能否|能不能|可不可以|可以吗|行吗|是否|怎么|如何|以后再(?:说|讨论|聊|问|考虑|决定)|今后再(?:说|讨论|决定)")
_SENTENCE_BREAK = re.compile(r"[。！？!?；;\n]")
_QUOTES = re.compile(r'```[\s\S]*?(?:```|$)|`[^`\n]*`|“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|"[^"\n]*"|\x27[^\x27\n]*\x27|(?m:^\s*>[^\n]*)')


def _out(status, reason, values=None, quote=""):
    return {"status": status, "values": dict(values or {}), "source_quote": quote, "reason": reason}


def _sentence_at(message: str, start: int, end: int) -> str:
    before = [m.end() for m in _SENTENCE_BREAK.finditer(message, 0, start)]
    after = _SENTENCE_BREAK.search(message, end)
    return message[before[-1] if before else 0:after.end() if after else len(message)].strip()


def _recognized_values(text: str) -> dict[str, set[str]]:
    """有限的意义核对，‘用分段展示’不能证明金额单位为分。"""
    found = {"amount_format": set(), "answer_style": set()}
    for unit, canonical in (("元", "yuan"), ("分", "fen")):
        patterns = (
            rf"(?:用|按|以)(?:人民币)?{unit}(?=$|[\s，,。；;！!？?]|为单位|作为单位|显示|展示|表示|输出|计价|回答|列出)",
            rf"金额单位(?:设为|改为|为|是)[：:\s]*(?:人民币)?{unit}(?=$|[\s，,。；;！!？?])",
            rf"(?:金额|价格)(?:改成|设成|改为|设为)(?:人民币)?{unit}(?=$|[\s，,。；;！!？?]|显示|展示|表示|输出)",
        )
        if any(re.search(pattern, text) for pattern in patterns):
            found["amount_format"].add(canonical)
    if re.search(r"精确到分|保留(?:两|二|2)位小数|小数点后(?:两|二|2)位", text):
        found["amount_format"].add("yuan_fixed2")
        # “用元、保留两位”是一致的精度补充，不是互斥单位。
        found["amount_format"].discard("yuan")
    for canonical, pattern in (
        ("brief", r"简短|简洁|精简|简明|简要|简练|言简意赅|短一点|短一些|太长|太啰嗦"),
        ("detailed", r"详细|详尽|展开(?:讲|说|说明)|细致(?:说明|讲解)|太简略|太简短"),
        ("standard", r"标准(?:详略|长度|回答)|(?:恢复|改回|使用|采用)(?:默认|标准)(?:长度|详略|回答)|按正常长度"),
    ):
        if re.search(pattern, text):
            found["answer_style"].add(canonical)
    return found


def validate_memory_proposal(raw, message) -> dict:
    """返回none/rejected/proposed/explicit；调用方仍须做版本、身份和持久化校验。"""
    if raw is None:
        return _out("none", "not_proposed")
    if type(raw) is not dict or set(raw) != {"values", "source_quote", "scope"}:
        return _out("rejected", "invalid_proposal_structure")
    if raw["scope"] not in ("explicit_long_term", "suggestion") or type(raw["scope"]) is not str:
        return _out("rejected", "invalid_proposal_scope")
    try:
        values = validate_values(raw["values"])
    except (PreferenceValidationError, ValueError, TypeError):
        return _out("rejected", "unsupported_preference")
    if not values:
        return _out("none", "empty_values")
    quote = raw["source_quote"]
    if (type(message) is not str or not 1 <= len(message) <= 4000
            or type(quote) is not str or not 1 <= len(quote) <= 300 or not quote.strip()
            or any(ord(char) < 32 and char not in "\n\t" for char in quote)):
        return _out("rejected", "invalid_source_quote")
    if quote not in message:
        return _out("rejected", "source_not_in_current_user_message")
    # 明确拒绝保存不能因模型只摘取后半句而被绕过；不让候选截掉否定前提。
    if _NO_SAVE.search(message):
        return _out("rejected", "user_declined_memory")
    spans = [(m.start(), m.end()) for m in _QUOTES.finditer(message)]
    positions = [(m.start(), m.end()) for m in re.finditer(re.escape(quote), message)]
    contexts = []
    for start, end in positions:
        if any(start < stop and end > begin for begin, stop in spans):
            return _out("rejected", "quoted_or_code_source")
        context = _sentence_at(message, start, end)
        contexts.append(context)
        if _THIRD_PARTY.search(context) or _EXTERNAL.search(context) or _HYPOTHETICAL.search(context):
            return _out("rejected", "non_user_or_hypothetical_source")
        if _TEMPORARY.search(context):
            return _out("rejected", "temporary_request_not_long_term")
    context = "\n".join(contexts)
    recognized = _recognized_values(context)
    # 单重明确否定的对象不能反向提为保存值；更复杂的否定仍交给确认。
    forbidden = {"amount_format": set(), "answer_style": set()}
    for unit, canonical in (("元", "yuan"), ("分", "fen")):
        if re.search(rf"(?:不要|别|不用|不使用)(?:再)?(?:用|按|以)?{unit}(?=$|[\s，,。；;！!？?]|为单位|显示|展示|表示|输出)", context):
            forbidden["amount_format"].add(canonical)
    for word, canonical in (("详细", "detailed"), ("简短", "brief"), ("简洁", "brief")):
        if re.search(rf"(?:不要|别|不喜欢|不想)(?:再|总是|一直|过于|太)?{word}", context):
            forbidden["answer_style"].add(canonical)
    if any(value in forbidden[field] for field, value in values.items()):
        return _out("rejected", "value_is_explicitly_negated")
    ambiguous = False
    for field, value in values.items():
        known = recognized[field]
        if known and value not in known and not _NEGATED.search(context):
            return _out("rejected", "value_contradicts_source")
        if len(known) != 1 or value not in known:
            ambiguous = True
    # 模型的scope不是写入授权。范围、否定、条件或未知改写一律转给人确认。
    if _ROLE_SCOPE.search(context):
        return _out("proposed", "role_scope_requires_confirmation", values, quote)
    if _CONDITIONAL.search(context) or _NEGATED.search(context) or _QUESTION_OR_DEFERRED.search(context):
        return _out("proposed", "conditional_or_negative_intent", values, quote)
    if ambiguous:
        return _out("proposed", "meaning_requires_confirmation", values, quote)
    if raw["scope"] == "suggestion" or not _LONG_TERM.search(quote):
        return _out("proposed", "long_term_intent_not_explicit", values, quote)
    return _out("explicit", "grounded_explicit_long_term", values, quote)


def fallback_memory_proposal(message) -> dict | None:
    """模型漏提时，将少量可核对的展示反馈补成待确认建议；绝不据此自动保存。

    不猜任意自然语言，也不累计行为次数。保留完整短消息，避免摘录丢掉否定、
    临时范围或第三方来源；复杂消息交给模型提议或设置页，不增加模型调用。
    """
    if type(message) is not str or not 1 <= len(message) <= 300:
        return None
    quote = message.strip()
    if not quote:
        return None
    # 验证器允许部分含混模型候选等待确认；无模型的补位采用更小的识别范围。
    if any(pattern.search(quote) for pattern in (
            _TEMPORARY, _NO_SAVE, _THIRD_PARTY, _EXTERNAL, _HYPOTHETICAL,
            _CONDITIONAL, _NEGATED, _ROLE_SCOPE, _QUESTION_OR_DEFERRED)):
        return None
    if re.search(r"不|没|无|他(?:的|们|人)?|她(?:的|们)?|别人|其他|同事|客户|朋友|老板", quote):
        return None
    # 无标点的“用元也用分”同样冲突；不能只认出后一个单位便替用户选择。
    units = set(re.findall(
        r"(?:用|按|以)(?:人民币)?([元分])(?=$|[\s，,。；;！!？?]|也|或|和|并|为单位|显示|展示|表示|输出)", quote))
    if len(units) > 1:
        return None
    recognized = _recognized_values(quote)
    if any(len(values) > 1 for values in recognized.values()):
        return None
    values = {}
    # “订单号太长”不是回答偏好。省略对象仅支持明确的短反馈，不泛化其他对象。
    response_target = re.search(r"回答|答复|回复|解释|讲解|说明|说得|讲得", quote)
    bare_feedback = re.fullmatch(
        r"(?:请)?(?:太长(?:了)?|太啰嗦(?:了)?|简短(?:一点|一些|些)?|精简(?:一点|一些|些)?|"
        r"简洁(?:一点|一些|些)?|详细(?:一点|一些|些)?|太简略(?:了)?)[。！!\s]*", quote)
    style_feedback = (re.search(r"请|希望|想要|需要|太长|太啰嗦|太简略|一点|一些|些|以后|今后|往后|后续|默认|恢复|改回|每次", quote)
                      or re.fullmatch(r"(?:回答|回复|答复)(?:简短|简洁|精简|详细|详尽)[。！!\s]*", quote))
    other_length = re.search(r"订单号|订单编号|链接|网址|路径|文件名|等待|耗时|延迟|周期|图片", quote)
    if recognized["answer_style"] and ((response_target and style_feedback) or bare_feedback) and not other_length:
        values["answer_style"] = next(iter(recognized["answer_style"]))
    if recognized["amount_format"] and re.search(r"金额|价格", quote):
        values["amount_format"] = next(iter(recognized["amount_format"]))
    if not values:
        return None
    raw = {"values": values, "source_quote": quote, "scope": "suggestion"}
    checked = validate_memory_proposal(raw, message)
    return raw if checked["status"] == "proposed" else None


def source_text(task_id: str, quote: str, confirmed: bool = False) -> str:
    """沿用现有source.text；完整保存短原话，不截断以免改变否定含义。"""
    if type(task_id) is not str or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", task_id):
        raise ValueError("invalid source task id")
    if (type(quote) is not str or not 1 <= len(quote) <= 300 or not quote.strip()
            or any(ord(char) < 32 and char not in "\n\t" for char in quote)):
        raise ValueError("invalid source quote")
    if type(confirmed) is not bool:
        raise ValueError("invalid confirmation flag")
    text = f"task={task_id}; {'user_confirmed' if confirmed else 'explicit_user_request'}; 原话：{quote}"
    if len(text) > 500:
        raise ValueError("source too long")
    return text
