"""Offline display contract: preferences cannot change facts or their currency."""
from copy import deepcopy
from pathlib import Path
import unittest
import json

from display_preferences import answer_style, format_minor_amount
from general_policy import render_general_policy
from policy_documents import parse_policy_text
from shopify_chat import _attach_citations, render_shopify_fact


ROOT = Path(__file__).resolve().parents[1]


def order_fact():
    return {
        "status": "ok", "order_id": "gid://shopify/Order/1001", "order_name": "#1001",
        "shop_domain": "offline-demo.myshopify.com", "currency": "USD", "minor_unit_digits": 2,
        "paid_amount_minor": 10000, "refund_amount_minor": 3000, "net_amount_minor": 7000,
        "refund_before": "2026-11-20T07:00:00+00:00", "observed_at": "2026-11-20T08:00:00+00:00",
        "successful_payments": [{"transaction_id": "gid://shopify/OrderTransaction/1", "kind": "CAPTURE",
            "status": "SUCCESS", "processed_at": "2026-11-20T06:15:00+00:00", "currency": "USD", "amount_minor": 10000}],
        "successful_refunds": [{"transaction_id": "gid://shopify/OrderTransaction/2", "kind": "REFUND",
            "status": "SUCCESS", "processed_at": "2026-11-20T06:30:00+00:00", "currency": "USD", "amount_minor": 3000,
            "included_before_cutoff": True}],
    }


def policy_fact():
    doc = parse_policy_text((ROOT / "knowledge/2026-11-shoes.md").read_text())
    sources = {source["citation_id"]: deepcopy(source) for source in doc["rule_citations"].values()}
    return {
        "kind": "general_policy", "status": "ok", "policy_version": doc["version"],
        "applicability": {"effective_from": doc["effective_from"], "effective_before": doc["effective_before"]},
        "document": doc, "citation_index": sources,
        "claims": [
            {"text": "S001普通鞋类，政策查询时刻2026-11-20T10:00:00+08:00，版本2026-11-v1。",
             "citation_ids": [doc["rule_citations"]["allowed_categories"]["citation_id"]]},
            {"text": "收货起336小时内，含起点、不含截止；未穿着且吊牌完好；定制商品不适用；自述仍需核实。",
             "citation_ids": [doc["rule_citations"]["return_window_hours"]["citation_id"]]},
            {"text": "尺码或个人偏好退货由买家承担运费；商家责任须经核实，否则责任与运费待核实。",
             "citation_ids": [doc["rule_citations"]["shipping_by_reason"]["citation_id"]]},
        ],
        "notice": "仅为合成演示条款；未核查任何订单，不能承诺本单可退。",
    }


class MinorMoneyDisplayTests(unittest.TestCase):
    def test_cny_legacy_formats_keep_fraction_and_currency(self):
        expected = {"yuan": "85.01元（CNY）", "yuan_fixed2": "85.01元（CNY）", "fen": "8501分（CNY）"}
        for mode, text in expected.items():
            self.assertEqual(format_minor_amount(8501, "CNY", 2, {"amount_format": mode}), text)
        self.assertEqual(format_minor_amount(8500, "CNY", 2, {"amount_format": "yuan"}), "85元（CNY）")
        self.assertEqual(format_minor_amount(0, "CNY", 2, {"amount_format": "yuan"}), "0元（CNY）")
        self.assertEqual(format_minor_amount(8500, "CNY", 2, {"amount_format": "yuan_fixed2"}), "85.00元（CNY）")

    def test_foreign_money_keeps_source_currency_digits_despite_language_or_yuan(self):
        for currency, digits, value, expected in (("USD", 2, 8501, "85.01 USD"),
                                                  ("JPY", 0, 8501, "8501 JPY"),
                                                  ("KWD", 3, 8501, "8.501 KWD")):
            for mode in ("yuan", "yuan_fixed2", "fen"):
                with self.subTest(currency=currency, mode=mode):
                    self.assertEqual(format_minor_amount(value, currency, digits,
                        {"amount_format": mode, "language": "中文", "currency": "CNY"}), expected)

    def test_no_float_rounding_for_large_or_negative_minor_amount(self):
        self.assertEqual(format_minor_amount(9007199254740991, "USD"), "90071992547409.91 USD")
        self.assertEqual(format_minor_amount(-50, "USD"), "-0.50 USD")
        self.assertEqual(format_minor_amount(-5, "CNY", 2, {"amount_format": "yuan"}), "-0.05元（CNY）")

    def test_default_and_invalid_preferences_do_not_change_source_units(self):
        for display in (None, {}, {"answer_style": "standard"}, {"amount_format": "usd"}, "yuan"):
            self.assertEqual(format_minor_amount(8500, "CNY", 2, display), "85.00 CNY")
        for display in (None, {}, {"answer_style": []}, {"answer_style": "untrusted"}):
            self.assertEqual(answer_style(display), "standard")

    def test_invalid_source_money_is_not_silently_coerced(self):
        for value, currency, digits in ((1.5, "USD", 2), (True, "USD", 2), (100, "美元", 2),
                                        (100, "USD", -1), (100, "USD", True), (100, "USD", 7)):
            with self.assertRaises(ValueError):
                format_minor_amount(value, currency, digits)
        with self.assertRaises(ValueError):
            format_minor_amount(123, "CNY", 3, {"amount_format": "fen"})


class PreferenceRendererTests(unittest.TestCase):
    def test_standard_shopify_output_remains_legacy_text(self):
        expected = (
            "Shopify测试订单#1001：成功收款100.00 USD，截止前成功退款30.00 USD，净额70.00 USD。[SHOPIFY-ORDER:1001]\n"
            "退款截止2026-11-20T07:00:00+00:00，不含该时刻；全部计入收款亦须早于截止。\n"
            "成功收款处理时间2026-11-20T06:15:00+00:00。[SHOPIFY-TX:1]\n"
            "成功退款处理时间2026-11-20T06:30:00+00:00。[SHOPIFY-TX:2]\n"
            "依据为开发店测试交易；授权、待处理交易不计成功收退款，不证明真实到账、买家身份绑定或退货资格。")
        self.assertEqual(render_shopify_fact(order_fact()), expected)
        self.assertEqual(render_shopify_fact(order_fact(), {"answer_style": "standard"}), expected)

    def test_brief_retains_money_cutoff_exception_and_all_transaction_references(self):
        fact = order_fact()
        fact["successful_refunds"].append({**fact["successful_refunds"][0],
            "transaction_id": "gid://shopify/OrderTransaction/3", "amount_minor": 1000,
            "processed_at": "2026-11-20T07:30:00+00:00", "included_before_cutoff": False})
        text = render_shopify_fact(fact, {"answer_style": "brief", "amount_format": "fen"})
        for phrase in ("净额70.00 USD", "不含该时刻", "成功退款10.00 USD（不早于截止，本次未计入）",
                       "[SHOPIFY-TX:1]", "[SHOPIFY-TX:2]", "[SHOPIFY-TX:3]", "不证明真实到账", "退货资格"):
            self.assertIn(phrase, text)
        self.assertNotIn("元", text)
        self.assertNotIn("处理时间", text)

    def test_detailed_explains_transaction_state_exclusion_and_calculation(self):
        fact = order_fact()
        fact["excluded_transactions"] = [{"transaction_id": "gid://shopify/OrderTransaction/4",
            "kind": "AUTHORIZATION", "status": "SUCCESS", "reason": "not_successful_receipt_or_refund"}]
        text = render_shopify_fact(fact, {"answer_style": "detailed", "amount_format": "yuan"})
        for phrase in ("类型CAPTURE，状态SUCCESS", "处理时间2026-11-20T06:15:00+00:00", "未计入交易4",
                       "AUTHORIZATION", "不属于成功收款或成功退款", "100.00 USD − 截止前成功退款30.00 USD = 净额70.00 USD",
                       "offline-demo.myshopify.com", "平台观察时刻", "不证明真实到账"):
            self.assertIn(phrase, text)

    def test_excluded_transaction_reference_points_to_exact_new_source_or_legacy_evidence(self):
        fact = order_fact()
        excluded = {"transaction_id": "gid://shopify/OrderTransaction/4", "kind": "AUTHORIZATION",
                    "status": "SUCCESS", "reason": "not_successful_receipt_or_refund"}
        fact["excluded_transactions"] = [excluded]
        legacy = render_shopify_fact(fact, {"answer_style": "detailed"})
        self.assertIn("完整工具依据中的excluded_transactions", legacy)
        self.assertNotIn("[SHOPIFY-TX:4]", legacy)
        current = _attach_citations(fact)
        self.assertEqual(current["citation_index"]["SHOPIFY-TX:4"]["pointer"], "/excluded_transactions/0")
        self.assertEqual(json.loads(current["citation_index"]["SHOPIFY-TX:4"]["text"]), excluded)
        text = render_shopify_fact(current, {"answer_style": "detailed"})
        self.assertIn("[SHOPIFY-TX:4]", text)
        self.assertNotIn("完整工具依据中的excluded_transactions", text)
        for ref in ("SHOPIFY-ORDER:1001", "SHOPIFY-TX:1", "SHOPIFY-TX:2", "SHOPIFY-TX:4"):
            self.assertIn(ref, current["citation_index"])

    def test_policy_brief_keeps_conditions_exception_unknown_and_own_sources(self):
        fact = policy_fact()
        standard = render_general_policy(fact)
        text = render_general_policy(fact, {"answer_style": "brief"})
        self.assertEqual(text, standard)
        for claim in fact["claims"]:
            self.assertIn(claim["text"], text)
            for ref in claim["citation_ids"]:
                self.assertIn("[" + ref + "]", text)
        self.assertIn(fact["notice"], text)

    def test_policy_detailed_adds_interval_and_each_cited_source_once(self):
        fact = policy_fact()
        text = render_general_policy(fact, {"answer_style": "detailed"})
        self.assertIn("版本有效区间：", text)
        self.assertIn("未核查订单支付时版本", text)
        for source in fact["citation_index"].values():
            self.assertEqual(text.count(source["text"]), 1)
        self.assertNotIn("---json", text)
        shipping_only = deepcopy(fact)
        shipping_only["claims"] = [shipping_only["claims"][-1]]
        text = render_general_policy(shipping_only, {"answer_style": "detailed"})
        self.assertIn("## 退货运费", text)
        self.assertNotIn("## 退货资格", text)

    def test_rendering_never_writes_preference_snapshot_or_prior_facts(self):
        fact, preferences = order_fact(), {"answer_style": "brief", "amount_format": "yuan"}
        original_fact, original_preferences = deepcopy(fact), deepcopy(preferences)
        old_answer = render_shopify_fact(fact, preferences)
        detailed_answer = render_shopify_fact(fact, {**preferences, "answer_style": "detailed"})
        self.assertNotEqual(old_answer, detailed_answer)
        self.assertEqual(fact, original_fact)
        self.assertEqual(preferences, original_preferences)
        self.assertEqual(old_answer, render_shopify_fact(fact, preferences))

    def test_failure_and_empty_policy_cannot_be_styled_into_success(self):
        for style in ("brief", "standard", "detailed"):
            self.assertIn("只读查询失败", render_shopify_fact({"status": "failed"}, {"answer_style": style}))
            self.assertIn("不证明订单在平台不存在", render_shopify_fact({"status": "not_found"}, {"answer_style": style}))
            self.assertIn("无法判断本单可退", render_shopify_fact({"policies": []}, {"answer_style": style}))
            self.assertEqual(render_general_policy({"kind": "general_policy", "status": "clarification",
                "message": "请提供品类。", "notice": "尚未核查订单。"}, {"answer_style": style}),
                "请提供品类。\n尚未核查订单。")


if __name__ == "__main__":
    unittest.main()
