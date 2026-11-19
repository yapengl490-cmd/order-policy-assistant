"""真实运行循环+替身网络/工具，证明共享预算、职责限制与停止边界。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_runtime import run_agent
from execution_budget import ExecutionBudget
from mcp_order_client import OrderProtocolClient
from test_agent_runtime import settings, call, response


class RuntimeBudgetTests(unittest.TestCase):
    def run_with(self, replies, *, cfg=None, budget=None, specialist=None, handlers=None, cancel=lambda: False):
        sent = []
        def send(request):
            sent.append(json.loads(request.content))
            item = replies.pop(0)
            if callable(item):
                item = item()
            if isinstance(item, Exception):
                raise item
            return httpx.Response(200, json=item)
        result = run_agent(cfg or settings(), None, "核查O001退款并解释演示政策", "B001", [], cancel,
                           transport=httpx.MockTransport(send), tool_handlers=handlers or {},
                           shared_budget=budget, specialist=specialist)
        return result, sent

    def test_two_real_loops_share_four_total_requests(self):
        cfg = settings(max_model_calls=3)
        budget = ExecutionBudget(settings(max_model_calls=4), lambda: False)
        def run(index):
            return self.run_with([
                response(call("list_orders", identifier=f"{index}-1")),
                response(call("list_orders", identifier=f"{index}-2")),
                response(),
            ], cfg=cfg, budget=budget,
                handlers={"list_orders": lambda **_: {"status": "ok", "orders": []}})[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, range(2)))
        self.assertEqual(sum(r["usage"]["model_requests"] for r in results), 4)
        self.assertEqual(budget.snapshot()["used"]["model_requests"], 4)
        self.assertIn("limited", [r["status"] for r in results])

    def test_model_timeout_still_consumes_shared_request(self):
        budget = ExecutionBudget(settings(max_model_calls=1), lambda: False)
        first, sent = self.run_with([httpx.ReadTimeout("hidden")], budget=budget)
        second, sent2 = self.run_with([], budget=budget)
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["usage"]["unmetered_requests"], 1)
        self.assertEqual(second["status"], "limited")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent2, [])

    def test_unexpected_error_preserves_prior_facts_and_attempt_usage(self):
        result, sent = self.run_with([
            response(call("list_orders")), RuntimeError("SECRET_REMOTE_CONTEXT"),
        ], handlers={"list_orders": lambda **_: {"status": "ok", "orders": []}})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(result["facts"]), 1)
        self.assertEqual(result["usage"]["model_requests"], 2)
        self.assertEqual(result["usage"]["unmetered_requests"], 1)
        self.assertEqual(result["usage"]["total_tokens"], 13)
        self.assertEqual(result["trace"][-1], {"stage": "runtime_exception", "error_type": "RuntimeError"})
        self.assertNotIn("SECRET_REMOTE_CONTEXT", json.dumps(result))

    def test_deadline_after_response_prevents_tool_execution(self):
        now = [0.0]
        cfg = settings(task_timeout_seconds=1)
        budget = ExecutionBudget(cfg, lambda: False, clock=lambda: now[0])
        def delayed():
            now[0] = 2.0
            return response(call("list_orders"))
        result, _ = self.run_with([delayed], cfg=cfg, budget=budget,
            handlers={"list_orders": lambda **_: self.fail("must not execute late tool")})
        self.assertEqual(result["status"], "limited")
        self.assertEqual(result["usage"]["tool_calls"], 0)
        self.assertEqual(result["usage"]["total_tokens"], 13)

    def test_deadline_after_tool_keeps_fact_and_does_not_call_model_again(self):
        now = [0.0]
        cfg = settings(task_timeout_seconds=1)
        budget = ExecutionBudget(cfg, lambda: False, clock=lambda: now[0])
        def delayed_tool(**kwargs):
            now[0] = 2.0
            return {"status": "ok", "orders": []}
        result, sent = self.run_with([response(call("list_orders"))], cfg=cfg, budget=budget,
                                     handlers={"list_orders": delayed_tool})
        self.assertEqual(result["status"], "limited")
        self.assertEqual(len(result["facts"]), 1)
        self.assertEqual(len(sent), 1)

    def test_specialist_whitelist_is_execution_enforced(self):
        result, sent = self.run_with([response(call("check_order", {"order_id": "O001"})),
                                     response(status="clarification")], specialist="policy",
            handlers={"check_order": lambda **_: self.fail("policy cannot query order")})
        self.assertEqual([t["function"]["name"] for t in sent[0]["tools"]], ["search_policies"])
        self.assertEqual(result["trace"][1]["result"]["error"], "tool_not_allowed")
        self.assertEqual(result["facts"], [])
        self.assertIn("政策解释分支", sent[0]["messages"][0]["content"])

    def test_amount_branch_has_no_policy_tool(self):
        result, sent = self.run_with([response()], specialist="amount")
        self.assertEqual({t["function"]["name"] for t in sent[0]["tools"]},
                         {"list_orders", "check_order", "summary_orders"})
        self.assertIn("金额核查分支", sent[0]["messages"][0]["content"])
        self.assertEqual(result["status"], "completed")

    def test_policy_does_not_require_order_protocol(self):
        cfg = settings(mcp_enabled=True)
        with patch("agent_runtime._default_handlers", return_value={}), patch(
                "mcp_order_client.OrderProtocolClient", side_effect=AssertionError("must not discover order protocol")):
            result = run_agent(cfg, None, "退款政策", "B001", [], lambda: False, specialist="policy",
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response())))
        self.assertEqual(result["status"], "completed")

    def test_protocol_whole_operation_has_deadline(self):
        client = OrderProtocolClient("buyer:B001", deadline_seconds=lambda: .01)
        async def stalled(arguments=None):
            await asyncio.sleep(10)
        client._request = stalled
        with self.assertRaises(TimeoutError):
            client.discover()
        self.assertEqual(client.last_trace["error_type"], "TaskDeadlineExceeded")

    def test_protocol_never_starts_after_deadline(self):
        client = OrderProtocolClient("buyer:B001", deadline_seconds=lambda: 0)
        async def forbidden(arguments=None):
            self.fail("request after deadline")
        client._request = forbidden
        with self.assertRaises(TimeoutError):
            client.discover()


if __name__ == "__main__":
    unittest.main()
