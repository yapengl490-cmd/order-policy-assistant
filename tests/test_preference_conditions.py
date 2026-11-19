"""展示偏好与业务条件分层回归：原文依据、时间边界和模型入口均需保留。

原解析器另有待办（本轮未修改）：同条消息写“核查O001，退款截止
2026-11-03T00:00:00+08:00。把退款截止改为最近一次。”时，已解析项touched
会让后面的未知修改未触发澄清；甚至同轮只有已解析订单号也可能抑制该判断。
该子句没有被本轮偏好遮蔽删除，不能把此旧缺口记为本轮已经通过。
"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_runtime import run_agent
from app_config import AppSettings
from condition_state import reconcile_conditions, validate_tool_arguments


NOW = datetime.fromisoformat("2026-11-17T10:00:00+08:00")
CUTOFF = "2026-11-03T00:00:00+08:00"
ORDER = "核查订单O001，退款截止" + CUTOFF


class PreferenceConditionTests(unittest.TestCase):
    def state(self, message, previous=None):
        return reconcile_conditions(message, previous, NOW, "preference-test")

    def test_known_display_clauses_do_not_change_order_condition(self):
        preferences = ("以后简短回答", "请以后都简洁回答", "今后回答详细一点", "之后简明地答复",
                       "默认金额用元", "以后金额用分显示", "以后金额精确到分", "这次按分展示",
                       "本次详细回答", "把回答改为简短", "请把金额改为元", "以后简短准确的回答问题")
        for clause in preferences:
            with self.subTest(preference=clause):
                state = self.state(ORDER + "。" + clause + "。")
                self.assertTrue(state["ready"])
                self.assertEqual(state["issues"], [])
                self.assertEqual(state["allowed_tools"], ["check_order"])
                self.assertEqual(state["confirmed"]["refund_before"]["value"], CUTOFF)

    def test_mixed_policy_and_display_request_keeps_both_authorized_tools(self):
        state = self.state(ORDER + "，并解释退款到账政策。以后金额用元，以后回答简短。")
        self.assertEqual(set(state["allowed_tools"]), {"check_order", "search_policies"})
        self.assertFalse(state["needs_clarification"])

    def test_summary_constraints_are_not_relaxed_by_display_preferences(self):
        complete = self.state("以后简短回答。统计支付月份2026年10月，退款截止2026年11月3日零点。")
        self.assertEqual(complete["allowed_tools"], ["summary_orders"])
        incomplete = self.state("统计支付月份2026年10月。以后简短回答。")
        self.assertEqual(incomplete["missing"], ["refund_before"])
        self.assertTrue(incomplete["needs_clarification"])
        self.assertEqual(incomplete["allowed_tools"], [])

    def test_original_text_and_character_offsets_are_preserved(self):
        text = "请以后金额用元。" + ORDER + "，以后回答简短。"
        state = self.state(text)
        self.assertEqual(state["original_question"], text)
        for field in ("order_id", "refund_before"):
            source = state["confirmed"][field]["source"]
            start, end = source["span"]
            self.assertEqual(source["quote"], text[start:end])
            self.assertEqual(source["quote"], "O001" if field == "order_id" else CUTOFF)

    def test_pure_preferences_are_not_business_queries_or_updates(self):
        for text in ("以后简短回答", "把回答改为详细", "请把金额改为元", "这次用分，详细回答"):
            with self.subTest(text=text):
                state = self.state(text)
                self.assertFalse(state["active"])
                self.assertFalse(state["needs_clarification"])
                self.assertEqual(state["allowed_tools"], [])
                self.assertEqual(state["changed"], [])

    def test_pure_preferences_do_not_resume_or_erase_pending_business_conditions(self):
        previous = self.state("核查O001，退款截止月底")
        original = deepcopy(previous)
        state = self.state("以后简短回答。把金额改为元。", previous)
        self.assertFalse(state["active"])
        self.assertFalse(state["needs_clarification"])
        self.assertEqual(state["allowed_tools"], [])
        self.assertIn("refund_before", state["missing"])
        self.assertIn("unsupported_relative_date", state["issues"])
        resumed = self.state("再查", state)
        self.assertTrue(resumed["needs_clarification"])
        self.assertEqual(previous, original)

    def test_order_without_cutoff_retains_documented_program_clock_default(self):
        state = self.state("核查O001。以后简短回答。")
        self.assertTrue(state["ready"])
        self.assertNotIn("refund_before", state["confirmed"])
        self.assertEqual(state["defaults"]["refund_before"]["value"], NOW.isoformat())
        values = validate_tool_arguments("check_order", {"order_id": "O001"}, state)
        self.assertEqual(values["refund_before"], NOW.isoformat())

    def test_real_after_boundary_still_requires_clarification(self):
        for text in (ORDER + "以后。以后简短回答。", ORDER + "之后。今后金额用元。",
                     "核查O001，退款截止2026年11月3日零点以后，请本次简短回答。",
                     "核查O001，退款截止月底以后。以后简短回答。"):
            with self.subTest(text=text):
                state = self.state(text)
                self.assertTrue(state["needs_clarification"])
                self.assertIn("unsupported_or_ambiguous_time_boundary", state["issues"])
                self.assertIn("refund_before", state["blocked_fields"])
                self.assertNotIn("refund_before", state["defaults"])
                self.assertEqual(state["allowed_tools"], [])

    def test_unknown_business_time_is_not_removed_as_a_preference(self):
        for text in ("以后查订单O001", "核查O001，之后的退款用元显示", "核查O001，退款截止以后",
                     "核查O001，退款截止下个月。以后简短回答。",
                     "核查O001，退款截止2026-11-03。以后简短回答。",
                     "核查O001，从下个月以后用元显示",
                     ORDER + "以后简短回答"):
            with self.subTest(text=text):
                state = self.state(text)
                self.assertTrue(state["needs_clarification"])
                self.assertEqual(state["allowed_tools"], [])

    def test_other_unsupported_business_instructions_still_fail_closed(self):
        for text in (ORDER + "含截止。以后简短回答。", "核查O001，退款截止今天。以后简短回答。",
                     "核查O001，退款截止2026年11月3日下午。以后简短回答。",
                     "核查O001，退款截止2026年11月3日零点日本时间。以后简短回答。"):
            with self.subTest(text=text):
                state = self.state(text)
                self.assertTrue(state["needs_clarification"])
                self.assertEqual(state["allowed_tools"], [])

    def test_unrecognized_followup_update_does_not_become_display_preference(self):
        state = self.state("把退款截止改为最近一次。以后简短回答。", self.state(ORDER))
        self.assertTrue(state["needs_clarification"])
        self.assertIn("unsupported_condition_update", state["issues"])
        self.assertEqual(state["allowed_tools"], [])

    def test_temporary_preference_does_not_erase_real_business_update(self):
        previous = self.state(ORDER)
        new_cutoff = "2026-11-04T00:00:00+08:00"
        state = self.state("这次金额用分。退款截止改为" + new_cutoff, previous)
        self.assertTrue(state["ready"])
        self.assertEqual(state["confirmed"]["refund_before"]["value"], new_cutoff)

    def test_pure_preference_reaches_model_without_business_tools_or_snapshot_mutation(self):
        text = "以后简短回答。把金额改为元。"
        state = self.state(text, self.state("核查O001，退款截止月底"))
        preference = {"role": "buyer", "values": {}, "version": 0, "updated_at": None, "source": None}
        original = deepcopy(preference)
        requests = []
        def send(request):
            requests.append(json.loads(request.content))
            answer = {"status": "completed", "answer": "收到展示偏好。",
                      "memory_proposal": {"values": {"answer_style": "brief"},
                                          "source_quote": "以后简短回答。", "scope": "explicit_long_term"}}
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": json.dumps(answer)},
                                                          "finish_reason": "stop"}]})
        result = run_agent(AppSettings(model_api_key="offline-placeholder", model_enabled=True), None,
                           text, "B001", [], lambda: False, transport=httpx.MockTransport(send),
                           tool_handlers={}, condition_state=state, preference_snapshot=preference)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get("tools", []), [])
        self.assertEqual(requests[0]["messages"][-1]["content"], text)
        self.assertEqual(result["memory_proposal"]["scope"], "explicit_long_term")
        self.assertEqual(preference, original)
        self.assertEqual(result["usage"]["tool_calls"], 0)


if __name__ == "__main__":
    unittest.main()
