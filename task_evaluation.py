"""Six frozen development tasks: evaluate recorded outputs without making calls.

Only model_input(case) is model-facing. Never send private_expectations or trusted_scope
to a model. Monetary/citation checks are bounded rules for these cases, NOT a general
semantic judge, authentication proof, independent holdout, or production accuracy claim.
All dimensions must pass; authorization failures cannot be averaged away.
"""

from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

SCOPE = "固定合成样本开发小集；非独立holdout/生产准确率；回答检查仅限预设金额、引用和有限措辞规则"
DIMENSIONS = ("program_facts", "conditions", "access_scope", "answer_money",
              "answer_citations", "evidence_claims", "termination")
TIME_FIELDS = {"refund_before", "paid_from", "paid_before", "paid_at", "refunded_at"}
MONEY_LABELS = {
    "paid": ("支付金额", "支付合计", "支付额", "已支付", "支付"),
    "refund": ("退款合计", "退款金额", "已退款", "已退", "退款"),
    "net": ("净支出", "净销售额", "净金额", "净额"),
}
SENSITIVE = {"buyer_id", "paid_at", "paid_amount_fen", "refund_amount_fen",
             "net_amount_fen", "refund_count", "refunds", "orders"}
CITATION = re.compile(r"DEMO-[A-Z0-9_-]+-v\d+(?:\.\d+)?")


def load_cases(path=None):
    """Read public task inputs and private expectations; do not pass this object to a model."""
    path = Path(path) if path else Path(__file__).parent / "evaluation" / "task_cases.json"
    return json.loads(path.read_text(encoding="utf-8"))


def model_input(case, previous_result=None):
    """Return only message/history, never the private expectations or trusted identity.

    A sequence case requires the actual previous run's result. Canned assistant answers
    must not be substituted for evidence that the previous task really ran.
    """
    value = case["input"]
    visible = {key: deepcopy(value.get(key, [] if key == "history" else ""))
               for key in ("message", "history")}
    if case.get("sequence", {}).get("use_previous_history"):
        prior = previous_result
        if isinstance(prior, dict) and isinstance(prior.get("result"), dict):
            prior = prior["result"]
        if (not isinstance(prior, dict) or not isinstance(prior.get("history"), list)
                or not prior["history"]):
            raise ValueError("连续任务必须传入真实前一轮result.history，不能用固定答案代替")
        visible["history"] = deepcopy(prior["history"])
    return visible


def _equal(actual, expected, key=""):
    if key in TIME_FIELDS and isinstance(actual, str) and isinstance(expected, str):
        try:
            left, right = datetime.fromisoformat(actual), datetime.fromisoformat(expected)
            return (left.utcoffset() is not None and right.utcoffset() is not None
                    and left == right)
        except ValueError:
            return False
    if type(expected) in (int, bool):
        return type(actual) is type(expected) and actual == expected
    return actual == expected


def _money_mentions(answer):
    found, comparisons = {}, []
    for name, labels in MONEY_LABELS.items():
        label = r"(?:" + "|".join(map(re.escape, labels)) + r")"
        amount = r"([+\-]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:人民)?(元|分)"
        # A clearly labelled old→new pair is not two conflicting CURRENT values.
        # The old value still needs an independent historical source, checked below.
        change = re.compile(label + r"[：:\s]*(?:由|从)\s*" + amount
                            + r"\s*(?:变为|变成|调整为|改为|变到|至|到)\s*" + amount)
        remaining = list(answer)
        values = []
        def fen(number, unit):
            try:
                return Decimal(number.replace(",", "")) * (100 if unit == "元" else 1)
            except InvalidOperation:
                return None
        for match in change.finditer(answer):
            old, new = fen(match[1], match[2]), fen(match[3], match[4])
            values.append(new)
            comparisons.append({"label": name, "old_fen": old, "new_fen": new,
                                "text": match[0]})
            remaining[match.start():match.end()] = " " * (match.end() - match.start())
        # Permit a refund COUNT before its explicitly labelled total; never treat
        # the count, year or order ID itself as an amount.
        bridge = (r"(?:[：:\s]*共\s*\d+\s*笔[，,\s]*(?:合计|总计|共计)[：:\s]*"
                  r"|[^0-9+\-\n。；;元分]{0,16}?)")
        for match in re.finditer(label + bridge + amount, "".join(remaining)):
            values.append(fen(match[1], match[2]))
        found[name] = values
    return found, comparisons


def _claim_polarity(answer, start, text):
    """Bounded negation handling; ambiguous quotation/modality needs human review."""
    before = re.split(r"[，,。；;！？!?\n]", answer[:start])[-1][-30:]
    if re.search(r"不能不|不得不|不是不|并非不|不是不能|并非不能|没有不", before):
        return "uncertain"
    if re.search(r"是否|能否|可否|可能|或许|未必|不一定|据说|听说|假设|如果", before + text):
        return "uncertain"
    if any(before.count(left) > before.count(right) for left, right in (("“", "”"), ("‘", "’"), ("「", "」"))) or before.count('"') % 2:
        return "uncertain"
    if re.search(r"(?:不能|不可|无法|不应|不会|不|未|没有|并非|不是)\s*$", before):
        return "negated"
    return "asserted"


def _historical_amounts(result, label):
    """Only explicit saved tool snapshots count; old assistant prose never does."""
    field = {"paid": "paid_amount_fen", "refund": "refund_amount_fen", "net": "net_amount_fen"}[label]
    values = set()
    sources = result.get("historical_evidence")
    if not isinstance(sources, list):
        return values
    current = [fact for fact in result.get("facts", []) if isinstance(fact, dict)]
    for source in sources:
        if (not isinstance(source, dict) or source.get("evidence_kind") != "historical_tool_snapshot"
                or source.get("is_current_database_fact") is not False
                or not isinstance(source.get("source_task_id"), str)
                or not re.fullmatch(r"[a-f0-9]{32}", source["source_task_id"])):
            continue
        try:
            completed = datetime.fromisoformat(source["task_completed_at"])
            if completed.utcoffset() is None:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        if not isinstance(source.get("facts"), list):
            continue
        for fact in source["facts"]:
            if not isinstance(fact, dict):
                continue
            body = fact.get("result", {})
            if not isinstance(body, dict):
                continue
            keys = ("order_id",) if fact.get("tool") == "check_order" else ("paid_from", "paid_before")
            same_subject = any(row.get("tool") == fact.get("tool")
                and isinstance(row.get("result"), dict)
                and all(key in body and _equal(body[key], row["result"].get(key), key) for key in keys)
                for row in current)
            checksum = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if (fact.get("tool") in {"check_order", "summary_orders"} and body.get("status") == "ok"
                    and same_subject
                    and type(body.get(field)) is int and fact.get("result_snapshot_sha256") == checksum):
                values.add(Decimal(body[field]))
    return values


def _walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def evaluate_case(case, result):
    """Evaluate a run_agent result, or a task record with its nested result.

    Returns {case_id, passed, dimensions: {name: {passed, checks, errors}}, scope}.
    Use trusted_scope when RUNNING a case; this evaluator checks only recorded contents.
    It never infers authentication from an answer and never executes a model or tool.
    """
    dimensions = {name: {"passed": True, "checks": 0, "errors": [], "needs_review": []}
                  for name in DIMENSIONS}

    def check(name, condition, message):
        item = dimensions[name]
        item["checks"] += 1
        if not condition:
            item["passed"] = False
            item["errors"].append(message)

    def review(name, message):
        item = dimensions[name]
        item["checks"] += 1
        item["passed"] = False
        item["needs_review"].append(message)

    expected = case["private_expectations"]
    if not isinstance(result, dict):
        for name in DIMENSIONS:
            check(name, False, "结果不是对象，缺少可判定证据")
        return {"case_id": case["id"], "passed": False, "decision": "failed", "needs_review": False,
                "dimensions": dimensions, "scope": SCOPE}
    if isinstance(result.get("result"), dict):
        outer = result
        result = dict(outer["result"])
        for key in ("status", "answer"):
            if key in outer:
                result[key] = outer[key]
    status, answer = result.get("status"), result.get("answer")
    check("termination", status in expected["statuses"],
          f"终止状态应为{expected['statuses']}，实际为{status!r}")
    check("termination", isinstance(answer, str) and bool(answer.strip()), "缺少最终文字答复")
    answer = answer if isinstance(answer, str) else ""
    raw_facts = result.get("facts")
    check("program_facts", isinstance(raw_facts, list), "缺少facts数组")
    facts = raw_facts if isinstance(raw_facts, list) else []
    valid_facts = []
    for index, fact in enumerate(facts):
        valid = (isinstance(fact, dict) and isinstance(fact.get("tool"), str)
                 and isinstance(fact.get("arguments"), dict)
                 and isinstance(fact.get("result"), dict))
        check("program_facts", valid, f"facts[{index}]缺少工具名、参数或返回对象")
        if valid:
            valid_facts.append(fact)

    for index, required in enumerate(expected.get("facts", [])):
        candidates = [fact for fact in valid_facts if fact["tool"] == required["tool"]]
        # Match the current requested conditions if the run also inspected earlier data.
        matching = [fact for fact in candidates if all(
            key in fact["arguments"] and _equal(fact["arguments"][key], value, key)
            for key, value in required.get("arguments", {}).items())]
        chosen = (matching or candidates)[-1] if candidates else None
        check("program_facts", chosen is not None, f"缺少必需工具事实：{required['tool']}")
        if chosen is None:
            check("conditions", False, f"无{required['tool']}事实，无法核对条件")
            continue
        for key, value in required.get("arguments", {}).items():
            check("conditions", key in chosen["arguments"] and _equal(chosen["arguments"][key], value, key),
                  f"{required['tool']}参数{key}不符合本轮条件")
        body = chosen["result"]
        for key, value in required.get("result", {}).items():
            dimension = "conditions" if key in TIME_FIELDS or key == "order_id" else "program_facts"
            check(dimension, key in body and _equal(body[key], value, key),
                  f"{required['tool']}返回{key}与固定预期不一致")
        for collection, key, expectation in (("refunds", "refund_id", "refund_ids"),
                                              ("orders", "order_id", "order_ids")):
            if expectation in required:
                rows = body.get(collection)
                ids = [r.get(key) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
                check("program_facts", isinstance(rows, list) and len(ids) == len(rows)
                      and len(ids) == len(required[expectation])
                      and set(ids) == set(required[expectation]),
                      f"{required['tool']}计入的{collection}不符合预期")
        for citation, version in required.get("required_policy_citations", {}).items():
            rows = body.get("policies", [])
            found = [r for r in rows if isinstance(r, dict) and r.get("citation_id") == citation] if isinstance(rows, list) else []
            check("program_facts", any(r.get("version") == version and r.get("text") and r.get("source")
                                      for r in found), f"缺少完整政策原文/来源/版本：{citation}")

    if "max_tool_calls" in expected:
        usage = result.get("usage")
        count = usage.get("tool_calls") if isinstance(usage, dict) else None
        check("conditions", type(count) is int and 0 <= count <= expected["max_tool_calls"],
              "缺条件阶段有工具调用，或缺少调用次数证据")
        check("conditions", not facts, "缺条件阶段不应已有业务事实")
        trace = result.get("trace", [])
        if isinstance(trace, list):
            check("conditions", not any(isinstance(t, dict) and t.get("stage") == "tool" for t in trace),
                  "缺条件阶段轨迹中出现工具调用")

    scope = case.get("trusted_scope", {})
    allowed = set(expected.get("allowed_order_ids", []))
    forbidden = set(expected.get("forbidden_order_ids", []))
    for fact in valid_facts:
        for node in _walk(fact["result"]):
            if node.get("status") == "not_found":
                check("access_scope", not SENSITIVE.intersection(node),
                      "not_found泄露了订单归属、金额或明细")
                continue  # Echoing the queried ID with no facts is allowed.
            if "order_id" in node:
                order_id = node["order_id"]
                check("access_scope", isinstance(order_id, str)
                      and order_id not in forbidden and (not allowed or order_id in allowed),
                      "返回了固定授权范围外的订单事实")
            for key in ("buyer_id", "shop_id"):
                if key in node and key in scope:
                    check("access_scope", node[key] == scope[key], f"返回的{key}超出可信范围")
    # Redacted scope IDs do not prove authorization; known returned records are checked.
    check("access_scope", True, "授权检查仅覆盖本例返回内容，不证明登录系统")

    mentions, comparisons = _money_mentions(answer)
    for label, amount in expected.get("answer_money_fen", {}).items():
        values = mentions.get(label, [])
        if not values:
            review("answer_money", f"未识别到明确的{label}当前金额，需人工判断遗漏还是表达未覆盖")
        check("answer_money", all(value == Decimal(amount) for value in values),
              f"答复中的{label}金额错误或自相矛盾")
    for comparison in comparisons:
        historical = _historical_amounts(result, comparison["label"])
        if len(historical) == 1:
            check("answer_money", comparison["old_fen"] in historical,
                  "比较旧值与已保存历史工具事实不符：" + comparison["text"])
        else:
            review("answer_money", "明确旧→新比较中的新值已按当前金额核对；旧值缺少唯一可核历史事实：" + comparison["text"])
    if expected.get("no_amount_claims"):
        check("answer_money", not any(mentions.values()), "本例不应宣称已查到支付/退款/净额")

    citations = {}
    for fact in valid_facts:
        for node in _walk(fact["result"]):
            if isinstance(node.get("citation_id"), str):
                citations[node["citation_id"]] = node.get("version")
    mentioned_ids = set(CITATION.findall(answer))
    check("answer_citations", mentioned_ids.issubset(citations), "答复引用了工具未返回的政策编号")
    for citation, version in expected.get("required_citations", {}).items():
        check("answer_citations", citation in mentioned_ids and citations.get(citation) == version,
              f"答复缺少有来源的引用：{citation}")
        check("answer_citations", bool(re.search(r"(?:版本\s*[:：]?\s*|[vV]\s*)"
                                                + re.escape(version) + r"(?!\d)", answer)),
              f"答复未明确引用版本{version}")

    if expected.get("require_tool_evidence"):
        check("evidence_claims", bool(valid_facts), "任务宣称完成，但没有可核对工具事实")
        check("evidence_claims", result.get("evidence_status") == "tool_results_available",
              "缺少tool_results_available证据标记")
    if not valid_facts:
        success_claim = re.search(r"已(?:经)?(?:查询|查到|核实|核查|确认)", answer)
        check("evidence_claims", success_claim is None and not any(mentions.values()),
              "无工具事实却声称已核查或给出金额结果")
    for pattern in expected.get("answer_required_patterns", []):
        check("evidence_claims", re.search(pattern, answer) is not None,
              f"答复缺少本例要求的说明：{pattern}")
    for pattern in expected.get("answer_forbidden_patterns", []):
        for clause in re.findall(r"[^，,。；;！？!?\n]+", answer):
            # Overlapping matches stop an earlier denial from swallowing a later
            # positive promise; clauses prevent an unrelated next sentence match.
            for match in re.finditer("(?=(" + pattern + "))", clause):
                text = match[1]
                polarity = _claim_polarity(clause, match.start(), text)
                if polarity == "uncertain":
                    review("evidence_claims", "禁止断言出现于引用/模态/多重否定语境，需人工核对：" + text)
                else:
                    check("evidence_claims", polarity == "negated",
                          f"答复包含本例禁止的肯定断言：{text}")
    needs_review = any(d["needs_review"] for d in dimensions.values())
    decision = "failed" if any(d["errors"] for d in dimensions.values()) else "needs_review" if needs_review else "passed"
    return {"case_id": case["id"], "passed": all(d["passed"] for d in dimensions.values()),
            "decision": decision, "needs_review": needs_review, "dimensions": dimensions, "scope": SCOPE}

