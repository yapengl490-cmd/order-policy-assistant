"""纯解析/校验测试：不读取配置、不写任务文件、不调用模型或数据库。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from condition_state import (ConditionValidationError, public_condition_state,
                             reconcile_conditions, validate_tool_arguments)

NOW = datetime(2026, 11, 16, 10, 30, tzinfo=timezone.utc)


class ConditionStateTests(unittest.TestCase):
    def state(self, text, previous=None, turn="t1", now=NOW):
        return reconcile_conditions(text, previous, now, turn)

    def summary(self):
        return self.state("统计支付月份2026年10月，退款截止北京时间2026年11月3日零点")

    def assert_rejected(self, name, arguments, state, code=None):
        with self.assertRaises(ConditionValidationError) as caught:
            validate_tool_arguments(name, arguments, state)
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_single_order_default_is_program_time_not_user_confirmation(self):
        state = self.state("核查订单O001")
        self.assertTrue(state["ready"])
        self.assertNotIn("refund_before", state["confirmed"])
        default = state["defaults"]["refund_before"]
        self.assertEqual(default["value"], "2026-11-16T18:30:00+08:00")
        self.assertIsNone(default["source"]["quote"])
        args = validate_tool_arguments("check_order", {"order_id": "O001"}, state)
        self.assertEqual(args["refund_before"], default["value"])
        self.assert_rejected("check_order", {"order_id": "O001", "refund_before": "2026-11-03T00:00:00+08:00"}, state, "condition_mismatch")

    def test_order_cutoff_exact_quote_and_provenance(self):
        text = "核查订单O001，退款截止北京时间2026年11月3日零点"
        state = self.state(text)
        cutoff = state["confirmed"]["refund_before"]
        self.assertEqual(cutoff["value"], "2026-11-03T00:00:00+08:00")
        self.assertEqual(cutoff["source"]["turn_id"], "t1")
        start, end = cutoff["source"]["span"]
        self.assertEqual(text[start:end], cutoff["source"]["quote"])
        self.assertEqual(state["defaults"], {})
        # 模型遗漏可选截止也不能退回当前时间。
        self.assertEqual(validate_tool_arguments("check_order", {"order_id": "O001", "refund_before": None}, state)["refund_before"], cutoff["value"])

    def test_wrong_order_and_identity_fields_never_pass(self):
        state = self.state("订单号ABC-17，退款截止2026-11-03T00:00:00+08:00")
        self.assertEqual(state["confirmed"]["order_id"]["value"], "ABC-17")
        self.assert_rejected("check_order", {"order_id": "OTHER"}, state)
        self.assert_rejected("check_order", {"order_id": "ABC-17", "buyer_id": "B999"}, state, "unexpected_arguments")

    def test_equivalent_iso_offset_is_allowed_but_naive_model_time_is_not(self):
        state = self.state("查O001，截止2026-11-03T00:00:00+08:00")
        args = validate_tool_arguments("check_order", {"order_id": "O001", "refund_before": "2026-11-02T16:00:00Z"}, state)
        self.assertEqual(args["refund_before"], "2026-11-03T00:00:00+08:00")
        self.assert_rejected("check_order", {"order_id": "O001", "refund_before": "2026-11-03T00:00:00"}, state)

    def test_summary_month_expansion_does_not_invent_refund_cutoff(self):
        state = self.state("统计2026年10月支付的订单")
        self.assertEqual(state["confirmed"]["paid_from"]["value"], "2026-10-01T00:00:00+08:00")
        self.assertEqual(state["confirmed"]["paid_before"]["value"], "2026-11-01T00:00:00+08:00")
        self.assertEqual(state["missing"], ["refund_before"])
        self.assertEqual(state["defaults"], {})
        self.assert_rejected("summary_orders", {"paid_from": "2026-10-01T00:00:00+08:00", "paid_before": "2026-11-01T00:00:00+08:00", "refund_before": "2026-11-03T00:00:00+08:00"}, state, "conditions_not_ready")

    def test_bare_followup_fills_only_missing_field(self):
        old = self.state("统计2026年10月订单")
        state = self.state("2026年11月3日零点", old, turn="t2")
        self.assertTrue(state["ready"], state)
        self.assertIn("only_missing_field", state["confirmed"]["refund_before"]["source"]["rule"])
        self.assertEqual(state["confirmed"]["paid_from"]["source"]["turn_id"], "t1")
        self.assertEqual(state["confirmed"]["refund_before"]["source"]["turn_id"], "t2")

    def test_complete_summary_rejects_fabricated_but_legal_date(self):
        state = self.summary()
        self.assertTrue(state["ready"], state)
        args = {field: state["confirmed"][field]["value"] for field in ("paid_from", "paid_before", "refund_before")}
        self.assertEqual(validate_tool_arguments("summary_orders", args, state), args)
        args["refund_before"] = "2026-12-01T00:00:00+08:00"
        self.assert_rejected("summary_orders", args, state, "condition_mismatch")

    def test_explicit_three_iso_fields_and_month_year_rollover(self):
        state = self.state("汇总 paid_from=2026-10-01T00:00:00+08:00; paid_before=2026-11-01T00:00:00+08:00; refund_before=2026-11-03T00:00:00+08:00")
        self.assertTrue(state["ready"], state)
        state = self.state("统计支付月份2026年12月，退款截止2027年1月3日零点")
        self.assertEqual(state["confirmed"]["paid_before"]["value"], "2027-01-01T00:00:00+08:00")
        self.assertTrue(state["ready"])

    def test_followup_order_cutoff_updates_without_mutating_previous(self):
        old = self.state("查O001，截止2026年11月3日零点")
        snapshot = deepcopy(old)
        state = self.state("改成2026年11月1日零点", old, turn="t2")
        self.assertEqual(old, snapshot)
        self.assertTrue(state["ready"])
        self.assertEqual(state["confirmed"]["refund_before"]["value"], "2026-11-01T00:00:00+08:00")
        self.assertEqual(state["confirmed"]["order_id"]["source"]["turn_id"], "t1")
        self.assertEqual(state["changed"][0]["before"], "2026-11-03T00:00:00+08:00")

    def test_summary_unlabelled_override_needs_unique_recent_target(self):
        old = self.summary()
        ambiguous = self.state("改成2026年11月5日零点", old, turn="t2")
        self.assertIn("ambiguous_date_target", ambiguous["issues"])
        self.assertFalse(ambiguous["ready"])
        explicit = self.state("退款截止改为2026年11月5日零点", old, turn="t2")
        self.assertTrue(explicit["ready"])
        followup = self.state("改成2026年11月6日零点", explicit, turn="t3")
        self.assertTrue(followup["ready"])
        self.assertIn("previous_single_updated_field", followup["confirmed"]["refund_before"]["source"]["rule"])

    def test_month_update_retains_confirmed_cutoff_and_checks_range(self):
        old = self.summary()
        state = self.state("支付月份改成2026年9月", old, turn="t2")
        self.assertTrue(state["ready"])
        self.assertEqual(state["confirmed"]["refund_before"], old["confirmed"]["refund_before"])
        state = self.state("支付月份改成2026年12月", old, turn="t2")
        self.assertIn("invalid_time_range", state["issues"])
        self.assertFalse(state["ready"])

    def test_switching_business_drops_previous_scope(self):
        old = self.summary()
        order = self.state("改查订单O_DEMO_002", old, turn="t2")
        self.assertTrue(order["switched"])
        self.assertNotIn("paid_from", order["confirmed"])
        self.assertNotIn("refund_before", order["confirmed"])
        self.assertEqual(order["confirmed"]["order_id"]["value"], "O_DEMO_002")
        summary = self.state("统计2026年10月订单", order, turn="t3")
        self.assertNotIn("order_id", summary["confirmed"])
        self.assertEqual(summary["missing"], ["refund_before"])

    def test_new_full_query_does_not_inherit_old_task_dates(self):
        old = self.summary()
        state = self.state("统计2026年9月订单", old, turn="t2")
        self.assertTrue(state["new_query"])
        self.assertEqual(state["missing"], ["refund_before"])
        state = self.state("统计上个月订单", old, turn="t2")
        self.assertFalse(state["ready"])
        self.assertNotIn("paid_from", state["confirmed"])
        order = self.state("查O001，截止2026年11月3日零点")
        new_order = self.state("核查O_DEMO_002", order, turn="t2")
        self.assertNotIn("refund_before", new_order["confirmed"])
        self.assertIn("refund_before", new_order["defaults"])
        retained = self.state("改查O_DEMO_002，其他条件不变", order, turn="t2")
        self.assertEqual(retained["confirmed"]["refund_before"], order["confirmed"]["refund_before"])

    def test_reset_and_expiry_cannot_reuse_old_order(self):
        old = self.state("查O001，截止2026年11月3日零点")
        state = self.state("重置，查订单", old, turn="t2")
        self.assertTrue(state["reset"])
        self.assertEqual(state["missing"], ["order_id"])
        expired = self.state("截止改为2026年11月1日零点", old, turn="t2", now=NOW + timedelta(minutes=30))
        self.assertTrue(expired["expired"])
        self.assertFalse(expired["ready"])
        self.assertNotIn("order_id", expired["confirmed"])

    def test_order_and_policy_can_combine(self):
        state = self.state("查O001截止2026年11月3日零点，并解释演示退款政策")
        self.assertEqual(state["kind"], "order")
        self.assertIn("check_order", state["allowed_tools"])
        self.assertIn("search_policies", state["allowed_tools"])
        args = validate_tool_arguments("search_policies", {"query": "演示退款政策"}, state)
        self.assertEqual(args["as_of"], "2026-11-16")
        self.assert_rejected("search_policies", {"query": "退款", "as_of": "2020-01-01"}, state)

    def test_plain_business_queries_do_not_expose_policy_tool(self):
        samples = [("核查O001，退款截止2026年11月3日零点", ["check_order"]),
                   ("统计2026年10月订单，退款截止2026年11月3日零点", ["summary_orders"]),
                   ("列出最近订单", ["list_orders"]),
                   ("查询订单", ["list_orders"])]
        for text, tools in samples:
            with self.subTest(text=text):
                state = self.state(text)
                self.assertEqual(state["allowed_tools"], tools)
                self.assert_rejected("search_policies", {"query": "退款"}, state, "conditions_not_ready")

    def test_explicit_policy_rules_or_support_request_can_add_policy_tool(self):
        for text in ("核查O001并解释退款规则", "核查O001并查售后条款", "列出最近订单并解释查询政策",
                     "统计2026年10月订单，退款截止2026年11月3日零点，并解释退款政策"):
            with self.subTest(text=text):
                self.assertIn("search_policies", self.state(text)["allowed_tools"])
        self.assertEqual(self.state("请解释演示售后规则")["allowed_tools"], ["search_policies"])

    def test_policy_explicit_date_and_switch_from_order(self):
        state = self.state("查询2030-01-01的退款政策")
        self.assertTrue(state["ready"], state)
        self.assertEqual(state["kind"], "general_policy")
        self.assertEqual(state["allowed_tools"], ["lookup_general_policy"])
        self.assertEqual(state["confirmed"]["policy_at"]["value"], "2030-01-01T00:00:00+08:00")
        self.assertNotIn("order_id", state["missing"])
        old = self.state("查O001")
        policy = self.state("解释退款截止政策", old, turn="t2")
        self.assertEqual(policy["kind"], "policy")
        self.assertNotIn("check_order", policy["allowed_tools"])

    def test_missing_order_permits_selection_only(self):
        state = self.state("查询订单")
        self.assertEqual(state["missing"], ["order_id"])
        self.assertEqual(validate_tool_arguments("list_orders", {"limit": 3}, state), {"limit": 3})
        self.assert_rejected("check_order", {"order_id": "O001"}, state)
        self.assert_rejected("list_orders", {"limit": True}, state)

    def test_invalid_calendar_date_and_conflicts_do_not_fall_back_to_now(self):
        for text in ("查O001，截止2026年2月30日零点", "查O001，截止2026年11月3日", "查O001，截止2026年11月3日零点，截止2026年11月4日零点"):
            with self.subTest(text=text):
                state = self.state(text)
                self.assertFalse(state["ready"])
                self.assertIn("refund_before", state["missing"])
                self.assertEqual(state["defaults"], {})
                followup = self.state("再查一次", state, turn="t2")
                self.assertFalse(followup["ready"])
                self.assertIn("refund_before", followup["missing"])

    def test_unsupported_update_cannot_silently_reuse_old_value(self):
        old = self.state("查O001，截止2026年11月3日零点")
        for text in ("改成昨天", "截止11月", "改成11月1日零点", "截止2026年11月1日下午3点", "截止2026-11-01T00:00:00+0900", "截止2026年11月1日3点半", "截止2026年11月1日零点30分"):
            with self.subTest(text=text):
                bad = self.state(text, old, turn="t2")
                self.assertFalse(bad["ready"])
                self.assertTrue(bad["issues"])
                followup = self.state("再查一次", bad, turn="t3")
                self.assertFalse(followup["ready"])
                fixed = self.state("退款截止2026年11月1日零点", followup, turn="t4")
                self.assertTrue(fixed["ready"], fixed)

    def test_negative_and_boundary_ambiguity_are_not_ignored(self):
        for text in ("不要查订单O001", "查O001，截止2026年11月3日零点包含截止", "查O001，截止东京时间2026年11月3日零点"):
            with self.subTest(text=text):
                state = self.state(text)
                self.assertFalse(state["ready"])
                self.assertTrue(state["issues"])

    def test_explicit_now_is_user_request_not_batch_default(self):
        state = self.state("统计2026年10月订单，退款截至现在")
        self.assertTrue(state["ready"], state)
        self.assertEqual(state["confirmed"]["refund_before"]["source"]["quote"], "退款截至现在")
        self.assertEqual(state["defaults"], {})

    def test_multiple_orders_or_months_do_not_silently_pick_one(self):
        for text in ("查O001和O002", "统计2026年10月到2026年11月订单，退款截止2026年12月1日零点"):
            with self.subTest(text=text):
                state = self.state(text)
                self.assertFalse(state["ready"])
                self.assertTrue(state["issues"])

    def test_no_user_evidence_means_no_tool_execution(self):
        state = self.state("你好")
        self.assertFalse(state["ready"])
        self.assertEqual(state["confirmed"], {})
        self.assert_rejected("check_order", {"order_id": "O001"}, state)
        self.assert_rejected("summary_orders", {}, state)

    def test_public_state_is_detached_and_contains_no_identity(self):
        state = self.summary()
        public = public_condition_state(state)
        self.assertNotIn("original_question", public)
        self.assertNotIn("buyer_id", public)
        self.assertNotIn("shop_id", public)
        public["confirmed"].clear()
        self.assertTrue(state["confirmed"])

    def test_rejects_invalid_program_inputs(self):
        with self.assertRaises(ValueError):
            self.state("查O001", now=datetime(2026, 11, 16))
        with self.assertRaises(ValueError):
            self.state("查O001", previous={"schema_version": 999, "kind": "order"})
        with self.assertRaises(ValueError):
            self.state(" ")

    def test_frozen_six_task_inputs_keep_expected_conditions(self):
        path = Path(__file__).resolve().parents[1] / "evaluation" / "task_cases.json"
        cases = json.loads(path.read_text())["cases"]
        self.assertEqual(len(cases), 6)
        previous = {}
        for case in cases:
            with self.subTest(case=case["id"]):
                parent = case.get("sequence", {}).get("after")
                state = self.state(case["input"]["message"], previous.get(parent), turn=case["id"])
                previous[case["id"]] = state
                self.assertEqual(state["issues"], [])
                if case["id"] == "summary_missing_cutoff":
                    self.assertEqual(state["missing"], ["refund_before"])
                    self.assertFalse(state["ready"])
                    self.assertTrue(state["needs_clarification"])
                else:
                    self.assertTrue(state["ready"], state)
                if case["id"] == "merchant_outside_shop":
                    self.assertEqual(state["allowed_tools"], ["check_order"])
                if case["id"] == "policy_insufficient_eligibility":
                    # 原冻结题/旧失败保留；R06已新增订单售后工具，当前路由应读订单与支付版政策。
                    self.assertIn("review_after_sales", state["allowed_tools"])
                for fact in case["private_expectations"]["facts"]:
                    for field, value in fact.get("arguments", {}).items():
                        self.assertEqual(state["confirmed"][field]["value"], value)

    def test_payment_range_inherits_year_only_from_same_range_source(self):
        text = "汇总我北京时间2026年10月1日零点到11月1日零点前支付的订单。"
        state = self.state(text)
        self.assertEqual(state["missing"], ["refund_before"])
        self.assertEqual(state["confirmed"]["paid_before"]["value"], "2026-11-01T00:00:00+08:00")
        for field in ("paid_from", "paid_before"):
            source = state["confirmed"][field]["source"]
            self.assertIn("end_year_from_same_range_start", source["rule"])
            self.assertEqual(source["quote"], "2026年10月1日零点到11月1日零点")
            start, end = source["span"]
            self.assertEqual(source["quote"], text[start:end])

    def test_range_does_not_silently_invent_next_year(self):
        implicit = self.state("汇总2026年12月1日零点到1月1日零点前支付的订单，退款截止2027年1月3日零点")
        self.assertFalse(implicit["ready"])
        self.assertIn("invalid_or_ambiguous_payment_range", implicit["issues"])
        self.assertNotIn("paid_before", implicit["confirmed"])
        explicit = self.state("汇总2026年12月1日零点到2027年1月1日零点前支付的订单，退款截止2027年1月3日零点")
        self.assertTrue(explicit["ready"], explicit)
        self.assertEqual(explicit["confirmed"]["paid_before"]["value"], "2027-01-01T00:00:00+08:00")

    def test_range_endpoint_clock_cannot_be_partially_consumed(self):
        for endpoint in ("11月1日3点半", "11月1日零点30分", "11月1日3点30分15秒", "11月1日零点+0900"):
            with self.subTest(endpoint=endpoint):
                state = self.state(f"汇总2026年10月1日零点到{endpoint}支付的订单，退款截止2026年11月3日零点")
                self.assertFalse(state["ready"])
                self.assertNotIn("paid_before", state["confirmed"])

    def test_unknown_named_timezones_are_never_interpreted_as_beijing(self):
        for zone in ("日本时间", "日本标准时", "火星时间", "东京时间", "纽约时间", "America/New_York", "Pacific/Auckland", "PST", "JST", "XYZ", "XYZ time ", "UTC+09:00", "GMT", "XYZ时区"):
            with self.subTest(zone=zone):
                state = self.state(f"核查O001，退款截止{zone}2026年11月3日零点")
                self.assertFalse(state["ready"])
                self.assertIn("unsupported_timezone_label_use_iso_offset", state["issues"])
                self.assertNotIn("refund_before", state["confirmed"])
        for zone in ("北京时间", "中国标准时间", "中国时间", "Asia/Shanghai", "Asia/Shanghai时区", "时区：Asia/Shanghai"):
            with self.subTest(zone=zone):
                self.assertTrue(self.state(f"核查O001，退款截止{zone}2026年11月3日零点")["ready"])
        offset = self.state("核查O001，退款截止2026-11-03T00:00:00+09:00")
        self.assertTrue(offset["ready"])
        self.assertEqual(offset["confirmed"]["refund_before"]["value"], "2026-11-03T00:00:00+09:00")

    def test_unparsed_extra_date_does_not_hide_behind_a_complete_date(self):
        state = self.state("核查O001，退款截止2026年11月3日零点，排除11月2日退款")
        self.assertFalse(state["ready"])
        self.assertIn("date_year_required", state["issues"])
        self.assertNotIn("refund_before", state["confirmed"])

    def test_ordinary_chat_does_not_enable_tools_or_change_confirmed_values(self):
        original = self.state("核查O001，退款截止2026年11月3日零点")
        chat = self.state("好的谢谢", original, turn="t2")
        self.assertEqual(chat["confirmed"], original["confirmed"])
        self.assertFalse(chat["active"])
        self.assertFalse(chat["ready"])
        self.assertEqual(chat["allowed_tools"], [])
        update = self.state("改成2026年11月1日零点", chat, turn="t3")
        self.assertTrue(update["ready"])
        self.assertEqual(update["confirmed"]["order_id"]["value"], "O001")
        new_query = self.state("统计2026年10月订单", chat, turn="t3")
        self.assertEqual(new_query["missing"], ["refund_before"])
        self.assertNotIn("order_id", new_query["confirmed"])


if __name__ == "__main__":
    unittest.main()
