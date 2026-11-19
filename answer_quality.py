"""回答长度的可观察检查；不截断正文、不把长度通过当事实正确。"""

BRIEF_LIMITS = {"single": 260, "amount": 170, "policy": 150, "multi": 420}


def measure_answer(answer, values, mode="single"):
    count = sum(not char.isspace() for char in answer)
    limit = BRIEF_LIMITS.get(mode, 260) if values.get("answer_style") == "brief" else None
    return {"non_whitespace_characters": count, "brief_limit": limit,
            "brevity": "not_required" if limit is None else "passed" if count <= limit else "failed",
            "facts_and_citations": "requires_separate_verification",
            "text_truncated": False}


def brief_instruction(values, specialist=None):
    if values.get("answer_style") != "brief":
        return ""
    limit = BRIEF_LIMITS.get(specialist or "single", 260)
    return (f"本次简短要求：answer正文不超过{limit}个非空白字符（数字、字母、标点也计入）。"
            "先给结论，再用一两句保留必要金额、截止及不确定性；政策保留引用ID和版本。"
            "不复述题目、工具JSON、字段英文或整段权限说明，不重复相同限制。"
            "事实与来源会另存供查看；正文仍须独立说清关键口径，不为缩短丢掉限制或截断句子。")


def compact_instruction(values):
    """普通回答去重复，不新增硬字数上限；本轮明确详细要求仍优先。"""
    if values.get("answer_style") == "detailed":
        return ""
    return ("回答组织：除用户本轮明确要求详细解释外，先给所问结果，再简要交代必要条件和依据。"
            "同一截止只出现一次含时区的表达，优先用户明确的时区，未指定则用北京时间；"
            "除非用户要求时区换算或技术字段，不同时复述UTC与北京时间的等价时刻，不抄字段英文或比较表达式。"
            "用中文一次说清上界是否包含；同一只读范围、合成数据或非实时边界不在开头和结尾重复。"
            "用户未问政策且政策不影响本次结论时，不主动添加‘本次未涉及政策’或政策检索建议。"
            "保留所需金额、截止、必要范围和来源不确定性；来源unknown须说明来源未确认，不能写成已验证。"
            "仅删除重复或无关扩展，不省略用户明确要求的明细、核对步骤、引用或关键限制。")
