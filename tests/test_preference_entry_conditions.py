"""Display overrides must not switch business or turn CNY display units into a USD budget."""
from datetime import datetime
import unittest
from agent_runtime import _current_preference_overrides
from condition_state import reconcile_conditions

class EntryPreferenceConditionTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.fromisoformat("2026-11-22T10:00:00+08:00")
        self.base=reconcile_conditions("推荐M码纯棉T恤，预算不超过30美元",None,self.now,"first")

    def test_policy_followup_keeps_catalog_with_display_suffix(self):
        for separator in ("；", "，", "？", "。"):
            for question in ("那退货政策是什么", "这款商品退货运费谁承担"):
                q=question+separator+"本次详细回答"
                with self.subTest(q=q):
                    state=reconcile_conditions(q,self.base,self.now,"second")
                    self.assertEqual(state["kind"],"catalog")
                    self.assertTrue(state["ready"],state)
                    self.assertTrue(state["policy_followup"])
                    # A comma-attached policy clause remains conservative in the finite parser.
                    if separator != "，":
                        self.assertEqual(_current_preference_overrides(q),{"answer_style":"detailed"})

    def test_units_for_display_do_not_replace_shopping_budget_currency(self):
        q="推荐M码纯棉T恤，预算不超过30美元；本次用元；本次详细说明"
        state=reconcile_conditions(q,None,self.now,"first")
        self.assertTrue(state["ready"],state)
        self.assertEqual(state["confirmed"]["currency"]["value"],"USD")
        self.assertEqual(state["confirmed"]["budget_max_minor"]["value"],3000)
        self.assertEqual(_current_preference_overrides(q),{"amount_format":"yuan","answer_style":"detailed"})
        for value in state["confirmed"].values():
            source=value["source"]
            if source.get("turn_id")=="first" and "span" in source:
                start,end=source["span"]
                self.assertEqual(q[start:end],source["quote"])

    def test_unknown_business_request_remains_unfulfilled(self):
        q="那退货政策是什么；本次详细回答；还要带帽子"
        state=reconcile_conditions(q,self.base,self.now,"second")
        self.assertTrue(state["needs_clarification"],state)

    def test_questions_and_quoted_scopes_are_not_commands(self):
        for text in ("本次用分？","本次详细回答吗？","政策要求；本次用分","如果查到订单；本次用分","原文如下；本次详细回答"):
            with self.subTest(text=text):
                self.assertEqual(_current_preference_overrides(text),{})
        self.assertEqual(_current_preference_overrides("退货政策是什么？本次详细回答"),{"answer_style":"detailed"})
