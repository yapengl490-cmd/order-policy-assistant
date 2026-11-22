"""Independent HTTP regressions through real routing, run_agent and TaskStore.

Only model HTTP is simulated. Policy lookup uses a private SQLite policy library;
catalog queries use the actual enabled default handler and the synthetic file.
No real model, Shopify, formal database, credentials or external network is used.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

import general_policy
import catalog_recommendations
from agent_runtime import run_agent
from app_config import AppSettings
from app_server import create_app
from policy_kb import metadata, import_policy_files


POLICY_TIME = "2026-11-20T12:00:00+08:00"
POLICY_QUESTION = "本店普通鞋类退货和运费规则，政策日期" + POLICY_TIME
CATALOG_QUESTION = "推荐M码纯棉T恤，预算不超过30美元"


class ShoppingChatHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="datus-shopping-http-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tokens = {"B001": "http-test-buyer-one", "B002": "http-test-buyer-two"}
        access = self.root / "access.json"
        access.write_text(json.dumps({"accounts": [{"buyer_id": buyer, "token": token}
                                                    for buyer, token in self.tokens.items()]}), encoding="utf-8")
        access.chmod(0o600)
        self.settings = AppSettings(tokens_file=access, runtime_dir=self.root / "runtime",
            model_enabled=True, model_api_key="fake-offline-shopping-key", model_base_url="https://offline.invalid",
            model_name="mock-model-no-network", catalog_demo_enabled=True, mcp_enabled=False, memory_enabled=False)
        self.engine = create_engine("sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False}, poolclass=StaticPool).execution_options(schema_translate_map={"public": None})
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        policy_root = Path(general_policy.__file__).parent / "knowledge"
        import_policy_files(self.engine, [policy_root / "2026-10-shoes.md", policy_root / "2026-11-shoes.md"])
        self.requests, self.invocations = [], []
        self.request_number = 0
        self.transport = httpx.MockTransport(self.model_http)
        # ASGI（进程内HTTP）仍正常工作；任何未注入的真实HTTP连接直接失败。
        for target in ("httpx.HTTPTransport.handle_request", "socket.create_connection"):
            blocker = patch(target, side_effect=AssertionError("external network forbidden in shopping HTTP tests"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def auth(self, buyer="B001"):
        return {"Authorization": "Bearer " + self.tokens[buyer]}

    def model_http(self, request):
        self.assertEqual(request.url.host, "offline.invalid")
        payload = json.loads(request.content)
        self.requests.append(payload)
        tool_messages = [message for message in payload["messages"] if message.get("role") == "tool"]
        if not tool_messages:
            offered = payload["tools"]
            self.assertEqual(len(offered), 1)
            name = offered[0]["function"]["name"]
            self.assertIn(name, {"lookup_general_policy", "recommend_products"})
            reply = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "offline-tool-1", "type": "function", "function": {"name": name, "arguments": "{}"}}]}
            finish_reason = "tool_calls"
        else:
            # Model double selects actual tool evidence; it does not create business facts.
            fact = json.loads(tool_messages[-1]["content"])
            if "candidates" in fact:
                content = {"selected_variant_ids": [candidate["variant_id"] for candidate in fact["candidates"]]}
            else:
                content = {"citation_ids": list(fact.get("citation_index", {}))}
            reply = {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)}
            finish_reason = "stop"
        return httpx.Response(200, json={"choices": [{"finish_reason": finish_reason, "message": reply}],
                                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    def actual_agent(self, settings, engine, message, buyer_id, history, cancelled, **kwargs):
        self.invocations.append({"buyer_id": buyer_id, "message": message, "history": deepcopy(history),
                                 "state": deepcopy(kwargs.get("condition_state"))})
        # Preserve actual runtime and default handlers; only inject the HTTP model transport.
        return run_agent(settings, engine, message, buyer_id, history, cancelled,
                         transport=self.transport, **kwargs)

    def forbidden_legacy_branch(self, *args, **kwargs):
        self.fail("new shopping business must not enter legacy multi-agent branches")

    @contextmanager
    def client(self):
        app = create_app(self.settings, self.engine, agent=self.actual_agent, multi_agent=self.forbidden_legacy_branch)
        with TestClient(app) as client:
            yield client, app

    def ask(self, client, message, *, session="shopping-chat", buyer="B001", mode="single"):
        self.request_number += 1
        response = client.post("/api/tasks", headers=self.auth(buyer), json={"message": message,
            "request_id": f"shopping-request-{self.request_number:04d}", "session_id": session, "execution_mode": mode})
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = client.get("/api/tasks/" + task_id, headers=self.auth(buyer))
            self.assertEqual(response.status_code, 200, response.text)
            task = response.json()
            if task["status"] not in {"queued", "running", "cancelling"}:
                return task
            threading.Event().wait(.005)
        self.fail("offline HTTP task did not finish within 5 seconds")

    def export(self, client, task):
        response = client.get("/api/tasks/" + task["id"] + "/export", headers=self.auth())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("attachment", response.headers.get("content-disposition", ""))
        return response.json()

    def test_general_policy_queries_real_policy_library_without_order_tables(self):
        self.assertNotIn("orders", inspect(self.engine).get_table_names())
        with self.client() as (client, _):
            task = self.ask(client, POLICY_QUESTION)
            self.assertEqual(task["status"], "completed", task)
            self.assertEqual(task["conditions"]["kind"], "general_policy")
            self.assertNotIn("order_id", task["conditions"]["confirmed"])
            self.assertNotIn("订单编号", task["answer"])
            fact = task["facts"][0]
            self.assertEqual(fact["tool"], "lookup_general_policy")
            self.assertEqual(fact["result"]["order_eligibility"], "not_evaluated")
            self.assertEqual(fact["result"]["rules"]["return_window_hours"], 336)
            self.assertEqual(fact["result"]["selection_basis"], "policy_query_time")
            self.assertTrue(task["shopping_validation"]["passed"])
            self.assertEqual(len(self.requests), 2)

    def test_missing_policy_category_can_be_supplied_in_same_http_session(self):
        with self.client() as (client, _):
            first = self.ask(client, "本店退货和运费规则，政策日期" + POLICY_TIME)
            self.assertEqual(first["status"], "clarification", first)
            self.assertEqual(first["facts"][0]["result"]["missing"], ["category"])
            self.assertIn("无需提供订单号", first["answer"])
            second = self.ask(client, "普通鞋类")
            self.assertEqual(second["status"], "completed", second)
            self.assertEqual(second["conditions"]["confirmed"]["category"]["value"], "ordinary_shoes")
            self.assertEqual(second["conditions"]["confirmed"]["policy_at"]["value"], POLICY_TIME)
            self.assertEqual(second["facts"][0]["result"]["rules"]["return_window_hours"], 336)
            self.assertNotIn("order_id", second["conditions"]["confirmed"])
            stored = self.export(client, second)["result"]
            self.assertEqual(stored["history"][-4]["content"], first["message"])
            self.assertEqual(stored["history"][-1]["content"], second["answer"])

    def test_explicit_t_shirt_store_policy_is_not_a_shopping_budget_question(self):
        with self.client() as (client, _):
            task = self.ask(client, "本店T恤退货政策是什么")
            self.assertEqual(task["status"], "completed", task)
            self.assertEqual(task["conditions"]["kind"], "general_policy")
            self.assertEqual(task["conditions"]["confirmed"]["category"]["value"], "t_shirt")
            self.assertNotIn("budget_max_minor", task["conditions"]["missing"])
            self.assertNotIn("预算", task["answer"])
            self.assertEqual(task["facts"][0]["tool"], "lookup_general_policy")
            fact = task["facts"][0]["result"]
            self.assertEqual(fact["status"], "insufficient_evidence")
            self.assertEqual(fact["reason"], "no_supported_policy")
            self.assertEqual(fact["scope"]["category"], "t_shirt")
            self.assertEqual(fact["rules"], {})
            self.assertEqual(fact["citation_index"], {})
            self.assertNotIn("336小时", task["answer"])
            self.assertNotIn("168小时", task["answer"])

    def test_catalog_switches_to_t_shirt_policy_but_explicit_recommendation_returns_to_catalog(self):
        with self.client() as (client, _):
            first = self.ask(client, CATALOG_QUESTION)
            self.assertEqual(first["status"], "completed", first)
            policy = self.ask(client, "本店T恤退货政策是什么")
            self.assertEqual(policy["status"], "completed", policy)
            self.assertEqual(policy["conditions"]["kind"], "general_policy")
            self.assertTrue(policy["conditions"]["switched"])
            for field in ("budget_max_minor", "currency", "material", "cotton_percentage", "size"):
                self.assertNotIn(field, policy["conditions"]["confirmed"])
            self.assertEqual(self.invocations[-1]["history"], [])
            self.assertEqual(policy["facts"][0]["tool"], "lookup_general_policy")
            self.assertEqual(policy["facts"][0]["result"]["reason"], "no_supported_policy")
            self.assertEqual(policy["facts"][0]["result"]["rules"], {})
            self.assertEqual(policy["facts"][0]["result"]["citation_index"], {})
            self.assertNotIn("预算", policy["answer"])
            # A genuine recommendation request with a return condition still uses the catalog.
            recommendation = self.ask(client, "推荐要能退的M码纯棉T恤，预算30美元")
            self.assertEqual(recommendation["status"], "completed", recommendation)
            self.assertEqual(recommendation["conditions"]["kind"], "catalog")
            self.assertTrue(recommendation["conditions"]["switched"])
            self.assertTrue(recommendation["conditions"]["confirmed"]["require_returnable"]["value"])
            self.assertEqual(recommendation["facts"][0]["tool"], "recommend_products")
            self.assertEqual(recommendation["facts"][0]["result"]["status"], "insufficient_evidence")
            self.assertEqual(recommendation["facts"][0]["result"]["candidates"], [])

    def test_specific_order_leaves_general_policy_even_when_assessment_time_needs_clarification(self):
        with self.client() as (client, _):
            first = self.ask(client, POLICY_QUESTION)
            self.assertEqual(first["status"], "completed", first)
            before = len(self.requests)
            # No order database is needed: missing precise assessment time stops before tools.
            task = self.ask(client, "我的订单O_SUPPORT_OK能退吗，判断时间不确定")
            self.assertEqual(task["status"], "clarification", task)
            self.assertEqual(task["conditions"]["kind"], "support")
            self.assertTrue(task["conditions"]["switched"])
            self.assertEqual(task["conditions"]["confirmed"]["order_id"]["value"], "O_SUPPORT_OK")
            self.assertNotIn("policy_at", task["conditions"]["confirmed"])
            self.assertEqual(task["facts"], [])
            self.assertEqual(len(self.requests), before)
            self.assertEqual(self.invocations[-1]["history"], [])

    def test_catalog_missing_currency_then_usd_uses_actual_default_filter(self):
        with self.client() as (client, _):
            first = self.ask(client, "推荐M码纯棉T恤，预算30")
            self.assertEqual(first["status"], "clarification", first)
            self.assertIn("currency", first["conditions"]["missing"])
            self.assertEqual(self.requests, [])
            second = self.ask(client, "USD")
            self.assertEqual(second["status"], "completed", second)
            self.assertEqual(second["conditions"]["confirmed"]["budget_max_minor"]["value"], 3000)
            fact = second["facts"][0]["result"]
            self.assertEqual([item["variant_id"] for item in fact["candidates"]], ["V-A", "V-B", "V-BOUNDARY"])
            self.assertEqual(fact["evaluation_basis"], "explicit_synthetic_snapshot_replay")
            self.assertEqual(fact["data_kind"], "synthetic_demo")
            self.assertIn("不是当前真实库存", second["answer"])
            self.assertEqual(second["usage"]["tool_calls"], 1)
            third = self.ask(client, "预算改为25美元")
            self.assertEqual(third["status"], "completed", third)
            self.assertEqual([item["variant_id"] for item in third["facts"][0]["result"]["candidates"]], ["V-A"])
            self.assertEqual(third["conditions"]["confirmed"]["size"]["value"], "M")

    def test_catalog_references_survive_http_history_persistence_and_export(self):
        with self.client() as (client, app):
            task = self.ask(client, CATALOG_QUESTION)
            self.assertEqual(task["status"], "completed", task)
            exported = self.export(client, task)
            result = exported["result"]
            self.assertEqual(result["facts"], task["facts"])
            self.assertEqual(result["history"][-1]["content"], task["answer"])
            persisted = app.state.store.get(task["id"], "buyer:B001")
            self.assertEqual(persisted, exported)
            fact = result["facts"][0]
            self.assertEqual(fact["result_snapshot_sha256"], hashlib.sha256(json.dumps(fact["result"], ensure_ascii=False, sort_keys=True).encode()).hexdigest())
            raw = (Path(catalog_recommendations.__file__).parent / "samples/catalog_demo.json").read_bytes()
            self.assertEqual(fact["result"]["source"]["sha256"], hashlib.sha256(raw).hexdigest())
            source_catalog = json.loads(raw)
            for candidate in fact["result"]["candidates"]:
                reference = "CATALOG:" + candidate["variant_id"]
                self.assertIn("[" + reference + "]", task["answer"])
                citation = fact["result"]["citation_index"][reference]
                pointer = source_catalog
                for part in citation["pointer"].split("/")[1:]:
                    pointer = pointer[int(part)] if isinstance(pointer, list) else pointer[part]
                self.assertEqual(pointer["variant_id"], candidate["variant_id"])
                self.assertEqual(citation["source_sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(candidate["matched_attributes"]["cotton_percentage"], 100)
            recent = client.get("/api/tasks", headers=self.auth()).json()
            self.assertEqual(recent[0]["facts"], task["facts"])
            serialized = json.dumps(exported, ensure_ascii=False)
            self.assertNotIn(self.settings.model_api_key, serialized)
            self.assertNotIn(self.tokens["B001"], serialized)
            self.assertNotIn(str(Path(catalog_recommendations.__file__).parent), serialized)

    def test_cross_business_switch_discards_old_catalog_or_policy_conditions(self):
        with self.client() as (client, _):
            catalog = self.ask(client, CATALOG_QUESTION)
            self.assertEqual(catalog["status"], "completed", catalog)
            policy = self.ask(client, POLICY_QUESTION)
            self.assertEqual(policy["status"], "completed", policy)
            self.assertTrue(policy["conditions"]["switched"])
            for field in ("budget_max_minor", "currency", "material", "size"):
                self.assertNotIn(field, policy["conditions"]["confirmed"])
            self.assertEqual(self.invocations[-1]["history"], [])
            fresh = self.ask(client, "推荐T恤")
            self.assertEqual(fresh["status"], "clarification", fresh)
            self.assertTrue(fresh["conditions"]["switched"])
            self.assertIn("currency", fresh["conditions"]["missing"])
            self.assertIn("budget_max_minor", fresh["conditions"]["missing"])
            self.assertNotIn("policy_at", fresh["conditions"]["confirmed"])
            self.assertNotIn("size", fresh["conditions"]["confirmed"])
            self.assertEqual(self.invocations[-1]["history"], [])

    def test_other_buyer_cannot_read_export_cancel_or_inherit_shopping_task(self):
        with self.client() as (client, _):
            task = self.ask(client, CATALOG_QUESTION)
            self.assertEqual(task["status"], "completed", task)
            for suffix in ("", "/export"):
                response = client.get("/api/tasks/" + task["id"] + suffix, headers=self.auth("B002"))
                self.assertEqual(response.status_code, 404)
            self.assertEqual(client.post("/api/tasks/" + task["id"] + "/cancel", headers=self.auth("B002")).status_code, 404)
            self.assertEqual(client.get("/api/tasks", headers=self.auth("B002")).json(), [])
            other = self.ask(client, "推荐T恤", buyer="B002")
            self.assertEqual(other["status"], "clarification", other)
            self.assertIn("currency", other["conditions"]["missing"])
            self.assertNotIn("size", other["conditions"]["confirmed"])

    def test_multi_selection_truthfully_reports_single_shopping_execution(self):
        with self.client() as (client, _):
            for index, message in enumerate((POLICY_QUESTION, CATALOG_QUESTION)):
                with self.subTest(message=message):
                    task = self.ask(client, message, mode="multi", session="multi-shopping-" + str(index))
                    self.assertEqual(task["status"], "completed", task)
                    self.assertEqual(task["execution_mode"], "multi")
                    self.assertEqual(task["orchestration"]["requested"], "multi")
                    self.assertEqual(task["orchestration"]["effective"], "single")
                    self.assertEqual(task["orchestration"]["branches"], [])
                    self.assertIn("不拆成旧金额", task["orchestration"]["reason"])

    def test_returnability_requirement_cannot_reuse_previous_candidates_as_verified(self):
        with self.client() as (client, _):
            first = self.ask(client, CATALOG_QUESTION)
            self.assertEqual(first["status"], "completed", first)
            task = self.ask(client, "要能退")
            self.assertEqual(task["status"], "completed", task)
            self.assertTrue(task["conditions"]["confirmed"]["require_returnable"]["value"])
            fact = task["facts"][0]["result"]
            self.assertEqual(fact["status"], "insufficient_evidence")
            self.assertEqual(fact["candidates"], [])
            self.assertEqual(fact["citation_index"], {})
            self.assertIn("尚缺此商品适用的退货政策", task["answer"])
            self.assertNotIn("[CATALOG:", task["answer"])

    def test_unsupported_followup_prevents_new_tool_and_keeps_original_requirements(self):
        with self.client() as (client, _):
            first = self.ask(client, CATALOG_QUESTION)
            self.assertEqual(first["status"], "completed", first)
            before = len(self.requests)
            task = self.ask(client, "要蓝色")
            self.assertEqual(task["status"], "clarification", task)
            self.assertIn("unsupported_color", task["conditions"]["issues"])
            self.assertEqual(task["conditions"]["confirmed"]["budget_max_minor"]["value"], 3000)
            self.assertEqual(task["facts"], [])
            self.assertEqual(len(self.requests), before)


if __name__ == "__main__":
    unittest.main()
