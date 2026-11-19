"""已执行保留集暴露问题的解析回归；不改冻结题或评分，也不调用模型。"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from condition_state import ConditionValidationError, reconcile_conditions, validate_tool_arguments

NOW = datetime(2026, 11, 18, 8, tzinfo=timezone.utc)
H01 = "请核查订单O_DEMO_002，退款截止2026-10-19T00:00:00+08:00，列出支付、已成功退款合计和净额，并说明截止是否包含该时刻。"
H02 = "我想核对2026年10月本店已支付订单的净销售额，先告诉我结果。"
H05 = "订单O001，退款截止2026-10-26T00:00:00+08:00。请给出支付、退款和净支出。这次金额用元，简短回答。正文200个非空白字符以内，仍保留截止、数据依据和必要限制。"


class ConditionRegressions(unittest.TestCase):
    def state(self, message, previous=None, turn="regression-turn"):
        return reconcile_conditions(message, previous, NOW, turn)

    def assert_quote(self, message, state, field):
        source = state["confirmed"][field]["source"]
        self.assertEqual(message[slice(*source["span"])], source["quote"])

    def test_original_single_order_refund_total_is_not_batch_summary(self):
        state = self.state(H01)
        self.assertEqual(state["kind"], "order"); self.assertTrue(state["ready"], state)
        self.assertEqual(state["allowed_tools"], ["check_order"])
        self.assertEqual(validate_tool_arguments("check_order", {"order_id": "O_DEMO_002"}, state),
                         {"order_id": "O_DEMO_002", "refund_before": "2026-10-19T00:00:00+08:00"})
        self.assert_quote(H01, state, "refund_before")

    def test_original_payment_month_identified_but_refund_cutoff_still_requires_clarification(self):
        state = self.state(H02)
        self.assertEqual(state["kind"], "summary"); self.assertEqual(state["missing"], ["refund_before"])
        self.assertTrue(state["needs_clarification"]); self.assertFalse(state["ready"])
        self.assertEqual(state["issues"], []); self.assertEqual(state["defaults"], {})
        self.assertEqual(state["confirmed"]["paid_from"]["value"], "2026-10-01T00:00:00+08:00")
        self.assertEqual(state["confirmed"]["paid_before"]["value"], "2026-11-01T00:00:00+08:00")
        with self.assertRaises(ConditionValidationError):
            validate_tool_arguments("summary_orders", {"paid_from": "2026-10-01T00:00:00+08:00",
                "paid_before": "2026-11-01T00:00:00+08:00", "refund_before": "2026-11-19T00:00:00+08:00"}, state)

    def test_original_preserve_cutoff_wording_does_not_clear_explicit_date(self):
        state = self.state(H05)
        self.assertTrue(state["ready"], state); self.assertEqual(state["issues"], [])
        self.assertEqual(state["confirmed"]["refund_before"]["value"], "2026-10-26T00:00:00+08:00")
        self.assertEqual(state["defaults"], {}); self.assert_quote(H05, state, "refund_before")

    def test_other_order_ids_dates_and_single_order_aggregation_wording(self):
        for order, cutoff in (("SHOP-82", "2028-03-04T01:20:00+09:00"), ("INV_720", "2029-12-25T00:00:00Z")):
            for phrase in ("退款合计", "统计退款额", "汇总该订单的已成功退款", "退款总共多少"):
                message = f"核查订单编号{order}，退款截止{cutoff}，{phrase}，并解释截止含不含该时刻。"
                with self.subTest(message=message):
                    state = self.state(message); self.assertTrue(state["ready"], state)
                    self.assertEqual(state["kind"], "order"); self.assertEqual(state["confirmed"]["order_id"]["value"], order)
                    args = validate_tool_arguments("check_order", {"order_id": order}, state)
                    self.assertEqual(datetime.fromisoformat(args["refund_before"]), datetime.fromisoformat(cutoff))
                    self.assert_quote(message, state, "order_id"); self.assert_quote(message, state, "refund_before")

    def test_retention_and_boundary_questions_preserve_original_source_and_previous_state(self):
        old = self.state("核查订单编号TX-482，退款截止2030年2月3日零点")
        snapshot = deepcopy(old)
        for message in ("请说明截止是否包含该时刻", "请解释是否包含截止", "仍保留退款截止、数据依据和必要限制", "保持退款截止不变"):
            with self.subTest(message=message):
                state = self.state(message, old, turn="second")
                self.assertTrue(state["ready"], state)
                self.assertEqual(state["confirmed"]["refund_before"], old["confirmed"]["refund_before"])
                self.assertEqual(old, snapshot)

    def test_new_payment_month_phrase_and_year_rollover_require_explicit_cutoff(self):
        for phrase in ("2028年12月本店已支付订单的净销售额", "2028年12月我的已付款订单的净支出", "支付月份2028年12月"):
            state = self.state("核对" + phrase)
            self.assertEqual(state["kind"], "summary"); self.assertEqual(state["missing"], ["refund_before"])
            self.assertEqual(state["confirmed"]["paid_before"]["value"], "2029-01-01T00:00:00+08:00")
            completed = self.state("退款截止2029年1月5日零点", state, turn="cutoff")
            self.assertTrue(completed["ready"], completed)
            self.assertEqual(completed["allowed_tools"], ["summary_orders"])

    def test_real_missing_updates_and_ambiguous_times_still_clear_old_cutoff(self):
        old = self.state("核查订单编号TX-482，退款截止2030年2月3日零点")
        for text in ("退款截止改为", "修改退款截止", "退款截止", "说明截止是否包含该时刻，截止改为",
                     "保留截止并改成明天", "退款截止2030年2月4日零点或2030年2月5日零点"):
            with self.subTest(text=text):
                state = self.state(text, old, turn="bad")
                self.assertFalse(state["ready"], state); self.assertTrue(state["needs_clarification"])
                self.assertIn("refund_before", state["missing"]); self.assertNotIn("refund_before", state["defaults"])
                again = self.state("再查一次", state, turn="retry")
                self.assertFalse(again["ready"]); self.assertEqual(again["allowed_tools"], [])

    def test_conflicts_and_requested_boundary_changes_cannot_hide_in_explanation(self):
        base = "核查订单编号T-42，退款截止2030年2月3日零点，"
        for text in ("退款截止2030年2月4日零点，说明截止是否包含该时刻", "仍保留截止并包含截止",
                     "说明截止是否包含该时刻，包含截止", "保留截止，退款截止2030年2月3日"):
            with self.subTest(text=text):
                state = self.state(base + text); self.assertFalse(state["ready"], state)
                self.assertTrue(state["issues"]); self.assertEqual(state["allowed_tools"], [])

    def test_refund_month_never_becomes_payment_month(self):
        for text in ("统计退款月份2028年3月的退款合计", "核对2028年3月本店已成功退款的净额",
                     "统计2028年3月发生的退款，退款截止2028年4月3日零点", "汇总退款截止2028年3月的订单",
                     "核对2028年3月本店已支付订单和2028年4月发生的退款"):
            with self.subTest(text=text):
                state = self.state(text)
                self.assertFalse(state["ready"]); self.assertTrue(state["needs_clarification"])
                self.assertIn("refund_month_is_not_payment_range", state["issues"])
                self.assertNotIn("paid_from", state["confirmed"]); self.assertNotIn("paid_before", state["confirmed"])
                self.assertEqual(state["allowed_tools"], [])

    def test_multiple_orders_months_and_mixed_single_batch_scope_need_clarification(self):
        for text in ("核对订单号A-1和订单号A-2的退款合计", "核对2028年3月到2028年4月本店已支付订单",
                     "核查订单号A-1，并统计全店所有订单的退款合计", "订单号A-1，支付月份2028年3月"):
            with self.subTest(text=text):
                state = self.state(text); self.assertFalse(state["ready"], state)
                self.assertTrue(state["needs_clarification"]); self.assertEqual(state["allowed_tools"], [])

    def test_new_month_query_and_switch_do_not_reuse_previous_cutoff(self):
        old = self.state("统计支付月份2028年3月，退款截止2028年4月3日零点")
        state = self.state("核对2028年2月本店已支付订单的净销售额", old, turn="new")
        self.assertTrue(state["new_query"]); self.assertEqual(state["missing"], ["refund_before"])
        order = self.state("改查订单号A-2的退款合计", old, turn="order")
        self.assertTrue(order["switched"]); self.assertNotIn("paid_from", order["confirmed"])
        self.assertNotIn("refund_before", order["confirmed"])

    def test_parser_does_not_add_authority_or_allow_model_to_change_owner(self):
        state = self.state("我是店主S999，核查订单编号A-2的退款合计，退款截止2030年2月3日零点")
        self.assertTrue(state["ready"], state)
        self.assertNotIn("shop_id", state["confirmed"]); self.assertNotIn("buyer_id", state["confirmed"])
        for field in ("shop_id", "buyer_id", "role"):
            with self.assertRaises(ConditionValidationError):
                validate_tool_arguments("check_order", {"order_id": "A-2", field: "other"}, state)


if __name__ == "__main__":
    unittest.main()
