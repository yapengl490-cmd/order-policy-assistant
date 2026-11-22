from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from catalog_conditions import uses_catalog, reconcile_catalog, catalog_requirements, validate_catalog_arguments
from catalog_recommendations import CatalogScope, CatalogSnapshot, recommend


class CatalogConditionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 20, 4, tzinfo=timezone.utc)
        self.base_message = "给我推荐一件M码纯棉T恤，预算不超过30美元。"
        self.base = self.parse(self.base_message)

    def parse(self, message, previous=None, **kwargs):
        return reconcile_catalog(message, previous, kwargs.get("now", self.now), kwargs.get("turn_id", "turn1"))

    def test_common_request_uses_exact_minor_currency_units(self):
        self.assertTrue(self.base["ready"], self.base)
        self.assertEqual(catalog_requirements(self.base), {"category": "t_shirt", "currency": "USD", "budget_max_minor": 3000,
                                                         "material": "cotton", "cotton_percentage": 100, "size": "M"})
        self.assertEqual(self.base["allowed_tools"], ["recommend_products"])

    def test_ordinary_wording_variants(self):
        for message in ("推荐M码纯棉T恤，30美元以内", "推荐T-shirt，cotton，M，预算USD 30", "推荐T恤，预算30人民币，材质棉，尺码M", "推荐T恤，预算不要超过30美元"):
            with self.subTest(message=message):
                self.assertTrue(self.parse(message)["ready"], self.parse(message))

    def test_only_missing_essentials_need_clarification(self):
        state = self.parse("推荐M码纯棉T恤")
        self.assertEqual(state["missing"], ["currency", "budget_max_minor"])
        self.assertTrue(state["needs_clarification"])
        self.assertEqual(state["allowed_tools"], [])
        self.assertTrue(self.parse("推荐T恤，预算30美元")["ready"])

    def test_followup_adds_budget_without_losing_size_and_material(self):
        state = self.parse("推荐M码纯棉T恤")
        updated = self.parse("预算30美元", state, turn_id="turn2")
        self.assertTrue(updated["ready"], updated)
        self.assertEqual(updated["confirmed"]["size"]["source"]["turn_id"], "turn1")
        self.assertEqual(updated["confirmed"]["budget_max_minor"]["source"]["turn_id"], "turn2")

    def test_missing_currency_keeps_unconverted_budget_then_converts(self):
        state = self.parse("推荐M码纯棉T恤，预算30")
        self.assertNotIn("budget_max_minor", state["confirmed"])
        self.assertEqual(state["pending_budget"]["number"], "30")
        updated = self.parse("美元", state, turn_id="turn2")
        self.assertTrue(updated["ready"], updated)
        self.assertEqual(updated["confirmed"]["budget_max_minor"]["value"], 3000)
        self.assertEqual(updated["confirmed"]["budget_max_minor"]["source"]["turn_id"], "turn1")
        self.assertEqual(updated["confirmed"]["budget_max_minor"]["source"]["currency_source_turn"], "turn2")

    def test_explicit_changes_preserve_other_conditions(self):
        updated = self.parse("预算改为25美元，尺码改成L", self.base, turn_id="turn2")
        self.assertTrue(updated["ready"], updated)
        self.assertEqual(catalog_requirements(updated)["budget_max_minor"], 2500)
        self.assertEqual(catalog_requirements(updated)["size"], "L")
        self.assertEqual(catalog_requirements(updated)["material"], "cotton")

    def test_explicit_old_to_new_correction(self):
        updated = self.parse("预算从30美元改成25美元", self.base)
        self.assertTrue(updated["ready"], updated)
        self.assertEqual(catalog_requirements(updated)["budget_max_minor"], 2500)

    def test_changed_currency_requires_new_budget(self):
        updated = self.parse("币种改为人民币", self.base)
        self.assertFalse(updated["ready"])
        self.assertIn("budget_max_minor", updated["missing"])
        fixed = self.parse("预算200", updated)
        self.assertTrue(fixed["ready"], fixed)
        self.assertEqual(catalog_requirements(fixed)["budget_max_minor"], 20000)

    def test_expired_context_does_not_supply_missing_fields(self):
        updated = self.parse("预算改为25美元", self.base, now=self.now + timedelta(minutes=30))
        self.assertTrue(updated["expired"])
        self.assertIn("category", updated["missing"])
        self.assertNotIn("size", updated["confirmed"])

    def test_new_query_and_reset_do_not_silently_reuse_fields(self):
        for message in ("重新推荐T恤", "重置条件，推荐T恤"):
            with self.subTest(message=message):
                state = self.parse(message, self.base)
                self.assertFalse(state["ready"])
                self.assertNotIn("size", state["confirmed"])
                self.assertNotIn("currency", state["confirmed"])

    def test_other_business_state_never_supplies_shopping_fields(self):
        prior = {"kind": "support", "schema_version": 1, "revision": 9,
                 "confirmed": {"currency": {"value": "USD"}, "order_id": {"value": "O001"}}}
        state = self.parse("推荐T恤", prior)
        self.assertTrue(state["switched"])
        self.assertIn("currency", state["missing"])
        self.assertNotIn("order_id", state["confirmed"])

    def test_order_query_leaves_catalog_routing(self):
        for message in ("我的订单O001能退吗", "查询订单O001净支出", "不要推荐，查订单", "统计支付月份2026年10月"):
            with self.subTest(message=message):
                self.assertFalse(uses_catalog(message, self.base))
        self.assertTrue(uses_catalog("推荐要能退的纯棉T恤", self.base))
        self.assertTrue(uses_catalog("我想买可以退的T恤", None))

    def test_unrelated_and_quoted_requests_do_not_reenter_catalog(self):
        for message in ("今天天气怎么样", "他说推荐T恤", '“推荐T恤，预算30美元”', "不要推荐", "假设推荐30美元T恤"):
            with self.subTest(message=message):
                self.assertFalse(uses_catalog(message, self.base))

    def test_quoted_and_hypothetical_values_do_not_become_requirements(self):
        state = self.parse('推荐T恤，“预算30美元”，如果尺码M')
        self.assertNotIn("currency", state["confirmed"])
        self.assertNotIn("size", state["confirmed"])
        self.assertFalse(state["ready"])

    def test_conflicting_fields_block_and_do_not_recover_from_old_values(self):
        for message, field in (("预算30美元或40美元", "budget_max_minor"), ("M码还是L码", "size"), ("纯棉或涤纶", "material"), ("USD还是CNY", "currency")):
            with self.subTest(message=message):
                state = self.parse(message, self.base)
                self.assertFalse(state["ready"])
                self.assertNotIn(field, state["confirmed"])
                self.assertFalse(self.parse("继续", state)["ready"])

    def test_specific_correction_resolves_conflicting_budget(self):
        state = self.parse("预算30美元或40美元", self.base)
        fixed = self.parse("预算改为25美元", state)
        self.assertTrue(fixed["ready"], fixed)

    def test_negated_material_is_not_positive_evidence(self):
        state = self.parse("不要纯棉", self.base)
        self.assertFalse(state["ready"])
        self.assertNotIn("material", state["confirmed"])

    def test_unknown_hard_attributes_are_not_ignored(self):
        for message in ("要红色", "要蓝色", "要耐克", "要长袖", "要透气", "要真丝", "我要可机洗", "我要纯棉且有魔术贴", "预算含运费30美元"):
            with self.subTest(message=message):
                state = self.parse(message, self.base)
                self.assertFalse(state["ready"], state)
                self.assertTrue(state["issues"])

    def test_unknown_color_persists_until_explicitly_cancelled(self):
        state = self.parse("要蓝色", self.base)
        self.assertFalse(self.parse("尺码改成L", state)["ready"])
        fixed = self.parse("取消颜色要求", state)
        self.assertTrue(fixed["ready"], fixed)

    def test_scope_requests_never_change_scope_or_reach_tool(self):
        for message in ("shop_id=DEMO-SHOP-B", "换另一家店", "查所有店铺"):
            with self.subTest(message=message):
                self.assertTrue(uses_catalog(message, self.base))
                state = self.parse(message, self.base)
                self.assertFalse(state["ready"])
                self.assertIn("scope_change_not_allowed", state["issues"])
                self.assertNotIn("shop_id", state["confirmed"])

    def test_return_requirement_is_kept_and_is_not_a_positive_policy_claim(self):
        state = self.parse("要能退", self.base)
        self.assertTrue(state["ready"], state)
        self.assertTrue(catalog_requirements(state)["require_returnable"])
        snapshot = CatalogSnapshot.load(ROOT / "samples/catalog_demo.json")
        result = recommend(snapshot, catalog_requirements(state), scope=CatalogScope(frozenset({"DEMO-SHOP-A"}), "US", "demo_storefront"), as_of=self.now)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["candidates"], [])
        self.assertIn("unknown_return_policy", result["excluded_counts"])
        self.assertIn("不能确认", result["policy_notice"])

    def test_recorded_three_turn_return_requirement_regression(self):
        # Development regression for preserved real failure fc8561e008c9523295148c72a47969aa.
        first = self.parse("推荐M码纯棉T恤，预算30", turn_id="recorded-turn-1")
        self.assertTrue(first["needs_clarification"])
        self.assertEqual(first["allowed_tools"], [])
        second = self.parse("USD", first, turn_id="recorded-turn-2", now=self.now + timedelta(minutes=1))
        self.assertTrue(second["ready"], second)
        previous_requirements = catalog_requirements(second)
        final = self.parse("还要求能退", second, turn_id="recorded-turn-3", now=self.now + timedelta(minutes=2))
        self.assertTrue(final["ready"], final)
        self.assertEqual(final["issues"], [])
        self.assertEqual(final["allowed_tools"], ["recommend_products"])
        self.assertEqual(catalog_requirements(final), previous_requirements | {"require_returnable": True})
        evidence = final["confirmed"]["require_returnable"]["source"]
        self.assertEqual(evidence["turn_id"], "recorded-turn-3")
        self.assertEqual(evidence["quote"], "要求能退")
        self.assertEqual(evidence["span"], [1, 5])
        self.assertEqual(final["source_message"], "还要求能退")
        self.assertNotIn("require_returnable", second["confirmed"])
        snapshot = CatalogSnapshot.load(ROOT / "samples/catalog_demo.json")
        fact = recommend(snapshot, catalog_requirements(final), scope=CatalogScope(frozenset({"DEMO-SHOP-A"}), "US", "demo_storefront"), as_of=self.now)
        self.assertEqual(fact["status"], "insufficient_evidence")
        self.assertEqual(fact["candidates"], [])
        self.assertIn("尚缺此商品适用的退货政策", fact["policy_notice"])

    def test_additive_prefix_is_scoped_to_recognized_requirement_at_clause_start(self):
        for message in ("另外还要求能退", "此外要求纯棉", "同时还要M码"):
            with self.subTest(message=message):
                state = self.parse(message, self.base)
                self.assertTrue(state["ready"], state)

    def test_additive_prefix_does_not_hide_unknown_conditions_before_or_after_known_one(self):
        for message in ("还要求能退且带帽子", "还要求能退，另外要魔术贴", "还要求不缩水的纯棉", "另外还要带帽子"):
            with self.subTest(message=message):
                state = self.parse(message, self.base)
                self.assertFalse(state["ready"], state)
                self.assertEqual(state["allowed_tools"], [])
                self.assertTrue(any(issue.startswith("unsupported_requirement") for issue in state["issues"]), state)

    def test_additive_return_requirement_does_not_clear_an_existing_unknown_condition(self):
        blocked = self.parse("还要带帽子", self.base)
        state = self.parse("还要求能退", blocked)
        self.assertFalse(state["ready"])
        self.assertTrue(state["confirmed"]["require_returnable"]["value"])
        self.assertTrue(state["issues"])
        self.assertEqual(state["allowed_tools"], [])

    def test_return_requirement_can_be_explicitly_removed(self):
        state = self.parse("要能退", self.base)
        fixed = self.parse("不要求能退", state)
        self.assertTrue(fixed["ready"], fixed)
        self.assertFalse(catalog_requirements(fixed)["require_returnable"])

    def test_double_negation_does_not_remove_return_requirement(self):
        state = self.parse("要能退", self.base)
        updated = self.parse("不是不要求能退", state)
        self.assertFalse(updated["ready"])
        self.assertIn("uncertain_return_requirement_cancel", updated["issues"])

    def test_new_unknown_requirement_survives_other_issue_resolution(self):
        state = self.parse("要蓝色", self.base)
        self.assertTrue(uses_catalog("我要带帽子", state))
        state = self.parse("我要带帽子", state)
        updated = self.parse("取消颜色要求", state)
        self.assertFalse(updated["ready"])
        self.assertTrue(self.parse("取消额外要求", updated)["ready"])

    def test_pure_cotton_service_exposes_exact_percentage_source(self):
        snapshot = CatalogSnapshot.load(ROOT / "samples/catalog_demo.json")
        result = recommend(snapshot, catalog_requirements(self.base), scope=CatalogScope(frozenset({"DEMO-SHOP-A"}), "US", "demo_storefront"), as_of=self.now)
        self.assertEqual(result["status"], "matched")
        for candidate in result["candidates"]:
            self.assertEqual(candidate["matched_attributes"]["cotton_percentage"], 100)
            self.assertEqual(candidate["source"]["fields"]["cotton_percentage"]["value"], 100)
            self.assertTrue(candidate["source"]["fields"]["cotton_percentage"]["pointer"].endswith("/attributes/cotton_percentage"))

    def test_pure_cotton_percentage_has_traceable_evidence(self):
        state = self.parse("推荐100%棉T恤M码，预算30美元")
        self.assertTrue(state["ready"], state)
        self.assertEqual(catalog_requirements(state)["cotton_percentage"], 100)
        item = state["confirmed"]["cotton_percentage"]
        start, end = item["source"]["span"]
        self.assertEqual(item["source"]["quote"], state["source_message"][start:end])
        self.assertEqual(item["source"]["quote"], "100%棉")

    def test_unknown_cotton_percentage_does_not_pass_pure_cotton(self):
        import json
        snapshot = CatalogSnapshot.load(ROOT / "samples/catalog_demo.json")
        data = snapshot.data()
        for product in data["products"]:
            for variant in product["variants"]:
                variant["attributes"].pop("cotton_percentage", None)
        altered = CatalogSnapshot("synthetic-test-memory.json", json.dumps(data).encode())
        result = recommend(altered, catalog_requirements(self.base), scope=CatalogScope(frozenset({"DEMO-SHOP-A"}), "US", "demo_storefront"), as_of=self.now)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["status"], "insufficient_evidence")

    def test_budget_precision_and_exclusive_boundary(self):
        state = self.parse("预算低于30美元", self.base)
        self.assertTrue(state["ready"], state)
        self.assertEqual(catalog_requirements(state)["budget_max_minor"], 2999)
        for message in ("预算30.001美元", "预算-1美元", "预算20到30美元", "预算30美元左右", "预算至少30美元"):
            with self.subTest(message=message):
                self.assertFalse(self.parse(message, self.base)["ready"])

    def test_ambiguous_currency_symbol_never_defaults_to_usd(self):
        state = self.parse("预算$30", self.base)
        self.assertFalse(state["ready"])
        self.assertNotIn("currency", state["confirmed"])

    def test_every_current_source_span_matches_original_text(self):
        for field, item in self.base["confirmed"].items():
            with self.subTest(field=field):
                start, end = item["source"]["span"]
                self.assertEqual(self.base_message[start:end], item["source"]["quote"])

    def test_original_state_is_not_mutated(self):
        saved = deepcopy(self.base)
        self.parse("预算改为25美元", self.base)
        self.assertEqual(self.base, saved)

    def test_tool_only_accepts_empty_arguments_and_ready_state(self):
        self.assertEqual(validate_catalog_arguments({}, self.base), {})
        for arguments in ({"budget_max_minor": 1}, {"shop_id": "B"}, None, []):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                validate_catalog_arguments(arguments, self.base)
        with self.assertRaises(ValueError):
            validate_catalog_arguments({}, self.parse("推荐T恤"))


if __name__ == "__main__":
    unittest.main()
