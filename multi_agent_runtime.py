"""有限双智能体对照：同一问题分给金额、政策两个独立模型上下文。

应用程序固定分工并执行工具；这不是模型自主创建其他智能体。两支共享任务预算，
每支最多两轮模型、三次工具尝试。应用按职责合并结果，不额外调用“总结模型”。
仅在服务器确认本题同时需要金额与政策时分支，否则明确退回原单智能体。
"""
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import is_dataclass, replace
import time

from agent_runtime import run_agent
from runtime_context import history_pairs
from execution_budget import ExecutionBudget
from answer_quality import measure_answer

AMOUNT_TOOLS = frozenset({"check_order", "list_orders", "summary_orders"})
BRANCHES = (("amount", "金额核查"), ("policy", "政策解释"))
USAGE_KEYS = ("model_requests", "tool_calls", "input_tokens", "output_tokens", "total_tokens", "unmetered_requests")


def _branch_settings(settings):
    changes = {"max_model_calls": min(2, settings.max_model_calls),
               "max_tool_calls": min(3, settings.max_tool_calls)}
    if is_dataclass(settings):
        return replace(settings, **changes)
    result = copy(settings)
    for name, value in changes.items():
        setattr(result, name, value)
    return result


def _branch_outcome(name, result, condition_state):
    """完成状态必须有本职责的工具证据；没有证据的流畅回答不能算核查完成。"""
    result = deepcopy(result)
    required = {"search_policies"} if name == "policy" else AMOUNT_TOOLS
    if name == "amount":
        primary = {"order": "check_order", "summary": "summary_orders", "list": "list_orders"}.get(condition_state.get("kind"))
        if primary:
            required = {primary}
    evidence = [f for f in result.get("facts", []) if isinstance(f, dict) and f.get("tool") in required]
    observed = {f.get("result", {}).get("status") for f in evidence if isinstance(f.get("result"), dict)}
    reason = None
    if result.get("status") == "completed" and not evidence:
        result.update(status="failed", answer="本分支未取得职责所需的工具证据，不能据模型文字宣称核查完成。")
        reason = "required_tool_evidence_missing"
    elif result.get("status") == "completed" and (not observed or not observed <= ({"ok", "insufficient_evidence"} if name == "policy" else {"ok", "not_found", "not_yet_paid"})):
        result["status"] = "partial"
        reason = "business_result_incomplete"
    if "insufficient_evidence" in observed:
        result["evidence_status"] = "insufficient_policy_evidence"
    return result, reason


def run_multi_agent(settings, engine, message, buyer_id, history, should_cancel, *,
                    access_role="buyer", shop_id=None, transport=None, tool_handlers=None,
                    condition_state=None, historical_evidence=None, preference_snapshot=None,
                    agent_runner=None):
    """与run_agent同入口；身份由后端认证，状态及偏好快照由后端冻结。

    两支各得同一份历史的独立副本，不互相读写中途消息。合并后才形成下一轮历史。
    agent_runner / transport / handlers只用于隔离测试，正式服务使用真实run_agent。
    """
    runner = agent_runner or run_agent
    if isinstance(condition_state, dict) and condition_state.get("kind") == "support":
        # 政策读取依赖本次授权订单事实，不进入下方的独立并行分支。
        from support_collaboration import run_support_collaboration
        return run_support_collaboration(settings, engine, message, buyer_id, history, should_cancel,
            access_role=access_role, shop_id=shop_id, transport=transport, tool_handlers=tool_handlers,
            condition_state=condition_state, historical_evidence=historical_evidence,
            preference_snapshot=preference_snapshot)
    started = time.monotonic()
    budget = ExecutionBudget(settings, should_cancel)
    common = {"access_role": access_role, "shop_id": shop_id, "transport": transport,
              "tool_handlers": tool_handlers, "condition_state": deepcopy(condition_state),
              "historical_evidence": deepcopy(historical_evidence),
              "preference_snapshot": deepcopy(preference_snapshot)}
    allowed = set(condition_state.get("allowed_tools", [])) if isinstance(condition_state, dict) else set()
    valid_identity = (access_role == "buyer" and isinstance(buyer_id, str) and bool(buyer_id.strip())) or (
        access_role == "merchant" and isinstance(shop_id, str) and bool(shop_id.strip()))
    fanout = (valid_identity and isinstance(condition_state, dict)
              and condition_state.get("schema_version") == 1
              and not condition_state.get("needs_clarification")
              and "search_policies" in allowed and bool(allowed & AMOUNT_TOOLS))
    if not fanout:
        # 原入口负责认证、澄清、取消及单职责处理，不增加无用的模型请求。
        result = runner(settings, engine, message, buyer_id, deepcopy(history), should_cancel,
                        shared_budget=budget, **common)
        result["orchestration"] = {"requested": "multi", "effective": "single",
            "reason": "conditions_not_ready" if isinstance(condition_state, dict) and condition_state.get("needs_clarification") else "no_independent_amount_and_policy_branches",
            "wall_seconds": round(time.monotonic() - started, 6), "branches": []}
        result["budget"] = budget.snapshot()
        return result

    def execute(name):
        branch_start = time.monotonic()
        try:
            result = runner(_branch_settings(settings), engine, message, buyer_id,
                deepcopy(history), should_cancel, shared_budget=budget, specialist=name,
                # handler函数共享，但映射及其他可变数据不跨分支修改。
                **{**deepcopy({k: v for k, v in common.items() if k not in {"transport", "tool_handlers"}}),
                   "transport": transport, "tool_handlers": dict(tool_handlers) if tool_handlers is not None else None})
            result, reason = _branch_outcome(name, result, condition_state)
        except Exception as error:
            # 原始异常可能含凭据或查询正文，不能拼入给用户/另一模型的回答。
            result = {"status": "failed", "answer": "本分支执行异常，未取得完整答复；不能用猜测补齐。",
                      "facts": [], "usage": {}, "trace": [{"stage": "branch_exception", "error_type": type(error).__name__}],
                      "evidence_status": "no_tool_evidence"}
            reason = "branch_exception"
        return {"name": name, "result": result, "reason": reason,
                "wall_seconds": round(time.monotonic() - branch_start, 6)}

    # 已发出的请求只能等待响应或其有限超时；不能靠Future.cancel伪称远端已经停止。
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="order-specialist") as pool:
        futures = [pool.submit(execute, name) for name, _ in BRANCHES]
        outcomes = [future.result() for future in futures]
    results = [item["result"] for item in outcomes]
    facts = [fact for item in results for fact in item.get("facts", [])]
    statuses = [item.get("status", "failed") for item in results]
    stop = budget.stop_status()
    if stop == "cancelled" or "cancelled" in statuses:
        status = "cancelled"
    elif all(value == "completed" for value in statuses) and stop is None:
        status = "completed"
    elif facts or "completed" in statuses:
        status = "partial"
    elif stop == "limited" or "limited" in statuses:
        status = "limited"
    elif "clarification" in statuses:
        status = "clarification"
    else:
        status = "failed"
    headings = {"completed": "两项核查已完成。",
                "partial": "本轮仅部分完成。已取得的工具事实保留；失败或未完成部分不能据此推断。",
                "cancelled": "任务已取消。已取得的事实保留；已发出的远端请求可能仍计费。",
                "limited": "任务达到调用或时间上限，未完成全部核查。",
                "clarification": "本轮尚有条件需要明确，未完成全部核查。",
                "failed": "本轮核查失败，不能当作无订单或零退款。"}
    pieces = [headings[status]]
    branches, trace = [], []
    for outcome, (_, title) in zip(outcomes, BRANCHES):
        item = outcome["result"]
        pieces.append(f"【{title}｜{item.get('status', 'failed')}】\n{item.get('answer', '')}")
        if item.get("evidence_status") == "insufficient_policy_evidence":
            pieces.append("本次政策工具明确返回证据不足；不能补造真实退款资格或到账承诺。")
        branches.append({"name": outcome["name"], "label": title, "status": item.get("status", "failed"),
                        "wall_seconds": outcome["wall_seconds"], "usage": deepcopy(item.get("usage", {})),
                        "fact_count": len(item.get("facts", [])), "evidence_status": item.get("evidence_status", "no_tool_evidence"),
                        "reason": outcome["reason"]})
        trace.extend({**event, "branch": outcome["name"]} for event in item.get("trace", []))
    answer = "\n\n".join(pieces)
    usage = {key: sum(item.get("usage", {}).get(key, 0) for item in results) for key in USAGE_KEYS}
    budget_snapshot = budget.snapshot()
    # 未预期异常可能在请求已经发出后发生；分支没返回usage不代表没有消耗。
    # 共享预留账本仍在，至少保留尝试次数；丢失的token回执不能伪造为0成本。
    missing_models = max(0, budget_snapshot["used"]["model_requests"] - usage["model_requests"])
    usage["model_requests"] = max(usage["model_requests"], budget_snapshot["used"]["model_requests"])
    usage["tool_calls"] = max(usage["tool_calls"], budget_snapshot["used"]["tool_calls"])
    usage["unmetered_requests"] += missing_models
    usage["usage_incomplete"] = bool(missing_models or any(item["reason"] == "branch_exception" for item in outcomes))
    usage["token_totals_are_lower_bound"] = bool(usage["usage_incomplete"] or usage["unmetered_requests"])
    result = {"status": status, "answer": answer,
        "history": history_pairs((history or []) + [{"role": "user", "content": message}, {"role": "assistant", "content": answer}]),
        "facts": facts, "trace": trace, "usage": usage, "budget": budget_snapshot,
        "condition_state": deepcopy(condition_state), "historical_evidence": results[0].get("historical_evidence", []),
        "memory": results[0].get("memory"), "answer_semantics_verified": False,
        "evidence_status": "partial_tool_results" if status != "completed" and facts else ("tool_results_available" if facts else "no_tool_evidence"),
        "orchestration": {"requested": "multi", "effective": "multi", "wall_seconds": round(time.monotonic() - started, 6),
            "branches": branches, "aggregation": "application_labeled_sections_no_extra_model",
            "per_branch_limits": {"model_requests": min(2, settings.max_model_calls), "tool_calls": min(3, settings.max_tool_calls)},
            "context_policy": "same_frozen_input_independent_copies_no_inter_branch_messages"}}
    result["answer_quality"] = measure_answer(answer, (result.get("memory") or {}).get("effective_values", {}), "multi")
    # 两支共用同一原文，只允许金额分支提交一次候选；应用再核验并写入偏好库。
    # 政策分支即使违规返回候选也不能成为写入来源。
    if (isinstance(results[0].get("memory"), dict)
            and results[0]["memory"].get("status") == "loaded"
            and "memory_proposal" in results[0]):
        result["memory_proposal"] = deepcopy(results[0]["memory_proposal"])
    return result
