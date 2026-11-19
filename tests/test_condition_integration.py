"""实际运行时+Mock模型、临时任务文件；不连接真实模型/业务库/协议服务。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_runtime import run_agent, SCHEMAS
from app_config import AppSettings
from app_server import create_app, public_record
from condition_state import reconcile_conditions

NOW = datetime(2026, 11, 16, 10, 30, tzinfo=timezone.utc)


def config(**changes):
    return SimpleNamespace(**({"model_api_key": "fake-integration-model-key", "model_base_url": "https://api.deepseek.com",
        "model_name": "offline", "max_model_calls": 4, "max_tool_calls": 6, "model_timeout_seconds": 30,
        "max_output_tokens": 1200, "mcp_enabled": False} | changes))


def tool(name, arguments, identifier="call-1"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def reply(*calls, status="completed", answer="已核对。"):
    message = {"role": "assistant", "content": None if calls else json.dumps({"status": status, "answer": answer}, ensure_ascii=False)}
    if calls:
        message["tool_calls"] = list(calls)
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}


class RuntimeConditionTests(unittest.TestCase):
    def state(self, text="核查O001，退款截止2026年11月3日零点"):
        return reconcile_conditions(text, None, NOW, "turn-1")

    def execute(self, replies, state, handlers=None, cfg=None, role="buyer", shop=None):
        self.requests = []
        pending = list(replies)
        def send(request):
            self.requests.append(json.loads(request.content))
            self.assertTrue(pending, "unexpected model request")
            item = pending.pop(0)
            if isinstance(item, Exception):
                raise item
            return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)
        return run_agent(cfg or config(), None, state["original_question"], "B001", [], lambda: False,
                         access_role=role, shop_id=shop, condition_state=state,
                         transport=httpx.MockTransport(send), tool_handlers=handlers)

    def test_missing_batch_condition_stops_before_model_or_mcp(self):
        state = self.state("统计2026年10月订单")
        forbidden = SimpleNamespace(OrderProtocolClient=lambda *_: self.fail("must not discover MCP"))
        with patch.dict(sys.modules, {"mcp_order_client": forbidden}):
            result = self.execute([], state, cfg=config(mcp_enabled=True))
        self.assertEqual(result["status"], "clarification")
        self.assertEqual(result["usage"]["model_requests"], 0)
        self.assertEqual(result["usage"]["tool_calls"], 0)
        self.assertEqual(result["condition_state"]["missing"], ["refund_before"])

    def test_unknown_cutoff_with_valid_id_stops_before_any_external_call(self):
        previous = self.state()
        state = reconcile_conditions("查O001，把退款截止改为最近一次", previous, NOW, "turn-2")
        forbidden = SimpleNamespace(OrderProtocolClient=lambda *_: self.fail("must not discover MCP"))
        with patch.dict(sys.modules, {"mcp_order_client": forbidden}):
            result = self.execute([], state, cfg=config(mcp_enabled=True))
        self.assertEqual(result["status"], "clarification")
        self.assertEqual(result["usage"]["model_requests"], 0)
        self.assertEqual(result["usage"]["tool_calls"], 0)
        self.assertEqual(result["facts"], [])
        self.assertIn("refund_before", result["condition_state"]["missing"])

    def test_invalid_but_well_formed_date_never_executes_then_one_repair(self):
        observed = []
        state = self.state()
        wrong = {"order_id": "O001", "refund_before": "2026-12-01T00:00:00+08:00"}
        right = {"order_id": "O001", "refund_before": "2026-11-03T00:00:00+08:00"}
        result = self.execute([reply(tool("check_order", wrong)), reply(tool("check_order", right, "call-2")), reply()], state,
            handlers={"check_order": lambda **args: observed.append(args) or {"status": "ok", "net_amount_fen": 8500}})
        self.assertEqual(observed, [right])
        self.assertEqual(result["status"], "completed")
        invalid = [event for event in result["trace"] if event.get("result", {}).get("error") == "condition_source_mismatch"]
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0]["error_category"], "condition_validation")

    def test_second_source_violation_stops_without_query(self):
        observed = []
        wrong = {"order_id": "O999", "refund_before": "2026-11-03T00:00:00+08:00"}
        result = self.execute([reply(tool("check_order", wrong)), reply(tool("check_order", wrong, "call-2"))], self.state(),
            handlers={"check_order": lambda **args: observed.append(args)})
        self.assertEqual(result["status"], "limited")
        self.assertEqual(observed, [])
        self.assertEqual(result["facts"], [])

    def test_omitted_cutoff_uses_fixed_program_condition(self):
        observed = []
        state = self.state("核查O001")
        result = self.execute([reply(tool("check_order", {"order_id": "O001"})), reply()], state,
            handlers={"check_order": lambda **args: observed.append(args) or {"status": "ok"}})
        self.assertEqual(observed[0]["refund_before"], state["defaults"]["refund_before"]["value"])
        self.assertEqual(result["facts"][0]["arguments"], observed[0])

    def test_order_selection_is_only_allowed_query_when_id_missing(self):
        observed = []
        state = self.state("查询订单")
        result = self.execute([reply(tool("list_orders", {"limit": 3})), reply(status="clarification", answer="请选择订单编号。")], state,
            handlers={"list_orders": lambda **args: observed.append(args) or {"status": "ok", "orders": [{"order_id": "O001"}]}})
        self.assertEqual(result["status"], "clarification")
        self.assertEqual(observed, [{"limit": 3}])
        self.assertEqual({item["function"]["name"] for item in self.requests[0]["tools"]}, {"list_orders"})

    def test_system_contains_provenance_and_policy_can_combine(self):
        state = self.state("核查O001，退款截止2026年11月3日零点，并解释退款政策")
        self.execute([reply()], state, handlers={})
        system = self.requests[0]["messages"][0]["content"]
        self.assertIn("turn-1", system)
        self.assertIn("2026年11月3日零点", system)
        self.assertNotIn("fake-integration-model-key", system)
        self.assertEqual({item["function"]["name"] for item in self.requests[0]["tools"]}, {"check_order", "search_policies"})

    def test_ordinary_unknown_chat_reaches_model_without_business_tools(self):
        state = self.state("你好，可以怎样使用这个助手？")
        result = self.execute([reply(answer="可以查询订单或演示政策；请提供订单编号或明确汇总条件。")], state, handlers={})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("tools", self.requests[0])
        self.assertNotIn("tool_choice", self.requests[0])
        self.assertEqual(result["facts"], [])

    def test_model_timeout_http_and_json_errors_have_timed_safe_categories(self):
        samples = [(httpx.ReadTimeout("SECRET_UPSTREAM"), "timeout"),
                   (httpx.Response(503, text="SECRET_UPSTREAM"), "http_status"),
                   (httpx.Response(200, text="not JSON SECRET_UPSTREAM"), "json_parse")]
        for failure, category in samples:
            with self.subTest(category=category):
                result = self.execute([failure], self.state(), handlers={})
                event = result["trace"][0]
                self.assertEqual(result["status"], "failed")
                self.assertEqual(event["error_category"], category)
                self.assertIn("started_at", event)
                self.assertIn("finished_at", event)
                self.assertGreaterEqual(event["elapsed_seconds"], 0)
                self.assertNotIn("SECRET_UPSTREAM", json.dumps(result))

    def test_database_and_permission_failure_categories_are_not_exception_text(self):
        def database_failure(**args):
            raise SQLAlchemyError("DATABASE_SECRET")
        for handler, category in ((database_failure, "database"),
                (lambda **args: {"status": "ok", "buyer_id": "B999", "private": "WRONG_OWNER"}, "permission")):
            with self.subTest(category=category):
                result = self.execute([reply(tool("check_order", {"order_id": "O001"}))], self.state(), handlers={"check_order": handler})
                event = next(event for event in result["trace"] if event["stage"] == "tool")
                self.assertEqual(event["error_category"], category)
                self.assertIn("finished_at", event)
                self.assertGreaterEqual(event["elapsed_seconds"], 0)
                self.assertNotIn("DATABASE_SECRET", json.dumps(result))
                self.assertNotIn("WRONG_OWNER", json.dumps(result))

    def test_every_successful_model_and_tool_step_has_elapsed_time(self):
        result = self.execute([reply(tool("check_order", {"order_id": "O001"})), reply()], self.state(),
            handlers={"check_order": lambda **args: {"status": "ok"}})
        self.assertEqual([event["stage"] for event in result["trace"]], ["model", "tool", "model"])
        for event in result["trace"]:
            self.assertIn("started_at", event)
            self.assertIn("finished_at", event)
            self.assertGreaterEqual(event["elapsed_seconds"], 0)

    def test_mcp_discovery_and_call_replace_only_check_order_without_scope_leak(self):
        observed, local = [], []
        class Protocol:
            def __init__(self, scope):
                observed.append(("scope", scope))
                self.last_trace = {}
            def discover(self):
                self.last_trace = {"operation": "tools/list", "protocol_version": "test-protocol", "request_id": "discover-1"}
                return {"type": "function", "function": {"name": "check_order", "description": "ACTUAL_DISCOVERED_SCHEMA",
                         "parameters": SCHEMAS["check_order"][0].model_json_schema()}}
            def check_order(self, **args):
                observed.append(("call", args))
                self.last_trace = {"operation": "tools/call", "request_id": "call-1"}
                return {"status": "ok", "shop_id": "S001", "net_amount_fen": 8500}
        with patch.dict(sys.modules, {"mcp_order_client": SimpleNamespace(OrderProtocolClient=Protocol)}), patch(
                "agent_runtime._default_handlers", return_value={"check_order": lambda **args: local.append(args)}):
            result = self.execute([reply(tool("check_order", {"order_id": "O001"})), reply()], self.state(),
                                  cfg=config(mcp_enabled=True), role="merchant", shop="S001")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(local, [])
        self.assertEqual(observed[0], ("scope", "merchant:S001"))
        schema = next(item for item in self.requests[0]["tools"] if item["function"]["name"] == "check_order")
        self.assertEqual(schema["function"]["description"], "ACTUAL_DISCOVERED_SCHEMA")
        self.assertEqual(result["trace"][0]["protocol"]["operation"], "tools/list")
        step = next(event for event in result["trace"] if event["stage"] == "tool")
        self.assertEqual(step["protocol"]["operation"], "tools/call")
        self.assertNotIn("merchant:S001", json.dumps(result))
        self.assertNotIn("S001", json.dumps(self.requests))

    def test_mcp_discovery_failure_does_not_fallback_or_request_model(self):
        local = []
        class BrokenProtocol:
            def __init__(self, scope):
                self.last_trace = {"operation": "tools/list"}
            def discover(self):
                raise TimeoutError("PRIVATE_PROTOCOL_TOKEN")
        with patch.dict(sys.modules, {"mcp_order_client": SimpleNamespace(OrderProtocolClient=BrokenProtocol)}), patch(
                "agent_runtime._default_handlers", return_value={"check_order": lambda **args: local.append(args)}):
            result = self.execute([], self.state(), cfg=config(mcp_enabled=True))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.requests, [])
        self.assertEqual(local, [])
        self.assertIn("未回退", result["answer"])
        self.assertIn("finished_at", result["trace"][0])
        self.assertNotIn("PRIVATE_PROTOCOL_TOKEN", json.dumps(result))

    def test_mcp_call_failure_keeps_protocol_error_without_local_retry(self):
        local = []
        class BrokenCall:
            def __init__(self, scope):
                self.last_trace = {}
            def discover(self):
                return {"type": "function", "function": {"name": "check_order", "parameters": {}}}
            def check_order(self, **args):
                self.last_trace = {"operation": "tools/call", "error_type": "ReadTimeout"}
                raise TimeoutError("PRIVATE_PROTOCOL_TOKEN")
        with patch.dict(sys.modules, {"mcp_order_client": SimpleNamespace(OrderProtocolClient=BrokenCall)}), patch(
                "agent_runtime._default_handlers", return_value={"check_order": lambda **args: local.append(args)}):
            result = self.execute([reply(tool("check_order", {"order_id": "O001"}))], self.state(), cfg=config(mcp_enabled=True))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(local, [])
        step = next(event for event in result["trace"] if event["stage"] == "tool")
        self.assertEqual(step["error_category"], "timeout")
        self.assertEqual(step["protocol"]["error_type"], "ReadTimeout")
        self.assertIn("finished_at", step)


class ServerConditionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="datus-condition-integration-")
        self.root = Path(self.temp.name)
        token_file = self.root / "access.json"
        token_file.write_text(json.dumps({"accounts": [{"token": "offline-account-token", "roles": [
            {"role": "buyer", "buyer_id": "B001"}, {"role": "merchant", "shop_id": "S001"}]}]}))
        self.settings = AppSettings(model_enabled=True, model_api_key="fake-integration-model-key",
                                    runtime_dir=self.root / "runtime", tokens_file=token_file, mcp_enabled=False)
        self.engine = create_engine("sqlite://")  # 没有建立表或执行查询，业务工具均为受控替身。
        self.observed, self.tool_calls = [], []
        def actual_agent(settings, engine, message, buyer_id, history, cancel, **kwargs):
            self.assertIn("condition_state", kwargs)
            state = kwargs["condition_state"]
            self.observed.append({"state": deepcopy(state), "history": deepcopy(history),
                                  "historical_evidence": deepcopy(kwargs.get("historical_evidence", [])),
                                  "role": kwargs.get("access_role", "buyer")})
            pending = [reply()]
            if state["ready"] and state["kind"] == "summary":
                args = {field: state["confirmed"][field]["value"] for field in ("paid_from", "paid_before", "refund_before")}
                pending = [reply(tool("summary_orders", args)), reply()]
            def send(request):
                return httpx.Response(200, json=pending.pop(0))
            def summary(**args):
                self.tool_calls.append(args)
                result = {"status": "ok", "order_count": 1, "net_amount_fen": 8500}
                if kwargs.get("access_role") == "merchant":
                    result["shop_id"] = kwargs["shop_id"]
                return result
            return run_agent(settings, engine, message, buyer_id, history, cancel,
                transport=httpx.MockTransport(send), tool_handlers={"summary_orders": summary}, **kwargs)
        self.app = create_app(self.settings, self.engine, agent=actual_agent)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.counter = 0

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.engine.dispose()
        self.temp.cleanup()

    def ask(self, message, mode="buyer", session="session1"):
        self.counter += 1
        headers = {"Authorization": "Bearer offline-account-token", "X-Access-Mode": mode}
        response = self.client.post("/api/tasks", headers=headers,
            json={"message": message, "session_id": session, "request_id": f"condition-request-{self.counter}"})
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            record = self.client.get("/api/tasks/" + task_id, headers=headers).json()
            if record["status"] not in {"queued", "running", "cancelling"}:
                return record
            time.sleep(.005)
        self.fail("task did not finish")

    def test_multi_turn_clarification_persists_provenance_and_public_conditions(self):
        first = self.ask("统计2026年10月订单")
        self.assertEqual(first["status"], "clarification")
        self.assertEqual(first["usage"]["model_requests"], 0)
        self.assertEqual(first["conditions"]["missing"], ["refund_before"])
        second = self.ask("退款截止2026年11月3日零点")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(len(self.tool_calls), 1)
        state = self.app.state.store.session_conditions("buyer:B001", "session1")
        self.assertEqual(state["confirmed"]["paid_from"]["source"]["turn_id"], first["id"])
        self.assertEqual(state["confirmed"]["refund_before"]["source"]["turn_id"], second["id"])
        self.assertNotIn("original_question", second["conditions"])

    def test_roles_and_sessions_never_share_conditions(self):
        self.ask("统计2026年10月订单")
        merchant = self.ask("退款截止2026年11月3日零点", mode="merchant")
        other_session = self.ask("退款截止2026年11月3日零点", session="session2")
        self.assertEqual(merchant["status"], "clarification")
        self.assertEqual(other_session["status"], "clarification")
        self.assertNotIn("paid_from", merchant["conditions"]["confirmed"])
        self.assertNotIn("paid_from", other_session["conditions"]["confirmed"])
        self.assertEqual(self.tool_calls, [])

    def test_reset_clears_previous_history_and_cutoff(self):
        self.ask("统计2026年10月订单，退款截止2026年11月3日零点")
        reset = self.ask("重置，统计2026年9月订单")
        self.assertEqual(reset["status"], "clarification")
        self.assertEqual(reset["conditions"]["missing"], ["refund_before"])
        self.assertEqual(self.observed[-1]["history"], [])
        self.assertTrue(reset["conditions"]["reset"])

    def test_expired_state_is_not_reused_even_with_recent_task_file(self):
        first = self.ask("统计2026年10月订单")
        store = self.app.state.store
        record = store.get(first["id"], "buyer:B001")
        record["result"]["condition_state"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        store.update(first["id"], "buyer:B001", result=record["result"])
        next_record = self.ask("退款截止2026年11月3日零点")
        self.assertEqual(next_record["status"], "clarification")
        self.assertTrue(next_record["conditions"]["expired"])
        self.assertNotIn("paid_from", next_record["conditions"]["confirmed"])
        self.assertEqual(self.observed[-1]["history"], [])
        self.assertEqual(self.tool_calls, [])

    def test_old_tasks_without_state_remain_readable(self):
        self.assertIsNone(public_record({"result": {}})["conditions"])
        self.assertIsNone(self.app.state.store.session_conditions("buyer:B001", "unused"))

    def test_server_passes_only_same_session_scope_historical_evidence(self):
        first = self.ask("统计2026年10月订单，退款截止2026年11月3日零点")
        self.ask("退款截止改成2026年11月5日零点")
        self.assertEqual(self.observed[-1]["historical_evidence"][0]["source_task_id"], first["id"])
        self.ask("退款截止改成2026年11月5日零点", mode="merchant")
        self.assertEqual(self.observed[-1]["historical_evidence"], [])
        self.ask("退款截止改成2026年11月5日零点", session="session2")
        self.assertEqual(self.observed[-1]["historical_evidence"], [])
        self.ask("重置，统计2026年9月订单")
        self.assertEqual(self.observed[-1]["historical_evidence"], [])


if __name__ == "__main__":
    unittest.main()
