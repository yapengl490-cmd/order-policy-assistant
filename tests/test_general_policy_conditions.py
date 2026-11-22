"""Independent offline checks for general-policy versus order routing."""
from datetime import datetime, timedelta
import unittest

from general_policy_conditions import uses_general_policy, reconcile_general_policy


NOW = datetime.fromisoformat("2026-11-20T12:00:00+08:00")


class GeneralPolicyConditionTests(unittest.TestCase):
    def state(self, message, previous=None, now=NOW):
        return reconcile_general_policy(message, previous, now, "policy-condition-test")

    def test_general_question_can_query_coverage_without_order_or_assumed_category(self):
        message = "本店退货规则是什么？"
        self.assertTrue(uses_general_policy(message, None))
        state = self.state(message)
        self.assertEqual(state["kind"], "general_policy")
        self.assertTrue(state["ready"])
        self.assertNotIn("category", state["confirmed"])
        self.assertNotIn("order_id", state["missing"])
        self.assertEqual(state["allowed_tools"], ["lookup_general_policy"])

    def test_category_followup_is_explicit_and_keeps_query_time(self):
        first = self.state("本店退货政策，政策日期2026-10-15T00:00:00+08:00")
        self.assertTrue(uses_general_policy("普通鞋类", first))
        second = self.state("普通鞋类", first)
        self.assertEqual(second["confirmed"]["category"]["value"], "ordinary_shoes")
        self.assertEqual(second["confirmed"]["policy_at"], first["confirmed"]["policy_at"])
        source = second["confirmed"]["category"]["source"]
        self.assertEqual("普通鞋类"[slice(*source["span"])], source["quote"])

    def test_tshirt_is_not_rewritten_to_supported_shoe_category(self):
        state = self.state("本店T恤退货政策")
        self.assertEqual(state["confirmed"]["category"]["value"], "t_shirt")

    def test_explicit_orders_and_order_pronouns_leave_general_mode(self):
        previous = self.state("普通鞋类退货政策")
        for message in ("O_SUPPORT_OK能退货吗", "订单号O001能退吗", "我的订单可以退吗", "这单能退吗", "那单能退吗", "本单运费谁出"):
            with self.subTest(message=message):
                self.assertFalse(uses_general_policy(message, previous))

    def test_recommendation_and_amount_queries_leave_general_mode(self):
        previous = self.state("普通鞋类退货政策")
        for message in ("推荐M码T恤", "查支付金额", "汇总订单净额"):
            self.assertFalse(uses_general_policy(message, previous))

    def test_invalid_time_survives_category_only_followup(self):
        first = self.state("本店退货政策，政策日期2026-11-31")
        self.assertFalse(first["ready"])
        second = self.state("普通鞋类", first)
        self.assertFalse(second["ready"])
        self.assertIn("invalid_policy_time", second["issues"])
        self.assertNotIn("policy_at", second["defaults"])

    def test_correcting_policy_time_recovers_without_changing_category(self):
        first = self.state("普通鞋类退货政策，政策日期2026-11-31")
        second = self.state("政策日期2026-11-20T00:00:00+08:00", first)
        self.assertTrue(second["ready"])
        self.assertEqual(second["confirmed"]["category"]["value"], "ordinary_shoes")

    def test_reset_and_expiry_drop_old_category(self):
        previous = self.state("普通鞋类退货政策")
        for message, now in (("清空查询", NOW), ("本店退货政策", NOW + timedelta(hours=1))):
            with self.subTest(message=message):
                state = self.state(message, previous, now)
                self.assertNotIn("category", state["confirmed"])

    def test_unknown_topics_are_not_replaced_by_return_eligibility(self):
        for message, expected in (("鞋类换货政策", "exchange"), ("鞋类退款到账政策", "arrival"), ("本店鞋类保修政策", "warranty")):
            with self.subTest(message=message):
                state = self.state(message)
                self.assertIn(expected, state["topics"])

    def test_negated_unknown_topic_does_not_block_supported_request(self):
        state = self.state("普通鞋类政策，不问换货，只问退货运费规则")
        self.assertNotIn("exchange", state["topics"])
        self.assertIn("shipping", state["topics"])

    def test_quoted_order_does_not_route_as_current_order(self):
        self.assertTrue(uses_general_policy('不是问“O_SUPPORT_OK”，想看本店鞋类一般退货政策', None))


if __name__ == "__main__":
    unittest.main()
