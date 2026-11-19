"""冻结保留题的离线评判，不调用模型、数据库或平台。

public_runs只交付运行入口所需的用户问题/模式/可信角色，不含私有预期。
规则只判定本组有限事实、引用和长度；无法机械确认的语义明确待人工复核。
两次重复必须是不同任务，不把重读同一结果当重复实验。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics

ROOT = Path(__file__).resolve().parent
SUITE_PATH = ROOT / "evaluation" / "holdout_20261118.json"
DIMENSIONS = ("structure", "facts", "conditions", "access_scope", "citations",
              "answer_fidelity", "brevity", "resources", "trace")
TIME_KEYS = {"paid_from", "paid_before", "refund_before", "paid_at", "refunded_at"}


def load_suite(path=None):
    path = Path(path or SUITE_PATH)
    suite = json.loads(path.read_text(encoding="utf-8"))
    if suite.get("schema_version") != 1 or len({c["id"] for c in suite["cases"]}) != len(suite["cases"]):
        raise ValueError("保留题结构或题号无效")
    return suite


def public_runs(suite, repeats=2):
    """role仅供可信测试入口选账号，不能拼入模型问题或被请求改写。"""
    if type(repeats) is not int or not 1 <= repeats <= 2:
        raise ValueError("仅支持1或2次显式安排，缺次不能宣称重复验收完成")
    return [{"case_id": case["id"], "repeat": number, "role": case["role"],
             "request": deepcopy(case["request"])}
            for number in range(1, repeats + 1) for case in suite["cases"]]


def suite_sha256(path=None):
    return hashlib.sha256(Path(path or SUITE_PATH).read_bytes()).hexdigest()


def verify_commitment(suite_path=None, source_root=None):
    """只核文件指纹；不把文件一致当作运行数据库数据一致。"""
    path = Path(suite_path or SUITE_PATH)
    commitment = json.loads(path.with_name(path.stem + "_commitment.json").read_text(encoding="utf-8"))
    suite = load_suite(path)
    errors = []
    if suite_sha256(path) != commitment["suite_sha256"]:
        errors.append("题面/预期与冻结指纹不同")
    for item in suite["source_fingerprints"]:
        source = Path(source_root or ROOT) / item["path"]
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]:
            errors.append("冻结依据已变化：" + item["path"])
    return {"status": "failed" if errors else "passed", "errors": errors,
            "suite_sha256": suite_sha256(path)}


def _preflight_data_fingerprint(observations):
    """来源读取时刻每次更新，只排除该观察元数据；不忽略业务时间或来源版本。"""
    stable = deepcopy(observations)
    for observation in stable:
        for key in ("result", "comparison"):
            result = observation.get(key)
            provenance = result.get("provenance") if isinstance(result, dict) else None
            if isinstance(provenance, dict):
                provenance.pop("observed_at", None)
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def preflight_suite(suite, engine):
    """显式只读本机数据预检；无模型/协议/平台调用，不导入或修补数据。

    engine由操作者提供；查当前已授权演示角色的事实，避免数据漂移后付费测错题。
    返回指纹不代替下游真实模型调用，也不把失败结果调整为新的预期。
    """
    from refund_service import check_order
    from merchant_service import check_shop_order, order_shops
    from sales_analysis import compare_periods
    from agent_runtime import search_policies, _owned_result
    from models import Order
    from sqlalchemy import select

    observations, issues = [], []
    for case in suite["cases"]:
        expected = case["private_expectations"]
        role = case["role"]
        for requirement in expected.get("required_facts", []):
            name, arguments = requirement["tool"], requirement.get("arguments", {})
            if name == "check_order":
                cutoff = datetime.fromisoformat(arguments["refund_before"])
                raw = (check_shop_order(engine, arguments["order_id"], "S001", cutoff) if role == "merchant"
                       else check_order(engine, arguments["order_id"], "B001", cutoff))
                actual = _owned_result(raw, "B001", access_role=role, shop_id="S001" if role == "merchant" else None)
            elif name == "search_policies":
                actual = search_policies("退货 运费 售后 证据 政策", arguments["as_of"])
            else:
                raise ValueError("保留预检未覆盖的工具；不得静默跳过")
            observations.append({"case_id": case["id"], "tool": name, "result": actual})
            if not _subset(actual, requirement["result"]):
                issues.append(case["id"] + "运行数据与冻结预期不符")
        if expected.get("comparison"):
            conditions = {k: datetime.fromisoformat(v) for k, v in case["request"]["period_conditions"].items()}
            actual = compare_periods(engine, {"role": role, "shop_id": "S001", "buyer_id": "B001"}, **conditions)
            observations.append({"case_id": case["id"], "comparison": actual})
            if not _subset(actual, expected["comparison"]):
                issues.append(case["id"] + "两期运行数据与冻结预期不符")
    # 检查负例确实存在于另一个合成店铺，避免把“全库已删”误当跨店拒绝证据。
    # 固定来源是samples/dual_role_demo.json及sql/005_demo_shop_mapping.sql，绝不传入模型。
    with engine.connect() as conn:
        row = conn.execute(select(Order.order_id, Order.buyer_id, order_shops.c.shop_id)
            .join(order_shops, order_shops.c.order_id == Order.order_id)
            .where(Order.order_id == "O_DEMO_003")).first()
    identity = list(row) if row else None
    if identity != ["O_DEMO_003", "B_DEMO_003", "S002"]:
        issues.append("跨店负例的独立存在性/归属已变化")
    observations.append({"cross_scope_fixture_identity": identity})
    fingerprint = _preflight_data_fingerprint(observations)
    return {"status": "failed" if issues else "passed", "errors": issues,
            "data_fingerprint": fingerprint, "observation_count": len(observations),
            "scope": "只读现有合成样本；不是新生成/未见数据或模型验收"}


def _equal(actual, expected, key=""):
    if key in TIME_KEYS and isinstance(actual, str) and isinstance(expected, str):
        try:
            a, b = datetime.fromisoformat(actual), datetime.fromisoformat(expected)
            return a.utcoffset() is not None and b.utcoffset() is not None and a == b
        except ValueError:
            return False
    return type(actual) is type(expected) and actual == expected


def _subset(actual, expected):
    if not isinstance(actual, dict):
        return False
    return all(key in actual and (_subset(actual[key], value) if isinstance(value, dict)
               else _equal(actual[key], value, key)) for key, value in expected.items())


def _walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _amounts(answer):
    """只提取显式元/分，不把日期、编号或笔数当金额；不自动推断省略单位。"""
    values = []
    for match in re.finditer(r"(?<![A-Za-z0-9_.])([+-]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:人民币)?(元|分)(?!钟)", answer):
        try:
            values.append(Decimal(match[1].replace(",", "")) * (100 if match[2] == "元" else 1))
        except InvalidOperation:
            continue
    return values


def _labeled_amounts(answer, field):
    labels = {"paid_amount_fen": r"支付(?:金额|额)?|实付(?:金额)?|付款(?:金额)?",
              "refund_amount_fen": r"退款(?:金额|额|合计)?",
              "net_amount_fen": r"净(?:销售额|支出|额)"}
    pattern = r"(?:" + labels[field] + r")[^。；;\n0-9+\-]{0,12}([+\-]?\d+(?:,\d{3})*(?:\.\d+)?\s*(?:人民币)?(?:元|分)(?!钟))"
    return [amount for match in re.finditer(pattern, answer) for amount in _amounts(match[1])]


def _evidence_value(value, path):
    """只接受字段/数组下标路径；不用eval执行模型或日志里的字符串。"""
    if not isinstance(path, str) or not re.fullmatch(r"[a-z_]+(?:\[\d+\])?(?:\.[a-z_]+(?:\[\d+\])?)*", path):
        raise ValueError("无效证据路径")
    for name, index in re.findall(r"([a-z_]+)|\[(\d+)\]", path):
        value = value[name] if name else value[int(index)]
    return value


def evaluate_record(case, record):
    """接完整任务记录或运行器result；不访问私有预期之外的任何外部状态。"""
    dims = {key: {"status": "passed", "errors": [], "review": []} for key in DIMENSIONS}

    def check(key, ok, message):
        if not ok:
            dims[key]["status"] = "failed"
            dims[key]["errors"].append(message)

    def review(key, message):
        if dims[key]["status"] != "failed":
            dims[key]["status"] = "needs_review"
        dims[key]["review"].append(message)

    valid = isinstance(record, dict)
    check("structure", valid, "缺少任务对象")
    record = record if valid else {}
    result = record.get("result") if isinstance(record.get("result"), dict) else record
    expected = case["private_expectations"]
    answer = record.get("answer", result.get("answer"))
    status = record.get("status", result.get("status"))
    facts = result.get("facts", [])
    check("structure", isinstance(answer, str) and bool(answer.strip()), "最终回答为空或类型错误")
    check("structure", status in expected["statuses"], "最终状态与冻结预期不符")
    check("structure", isinstance(facts, list), "facts不是列表")
    answer = answer if isinstance(answer, str) else ""
    facts = facts if isinstance(facts, list) else []
    for fact in facts:
        check("structure", isinstance(fact, dict) and isinstance(fact.get("tool"), str)
              and isinstance(fact.get("result"), dict), "工具事实缺少名称或对象结果")
    facts = [f for f in facts if isinstance(f, dict) and isinstance(f.get("result"), dict)]

    for requirement in expected.get("required_facts", []):
        candidates = [f for f in facts if f.get("tool") == requirement["tool"]]
        matching = [f for f in candidates if _subset(f.get("arguments", {}), requirement.get("arguments", {}))]
        check("conditions", bool(matching), "缺少满足冻结条件的实际工具调用")
        check("facts", any(_subset(f["result"], requirement.get("result", {})) for f in matching),
              "实际工具事实与冻结源数据预期不一致")

    if expected.get("no_tools"):
        check("facts", not facts, "缺条件阶段不应查业务事实")
    known_ids = expected.get("allowed_order_ids")
    if known_ids is not None:
        for item in _walk(facts):
            if isinstance(item, dict) and "order_id" in item:
                check("access_scope", item["order_id"] in known_ids, "工具事实含当前题范围外订单")
    # 无匹配可回显请求编号；不能披露其他店订单的金额、买家、时间及明细。
    if expected.get("deny_business_details"):
        private = {"buyer_id", "shop_id", "paid_amount_fen", "refund_amount_fen", "net_amount_fen",
                   "paid_at", "refund_count", "refunds", "orders"}
        check("access_scope", not any(isinstance(x, dict) and private.intersection(x) for x in _walk(facts)),
              "拒绝访问结果仍含业务明细")
        check("access_scope", not _amounts(answer), "无权/无匹配回答不应披露或补造金额")

    comparison_expected = expected.get("comparison")
    if comparison_expected:
        candidates = [x for x in _walk(result.get("comparison", {})) if isinstance(x, dict)]
        candidates += [f["result"] for f in facts]
        matches = [x for x in candidates if {"first", "second", "delta", "conditions"} <= set(x)]
        check("facts", any(_subset(x, comparison_expected) for x in matches), "两期事实/差额/截止不符合冻结预期")
        if not matches:
            check("conditions", False, "没有实际比较事实，不能仅凭模型金额判通过")
        if case.get("request", {}).get("execution_mode") == "period_analysis":
            catalog = result.get("analysis_evidence", {})
            references = set(re.findall(r"\bE\d+\b", answer))
            check("citations", isinstance(catalog, dict) and bool(references), "模型解释没有可回查的字段证据引用")
            for ref in references:
                try:
                    item = catalog[ref]
                    supported = any(_equal(_evidence_value(actual, item["path"]), item["value"]) for actual in matches)
                except (KeyError, IndexError, TypeError, ValueError):
                    supported = False
                check("citations", supported, "解释引用无法回到本任务实际比较字段：" + ref)
            check("structure", (result.get("analysis_validation") or {}).get("status") == "accepted",
                  "本次模型解释未被接受；固定事实回退不能冒充模型解释通过")

    for requirement in expected.get("result_fields", []):
        check("conditions", _subset(result, requirement), "任务条件/偏好快照与冻结要求不符")

    required_citations = expected.get("citations", {})
    for cid, version in required_citations.items():
        source_match = any(isinstance(x, dict) and x.get("citation_id") == cid and x.get("version") == version
                           and x.get("text") and x.get("source") for x in _walk(facts))
        check("citations", source_match, "引用缺少实际原文/来源/版本")
        check("citations", cid in answer and version in answer, "回答缺少必要引用编号或版本")
    cited = set(re.findall(r"DEMO-[A-Z0-9_-]+-v\d+(?:\.\d+)?", answer))
    available = {x["citation_id"] for x in _walk(facts) if isinstance(x, dict) and isinstance(x.get("citation_id"), str)}
    check("citations", cited <= available, "回答引用不在实际检索结果内")

    for pattern in expected.get("answer_required_patterns", []):
        if not re.search(pattern, answer, re.I):
            review("answer_fidelity", "必要条件/限制没有被有限规则识别，须人工核对")
    for pattern in expected.get("answer_forbidden_patterns", []):
        check("answer_fidelity", not re.search(pattern, answer, re.I), "回答存在与证据不符的肯定表述")
    money = _amounts(answer)
    required_money = expected.get("answer_money_fen", [])
    for amount in required_money:
        if Decimal(amount) not in money:
            review("answer_fidelity", "未识别必要金额及明确单位；不可按缺省单位直接判正确")
    for requirement in expected.get("required_facts", []):
        for field in ("paid_amount_fen", "refund_amount_fen", "net_amount_fen"):
            if field not in requirement.get("result", {}):
                continue
            values = _labeled_amounts(answer, field)
            if values:
                check("answer_fidelity", all(value == requirement["result"][field] for value in values),
                      "金额标签与冻结事实不对应：" + field)
            else:
                review("answer_fidelity", "金额标签配对未被有限规则识别，需人工对照：" + field)
    allowed_money = expected.get("allowed_answer_money_fen")
    if allowed_money is not None:
        check("answer_fidelity", set(money) <= {Decimal(x) for x in allowed_money}, "回答出现冻结事实之外的金额")
    if expected.get("no_tools"):
        check("answer_fidelity", not money, "缺条件阶段不应编造金额")
    if expected.get("needs_semantic_review"):
        review("answer_fidelity", "因果/适用条件语义由人对照原始资料确认，规则命中不替代语义审查")

    chars = len(re.sub(r"\s", "", answer))
    han = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", answer))
    check("brevity", chars <= expected["max_answer_nonspace_chars"], "完整回答超过冻结长度；不得截断修饰结果")
    usage = result.get("usage", {})
    check("resources", isinstance(usage, dict), "缺少使用量对象")
    usage = usage if isinstance(usage, dict) else {}
    for key, limit in case["resource_limits"].items():
        val = usage.get(key)
        check("resources", type(val) is int and 0 <= val <= limit, "调用计数缺失、类型错误或超冻结上限：" + key)
    if expected.get("no_tools"):
        check("resources", usage.get("tool_calls") == 0, "缺条件阶段已有工具尝试")
    else:
        check("resources", type(usage.get("model_requests")) is int and usage["model_requests"] > 0,
              "非澄清题缺真实模型调用记录，固定结果不能替代完整模型链")
    elapsed = (result.get("orchestration") or {}).get("wall_seconds")
    if elapsed is None:
        elapsed = (result.get("budget") or {}).get("elapsed_seconds")
    check("resources", type(elapsed) in (int, float) and 0 <= elapsed <= 100, "耗时证据缺失或超过90秒任务及有限收尾余量")
    trace = result.get("trace", [])
    check("trace", isinstance(trace, list), "缺少trace列表")
    trace = trace if isinstance(trace, list) else []
    model_events = [x for x in trace if isinstance(x, dict) and x.get("stage") == "model"]
    tool_events = [x for x in trace if isinstance(x, dict) and x.get("stage") == "tool"]
    check("trace", len(model_events) == usage.get("model_requests"), "模型计数与可核轨迹不符")
    check("trace", len(tool_events) == usage.get("tool_calls"), "工具尝试计数与轨迹不符")
    for event in model_events:
        check("trace", isinstance(event.get("request"), dict) and ("response" in event or "error_type" in event),
              "模型调用缺请求及响应/错误证据")
    check("trace", all(x.get("started_at") and x.get("finished_at") and type(x.get("elapsed_seconds")) in (int, float)
                        for x in model_events + tool_events), "已记录模型/工具步骤缺起止时间")
    failed = any(x["status"] == "failed" for x in dims.values())
    pending = any(x["status"] == "needs_review" for x in dims.values())
    return {"case_id": case["id"], "task_id": record.get("id"),
            "decision": "failed" if failed else "needs_review" if pending else "passed",
            "security_failure": dims["access_scope"]["status"] == "failed", "dimensions": dims,
            "metrics": {"answer_nonspace_chars": chars, "answer_han_chars": han, "elapsed_seconds": elapsed,
                        **{k: usage.get(k) for k in ("model_requests", "tool_calls", "total_tokens", "unmetered_requests")}}}


def record_sha256(record):
    """人工复核绑定完整原始任务，不接受换正文后仍套用原结论。"""
    return hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def apply_manual_review(report, record, manual):
    """逐题记录语义复核，规则通过也可被人判失败；不抹除自动硬失败。"""
    reviewed = deepcopy(report)
    reviewed["automatic_decision"] = report["decision"]
    reviewed["manual_review"] = deepcopy(manual)
    reviewed["manual_review_complete"] = False
    if (not isinstance(manual, dict) or not manual.get("reviewer") or not manual.get("reviewed_at")
            or manual.get("record_sha256") != record_sha256(record)):
        reviewed["manual_review_error"] = "复核缺身份/时间或原始记录指纹不匹配"
        return reviewed
    for key, value in manual.get("dimensions", {}).items():
        if key != "answer_fidelity":
            continue
        if not isinstance(value, dict) or value.get("status") not in {"passed", "failed"} or not value.get("reason") or not value.get("evidence"):
            continue
        dim = reviewed["dimensions"][key]
        reviewed["manual_review_complete"] = True
        dim["automatic_status"] = dim["status"]
        if dim["status"] != "failed":
            dim["status"] = value["status"]
        dim["manual_reason"] = value["reason"]
        dim["manual_evidence"] = value["evidence"]
    statuses = [d["status"] for d in reviewed["dimensions"].values()]
    reviewed["decision"] = "failed" if "failed" in statuses else "needs_review" if "needs_review" in statuses else "passed"
    return reviewed


def aggregate_runs(suite, runs):
    """两次不是统计显著性实验；所有权限失败单独列出且使总结果失败。"""
    by_id = {c["id"]: c for c in suite["cases"]}
    reports, seen, ids, issues = [], set(), set(), []
    for run in runs:
        key = (run.get("case_id"), run.get("repeat"))
        if key[0] not in by_id or key[1] not in (1, 2) or key in seen:
            issues.append("未知/重复题次"); continue
        seen.add(key)
        source_record = run.get("record")
        task_id = source_record.get("id") if isinstance(source_record, dict) else None
        if not task_id or task_id in ids:
            issues.append("缺少不同真实任务编号，不能把重放同一记录算重复运行")
        ids.add(task_id)
        report = evaluate_record(by_id[key[0]], run.get("record"))
        if run.get("manual_review") is not None:
            report = apply_manual_review(report, run.get("record"), run["manual_review"])
        report["repeat"] = key[1]
        reports.append(report)
    missing = [{"case_id": cid, "repeat": repeat} for cid in by_id for repeat in (1, 2) if (cid, repeat) not in seen]
    for field in ("config_fingerprint", "data_fingerprint"):
        values = [run.get(field) for run in runs]
        if not values or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in values) or len(set(values)) != 1:
            issues.append(field + "缺失或题间/重复间不一致；不能声称同配置同数据比较")
    summary = {}
    for cid in by_id:
        subset = [r for r in reports if r["case_id"] == cid]
        stats = {}
        for field in ("model_requests", "tool_calls", "total_tokens", "elapsed_seconds", "answer_nonspace_chars"):
            values = [r["metrics"][field] for r in subset if type(r["metrics"].get(field)) in (int, float)]
            stats[field] = {"n": len(values), "min": min(values) if values else None,
                            "max": max(values) if values else None, "mean": statistics.mean(values) if values else None}
        summary[cid] = {"runs": len(subset), "decisions": [r["decision"] for r in subset], "metrics": stats}
    security = [r for r in reports if r["security_failure"]]
    unreviewed = [{"case_id": r["case_id"], "repeat": r["repeat"]} for r in reports if not r.get("manual_review_complete")]
    decision = ("failed" if security or any(r["decision"] == "failed" for r in reports)
                else "incomplete" if missing or issues
                else "needs_review" if unreviewed or any(r["decision"] == "needs_review" for r in reports) else "passed")
    return {"suite_version": suite["version"], "decision": decision, "missing_runs": missing,
            "integrity_issues": issues, "security_failures": [{"case_id": r["case_id"], "repeat": r["repeat"]} for r in security],
            "manual_review_missing": unreviewed, "cases": summary, "runs": reports, "scope": suite["scope"],
            "limitations": "每题2次仅观察重复性；不是生产准确率/显著性能结论；规则或人工复核均不得抹去原失败。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("metadata", "inputs", "evaluate"))
    parser.add_argument("--suite", type=Path, default=SUITE_PATH)
    parser.add_argument("--records", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()
    suite = load_suite(args.suite)
    if args.command == "metadata":
        value = {"version": suite["version"], "case_count": len(suite["cases"]), "sha256": suite_sha256(args.suite), "scope": suite["scope"]}
    elif args.command == "inputs":
        value = public_runs(suite, args.repeats)
    else:
        if args.records is None:
            parser.error("evaluate需要--records；脚本不会替你调用服务")
        value = aggregate_runs(suite, json.loads(args.records.read_text()))
        value["suite_sha256"] = suite_sha256(args.suite)
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
