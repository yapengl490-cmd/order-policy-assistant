"""并发额度与截止的离线验证，不调用外部服务。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from execution_budget import ExecutionBudget


def settings(**overrides):
    return SimpleNamespace(**({"max_model_calls": 4, "max_tool_calls": 6,
                              "max_output_tokens": 1200, "task_timeout_seconds": 90} | overrides))


class BudgetTests(unittest.TestCase):
    def test_parallel_model_reservations_cannot_overdraw(self):
        budget = ExecutionBudget(settings(), lambda: False)
        with ThreadPoolExecutor(max_workers=16) as pool:
            outcomes = list(pool.map(lambda _: budget.reserve_model(1200), range(64)))
        self.assertEqual(sum(outcomes), 4)
        self.assertEqual(budget.snapshot()["used"], {"model_requests": 4, "tool_calls": 0, "reserved_output_tokens": 4800})
        self.assertIsNone(budget.stop_status())  # 正在返回的第四次请求仍可交付结果。
        self.assertTrue(budget.reserve_tool())  # 模型耗尽不抹除已获准工具工作的机会。

    def test_parallel_tools_share_one_six_call_limit(self):
        budget = ExecutionBudget(settings(), lambda: False)
        with ThreadPoolExecutor(max_workers=16) as pool:
            outcomes = list(pool.map(lambda _: budget.reserve_tool(), range(64)))
        self.assertEqual(sum(outcomes), 6)
        self.assertEqual(budget.snapshot()["last_denial"], "tool_calls_limit")

    def test_output_reservation_is_atomic_with_model_request(self):
        budget = ExecutionBudget(settings(max_model_calls=2, max_output_tokens=10), lambda: False)
        self.assertTrue(budget.reserve_model(15))
        self.assertFalse(budget.reserve_model(6))
        self.assertEqual(budget.snapshot()["used"]["model_requests"], 1)
        self.assertTrue(budget.reserve_model(5))
        self.assertEqual(budget.snapshot()["used"]["reserved_output_tokens"], 20)

    def test_deadline_stops_both_types_without_reset(self):
        now = [100.0]
        budget = ExecutionBudget(settings(task_timeout_seconds=10), lambda: False, clock=lambda: now[0])
        self.assertTrue(budget.reserve_model(1200))
        now[0] = 110.0
        self.assertEqual(budget.stop_status(), "limited")
        self.assertFalse(budget.reserve_model(1200))
        self.assertFalse(budget.reserve_tool())
        self.assertEqual(budget.snapshot()["used"]["model_requests"], 1)
        self.assertEqual(budget.snapshot()["stop_reason"], "deadline")

    def test_cancellation_is_sticky_and_errors_fail_closed(self):
        flag = [False]
        budget = ExecutionBudget(settings(), lambda: flag[0])
        flag[0] = True
        self.assertEqual(budget.stop_status(), "cancelled")
        flag[0] = False
        self.assertFalse(budget.reserve_tool())
        def broken():
            raise RuntimeError("status store unavailable")
        self.assertEqual(ExecutionBudget(settings(), broken).stop_status(), "cancelled")

    def test_budget_is_not_a_currency_guarantee(self):
        budget = ExecutionBudget(settings(), lambda: False)
        self.assertFalse(budget.snapshot()["monetary_hard_limit"])


if __name__ == "__main__":
    unittest.main()
