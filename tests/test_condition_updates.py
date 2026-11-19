"""其他字段成功不能掩盖尚未理解的修改；测试不访问外部服务。"""
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from condition_state import ConditionValidationError, reconcile_conditions, validate_tool_arguments

NOW = datetime(2026, 11, 18, 6, tzinfo=timezone.utc)


class FieldUpdateTests(unittest.TestCase):
    def state(self, text, old=None, turn="update"):
        return reconcile_conditions(text, old, NOW, turn)

    def test_unparsed_cutoff_is_not_hidden_by_an_order_id(self):
        old = self.state("查O001，退款截止2026年11月3日零点", turn="old")
        for previous in (None, old):
            for text in ("查O001，把退款截止改为最近一次", "查O002，退款截止按最后一次计算",
                         "查O001并把退款截止改成尚未确定", "把退款截止改为最近一次并查O001"):
                with self.subTest(text=text, previous=bool(previous)):
                    state = self.state(text, previous)
                    self.assertTrue(state["needs_clarification"])
                    self.assertEqual(state["allowed_tools"], [])
                    self.assertNotIn("refund_before", state["confirmed"])
                    self.assertNotIn("refund_before", state["defaults"])
                    self.assertIn("refund_before", state["blocked_fields"])
                    with self.assertRaises(ConditionValidationError):
                        validate_tool_arguments("check_order", {"order_id": "O001"}, state)

    def test_another_valid_date_cannot_satisfy_the_wrong_field(self):
        old = self.state("统计2026年10月订单，退款截止2026年11月3日零点", turn="old")
        state = self.state("支付开始改为最近一次，退款截止改成2026年11月4日零点", old)
        self.assertTrue(state["needs_clarification"])
        self.assertNotIn("paid_from", state["confirmed"])
        self.assertIn("paid_from", state["blocked_fields"])
        self.assertEqual(state["confirmed"]["refund_before"]["value"], "2026-11-04T00:00:00+08:00")
        self.assertFalse(self.state("再查", state)["ready"])
        fixed = self.state("支付开始改为2026年10月2日零点", state)
        self.assertTrue(fixed["ready"], fixed)

    def test_same_field_invalid_second_mention_cannot_keep_first_value(self):
        for text in ("查O001，退款截止2026年11月3日零点，退款截止改成最近一次",
                     "查O001，退款截止改成最近一次，退款截止2026年11月3日零点"):
            with self.subTest(text=text):
                state = self.state(text)
                self.assertTrue(state["needs_clarification"])
                self.assertNotIn("refund_before", state["confirmed"])
                self.assertNotIn("refund_before", state["defaults"])

    def test_ambiguous_multiple_values_do_not_select_a_date(self):
        for text in ("查O001，截止2026年11月3日零点或2026年11月4日零点",
                     "查O001，截止2026年11月3日零点或者最近一次"):
            with self.subTest(text=text):
                state = self.state(text)
                self.assertTrue(state["needs_clarification"])
                self.assertNotIn("refund_before", state["confirmed"])
                self.assertEqual(state["defaults"], {})

    def test_generic_update_cannot_hide_behind_a_previous_clause(self):
        old = self.state("查O001，退款截止2026年11月3日零点", turn="old")
        state = self.state("查O001，改成最近一次", old)
        self.assertTrue(state["needs_clarification"])
        self.assertNotIn("refund_before", state["confirmed"])
        self.assertFalse(self.state("再查", state)["ready"])
        fixed = self.state("退款截止改为2026年11月4日零点", state)
        self.assertTrue(fixed["ready"], fixed)

    def test_blocked_policy_date_is_not_replaced_by_default_next_turn(self):
        old = self.state("解释2026年11月1日的退款政策", turn="old")
        state = self.state("政策日期改成最近一次", old)
        self.assertTrue(state["needs_clarification"])
        self.assertNotIn("policy_as_of", state["confirmed"])
        self.assertFalse(self.state("再查", state)["ready"])
        fixed = self.state("政策日期改为2026年11月4日", state)
        self.assertTrue(fixed["ready"], fixed)

    def test_order_id_repeated_does_not_unblock_a_bad_cutoff(self):
        bad = self.state("查O001，把退款截止改为最近一次")
        repeated = self.state("再查O001", bad)
        self.assertTrue(repeated["needs_clarification"])
        self.assertEqual(repeated["defaults"], {})
        self.assertTrue(self.state("退款截止现在", repeated)["ready"])

    def test_clear_dates_clock_and_temporary_preferences_keep_working(self):
        for text in ("查O001，退款截止现在", "查O001，退款截止当前时刻",
                     "查O001，退款截止2026年11月3日零点。这次金额用分。",
                     "汇总支付开始2026年10月1日零点，支付结束2026年11月1日零点，退款截止现在",
                     "汇总2026年10月1日零点到11月1日零点支付的订单，退款截止2026年11月3日零点"):
            with self.subTest(text=text):
                state = self.state(text)
                self.assertTrue(state["ready"], state)
                for item in state["confirmed"].values():
                    start, end = item["source"]["span"]
                    self.assertEqual(text[start:end], item["source"]["quote"])

    def test_hyphen_date_explicit_midnight_and_continuation(self):
        for text in ("核查 O_DEMO_002，退款截止 2026-11-03 零点",
                     "列出本店最近2笔订单并逐笔核查，退款截止2026-11-03零点"):
            state=self.state(text)
            self.assertTrue(state["ready"],state)
            item=state["confirmed"]["refund_before"]
            self.assertEqual(item["value"],"2026-11-03T00:00:00+08:00")
            start,end=item["source"]["span"]
            self.assertEqual(text[start:end],item["source"]["quote"])
            self.assertIn("零点",item["source"]["quote"])
        old=self.state("核查 O_DEMO_002，退款截止最近一次")
        fixed=self.state("退款截止2026-11-03 零点",old)
        self.assertTrue(fixed["ready"],fixed)
        self.assertEqual(fixed["confirmed"]["order_id"]["value"],"O_DEMO_002")
        for bad in ("2026-11-03", "2026-02-30 零点", "2026-11-03 零点半"):
            self.assertFalse(self.state("查O001，退款截止"+bad)["ready"])


if __name__ == "__main__":
    unittest.main()
