"""HTTP and tool doubles only; these checks are not real model/shop evidence."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine

import catalog_recommendations
import general_policy
from catalog_conditions import reconcile_catalog
from catalog_recommendations import CatalogSnapshot
from execution_budget import ExecutionBudget
from general_policy import lookup_general_policy
from general_policy_conditions import reconcile_general_policy
from policy_kb import metadata, import_policy_files
from shopping_runtime import run_shopping_agent


NOW = datetime.fromisoformat("2026-11-20T12:00:00+08:00")
SECRET = "offline-fake-key-not-a-real-credential"


class ShoppingRuntimeTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("socket.socket", side_effect=AssertionError("all network forbidden"))
        patcher.start(); self.addCleanup(patcher.stop)
        self.engine = create_engine("sqlite+pysqlite:///:memory:").execution_options(schema_translate_map={"public": None})
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        root = Path(general_policy.__file__).parent
        import_policy_files(self.engine, [root / "knowledge/2026-11-shoes.md"])
        self.fact = lookup_general_policy(self.engine, store_id="S001", category="ordinary_shoes", policy_at=NOW)
        self.state = reconcile_general_policy("本店普通鞋类退货和运费规则", None, NOW, "test-run")

    def cfg(self, **changes):
        return SimpleNamespace(**({"model_api_key": SECRET, "model_base_url": "https://mock.invalid", "model_name": "offline-double",
            "max_model_calls": 4, "max_tool_calls": 6, "max_output_tokens": 1200, "model_timeout_seconds": 1,
            "task_timeout_seconds": 90, "model_enabled": True, "catalog_demo_enabled": False, "demo_policy_store_id": "S001"} | changes))

    def tool_reply(self, name="lookup_general_policy", args=None):
        return {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "tool-1", "type": "function", "function": {"name": name, "arguments": json.dumps({} if args is None else args)}}]}}]}

    def final(self, body=None):
        if body is None:
            body = {"citation_ids": list(self.fact["citation_index"])}
        return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(body)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}

    def run_case(self, replies=None, *, state=None, settings=None, cancel=None, shared=None, after=None, handler=None, history=None,
                 real_handler=False, access_role="buyer", shop_id=None):
        requests, calls = [], []
        replies = replies if replies is not None else [self.tool_reply(), self.final()]
        def transport(request):
            self.assertEqual(request.url.host, "mock.invalid")
            self.assertEqual(request.headers["Authorization"], "Bearer " + (settings or self.cfg()).model_api_key)
            payload = json.loads(request.content); requests.append(payload)
            self.assertLessEqual(len(requests), len(replies), "unexpected extra request")
            reply = replies[len(requests) - 1]
            if after: after(len(requests), payload)
            if isinstance(reply, Exception): raise reply
            return httpx.Response(200, json=reply)
        def tool():
            calls.append({})
            return deepcopy(self.fact) if handler is None else handler()
        result = run_shopping_agent(settings or self.cfg(), self.engine, "本次测试问题", "B_TEST", history or [], cancel or (lambda: False),
            access_role=access_role, shop_id=shop_id, condition_state=state or self.state, transport=httpx.MockTransport(transport),
            tool_handlers=None if real_handler else {"lookup_general_policy": tool}, shared_budget=shared)
        return result, requests, calls

    def test_tool_fact_selection_fixed_render_history_and_export_shape(self):
        previous = [{"role": "user", "content": "旧问题"}, {"role": "assistant", "content": "旧答案"}]
        result, requests, calls = self.run_case(history=previous)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(requests), 2); self.assertEqual(calls, [{}])
        self.assertTrue(result["shopping_validation"]["passed"])
        self.assertIn("336小时", result["answer"])
        self.assertIn("不能据此承诺本单可退", result["answer"])
        self.assertEqual(requests[0]["tools"][0]["function"]["parameters"], {"type": "object", "properties": {}, "additionalProperties": False})
        fact = result["facts"][0]
        self.assertEqual(fact["arguments"], {})
        self.assertEqual(fact["result_snapshot_sha256"], hashlib.sha256(json.dumps(fact["result"], ensure_ascii=False, sort_keys=True).encode()).hexdigest())
        exported = json.loads(json.dumps(result, ensure_ascii=False))
        self.assertEqual(exported["facts"], result["facts"])
        self.assertEqual(result["history"][:2], previous)
        self.assertEqual(result["history"][-1]["content"], result["answer"])
        self.assertFalse(result["answer_semantics_verified"])

    def test_unknown_citation_gets_exactly_one_repair_without_second_tool(self):
        result, requests, calls = self.run_case([self.tool_reply(), self.final({"citation_ids": ["invented"]}), self.final()])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["shopping_validation"]["repair_attempted"])
        self.assertTrue(result["shopping_validation"]["passed"])
        self.assertEqual(len(requests), 3); self.assertEqual(len(calls), 1)
        self.assertNotIn("tools", requests[2])

    def test_second_invalid_final_falls_back_without_more_calls(self):
        bad = self.final({"citation_ids": []})
        result, requests, calls = self.run_case([self.tool_reply(), bad, bad])
        self.assertEqual(len(requests), 3); self.assertEqual(len(calls), 1)
        self.assertFalse(result["shopping_validation"]["passed"])
        self.assertIn("程序依据", result["answer"])
        self.assertIn("定制商品不适用", result["answer"])

    def test_tool_call_during_repair_is_never_executed(self):
        result, requests, calls = self.run_case([self.tool_reply(), self.final({"citation_ids": []}), self.tool_reply()])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(requests), 3); self.assertEqual(len(calls), 1)

    def test_tool_cannot_accept_scope_or_requirements_from_model(self):
        for args in ({"store_id": "S_OTHER"}, {"category": "ordinary_shoes"}, {"policy_at": "2026-10-01"}, []):
            with self.subTest(args=args):
                result, requests, calls = self.run_case([self.tool_reply(args=args)])
                self.assertEqual(result["status"], "failed")
                self.assertEqual(calls, []); self.assertEqual(result["facts"], [])

    def test_wrong_tool_or_second_tool_call_is_rejected(self):
        for replies in ([self.tool_reply("check_order")], [self.tool_reply(), self.tool_reply()]):
            with self.subTest(replies=replies):
                result, requests, calls = self.run_case(replies)
                self.assertEqual(result["status"], "failed")
                self.assertLessEqual(len(calls), 1)

    def test_empty_wrapper_normalizes_to_same_zero_argument_capability(self):
        result, requests, calls = self.run_case([self.tool_reply(args={"parameters": {}}), self.final()])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(requests), 2)
        self.assertEqual(calls, [{}])
        self.assertTrue(result["shopping_validation"]["arguments_normalized"])
        compatibility = result["trace"][0]["tool_argument_compatibility"]
        self.assertEqual(compatibility["original_arguments"], {"parameters": {}})
        self.assertEqual(compatibility["executed_arguments"], {})
        self.assertEqual(result["facts"][0]["arguments"], {})

    def test_wrapper_with_any_business_value_or_extra_key_is_not_executed(self):
        for args in ({"parameters": {"shop_id": "other"}}, {"parameters": {}, "order_id": "#1002"},
                     {"parameters": []}, {"parameters": None}, {"parameters": {"parameters": {}}}):
            with self.subTest(args=args):
                result, requests, calls = self.run_case([self.tool_reply(args=args)])
                self.assertEqual(result["status"], "failed")
                self.assertEqual(calls, [])
                self.assertEqual(len(requests), 1)

    def test_no_fact_final_is_not_accepted(self):
        result, requests, calls = self.run_case([self.final()])
        self.assertEqual(result["status"], "failed")
        self.assertFalse(calls); self.assertFalse(result["facts"])

    def test_pre_cancel_and_post_response_cancel_stop_before_tool(self):
        result, requests, calls = self.run_case([], cancel=lambda: True)
        self.assertEqual(result["status"], "cancelled"); self.assertFalse(requests)
        cancelled = [False]
        result, requests, calls = self.run_case([self.tool_reply()], cancel=lambda: cancelled[0], after=lambda *_: cancelled.__setitem__(0, True))
        self.assertEqual(result["status"], "cancelled"); self.assertFalse(calls)

    def test_cancel_after_tool_keeps_fact_and_prevents_final_model_request(self):
        cancelled = [False]
        def tool():
            cancelled[0] = True
            return deepcopy(self.fact)
        result, requests, calls = self.run_case([self.tool_reply()], cancel=lambda: cancelled[0], handler=tool)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(len(requests), 1); self.assertEqual(len(result["facts"]), 1)

    def test_shared_model_budget_is_not_reset(self):
        cfg = self.cfg(); budget = ExecutionBudget(cfg, lambda: False)
        for _ in range(3): self.assertTrue(budget.reserve_model(cfg.max_output_tokens))
        result, requests, calls = self.run_case([self.tool_reply()], settings=cfg, shared=budget)
        self.assertEqual(result["status"], "limited")
        self.assertEqual(len(requests), 1); self.assertEqual(len(calls), 1)
        self.assertEqual(result["budget"]["used"]["model_requests"], 4)

    def test_shared_tool_and_output_reservations_are_enforced(self):
        cfg = self.cfg(); budget = ExecutionBudget(cfg, lambda: False)
        for _ in range(cfg.max_tool_calls): self.assertTrue(budget.reserve_tool())
        result, requests, calls = self.run_case([self.tool_reply()], settings=cfg, shared=budget)
        self.assertEqual(result["status"], "limited"); self.assertFalse(calls)
        budget = ExecutionBudget(cfg, lambda: False)
        self.assertTrue(budget.reserve_model(cfg.max_model_calls * cfg.max_output_tokens))
        result, requests, calls = self.run_case([], settings=cfg, shared=budget)
        self.assertEqual(result["status"], "limited"); self.assertFalse(requests)

    def test_deadline_after_model_response_prevents_tool_execution(self):
        clock = [0.0]
        cfg = self.cfg(task_timeout_seconds=5)
        budget = ExecutionBudget(cfg, lambda: False, clock=lambda: clock[0])
        result, requests, calls = self.run_case([self.tool_reply()], settings=cfg, shared=budget,
                                              after=lambda *_: clock.__setitem__(0, 6.0))
        self.assertEqual(result["status"], "limited")
        self.assertEqual(result["budget"]["stop_reason"], "deadline")
        self.assertFalse(calls)

    def test_tool_failure_is_not_reported_as_no_matching_policy(self):
        def broken():
            raise RuntimeError("offline test tool failure")
        result, requests, calls = self.run_case([self.tool_reply()], handler=broken)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["facts"])
        self.assertIn("失败", result["answer"])
        self.assertEqual(len(requests), 1)

    def test_clarification_and_missing_key_send_no_requests(self):
        state = deepcopy(self.state); state.update(needs_clarification=True, clarification="请补政策日期", allowed_tools=[])
        result, requests, calls = self.run_case([], state=state)
        self.assertEqual(result["status"], "clarification"); self.assertFalse(requests)
        result, requests, calls = self.run_case([], settings=self.cfg(model_api_key=""))
        self.assertEqual(result["status"], "failed"); self.assertFalse(requests)

    def test_default_catalog_is_disabled_without_external_access(self):
        state = reconcile_catalog("推荐M码棉质T恤，预算不超过30美元", None, NOW, "catalog-test")
        result, requests, calls = self.run_case([], state=state, real_handler=True)
        self.assertEqual(result["status"], "clarification"); self.assertFalse(requests)
        self.assertIn("尚未启用", result["answer"])

    def test_missing_identity_or_merchant_scope_stops_before_model(self):
        result, requests, calls = self.run_case([], real_handler=True, access_role="merchant")
        self.assertEqual(result["status"], "failed"); self.assertFalse(requests)

    def test_actual_policy_handler_injects_store_time_and_category(self):
        result, requests, calls = self.run_case(real_handler=True)
        self.assertEqual(result["status"], "completed")
        fact = result["facts"][0]["result"]
        self.assertEqual(fact["scope"], {"store_id": "S001", "category": "ordinary_shoes"})
        self.assertEqual(fact["selection_basis"], "policy_query_time")
        self.assertFalse(calls)

    def test_actual_policy_category_clarification_then_lookup_uses_query_version(self):
        self.state = reconcile_general_policy("本店退货规则", None, NOW, "generic")
        result, requests, calls = self.run_case([self.tool_reply(), self.final({"citation_ids": []})], real_handler=True)
        self.assertEqual(result["status"], "clarification")
        self.assertIn("category", result["facts"][0]["result"]["missing"])
        state = reconcile_general_policy("普通鞋类", self.state, NOW, "category")
        result, requests, calls = self.run_case(state=state, real_handler=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["facts"][0]["result"]["rules"]["return_window_hours"], 336)

    def test_policy_paths_are_redacted_from_facts_model_payload_and_export(self):
        result, requests, calls = self.run_case()
        for content in (json.dumps(result, ensure_ascii=False), json.dumps(requests, ensure_ascii=False)):
            self.assertNotIn(str(Path(general_policy.__file__).parent), content)
            self.assertNotIn(SECRET, content)
        self.assertIn("2026-11-shoes.md", json.dumps(result, ensure_ascii=False))

    def test_catalog_nested_citation_text_contains_no_local_paths(self):
        snapshot = CatalogSnapshot.load(Path(catalog_recommendations.__file__).parent / "samples/catalog_demo.json")
        state = reconcile_catalog("推荐M码棉质T恤，预算不超过30美元", None, NOW, "catalog-test")
        replies = [self.tool_reply("recommend_products"), self.final({"selected_variant_ids": ["V-A", "V-B", "V-BOUNDARY"]})]
        with patch.object(CatalogSnapshot, "load", return_value=snapshot):
            result, requests, calls = self.run_case(replies, state=state, settings=self.cfg(catalog_demo_enabled=True), real_handler=True)
        self.assertEqual(result["status"], "completed")
        for content in (json.dumps(result, ensure_ascii=False), json.dumps(requests, ensure_ascii=False)):
            self.assertFalse(snapshot.source_path in content, "catalog source path leaked into model/persisted result")
            self.assertFalse(str(Path(snapshot.source_path).parent.parent) in content, "catalog directory leaked into model/persisted result")


if __name__ == "__main__":
    unittest.main()
