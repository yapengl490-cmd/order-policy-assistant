"""新入口偏好 HTTP 回归：真实应用/状态/渲染/TaskStore，隔离 SQLite。

模型 HTTP 与 Shopify 外部读取边界是替身；商品使用合成目录。
禁止真实网络、正式数据库和付费调用，不代表本人或真实平台验收。
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from agent_runtime import run_agent
from app_config import AppSettings
from app_server import create_app
from policy_kb import metadata, import_policy_files
from preference_store import PreferenceStore, preference_metadata

CATALOG = "推荐M码纯棉T恤，预算不超过30美元"
POLICY = "本店普通鞋类退货和运费规则，政策日期2026-11-20T12:00:00+08:00"
PLATFORM_ORDER = "核查Shopify #1001，退款截止2026-11-20T15:00:00+08:00"
PLATFORM_POLICY = "Shopify当前退货政策"
SHOP = "offline-demo.myshopify.com"


def platform_order_fact():
    return {"status": "ok", "platform": "shopify", "shop_domain": SHOP,
        "order_id": "gid://shopify/Order/1001", "order_name": "#1001",
        "currency": "USD", "minor_unit_digits": 2, "paid_amount_minor": 10000,
        "refund_amount_minor": 3000, "net_amount_minor": 7000,
        "refund_before": "2026-11-20T07:00:00+00:00",
        "successful_payments": [{"transaction_id": "gid://shopify/OrderTransaction/1",
            "kind": "CAPTURE", "status": "SUCCESS", "processed_at": "2026-11-20T06:15:00+00:00",
            "currency": "USD", "amount_minor": 10000}],
        "successful_refunds": [{"transaction_id": "gid://shopify/OrderTransaction/2",
            "kind": "REFUND", "status": "SUCCESS", "processed_at": "2026-11-20T06:30:00+00:00",
            "currency": "USD", "amount_minor": 3000, "included_before_cutoff": True}]}


def platform_policy_fact():
    body = "<p>演示政策：普通商品收货七天内可申请；定制商品不适用。</p>"
    return {"status": "ok", "platform": "shopify", "shop_domain": SHOP,
        "historical_applicability": "unknown", "policies": [{
            "title": "演示退款政策", "source_id": "gid://shopify/ShopPolicy/1",
            "citation_id": "SHOPIFY-POLICY:1", "body_html": body,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "updated_at": "2026-11-20T03:00:00+00:00"}]}


class ShoppingPreferencesHTTPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="shopping-preferences-http-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        roles = [{"role": "buyer", "buyer_id": "B001"}, {"role": "merchant", "shop_id": "S001",
                  "platform_context": {"shop_domain": SHOP, "allow_network": True}}]
        self.accounts = [{"account_id": "person_A001", "token": "offline-preference-person-a", "roles": deepcopy(roles)},
                         {"account_id": "person_B001", "token": "offline-preference-person-b", "roles": deepcopy(roles)}]
        access = self.root / "access.json"
        access.write_text(json.dumps({"accounts": self.accounts}), encoding="utf-8")
        access.chmod(0o600)
        self.settings = AppSettings(tokens_file=access, runtime_dir=self.root / "runtime", model_enabled=True,
            model_api_key="offline-test-not-a-real-key", model_base_url="https://offline.invalid",
            model_name="http-model-double", catalog_demo_enabled=True, memory_enabled=True, max_daily_tasks=50)
        self.engine = create_engine("sqlite:///" + str(self.root / "business.db"),
            execution_options={"schema_translate_map": {"public": None}})
        self.memory_engine = create_engine("sqlite:///" + str(self.root / "memory.db"),
            execution_options={"schema_translate_map": {"app_memory": None}})
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.memory_engine.dispose)
        metadata.create_all(self.engine)
        preference_metadata.create_all(self.memory_engine)
        source = Path(__file__).resolve().parents[1] / "knowledge"
        import_policy_files(self.engine, [source / "2026-10-shoes.md", source / "2026-11-shoes.md"])
        self.requests, self.invocations = [], []
        self.serial = 0
        self.pause_next_model = False
        self.model_started, self.model_release = threading.Event(), threading.Event()
        self.addCleanup(self.model_release.set)
        self.transport = httpx.MockTransport(self.model_http)
        for target in ("httpx.HTTPTransport.handle_request", "socket.create_connection"):
            blocker = patch(target, side_effect=AssertionError("real network forbidden in preference tests"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def headers(self, person=0, role="buyer"):
        return {"Authorization": "Bearer " + self.accounts[person]["token"], "X-Access-Mode": role}

    def model_http(self, request):
        self.assertEqual(request.url.host, "offline.invalid")
        body = json.loads(request.content)
        self.requests.append(deepcopy(body))
        if self.pause_next_model:
            self.pause_next_model = False
            self.model_started.set()
            if not self.model_release.wait(5):
                raise AssertionError("test did not release paused model double")
        tools = [item for item in body["messages"] if item.get("role") == "tool"]
        if not tools:
            self.assertEqual(len(body["tools"]), 1)
            name = body["tools"][0]["function"]["name"]
            self.assertIn(name, {"recommend_products", "lookup_general_policy", "shopify_check_order", "shopify_current_policy"})
            answer = {"role": "assistant", "content": None, "tool_calls": [{"id": "offline-call", "type": "function",
                "function": {"name": name, "arguments": "{}"}}]}
            finish = "tool_calls"
        else:
            fact = json.loads(tools[-1]["content"])
            selected = ({"selected_variant_ids": [item["variant_id"] for item in fact["candidates"]]}
                        if "candidates" in fact else {"citation_ids": list(fact.get("citation_index", {}))})
            answer = {"role": "assistant", "content": json.dumps(selected, ensure_ascii=False)}
            finish = "stop"
        return httpx.Response(200, json={"choices": [{"finish_reason": finish, "message": answer}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    def actual_agent(self, settings, engine, message, buyer_id, history, cancelled, **kwargs):
        self.invocations.append({"snapshot": deepcopy(kwargs.get("preference_snapshot")),
                                 "history": deepcopy(history), "role": kwargs.get("access_role", "buyer")})
        return run_agent(settings, engine, message, buyer_id, history, cancelled, transport=self.transport, **kwargs)

    @contextmanager
    def client(self):
        app = create_app(self.settings, self.engine, agent=self.actual_agent, preference_engine=self.memory_engine)
        # Actual factory validation and citations run; only platform I/O is replaced.
        with patch("shopify_chat.ShopifySettings.load"), patch("shopify_chat.ShopifyReadClient") as reader:
            reader.return_value.read_order.side_effect = lambda *a, **kw: platform_order_fact()
            reader.return_value.read_current_policies.side_effect = platform_policy_fact
            with TestClient(app) as client:
                yield client

    def save(self, client, values, *, version=0, person=0, role="buyer"):
        response = client.put("/api/preferences", headers=self.headers(person, role), json={"values": values, "expected_version": version})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def preferences(self, client, person=0, role="buyer"):
        response = client.get("/api/preferences", headers=self.headers(person, role))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def submit(self, client, message, *, person=0, role="buyer", session=None):
        self.serial += 1
        response = client.post("/api/tasks", headers=self.headers(person, role), json={"message": message,
            "request_id": f"preference-request-{self.serial:04d}", "session_id": session or f"session-{self.serial:04d}"})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["id"]

    def wait_task(self, client, task_id, *, person=0, role="buyer"):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = client.get("/api/tasks/" + task_id, headers=self.headers(person, role))
            self.assertEqual(response.status_code, 200, response.text)
            task = response.json()
            if task["status"] not in {"queued", "running", "cancelling"}:
                return task
            threading.Event().wait(.005)
        self.fail("offline task did not finish")

    def ask(self, client, message, *, person=0, role="buyer", session=None):
        return self.wait_task(client, self.submit(client, message, person=person, role=role, session=session), person=person, role=role)

    def export(self, client, task, *, person=0, role="buyer"):
        response = client.get("/api/tasks/" + task["id"] + "/export", headers=self.headers(person, role))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("attachment", response.headers["content-disposition"])
        return response.json()

    def assert_loaded(self, task, values, version=1):
        self.assertEqual(task["status"], "completed", task)
        self.assertIsInstance(task["memory"], dict, "new entry must retain consumed preference evidence")
        self.assertEqual(task["memory"]["status"], "loaded")
        self.assertEqual(task["memory"]["stored_values"], values)
        self.assertEqual(task["memory"]["version"], version)

    def test_all_four_new_entries_consume_saved_account_preferences(self):
        with self.client() as client:
            values = {"answer_style": "brief", "amount_format": "yuan"}
            self.save(client, values)
            self.save(client, values, role="merchant")
            for question, kind, role in ((CATALOG, "catalog", "buyer"), (POLICY, "general_policy", "buyer"),
                    (PLATFORM_ORDER, "shopify_order", "merchant"), (PLATFORM_POLICY, "shopify_policy", "merchant")):
                with self.subTest(kind=kind):
                    task = self.ask(client, question, role=role)
                    self.assert_loaded(task, values)
                    self.assertEqual(task["conditions"]["kind"], kind)
                    self.assertEqual(task["memory"]["effective_values"], values)
                    self.assertEqual(task["memory"]["overrides"], {})
                    self.assertEqual(self.export(client, task, role=role)["result"]["memory"], task["memory"])
            self.assertEqual(len(self.requests), 8)

    def test_current_detailed_overrides_brief_without_persisting_and_keeps_sources(self):
        with self.client() as client:
            saved = {role: self.save(client, {"answer_style": "brief"}, role=role)
                     for role in ("buyer", "merchant")}
            for question, role in ((CATALOG, "buyer"), (POLICY, "buyer"),
                                   (PLATFORM_ORDER, "merchant"), (PLATFORM_POLICY, "merchant")):
                with self.subTest(question=question):
                    brief = self.ask(client, question, role=role)
                    detailed = self.ask(client, question + "；本次详细说明", role=role)
                    self.assert_loaded(detailed, saved[role]["values"])
                    self.assertEqual(detailed["memory"]["effective_values"]["answer_style"], "detailed")
                    self.assertEqual(detailed["memory"]["overrides"], {"answer_style": "current_turn_explicit"})
                    self.assertEqual(self.preferences(client, role=role), saved[role])
                    self.assertEqual(detailed["facts"][0]["result"], brief["facts"][0]["result"])
                    self.assertNotEqual(detailed["answer"], brief["answer"])
                    self.assertGreater(len(detailed["answer"]), len(brief["answer"]))
                    for citation in re.findall(r"\[[^\[\]\n]+\]", brief["answer"]):
                        self.assertIn(citation, detailed["answer"])
                    exported = self.export(client, detailed, role=role)
                    self.assertEqual(exported["answer"], detailed["answer"])
                    self.assertEqual(exported["result"]["history"][-1]["content"], detailed["answer"])
                    audit = exported["result"]["presentation"]
                    self.assertEqual(audit["answer_style"], "detailed")
                    self.assertEqual(audit["amount_format_scope"], "CNY_only")
                    self.assertEqual(audit["currency_policy"], "preserve_source_currency_no_conversion")
                    self.assertFalse(audit["renderer_writes_long_term_preferences"])
                    self.assertEqual(detailed["usage"]["model_requests"], 2)
                    self.assertEqual(detailed["usage"]["tool_calls"], 1)

    def test_update_while_model_waits_keeps_snapshot_and_next_task_reads_new_version(self):
        with self.client() as client:
            saved = self.save(client, {"answer_style": "brief"})
            self.pause_next_model = True
            task_id = self.submit(client, CATALOG)
            try:
                self.assertTrue(self.model_started.wait(3), "task must reach actual HTTP model boundary")
                changed = self.save(client, {"answer_style": "detailed"}, version=1)
                self.assertEqual(changed["version"], 2)
            finally:
                self.model_release.set()
            first = self.wait_task(client, task_id)
            self.assert_loaded(first, saved["values"])
            self.assertEqual(first["memory"]["effective_values"], saved["values"])
            frozen = self.export(client, first)
            self.assertEqual(frozen["preference_snapshot"], saved)
            second = self.ask(client, CATALOG)
            self.assert_loaded(second, changed["values"], version=2)
            self.assertEqual(second["memory"]["effective_values"], changed["values"])
            self.assertNotEqual(first["answer"], second["answer"])
            self.assertEqual(self.export(client, first), frozen)

    def test_same_shop_accounts_and_same_account_roles_are_isolated(self):
        with self.client() as client:
            self.save(client, {"answer_style": "brief"})
            self.save(client, {"answer_style": "detailed"}, role="merchant")
            self.save(client, {"answer_style": "standard"}, person=1, role="merchant")
            results = []
            for person, role, style in ((0, "buyer", "brief"), (0, "merchant", "detailed"), (1, "merchant", "standard")):
                task = self.ask(client, POLICY, person=person, role=role, session="shared-session")
                self.assert_loaded(task, {"answer_style": style})
                self.assertEqual(task["memory"]["effective_values"], {"answer_style": style})
                results.append(task)
            self.assertEqual(self.preferences(client, 1, "buyer")["values"], {})
            self.assertEqual(client.get("/api/tasks/" + results[1]["id"] + "/export", headers=self.headers(1, "merchant")).status_code, 404)
            self.assertEqual(client.get("/api/tasks/" + results[1]["id"], headers=self.headers(0, "buyer")).status_code, 404)

    def test_history_export_unchanged_after_preference_update_delete_and_restart(self):
        with self.client() as client:
            self.save(client, {"answer_style": "detailed"})
            task = self.ask(client, POLICY)
            self.assert_loaded(task, {"answer_style": "detailed"})
            original = self.export(client, task)
            self.save(client, {"answer_style": "brief"}, version=1)
            deleted = client.request("DELETE", "/api/preferences", headers=self.headers(), json={"expected_version": 2})
            self.assertEqual(deleted.status_code, 200)
            self.assertEqual(self.export(client, task), original)
        with self.client() as restarted:
            self.assertEqual(self.export(restarted, task), original)
            history = restarted.get("/api/tasks", headers=self.headers()).json()
            self.assertEqual(history[0]["answer"], task["answer"])
            self.assertEqual(history[0]["memory"], task["memory"])
            self.assertEqual(self.preferences(restarted)["values"], {})

    def test_usd_facts_never_convert_or_relabel_by_cny_display_preference(self):
        with self.client() as client:
            previous = None
            for version, amount_format in enumerate(("yuan", "fen", "yuan_fixed2")):
                with self.subTest(amount_format=amount_format):
                    values = {"amount_format": amount_format, "answer_style": "detailed"}
                    self.save(client, values, version=version, role="merchant")
                    task = self.ask(client, PLATFORM_ORDER, role="merchant")
                    self.assert_loaded(task, values, version=version + 1)
                    fact = task["facts"][0]["result"]
                    self.assertEqual(fact["currency"], "USD")
                    self.assertEqual((fact["paid_amount_minor"], fact["refund_amount_minor"], fact["net_amount_minor"]), (10000, 3000, 7000))
                    for amount in ("100.00 USD", "30.00 USD", "70.00 USD"):
                        self.assertIn(amount, task["answer"])
                    for wrong in ("100.00 元", "10000分", "CNY", "人民币"):
                        self.assertNotIn(wrong, task["answer"])
                    if previous is not None:
                        self.assertEqual(fact, previous)
                    previous = fact
                    self.assertEqual(self.export(client, task, role="merchant")["result"]["facts"], task["facts"])

    def test_wrong_role_snapshot_is_not_consumed(self):
        with self.client() as client:
            merchant = self.save(client, {"answer_style": "brief", "amount_format": "fen"}, role="merchant")
            # Inject storage fault, not a forged public request field.
            with patch.object(PreferenceStore, "read", return_value=merchant):
                task = self.ask(client, CATALOG)
            self.assertEqual(task["status"], "completed", task)
            self.assertIsInstance(task["memory"], dict)
            self.assertEqual(task["memory"]["status"], "role_mismatch")
            self.assertEqual(task["memory"]["stored_values"], {})
            self.assertEqual(task["memory"]["effective_values"], {})
            self.assertIsNone(task["memory"]["version"])

    def test_preference_database_failure_stops_before_task_or_model(self):
        preference_metadata.drop_all(self.memory_engine)
        with self.client() as client:
            response = client.post("/api/tasks", headers=self.headers(), json={"message": CATALOG,
                "request_id": "preference-read-failure", "session_id": "failure-session"})
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(self.requests, [])
            self.assertEqual(self.invocations, [])
            self.assertEqual(list(self.settings.runtime_dir.glob("task-*.json")), [])

    def test_saved_cny_display_does_not_fill_missing_catalog_currency(self):
        with self.client() as client:
            self.save(client, {"amount_format": "yuan", "answer_style": "brief"})
            task = self.ask(client, "推荐M码纯棉T恤，预算不超过30")
            self.assertEqual(task["status"], "clarification", task)
            self.assertIn("currency", task["conditions"]["missing"])
            self.assertEqual(self.requests, [])
            self.assertIsInstance(task["memory"], dict)
            self.assertEqual(task["memory"]["effective_values"]["amount_format"], "yuan")


if __name__ == "__main__":
    unittest.main()
