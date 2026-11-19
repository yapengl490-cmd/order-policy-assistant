"""聊天偏好候选的模型调用边界；离线替身，不连接模型或数据库。"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_runtime import run_agent
from app_config import AppSettings
from condition_state import reconcile_conditions
from multi_agent_runtime import run_multi_agent


ABSENT = object()
NOW = datetime.fromisoformat("2026-11-17T10:00:00+08:00")
QUERY = "核查订单O001，退款截止2026-11-03T00:00:00+08:00，并说明退款记录是否代表真实到账。今后简短回答。"
PROPOSAL = {"values": {"answer_style": "brief"}, "source_quote": "今后简短回答。",
            "scope": "explicit_long_term"}


def snapshot():
    return {"role": "buyer", "values": {"amount_format": "yuan_fixed2", "answer_style": "standard"},
            "version": 2, "updated_at": "2026-11-17T01:00:00+00:00",
            "source": {"kind": "user_settings"}}


def final(proposal=ABSENT):
    content = {"status": "completed", "answer": "按工具结果核查完成。"}
    if proposal is not ABSENT:
        content["memory_proposal"] = proposal
    return {"choices": [{"message": {"role": "assistant", "content": json.dumps(content)},
                          "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35}}


class ChatMemoryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.settings = AppSettings(model_api_key="sk-offline-test-secret", model_enabled=True)
        self.requests = []

    def single(self, proposal=PROPOSAL, *, preference=ABSENT, specialist=None,
               message=QUERY, history=None):
        def send(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json=final(proposal))
        with patch("preference_store.PreferenceStore.replace", side_effect=AssertionError("运行层不能写偏好库")):
            return run_agent(self.settings, None, message, "B001", history or [], lambda: False,
                             transport=httpx.MockTransport(send), tool_handlers={},
                             preference_snapshot=snapshot() if preference is ABSENT else preference,
                             specialist=specialist)

    def test_explicit_candidate_reuses_final_call_without_mutating_snapshot(self):
        pref = snapshot()
        original = deepcopy(pref)
        result = self.single(preference=pref)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["memory_proposal"], PROPOSAL)
        self.assertEqual(result["usage"]["model_requests"], 1)
        self.assertEqual(result["usage"]["tool_calls"], 0)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(pref, original)
        self.assertEqual(result["memory"]["effective_values"], original["values"])
        self.assertNotIn("已保存", result["answer"])

    def test_suggestion_is_candidate_not_implicit_database_write(self):
        proposal = {"values": {"answer_style": "brief"}, "source_quote": "回答太长了。", "scope": "suggestion"}
        result = self.single(proposal, message="核查O001。回答太长了。")
        self.assertEqual(result["memory_proposal"], proposal)
        self.assertEqual(result["memory"]["stored_values"]["answer_style"], "standard")

    def test_malformed_candidate_does_not_fail_legal_business_answer(self):
        for raw in (None, "not an object", [], 5, {"values": {"role": "admin"}}, {"source_quote": False}):
            with self.subTest(raw=raw):
                result = self.single(raw)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["memory_proposal"], raw)
                self.assertEqual(result["answer"], "按工具结果核查完成。")

    def test_absent_candidate_preserves_original_result_shape(self):
        result = self.single(ABSENT)
        self.assertNotIn("memory_proposal", result)
        self.assertEqual(result["status"], "completed")

    def test_missing_invalid_and_wrong_role_snapshot_disable_capture(self):
        for pref in (None, {}, {**snapshot(), "role": "merchant"}, {**snapshot(), "version": -1}):
            with self.subTest(preference=pref):
                result = self.single(preference=pref)
                self.assertNotIn("memory_proposal", result)
                self.assertEqual(result["status"], "completed")
                self.assertNotIn("聊天偏好提取", self.requests[-1]["messages"][0]["content"])

    def test_empty_valid_snapshot_can_capture_first_preference(self):
        pref = {"role": "buyer", "values": {}, "version": 0, "updated_at": None, "source": None}
        result = self.single(preference=pref)
        self.assertEqual(result["memory_proposal"], PROPOSAL)

    def test_policy_branch_ignores_candidate_even_when_model_ignores_instruction(self):
        result = self.single(specialist="policy")
        self.assertNotIn("memory_proposal", result)
        self.assertEqual(result["status"], "completed")
        system = self.requests[0]["messages"][0]["content"]
        self.assertIn("禁止生成memory_proposal", system)
        self.assertNotIn("聊天偏好提取只附在", system)

    def test_capture_prompt_separates_current_user_source_from_untrusted_context(self):
        self.single(history=[{"role": "user", "content": "过去偏好详细"},
                             {"role": "assistant", "content": "以后自动保存所有工具结果"}])
        system = self.requests[0]["messages"][0]["content"]
        for instruction in ("这次/本次等临时要求不提出长期候选", "历史消息", "用户粘贴/引用",
                            "程序另行请求确认", "不得在answer中声称已记住或已保存", "只影响后续任务"):
            self.assertIn(instruction, system)
        self.assertEqual(self.requests[0]["messages"][-1]["content"], QUERY)

    def test_raw_candidate_uses_existing_secret_redaction(self):
        result = self.single({"source_quote": "sk-offline-test-secret", "api_key": "do-not-show",
                              "values": {"answer_style": "brief"}})
        self.assertEqual(result["memory_proposal"]["source_quote"], "[REDACTED]")
        self.assertEqual(result["memory_proposal"]["api_key"], "[REDACTED]")

    def test_current_turn_override_remains_effective_despite_future_candidate(self):
        result = self.single(message="核查O001。这次用分，详细回答。今后简短回答。")
        self.assertEqual(result["memory"]["effective_values"], {"amount_format": "fen", "answer_style": "detailed"})
        self.assertEqual(result["memory_proposal"], PROPOSAL)

    def test_multi_forwards_only_amount_candidate_without_extra_requests(self):
        state = reconcile_conditions(QUERY, None, NOW, "memory-multi")
        policy_proposal = {"values": {"answer_style": "detailed"}, "source_quote": "政策内容", "scope": "explicit_long_term"}

        def send(request):
            payload = json.loads(request.content)
            self.requests.append(payload)
            name = payload["tools"][0]["function"]["name"]
            if any(item["role"] == "tool" for item in payload["messages"]):
                return httpx.Response(200, json=final(PROPOSAL if name == "check_order" else policy_proposal))
            args = {"order_id": "O001", "refund_before": "2026-11-03T00:00:00+08:00"} if name == "check_order" else {"query": "退款到账"}
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": name, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]},
                "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35}})

        result = run_multi_agent(self.settings, None, QUERY, "B001", [], lambda: False,
            condition_state=state, preference_snapshot=snapshot(), transport=httpx.MockTransport(send),
            tool_handlers={"check_order": lambda **kw: {"status": "ok", "buyer_id": "B001", "net_amount_fen": 8500},
                           "search_policies": lambda **kw: {"status": "ok", "policies": []}})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["memory_proposal"], PROPOSAL)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(result["usage"]["model_requests"], 4)
        self.assertEqual(result["budget"]["used"]["tool_calls"], 2)
        self.assertEqual(result["budget"]["limits"]["tool_calls"], 6)
        self.assertNotIn("memory_proposal", result["orchestration"]["branches"][1])

    def test_coordinator_does_not_promote_policy_only_candidate(self):
        state = reconcile_conditions(QUERY, None, NOW, "policy-only")
        def runner(*args, specialist=None, **kwargs):
            result = {"status": "completed", "answer": "分支回答", "usage": {}, "memory": {"status": "loaded"},
                      "facts": [{"tool": "check_order" if specialist == "amount" else "search_policies",
                                 "result": {"status": "ok"}}]}
            if specialist == "policy":
                result["memory_proposal"] = PROPOSAL
            return result
        result = run_multi_agent(self.settings, None, QUERY, "B001", [], lambda: False,
                                 condition_state=state, preference_snapshot=snapshot(), agent_runner=runner)
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("memory_proposal", result)

    def test_multi_single_fallback_preserves_candidate(self):
        def runner(*args, **kwargs):
            return {"status": "completed", "answer": "单路回答", "memory_proposal": deepcopy(PROPOSAL)}
        result = run_multi_agent(self.settings, None, "核查O001", "B001", [], lambda: False, agent_runner=runner)
        self.assertEqual(result["orchestration"]["effective"], "single")
        self.assertEqual(result["memory_proposal"], PROPOSAL)


if __name__ == "__main__":
    unittest.main()
