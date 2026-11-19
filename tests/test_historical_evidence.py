"""历史依据/政策意图回归：仅临时文件与Mock模型，无真实数据库或外部请求。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_runtime import run_agent, SCHEMAS
from condition_state import reconcile_conditions
from task_store import TaskStore
from test_condition_integration import config, reply, tool


def fact(amount=17700, **extra):
    result = {"status": "ok", "order_id": "O001", "net_amount_fen": amount, **extra}
    return {"tool": "check_order", "arguments": {"order_id": "O001", "refund_before": "2026-11-03T00:00:00+08:00"},
            "result": result, "observed_at": datetime.now(timezone.utc).isoformat(),
            "result_snapshot_sha256": hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}


class HistoricalStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="datus-history-test-")
        self.store = TaskStore(Path(self.temp.name))
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def save(self, facts=None, *, scope="buyer:B001", session="s1", status="completed", state=None):
        self.counter += 1
        record, _ = self.store.create(scope, f"request-{self.counter}", session, "测试查询")
        self.store.update(record["id"], scope, status=status, result={"facts": [fact()] if facts is None else facts,
                          "condition_state": state or {}, "history": [{"role": "assistant", "content": "错误旧金额999999"}]})
        return record["id"]

    def test_snapshot_has_source_hash_and_no_old_assistant_text(self):
        task_id = self.save()
        evidence = self.store.session_historical_evidence("buyer:B001", "s1")
        self.assertEqual(evidence[0]["source_task_id"], task_id)
        self.assertEqual(evidence[0]["facts"][0]["result"]["net_amount_fen"], 17700)
        self.assertFalse(evidence[0]["is_current_database_fact"])
        self.assertIn("task_completed_at", evidence[0])
        self.assertNotIn("999999", json.dumps(evidence))
        self.assertNotIn("buyer:B001", json.dumps(evidence))
        evidence[0]["facts"][0]["result"]["net_amount_fen"] = 1
        self.assertEqual(self.store.get(task_id, "buyer:B001")["result"]["facts"][0]["result"]["net_amount_fen"], 17700)

    def test_other_role_session_and_buyer_are_not_selected(self):
        own = self.save()
        self.save([fact(2)], scope="merchant:S001")
        self.save([fact(3)], scope="buyer:B002")
        self.save([fact(4)], session="s2")
        self.assertEqual(self.store.session_historical_evidence("buyer:B001", "s1")[0]["source_task_id"], own)
        self.assertEqual(self.store.session_historical_evidence("buyer:B001", "missing"), [])

    def test_chat_can_keep_prior_snapshot_but_reset_is_a_barrier(self):
        own = self.save()
        self.save([], state={"kind": "order"})
        self.assertEqual(self.store.session_historical_evidence("buyer:B001", "s1")[0]["source_task_id"], own)
        self.save([], status="clarification", state={"reset": True})
        self.assertEqual(self.store.session_historical_evidence("buyer:B001", "s1"), [])

    def test_invalid_private_expired_or_large_snapshots_are_omitted_whole(self):
        bad_hash = fact(); bad_hash["result"]["net_amount_fen"] = 999
        expired = fact(); expired["observed_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        for number, snapshot in enumerate((bad_hash, expired, fact(buyer_id="PRIVATE_ID"), fact(blob="x" * 13000))):
            session = f"bad-{number}"
            self.save([snapshot], session=session)
            self.assertEqual(self.store.session_historical_evidence("buyer:B001", session), [])

    def test_only_successful_completed_results_and_max_two_snapshots(self):
        self.save(status="failed", session="failed")
        self.assertEqual(self.store.session_historical_evidence("buyer:B001", "failed"), [])
        self.save([fact(status="not_found")], session="missing")
        self.assertEqual(self.store.session_historical_evidence("buyer:B001", "missing"), [])
        self.save([fact(1), fact(2), fact(3)], session="many")
        evidence = self.store.session_historical_evidence("buyer:B001", "many")
        self.assertEqual([item["result"]["net_amount_fen"] for item in evidence[0]["facts"]], [2, 3])


class HistoricalRuntimeTests(unittest.TestCase):
    def execute(self, message, replies, *, evidence=None, history=None, handlers=None, mcp=False):
        self.requests = []
        state = reconcile_conditions(message, None, datetime.now(timezone.utc), "current-turn")
        def send(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json=replies.pop(0))
        return run_agent(config(mcp_enabled=mcp), None, message, "B001", history or [], lambda: False,
                         condition_state=state, historical_evidence=evidence, tool_handlers=handlers,
                         transport=httpx.MockTransport(send))

    def test_actual_snapshot_is_separate_from_current_facts_and_old_answer(self):
        evidence = [{"evidence_kind": "historical_tool_snapshot", "is_current_database_fact": False,
                     "source_task_id": "a" * 32, "task_completed_at": datetime.now(timezone.utc).isoformat(), "facts": [fact()]}]
        result = self.execute("核查O001，退款截止2026年11月1日零点，并对比上次金额", [reply(tool("check_order", {"order_id": "O001"})), reply()],
            evidence=evidence, history=[{"role": "user", "content": "上次查询"}, {"role": "assistant", "content": "旧模型错称金额999999"}],
            handlers={"check_order": lambda **args: {"status": "ok", "net_amount_fen": 16500}})
        system = self.requests[0]["messages"][0]["content"]
        self.assertIn("historical_evidence", system)
        self.assertIn("17700", system)
        self.assertIn("a" * 32, system)
        self.assertIn("不是当前数据库状态", system)
        self.assertNotIn("999999", system)
        self.assertEqual(result["historical_evidence"], evidence)
        self.assertEqual(result["facts"][0]["result"]["net_amount_fen"], 16500)
        self.assertEqual(result["trace"][0]["stage"], "historical_evidence")

    def test_policy_only_with_order_id_exposes_no_amount_tools(self):
        cases = json.loads((Path(__file__).resolve().parents[1] / "evaluation/task_cases.json").read_text())["cases"]
        message = next(case["input"]["message"] for case in cases if case["id"] == "policy_insufficient_eligibility")
        # R06当前入口必须读授权订单；旧冻结题与旧运行证据仍不修改。
        result = self.execute(message, [reply(tool("review_after_sales", {"order_id": "O001"})), reply()],
            handlers={"review_after_sales": lambda **args: {"status": "insufficient_evidence", "eligibility": "undetermined", "citations": {}}})
        self.assertEqual({item["function"]["name"] for item in self.requests[0]["tools"]}, {"review_after_sales"})
        self.assertEqual([item["tool"] for item in result["facts"]], ["review_after_sales"])
        self.assertNotIn("net_amount_fen", json.dumps(result["facts"]))
        combined = reconcile_conditions("查O001支付金额、退款金额并解释售后资格政策", None, datetime.now(timezone.utc), "combined")
        self.assertEqual(set(combined["allowed_tools"]), {"check_order", "search_policies"})

    def test_unavailable_order_mcp_does_not_block_unrelated_tasks(self):
        def forbidden(*args, **kwargs):
            self.fail("unrelated task must not construct MCP client")
        module = SimpleNamespace(OrderProtocolClient=forbidden)
        messages = ["O001能申请七天无理由退货吗？请查演示政策。",
                    "汇总2026年10月订单，退款截止2026年11月3日零点", "你好，可以怎样使用？"]
        with patch.dict(sys.modules, {"mcp_order_client": module}):
            for message in messages:
                with self.subTest(message=message):
                    result = self.execute(message, [reply()], mcp=True)
                    self.assertEqual(result["status"], "failed" if "退货" in message else "completed")
                    self.assertEqual(len(self.requests), 1)

    def test_mcp_safe_failure_codes_are_kept_without_raw_exception(self):
        class SafeError(RuntimeError):
            def __init__(self, code):
                self.code = code
                super().__init__("SECRET_EXCEPTION_TEXT")
        class Protocol:
            def __init__(self, scope):
                self.last_trace = {}
            def discover(self):
                return {"type": "function", "function": {"name": "check_order", "parameters": SCHEMAS["check_order"][0].model_json_schema()}}
            def check_order(self, **arguments):
                self.last_trace = {"is_error": True}
                raise SafeError(code)
        for code in ("database_error", "busy", "audit_error", "execution_error"):
            with self.subTest(code=code), patch.dict(sys.modules, {"mcp_order_client": SimpleNamespace(OrderProtocolClient=Protocol)}):
                result = self.execute("核查O001", [reply(tool("check_order", {"order_id": "O001"}))], mcp=True)
                step = next(event for event in result["trace"] if event["stage"] == "tool")
                self.assertEqual(step["protocol_error_code"], code)
                self.assertEqual(result["status"], "failed")
                self.assertNotIn("SECRET_EXCEPTION_TEXT", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
