"""同合同普通工具基线：模型可一次请求多个工具，不人为增加轮次。"""
from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import unittest
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_config import AppSettings
from condition_state import reconcile_conditions
from programmatic_runtime import run_batch_sequential_agent

QUESTION = "列出我店最近2笔订单并逐笔核查，退款截止2026年11月3日零点"


def calls(*entries):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call-" + name + "-" + str(args.get("order_id", index)), "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for index, (name, args) in enumerate(entries)]}


class BaselineTests(unittest.TestCase):
    def run_case(self, messages, *, count=4):
        self.requests, self.observed = [], []
        pending = iter(messages)
        def send(request):
            self.requests.append(json.loads(request.content))
            message = next(pending)
            return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                                           "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
        def listing(**args):
            self.observed.append(("list", args))
            return {"status": "ok", "orders": [{"order_id": "O1"}, {"order_id": "O2"}]}
        def checking(**args):
            self.observed.append(("check", args))
            return {"status": "ok", "order_id": args["order_id"], "net_amount_fen": 100}
        state = reconcile_conditions(QUESTION, None, datetime(2026, 11, 18, tzinfo=timezone.utc), "turn")
        return run_batch_sequential_agent(AppSettings(model_api_key="test-secret", max_model_calls=count), None, QUESTION,
            "unused", [], lambda: False, access_role="merchant", shop_id="S001", condition_state=state,
            transport=httpx.MockTransport(send), tool_handlers={"list_orders": listing, "check_order": checking})

    def test_multiple_tool_calls_in_one_model_response_are_allowed(self):
        result = self.run_case([calls(("list_orders", {"limit": 2})),
            calls(("check_order", {"order_id": "O1"}), ("check_order", {"order_id": "O2"})),
            {"role": "assistant", "content": '{"answer":"两笔净额均为1元。"}'}])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage"]["model_requests"], 3)
        self.assertEqual(result["usage"]["tool_calls"], 3)
        self.assertEqual(result["orchestration"]["effective"], "batch_sequential")
        self.assertEqual(len([m for m in self.requests[-1]["messages"] if m["role"] == "tool"]), 3)
        for name, args in self.observed:
            if name == "check": self.assertEqual(args["refund_before"], "2026-11-03T00:00:00+08:00")

    def test_premature_model_completion_is_not_a_complete_business_result(self):
        result = self.run_case([calls(("list_orders", {"limit": 2})),
                               {"role": "assistant", "content": '{"answer":"已全部完成。"}'}])
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["answer"].startswith("【程序状态】"))

    def test_same_total_budget_stops_without_extra_summary_call(self):
        result = self.run_case([calls(("list_orders", {"limit": 2})),
            calls(("check_order", {"order_id": "O1"}), ("check_order", {"order_id": "O2"}))], count=2)
        self.assertEqual(result["status"], "limited")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(result["facts"]), 3)


    def test_empty_and_same_batch_duplicate_ids_execute_no_tools(self):
        for empty in ["", "   "]:
            first=calls(("list_orders", {"limit": 2}));first["tool_calls"][0]["id"]=empty
            result=self.run_case([first])
            self.assertEqual(result["status"],"failed");self.assertEqual(self.observed,[])
        first=calls(("list_orders", {"limit": 2}),("list_orders", {"limit": 2}))
        first["tool_calls"][1]["id"]=first["tool_calls"][0]["id"]
        result=self.run_case([first])
        self.assertEqual(result["status"],"failed");self.assertEqual(self.observed,[])

    def test_cross_turn_duplicate_ids_reject_whole_new_batch(self):
        listing=calls(("list_orders", {"limit": 2}))
        checking=calls(("check_order", {"order_id": "O1"}),("check_order", {"order_id": "O2"}))
        checking["tool_calls"][1]["id"]=listing["tool_calls"][0]["id"]
        result=self.run_case([listing,checking])
        self.assertEqual(result["status"],"partial")
        self.assertEqual([name for name,_ in self.observed],["list"])
        self.assertEqual(result["usage"]["tool_calls"],1)


if __name__ == "__main__": unittest.main()
