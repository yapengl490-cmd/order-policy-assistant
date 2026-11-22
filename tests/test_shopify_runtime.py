"""Independent platform chat integration; model and platform tools are doubles.

These tests execute real routing/run_agent/app authorization/TaskStore/rendering,
but never read live credentials or connect to Shopify/a model endpoint.
"""
from copy import deepcopy
from datetime import datetime
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
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from agent_runtime import run_agent
from app_config import AppSettings
from app_server import create_app
from condition_state import reconcile_conditions
from shopify_chat import _attach_citations
from shopify_orders import normalize_order, normalize_current_policies


NOW = datetime.fromisoformat("2026-11-20T06:45:00+00:00")
SHOP = "offline-runtime.myshopify.com"
CONTEXT = {"shop_domain": SHOP, "allow_network": True}
QUESTION = "Shopify #1001，退款截止2026-11-20T14:45:00+08:00"


def order_fact():
    def tx(number, kind, amount, stamp):
        return {"id": "gid://shopify/OrderTransaction/" + str(number), "kind": kind,
                "status": "SUCCESS", "test": True, "processedAt": stamp,
                "amountSet": {"shopMoney": {"amount": amount, "currencyCode": "USD"}}}
    raw = {"id": "gid://shopify/Order/1001", "name": "#1001", "test": True,
           "createdAt": "2026-11-20T06:14:00Z", "displayFinancialStatus": "PARTIALLY_REFUNDED",
           "totalPriceSet": {"shopMoney": {"amount": "100.00", "currencyCode": "USD"}},
           "totalRefundedSet": {"shopMoney": {"amount": "30.00", "currencyCode": "USD"}},
           "transactionsCount": {"count": 2, "precision": "EXACT"},
           "transactions": [tx(1, "CAPTURE", "100.00", "2026-11-20T06:15:00Z"),
                            tx(2, "REFUND", "30.00", "2026-11-20T06:30:00Z")]}
    return _attach_citations(normalize_order(raw, shop_domain=SHOP, observed_at=NOW.isoformat(), refund_before=NOW.isoformat()))


class ShopifyRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shopify-runtime-offline-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.access = self.root / "access.json"
        self.token = "offline-account-token"
        self.access.write_text(json.dumps({"accounts": [{"token": self.token, "buyer_id": "B_TEST", "roles": [
            {"role": "buyer", "buyer_id": "B_TEST"},
            {"role": "merchant", "shop_id": "LOCAL-MERCHANT", "platform_context": CONTEXT}]}]}))
        self.access.chmod(0o600)
        self.settings = AppSettings(tokens_file=self.access, runtime_dir=self.root / "runtime", model_enabled=True,
            model_api_key="offline-model-key-not-real", model_base_url="https://offline.invalid", model_name="offline-double",
            mcp_enabled=False, memory_enabled=False)
        self.engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
                                   poolclass=StaticPool).execution_options(schema_translate_map={"public": None})
        self.addCleanup(self.engine.dispose)
        self.fact = order_fact()
        self.requests, self.tool_calls, self.invocations = [], [], []
        self.arguments = {}; self.tool_name_override = None
        self.request_number = 0
        self.transport = httpx.MockTransport(self.model_http)
        for target in ("httpx.HTTPTransport.handle_request", "socket.create_connection", "shopify_chat.ShopifySettings.load"):
            blocker = patch(target, side_effect=AssertionError("live network/credentials forbidden"))
            blocker.start(); self.addCleanup(blocker.stop)

    def model_http(self, request):
        self.assertEqual(request.url.host, "offline.invalid")
        payload = json.loads(request.content); self.requests.append(payload)
        tool_messages = [item for item in payload["messages"] if item.get("role") == "tool"]
        if not tool_messages:
            offered = payload["tools"]
            self.assertEqual(len(offered), 1)
            name = self.tool_name_override or offered[0]["function"]["name"]
            self.assertIn(offered[0]["function"]["name"], {"shopify_check_order", "shopify_current_policy"})
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "platform-tool-1", "type": "function",
                "function": {"name": name, "arguments": json.dumps(self.arguments)}}]}
            reason = "tool_calls"
        else:
            fact = json.loads(tool_messages[-1]["content"])
            message = {"role": "assistant", "content": json.dumps({"citation_ids": list(fact.get("citation_index", {}))})}
            reason = "stop"
        return httpx.Response(200, json={"choices": [{"finish_reason": reason, "message": message}],
                                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    def handler(self):
        self.tool_calls.append({})
        return deepcopy(self.fact)

    def direct(self, question=QUESTION, *, role="merchant", context=CONTEXT, previous=None):
        state = reconcile_conditions(question, previous, NOW, "runtime-platform-turn")
        result = run_agent(self.settings, self.engine, question, "B_TEST", [], lambda: False, access_role=role,
            shop_id="LOCAL-MERCHANT" if role == "merchant" else None, condition_state=state,
            platform_context=deepcopy(context), transport=self.transport,
            tool_handlers={"shopify_check_order": self.handler, "shopify_current_policy": self.handler})
        return state, result

    def actual_agent(self, settings, engine, message, buyer_id, history, cancelled, **kwargs):
        self.invocations.append({"buyer_id": buyer_id, "history": deepcopy(history), "kwargs": deepcopy(kwargs)})
        return run_agent(settings, engine, message, buyer_id, history, cancelled, transport=self.transport,
            tool_handlers={"shopify_check_order": self.handler, "shopify_current_policy": self.handler}, **kwargs)

    def headers(self, role="merchant"):
        return {"Authorization": "Bearer " + self.token, "X-Access-Mode": role}

    def ask(self, client, message=QUESTION, *, role="merchant", session="platform-chat"):
        self.request_number += 1
        response = client.post("/api/tasks", headers=self.headers(role), json={"message": message, "session_id": session,
                              "request_id": "platform-request-" + str(self.request_number), "execution_mode": "single"})
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = client.get("/api/tasks/" + task_id, headers=self.headers(role))
            self.assertEqual(response.status_code, 200, response.text)
            task = response.json()
            if task["status"] not in {"queued", "running", "cancelling"}:
                return task
            threading.Event().wait(.005)
        self.fail("offline task did not finish")

    def test_real_route_run_agent_tool_evidence_and_render_keep_platform_money(self):
        state, result = self.direct()
        self.assertEqual(state["kind"], "shopify_order")
        self.assertEqual(state["allowed_tools"], ["shopify_check_order"])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["shopping_validation"]["passed"])
        self.assertEqual(len(self.requests), 2); self.assertEqual(self.tool_calls, [{}])
        fact = result["facts"][0]["result"]
        self.assertEqual((fact["paid_amount_minor"], fact["refund_amount_minor"], fact["net_amount_minor"]), (10000, 3000, 7000))
        self.assertEqual(fact["currency"], "USD"); self.assertEqual(fact["minor_unit_digits"], 2)
        for text in ("100.00 USD", "30.00 USD", "70.00 USD", "2026-11-20T06:15:00+00:00", "2026-11-20T06:30:00+00:00"):
            self.assertIn(text, result["answer"])
        self.assertNotIn("paid_amount_fen", fact)
        self.assertNotEqual(fact["created_at"], fact["successful_payments"][0]["processed_at"])

    def test_unbound_disabled_and_buyer_refused_before_any_model_or_tool(self):
        for role, context in (("merchant", None), ("merchant", {"shop_domain": SHOP}),
                              ("merchant", {"shop_domain": SHOP, "allow_network": False}), ("buyer", CONTEXT)):
            with self.subTest(role=role, context=context):
                state, result = self.direct(role=role, context=context)
                self.assertEqual(result["status"], "clarification")
                self.assertEqual(result["usage"]["model_requests"], 0)
                self.assertEqual(result["facts"], [])
        self.assertFalse(self.requests); self.assertFalse(self.tool_calls)

    def test_model_cannot_override_server_shop_order_or_cutoff(self):
        for args in ({"shop_domain": "other.myshopify.com"}, {"order_id": "#9999"}, {"refund_before": "2020-01-01T00:00:00Z"}):
            with self.subTest(args=args):
                self.arguments = args
                state, result = self.direct()
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["facts"])
        self.assertFalse(self.tool_calls)

    def test_failed_platform_fact_never_becomes_completed_or_no_orders(self):
        self.fact = _attach_citations({"status": "failed", "platform": "shopify", "notice": "offline failure"})
        state, result = self.direct()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["facts"][0]["result"]["status"], "failed")
        self.assertIn("只读查询失败", result["answer"])
        self.assertFalse(result["facts"][0]["result"]["citation_index"])

    def test_policy_without_refund_evidence_never_borrows_s001_or_privacy(self):
        self.fact = _attach_citations(normalize_current_policies({"myshopifyDomain": SHOP, "shopPolicies": [
            {"type": "PRIVACY_POLICY", "body": "Unrelated privacy source"}]}, shop_domain=SHOP, observed_at=NOW.isoformat()))
        with patch("general_policy.lookup_general_policy", side_effect=AssertionError("must not query synthetic shoe policy")):
            state, result = self.direct("Shopify当前退款政策")
        self.assertEqual(state["kind"], "shopify_policy")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["facts"][0]["tool"], "shopify_current_policy")
        self.assertEqual(result["facts"][0]["result"]["status"], "insufficient_evidence")
        self.assertEqual(result["facts"][0]["result"]["citation_index"], {})
        self.assertIn("不能用隐私政策或S001合成鞋类政策代替", result["answer"])
        self.assertNotIn("336", result["answer"])

    def test_explicit_local_order_exits_platform_route(self):
        platform = reconcile_conditions(QUESTION, None, NOW, "platform")
        local = reconcile_conditions("查O001退款金额", platform, NOW, "local")
        self.assertEqual(local["kind"], "order")
        self.assertEqual(local["confirmed"]["order_id"]["value"], "O001")
        self.assertNotIn("shopify_check_order", local["allowed_tools"])
        self.assertTrue(local["switched"])

    def test_app_server_binding_comes_from_account_role_and_history_export_preserve_money(self):
        app = create_app(self.settings, self.engine, agent=self.actual_agent)
        with TestClient(app) as client:
            task = self.ask(client)
            self.assertEqual(task["status"], "completed", task.get("answer"))
            self.assertEqual(task["conditions"]["kind"], "shopify_order")
            call = self.invocations[0]["kwargs"]
            self.assertEqual(call["access_role"], "merchant")
            self.assertEqual(call["shop_id"], "LOCAL-MERCHANT")
            self.assertEqual(call["platform_context"], CONTEXT)
            response = client.get("/api/tasks/" + task["id"] + "/export", headers=self.headers())
            self.assertEqual(response.status_code, 200)
            exported = response.json(); result = exported["result"]
            self.assertEqual(result["facts"], task["facts"])
            self.assertEqual(result["history"][-1]["content"], task["answer"])
            self.assertEqual(exported, app.state.store.get(task["id"], "merchant:LOCAL-MERCHANT"))
            fact = result["facts"][0]["result"]
            self.assertEqual(fact["currency"], "USD"); self.assertEqual(fact["minor_unit_digits"], 2)
            self.assertEqual(fact["refund_amount_minor"], 3000)
            self.assertEqual(fact["successful_refunds"][0]["processed_at"], "2026-11-20T06:30:00+00:00")
            self.assertEqual(set(fact["citation_index"]), {"SHOPIFY-ORDER:1001", "SHOPIFY-TX:1", "SHOPIFY-TX:2"})
            self.assertEqual(result["facts"][0]["result_snapshot_sha256"], hashlib.sha256(json.dumps(fact, ensure_ascii=False, sort_keys=True).encode()).hexdigest())
            recent = client.get("/api/tasks", headers=self.headers()).json()
            self.assertEqual(recent[0]["facts"], task["facts"])
            second = self.ask(client, "继续")
            self.assertEqual(second["status"], "completed")
            self.assertEqual(self.invocations[-1]["history"][-1]["content"], task["answer"])
            serialized = json.dumps(exported, ensure_ascii=False)
            self.assertNotIn(self.token, serialized)
            self.assertNotIn(self.settings.model_api_key, serialized)

    def test_app_body_cannot_supply_platform_context_or_merchant_identity(self):
        app = create_app(self.settings, self.engine, agent=self.actual_agent)
        with TestClient(app) as client:
            for field, value in (("platform_context", CONTEXT), ("shop_id", "LOCAL-MERCHANT"), ("access_role", "merchant")):
                response = client.post("/api/tasks", headers=self.headers("buyer"), json={"message": QUESTION,
                    "session_id": "body-attempt", "request_id": "body-attempt-123", field: value})
                self.assertEqual(response.status_code, 422)
            task = self.ask(client, role="buyer")
            self.assertEqual(task["status"], "clarification")
            self.assertFalse(task["facts"])
            self.assertNotIn("access_role", self.invocations[-1]["kwargs"])
            self.assertIsNone(self.invocations[-1]["kwargs"]["platform_context"])
        self.assertFalse(self.requests); self.assertFalse(self.tool_calls)


if __name__ == "__main__":
    unittest.main()
