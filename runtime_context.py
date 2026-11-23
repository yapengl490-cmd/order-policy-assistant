"""Shared conversation context: bounded history and trusted display preferences.

These functions only transform supplied values. Callers provide role-scoped history
and a snapshot already obtained by the trusted application entry; no database access,
model request, task mutation or preference persistence occurs here.
"""
from datetime import datetime, timedelta
import re

from preference_store import validate_values


def history_pairs(history: list) -> list:
    """仅保留完整user/assistant对，避免截断工具调用与结果配对。"""
    pairs = []
    for index in range(0, len(history) - 1, 2):
        first, second = history[index:index+2]
        if not isinstance(first, dict) or not isinstance(second, dict):
            continue
        if first.get("role") != "user" or second.get("role") != "assistant":
            continue
        if any(not isinstance(item.get("content"), str) for item in (first, second)):
            continue
        pairs.append([{"role": item["role"], "content": item["content"][:3000]} for item in (first, second)])
    return [message for pair in pairs[-3:] for message in pair]


def current_preference_overrides(message: str) -> dict:
    """只识别少量完整的本次展示指令；不是通用意图解析，也不写长期库。"""
    if not isinstance(message, str):
        return {}
    text = re.sub(r"```[\s\S]*?(?:```|$)", "", message)
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
    text = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|"[^"]*"|\x27[^\x27]*\x27|`[^`]*`', "〔引用内容〕", text)
    found = {}
    sentences=[]
    for whole in re.findall(r"[^。！!\n？?]+[？?]?", text):
        # 引用/假设的引导语可跨分号；不能把后半句当成用户本人指令。
        if re.search(r"原文|引用|例如|比如|假设|假如|如果|举例|(?:他|她|用户)说|政策要求|条款要求",whole):
            continue
        sentences.extend(re.split(r"[；;]",whole))
    for sentence in sentences:
        # 涉及引用、假设、政策或否定的句子保守不识别，交给模型按本轮原文理解。
        if re.search(r"政策|条款|原文|引用|例如|比如|假设|假如|如果|举例|(?:他|她|用户)说|不|别|勿|无需|[“”「」『』‘’\"'`]|[？?]", sentence):
            continue
        current_scope = False
        for clause in re.split(r"[，,、；;]|并且|同时|且", sentence):
            clause = clause.strip()
            marker = re.match(r"(?:请)?(?:这次|本次)(?:请)?", clause)
            if marker:
                clause = clause[marker.end():].strip()
            elif not current_scope:
                continue
            # 整个子句匹配，避免把“用分析法”等业务内容当成金额单位。
            amount = re.fullmatch(r"(?:金额)?(?:用|以|按)(分|元)(?:为单位)?(?:展示|显示|回答|表示|输出)?", clause)
            amount_style = re.fullmatch(r"(?:金额)?(?:用|以|按)(分|元)(?:为单位)?(详细|简短)(?:列出|回答|答复|说明|展示)", clause)
            fixed = re.fullmatch(r"(?:金额)?(?:保留两位小数|精确到分)(?:展示|显示|回答|输出)?", clause)
            style = re.fullmatch(r"(?:回答|答复)?(详细|简短)(?:一点|一些)?(?:回答|答复|说明|列出)?", clause)
            if amount_style:
                found["amount_format"] = "fen" if amount_style[1] == "分" else "yuan"
                found["answer_style"] = "detailed" if amount_style[2] == "详细" else "brief"
            elif amount:
                found["amount_format"] = "fen" if amount[1] == "分" else "yuan"
            elif fixed:
                found["amount_format"] = "yuan_fixed2"
            elif style:
                found["answer_style"] = "detailed" if style[1] == "详细" else "brief"
            else:
                current_scope = False
                continue
            current_scope = True
    return found


def preference_memory(snapshot, access_role: str, message: str) -> dict:
    """只接收可信入口读取的快照；身份、保存来源原话不进入模型或返回记录。"""
    memory = {"status": "not_provided", "stored_values": {}, "effective_values": {},
              "overrides": {}, "version": None, "updated_at": None}
    if snapshot is not None:
        memory["status"] = "invalid_snapshot"
        try:
            if not isinstance(snapshot, dict) or set(snapshot) != {"role", "values", "version", "updated_at", "source"}:
                raise ValueError("invalid snapshot fields")
            if snapshot["role"] not in ("buyer", "merchant"):
                raise ValueError("invalid snapshot role")
            values = validate_values(snapshot["values"])
            version, updated_at, source = snapshot["version"], snapshot["updated_at"], snapshot["source"]
            if type(version) is not int or version < 0:
                raise ValueError("invalid snapshot version")
            if version == 0:
                if values or updated_at is not None or source is not None:
                    raise ValueError("invalid empty snapshot")
            else:
                if not isinstance(updated_at, str) or len(updated_at) > 40:
                    raise ValueError("invalid snapshot time")
                # Preserve the original requirement: a timezone-aware UTC timestamp.
                if datetime.fromisoformat(updated_at).utcoffset() != timedelta(0):
                    raise ValueError("invalid snapshot time")
                if (not isinstance(source, dict) or not {"kind"} <= set(source) <= {"kind", "text"}
                        or source["kind"] not in ("user_settings", "user_chat", "deletion")
                        or ("text" in source and (not isinstance(source["text"], str) or len(source["text"]) > 500))):
                    raise ValueError("invalid snapshot source")
            if snapshot["role"] != access_role:
                memory["status"] = "role_mismatch"
            else:
                memory.update(status="loaded", stored_values=values, version=version, updated_at=updated_at)
        except (ValueError, TypeError, KeyError):
            pass
    overrides = current_preference_overrides(message)
    memory["effective_values"] = {**memory["stored_values"], **overrides}
    memory["overrides"] = {key: "current_turn_explicit" for key in overrides}
    return memory
