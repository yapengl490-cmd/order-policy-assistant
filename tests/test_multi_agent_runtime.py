"""双智能体离线回归：真实协调器/循环，MockTransport模型与固定工具替身。

固定预期事实不是线上真实查询证据；用于比较预算、分工、失败传播与输入隔离。
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_runtime import run_agent
from app_config import AppSettings
from condition_state import reconcile_conditions
from multi_agent_runtime import run_multi_agent

QUERY = "核查O001截至2026-11-03T00:00:00+08:00的支付金额、退款和净额，并说明政策记录是否能证明真实到账。"
NOW = datetime(2026, 11, 17, 9, tzinfo=timezone.utc)
MONEY = {"status": "ok", "buyer_id": "B001", "order_id": "O001", "paid_amount_fen": 12500,
         "refund_amount_fen": 4000, "net_amount_fen": 8500}
POLICY = {"status": "ok", "policies": [{"citation_id": "DEMO-REFUND-2026-v1", "version": "1.0",
          "text": "合成演示记录不能证明真实退款到账。"}]}


def final(answer="离线模型根据工具答复", status="completed"):
    return {"choices": [{"message": {"role": "assistant", "content": json.dumps({"status": status, "answer": answer})},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}


def tool_response(names):
    calls = []
    for name in names:
        args = {"order_id": "O001", "refund_before": "2026-11-03T00:00:00+08:00"} if name == "check_order" else {"query": "退款记录到账政策"}
        calls.append({"id": name, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}})
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": calls},
            "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}


class MultiAgentTests(unittest.TestCase):
    def setUp(self):
        self.settings = AppSettings(model_api_key="offline-secret", model_enabled=True)
        self.state = reconcile_conditions(QUERY, None, NOW, "test-turn")
        self.assertEqual(set(self.state["allowed_tools"]), {"check_order", "search_policies"})
        self.requests = []
        self.lock = threading.Lock()

    def transport(self, *, empty=False, all_tools_always=False):
        def send(request):
            payload = json.loads(request.content)
            with self.lock:
                self.requests.append(payload)
            names = [item["function"]["name"] for item in payload.get("tools", [])]
            seen_result = any(m["role"] == "tool" for m in payload["messages"])
            return httpx.Response(200, json=(final() if empty or (seen_result and not all_tools_always)
                                           else tool_response(names)))
        return httpx.MockTransport(send)

    def run_multi(self, *, handlers=None, transport=None, cancel=lambda: False, **kwargs):
        return run_multi_agent(self.settings, None, QUERY, "B001", [], cancel,
            condition_state=self.state, transport=transport or self.transport(),
            tool_handlers=handlers or {"check_order": lambda **kw: deepcopy(MONEY),
                                      "search_policies": lambda **kw: deepcopy(POLICY)}, **kwargs)

    def test_same_task_single_multi_facts_and_global_caps(self):
        handlers = {"check_order": lambda **kw: deepcopy(MONEY), "search_policies": lambda **kw: deepcopy(POLICY)}
        single = run_agent(self.settings, None, QUERY, "B001", [], lambda: False,
                          condition_state=self.state, transport=self.transport(), tool_handlers=handlers)
        multi = self.run_multi(handlers=handlers)
        self.assertEqual(single["status"], multi["status"])
        self.assertEqual(multi["status"], "completed")
        self.assertEqual({f["tool"]: f["result"] for f in single["facts"]},
                         {f["tool"]: f["result"] for f in multi["facts"]})
        self.assertEqual(single["usage"]["model_requests"], 2)
        self.assertEqual(multi["usage"]["model_requests"], 4)
        self.assertEqual(multi["budget"]["used"]["model_requests"], 4)
        self.assertEqual(multi["budget"]["used"]["tool_calls"], 2)
        self.assertEqual(multi["budget"]["used"]["reserved_output_tokens"], 4800)
        self.assertEqual(multi["budget"]["limits"], single["budget"]["limits"])
        self.assertEqual(multi["orchestration"]["aggregation"], "application_labeled_sections_no_extra_model")
        self.assertFalse(multi["answer_semantics_verified"])
        offered = [set(tool["function"]["name"] for tool in p.get("tools", [])) for p in self.requests[2:]]
        self.assertEqual(offered.count({"check_order"}), 2)
        self.assertEqual(offered.count({"search_policies"}), 2)

    def test_policy_failure_keeps_amount_and_is_partial(self):
        def fail(**kw): raise TimeoutError("private token must not be leaked")
        result = self.run_multi(handlers={"check_order": lambda **kw: deepcopy(MONEY), "search_policies": fail})
        self.assertEqual(result["status"], "partial")
        self.assertEqual([f["tool"] for f in result["facts"]], ["check_order"])
        self.assertEqual(result["facts"][0]["result"]["net_amount_fen"], 8500)
        self.assertEqual([b["status"] for b in result["orchestration"]["branches"]], ["completed", "failed"])
        self.assertIn("仅部分完成", result["answer"])
        self.assertNotIn("private token", json.dumps(result))

    def test_amount_scope_violation_keeps_policy_only(self):
        result = self.run_multi(handlers={"check_order": lambda **kw: {**MONEY, "buyer_id": "B999"},
                                         "search_policies": lambda **kw: deepcopy(POLICY)})
        self.assertEqual(result["status"], "partial")
        self.assertEqual([f["tool"] for f in result["facts"]], ["search_policies"])
        self.assertNotIn("B999", json.dumps(result))

    def test_both_tools_fail_does_not_complete(self):
        def fail(**kw): raise RuntimeError("private error")
        result = self.run_multi(handlers={"check_order": fail, "search_policies": fail})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["facts"], [])
        self.assertEqual(result["usage"]["model_requests"], 2)

    def test_before_cancel_no_model_or_tool(self):
        result = self.run_multi(cancel=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.requests, [])
        self.assertEqual(result["budget"]["used"]["model_requests"], 0)

    def test_global_budget_not_recreated_per_branch(self):
        self.settings = replace(self.settings, max_model_calls=1, max_tool_calls=1)
        result = self.run_multi()
        self.assertLessEqual(result["usage"]["model_requests"], 1)
        self.assertLessEqual(result["usage"]["tool_calls"], 1)
        self.assertNotEqual(result["status"], "completed")
        self.assertLessEqual(len(self.requests), 1)

    def test_completed_without_required_facts_rejected(self):
        result = self.run_multi(transport=self.transport(empty=True))
        self.assertEqual(result["status"], "failed")
        self.assertTrue(all(b["reason"] == "required_tool_evidence_missing" for b in result["orchestration"]["branches"]))
        self.assertEqual(result["facts"], [])

    def test_policy_insufficient_is_observed_outcome_not_fabricated_answer(self):
        result = self.run_multi(handlers={"check_order": lambda **kw: deepcopy(MONEY),
                                         "search_policies": lambda **kw: {"status": "insufficient_evidence", "policies": []}})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["orchestration"]["branches"][1]["evidence_status"], "insufficient_policy_evidence")
        self.assertIn("不能补造", result["answer"])

    def test_incomplete_business_result_is_partial(self):
        result = self.run_multi(handlers={"check_order": lambda **kw: {"status": "too_many_refunds", "buyer_id": "B001"},
                                         "search_policies": lambda **kw: deepcopy(POLICY)})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["orchestration"]["branches"][0]["reason"], "business_result_incomplete")

    def test_unknown_business_status_is_not_complete(self):
        for status in ("failed", "invalid", None):
            with self.subTest(status=status):
                result = self.run_multi(handlers={"check_order": lambda **kw: {"status": status, "buyer_id": "B001"},
                                                 "search_policies": lambda **kw: deepcopy(POLICY)})
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["orchestration"]["branches"][0]["reason"], "business_result_incomplete")

    def test_no_independent_branches_uses_single_and_records_fallback(self):
        state = reconcile_conditions("核查O001截至2026-11-03T00:00:00+08:00的金额", None, NOW, "single-turn")
        result = run_multi_agent(self.settings, None, "核查O001", "B001", [], lambda: False,
            condition_state=state, transport=self.transport(), tool_handlers={"check_order": lambda **kw: deepcopy(MONEY)})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["orchestration"]["requested"], "multi")
        self.assertEqual(result["orchestration"]["effective"], "single")
        self.assertEqual(result["usage"]["model_requests"], 2)

    def test_missing_condition_clarifies_without_fork_or_call(self):
        state = reconcile_conditions("统计2026年10月支付金额并说明退款政策", None, NOW, "clarify")
        result = run_multi_agent(self.settings, None, "统计2026年10月支付金额并说明退款政策", "B001", [], lambda: False,
                                condition_state=state, transport=self.transport(), tool_handlers={})
        self.assertEqual(result["status"], "clarification")
        self.assertEqual(self.requests, [])
        self.assertEqual(result["orchestration"]["reason"], "conditions_not_ready")

    def test_original_context_same_independent_copies(self):
        history = [{"role": "user", "content": "以前的问题"}, {"role": "assistant", "content": "以前的回答不是数据库依据"}]
        preference = {"role": "buyer", "values": {"amount_format": "yuan", "answer_style": "brief"}, "version": 1}
        seen = {}
        lock = threading.Lock()
        def runner(settings, engine, message, buyer_id, prior, cancel, **kwargs):
            name = kwargs["specialist"]
            with lock: seen[name] = deepcopy({"message": message, "prior": prior, "state": kwargs["condition_state"], "preference": kwargs["preference_snapshot"]})
            prior.clear(); kwargs["condition_state"]["allowed_tools"].clear(); kwargs["preference_snapshot"]["values"].clear()
            return {"status": "completed", "answer": name, "facts": [{"tool": "check_order" if name == "amount" else "search_policies", "result": {"status": "ok"}}], "usage": {}, "trace": []}
        result = run_multi_agent(self.settings, None, QUERY, "B001", history, lambda: False,
                                condition_state=self.state, preference_snapshot=preference, agent_runner=runner)
        self.assertEqual(seen["amount"], seen["policy"])
        self.assertEqual(seen["amount"]["message"], QUERY)
        self.assertEqual(len(history), 2)
        self.assertEqual(len(self.state["allowed_tools"]), 2)
        self.assertEqual(preference["values"]["amount_format"], "yuan")
        self.assertEqual(result["history"][-1]["content"], result["answer"])

    def test_workers_really_execute_independent_branches_concurrently(self):
        barrier = threading.Barrier(2, timeout=1)
        def amount(**kw):
            barrier.wait()
            return deepcopy(MONEY)
        def policy(**kw):
            barrier.wait()
            return deepcopy(POLICY)
        result = self.run_multi(handlers={"check_order": amount, "search_policies": policy})
        self.assertEqual(result["status"], "completed")

    def test_exception_after_reservation_keeps_usage_unknown_not_zero(self):
        def runner(settings, engine, message, buyer_id, history, cancel, **kwargs):
            if kwargs["specialist"] == "policy":
                self.assertTrue(kwargs["shared_budget"].reserve_model(1200))
                raise RuntimeError("could have happened after a paid request")
            return {"status": "completed", "answer": "金额已核查", "facts": [{"tool": "check_order", "result": {"status": "ok"}}], "trace": [], "usage": {}}
        result = self.run_multi(agent_runner=runner)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["usage"]["model_requests"], 1)
        self.assertEqual(result["usage"]["unmetered_requests"], 1)
        self.assertTrue(result["usage"]["usage_incomplete"])
        self.assertTrue(result["usage"]["token_totals_are_lower_bound"])
        self.assertEqual(result["budget"]["used"]["model_requests"], 1)

    def test_branch_unexpected_exception_does_not_erase_sibling(self):
        def runner(settings, engine, message, buyer_id, history, cancel, **kwargs):
            if kwargs["specialist"] == "policy": raise RuntimeError("top-secret")
            return {"status": "completed", "answer": "金额已核查", "facts": [{"tool": "check_order", "result": {"status": "ok", "net_amount_fen": 8500}}], "trace": [], "usage": {}}
        result = self.run_multi(agent_runner=runner)
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("top-secret", json.dumps(result))
        self.assertEqual(result["orchestration"]["branches"][1]["reason"], "branch_exception")


if __name__ == "__main__":
    unittest.main()
