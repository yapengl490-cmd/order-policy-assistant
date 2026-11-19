"""政策日期与退款截止独立：替身模型捕获输入，真实条件解析/校验及工具分派。"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_runtime import run_agent, SearchPoliciesArgs
from condition_state import reconcile_conditions
from test_agent_runtime import settings, call, response

NOW = datetime(2026, 11, 17, 10, tzinfo=timezone.utc)


class PolicyDateContractTests(unittest.TestCase):
    def execute(self, message, replies, *, specialist=None, with_state=True):
        self.requests, self.policy_inputs, self.order_inputs = [], [], []
        self.state = reconcile_conditions(message, None, NOW, "policy-contract-test") if with_state else None
        def send(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json=replies.pop(0))
        def policy(**values):
            self.policy_inputs.append(values)
            return {"status": "insufficient_evidence", "documents": []}
        def order(**values):
            self.order_inputs.append(values)
            return {"status": "ok", "net_amount_fen": 8500}
        return run_agent(settings(), None, message, "B001", [], lambda: False,
                         transport=httpx.MockTransport(send),
                         tool_handlers={"search_policies": policy, "check_order": order},
                         condition_state=self.state, specialist=specialist)

    def test_advertised_policy_arguments_are_query_only_for_single_and_policy_specialist(self):
        for specialist in (None, "policy"):
            with self.subTest(specialist=specialist):
                self.execute("核查O001，退款截止2026年11月3日零点，并解释退款政策", [response()], specialist=specialist)
                policy = next(t["function"] for t in self.requests[0]["tools"] if t["function"]["name"] == "search_policies")
                self.assertEqual(set(policy["parameters"]["properties"]), {"query"})
                self.assertEqual(policy["parameters"]["required"], ["query"])
                self.assertFalse(policy["parameters"]["additionalProperties"])
                system = self.requests[0]["messages"][0]["content"]
                self.assertIn("2026-11-17", system)
                self.assertIn("program_task_date_default", system)
                self.assertIn("政策适用日期与订单退款截止是两个独立条件", system)

    def test_default_policy_date_does_not_replace_refund_cutoff(self):
        result = self.execute("核查O001，退款截止2026年11月3日零点，并解释退款政策", [
            response(call("check_order", {"order_id": "O001"}),
                     call("search_policies", {"query": "退款政策"}, "policy-1")), response(),
        ])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.order_inputs[0]["refund_before"], "2026-11-03T00:00:00+08:00")
        self.assertEqual(self.policy_inputs, [{"query": "退款政策", "as_of": "2026-11-17"}])
        self.assertNotIn("policy_as_of", self.state["confirmed"])

    def test_explicit_policy_date_is_honored_separately(self):
        result = self.execute("核查O001，退款截止2026年11月3日零点，并解释退款政策，政策日期2026-10-20", [
            response(call("search_policies", {"query": "退款政策"})), response(),
        ])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.policy_inputs, [{"query": "退款政策", "as_of": "2026-10-20"}])
        self.assertEqual(self.state["confirmed"]["refund_before"]["value"], "2026-11-03T00:00:00+08:00")
        self.assertIn("user_confirmed_policy_date", self.requests[0]["messages"][0]["content"])

    def test_wrong_policy_date_rejected_then_query_only_repair_uses_server_date(self):
        result = self.execute("核查O001，退款截止2026年11月3日零点，并解释退款政策", [
            response(call("search_policies", {"query": "退款政策", "as_of": "2026-11-03"})),
            response(call("search_policies", {"query": "退款政策"}, "repair-1")), response(),
        ])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.policy_inputs, [{"query": "退款政策", "as_of": "2026-11-17"}])
        invalid = next(t for t in result["trace"] if t.get("condition_error_code") == "condition_mismatch")
        self.assertEqual(invalid["condition_field"], "policy_as_of")
        feedback = invalid["result"]
        self.assertIn("省略as_of", feedback["message"])
        self.assertEqual(feedback["policy_date_context"], {"value": "2026-11-17", "source": "program_task_date_default"})
        self.assertEqual(result["usage"]["tool_calls"], 2)

    def test_invalid_repeat_still_stops_without_policy_execution(self):
        result = self.execute("核查O001，退款截止2026年11月3日零点，并解释退款政策", [
            response(call("search_policies", {"query": "退款政策", "as_of": "2026-11-03"})),
            response(call("search_policies", {"query": "退款政策", "as_of": "2026-11-03"}, "bad-2")),
        ])
        self.assertEqual(result["status"], "limited")
        self.assertEqual(self.policy_inputs, [])

    def test_original_stateless_schema_still_supports_explicit_date(self):
        self.assertEqual(SearchPoliciesArgs(query="退款政策", as_of="2026-10-20").as_of, "2026-10-20")
        result = self.execute("退款政策", [
            response(call("search_policies", {"query": "退款政策", "as_of": "2026-10-20"})), response(),
        ], with_state=False)
        self.assertEqual(result["status"], "completed")
        self.assertIn("as_of", self.requests[0]["tools"][-1]["function"]["parameters"]["properties"])
        self.assertEqual(self.policy_inputs, [{"query": "退款政策", "as_of": "2026-10-20"}])


if __name__ == "__main__":
    unittest.main()
