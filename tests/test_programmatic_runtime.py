"""真实runtime＋模型/解释器替身，不调用付费模型、Docker或业务库。"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_config import AppSettings
from condition_state import reconcile_conditions
from program_worker import Interpreter, ProgramError
from programmatic_runtime import run_programmatic_agent

QUESTION = "列出我店最近3笔订单并逐笔核查，退款截止2026年11月3日零点"
SOURCE = '''page = list_orders(limit=3)
rows = []
for order in page["orders"]:
    checked = check_order(order_id=order["order_id"])
    rows.append(checked)
result = rows'''


def interpreter(source, handler, **kwargs):
    runner = Interpreter(handler)
    try:
        return {"status": "ok", "result": runner.run(source), "execution": {"test_double": True}}
    except Exception as exc:
        return {"status": "failed", "error": getattr(exc, "code", "tool_failed"), "execution": {"test_double": True}}


class ProgrammaticRuntimeTests(unittest.TestCase):
    def run_case(self, outputs=None, question=QUESTION, settings=None, handlers=None, should_cancel=lambda: False):
        self.requests, self.tools = [], []
        pending = list(outputs or [{"source": SOURCE}, {"answer": "本次核查两笔，净额分别85元。"}])
        def send(request):
            self.requests.append(json.loads(request.content))
            self.assertTrue(pending, "unexpected model request")
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": json.dumps(pending.pop(0), ensure_ascii=False)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
        def listing(**args):
            self.tools.append(("list_orders", args))
            return {"status": "ok", "shop_id": "S001", "orders": [{"order_id": "O001", "shop_id": "S001"}, {"order_id": "O002", "shop_id": "S001"}]}
        def checking(**args):
            self.tools.append(("check_order", args))
            return {"status": "ok", "order_id": args["order_id"], "shop_id": "S001", "net_amount_fen": 8500}
        state = reconcile_conditions(question, None, datetime(2026, 11, 18, tzinfo=timezone.utc), "turn")
        return run_programmatic_agent(settings or AppSettings(model_api_key="test-secret"), None, question, "unused", [], should_cancel,
            access_role="merchant", shop_id="S001", condition_state=state, transport=httpx.MockTransport(send),
            tool_handlers=handlers or {"list_orders": listing, "check_order": checking}, program_executor=interpreter)

    def test_model_source_executes_broker_and_facts_drive_second_model(self):
        result = self.run_case()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage"]["model_requests"], 2)
        self.assertEqual(result["usage"]["tool_calls"], 3)
        self.assertEqual(result["budget"]["used"]["tool_calls"], 3)
        self.assertEqual([name for name, _ in self.tools], ["list_orders", "check_order", "check_order"])
        for _, args in self.tools[1:]: self.assertEqual(args["refund_before"], "2026-11-03T00:00:00+08:00")
        final_input = str(self.requests[-1])
        self.assertIn("8500", final_input)
        self.assertNotIn("S001", final_input)
        self.assertNotIn("test-secret", str(result))

    def test_generation_receives_actual_tool_result_structure_before_running(self):
        question = QUESTION.replace("3笔", "4笔")
        self.run_case(question=question, outputs=[{"source": SOURCE.replace("limit=3", "limit=4")}, {"answer": "已核查。"}])
        prompt = self.requests[0]["messages"][0]["content"]
        self.assertIn('list_orders正常返回的是dict对象', prompt)
        self.assertIn('page["orders"]', prompt)
        self.assertIn('order["order_id"]', prompt)
        for field in ('"status":"ok"', '"has_more":布尔值', '"limit":整数', '"provenance":来源对象'):
            self.assertIn(field, prompt)
        self.assertIn('check_order返回一笔核查结果dict', prompt)
        self.assertIn('status不是ok时不保证包含金额', prompt)
        self.assertIn('不能补零', prompt)
        self.assertIn('"limit": 4', prompt)
        self.assertNotIn('O001', prompt)
        self.assertNotIn('8500', prompt)

    def test_real_failure_dict_iteration_still_rejected_without_tool_replay(self):
        # 留存真实失败形状；以下仅运行模型/工具替身，不重放原任务或收费调用。
        failed_source = ('orders = list_orders(limit=2)\nresult = []\nfor order in orders:\n'
            '    result.append(check_order(order_id=order["order_id"], refund_before="2026-11-03T00:00:00+08:00"))')
        question = QUESTION.replace("3笔", "2笔")
        failed = self.run_case(question=question, outputs=[{"source": failed_source}, {"answer": "只取得列表，未核查金额。"}])
        self.assertEqual(failed["status"], "partial")
        self.assertEqual(failed["program_executions"][0]["error"], "loop_limit")
        self.assertEqual([name for name, _ in self.tools], ["list_orders"])
        self.assertEqual(failed["usage"]["model_requests"], 2)
        # 只改返回对象的访问路径，原解释器约束、broker及合同保持相同。
        corrected = self.run_case(question=question, outputs=[{"source": failed_source.replace('for order in orders:', 'for order in orders["orders"]:')}, {"answer": "已核查。"}])
        self.assertEqual(corrected["status"], "completed")
        self.assertEqual([name for name, _ in self.tools], ["list_orders", "check_order", "check_order"])
        self.assertEqual(corrected["usage"]["tool_calls"], 3)

    def test_missing_cutoff_or_excess_quantity_clarifies_before_model(self):
        for question in ("列出我店最近3笔订单并逐笔核查", QUESTION.replace("3笔", "8笔"),
                         QUESTION.replace("2026年11月3日零点", "最近一次")):
            with self.subTest(question=question):
                result = self.run_case(question=question)
                self.assertEqual(result["status"], "clarification")
                self.assertEqual(self.requests, [])

    def test_one_invalid_source_repair_is_bounded(self):
        result = self.run_case(outputs=[{"source": "import os"}, {"source": SOURCE}, {"answer": "已核查。"}])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage"]["model_requests"], 3)
        failed = self.run_case(outputs=[{"source": "import os"}, {"source": "import os"}])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.tools, [])

    def test_unknown_order_and_changed_cutoff_never_reach_business_handler(self):
        for source in (SOURCE.replace('order["order_id"]', '"O999"'),
                       SOURCE.replace('order_id=order["order_id"]', 'order_id=order["order_id"], refund_before="2030-01-01T00:00:00+08:00"')):
            with self.subTest(source=source):
                result = self.run_case(outputs=[{"source": source}, {"answer": "未核查完整。"}])
                self.assertEqual(result["status"], "partial")
                self.assertEqual([name for name, _ in self.tools], ["list_orders"])
                self.assertEqual(result["usage"]["tool_calls"], 2)

    def test_program_cannot_claim_completion_after_skipping_orders(self):
        result = self.run_case(outputs=[{"source": 'page = list_orders(limit=3)\nresult = {"status": "completed"}'}, {"answer": "已完成。"}])
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["answer"].startswith("【程序状态】本次仅部分完成"))

    def test_shared_tool_limit_keeps_previous_facts(self):
        result = self.run_case(settings=AppSettings(model_api_key="test-secret", max_tool_calls=2))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["usage"]["tool_calls"], 2)
        self.assertEqual(len(result["facts"]), 2)

    def test_scope_violation_does_not_return_foreign_facts(self):
        result = self.run_case(handlers={"list_orders": lambda **_: {"status": "ok", "shop_id": "S999", "orders": []}})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["facts"], [])
        self.assertNotIn("S999", str(result))

    def test_pre_cancel_does_not_call_model_or_tools(self):
        result = self.run_case(should_cancel=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.tools, [])


if __name__ == "__main__":
    unittest.main()
