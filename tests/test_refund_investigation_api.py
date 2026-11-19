"""R05真实FastAPI边界＋离线执行器；不调用模型，不访问正式数据。"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app_config import AppSettings
from app_server import create_app
from test_period_analysis_api import CONDITIONS


class RefundInvestigationAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        accounts = self.root / "accounts.json"
        accounts.write_text(json.dumps({"accounts": [
            {"account_id": "account-test-one", "token": "offline-one", "roles": [
                {"role": "buyer", "buyer_id": "B1"}, {"role": "merchant", "shop_id": "S1"}]},
            {"account_id": "account-test-two", "token": "offline-two", "roles": [{"role": "merchant", "shop_id": "S2"}]},
            {"account_id": "account-test-peer", "token": "offline-peer", "roles": [{"role": "merchant", "shop_id": "S1"}]}]}))
        self.settings = AppSettings(tokens_file=accounts, runtime_dir=self.root / "runtime",
                                    model_enabled=True, model_api_key="offline-not-real")
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        self.calls = []
        self.started, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.status, self.raise_error = "completed", False
        self.headers = {"Authorization": "Bearer offline-one", "X-Access-Mode": "merchant"}
        self.patcher = patch("refund_investigation_runtime.run_refund_investigation", side_effect=self.runner)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def runner(self, settings, engine, message, buyer_id, history, cancelled, **kwargs):
        self.calls.append({"message": message, "buyer_id": buyer_id, "history": history, **kwargs})
        self.started.set()
        if not self.release.wait(2):
            raise TimeoutError("offline runner was not released")
        if self.raise_error:
            raise RuntimeError("offline-private-internal-error")
        return {"status": "cancelled" if cancelled() else self.status, "answer": "离线退款调查说明",
                "fixed_answer": "程序固定退款事实", "history": [], "facts": [{"tool": "investigate_refund_increase", "result": {"reason_status": "unknown"}}],
                "analysis_validation": {"status": "accepted" if self.status == "completed" else "rejected", "semantic_guarantee": False},
                "analysis_evidence": {"I1": {"path": "comparison.delta.refund_amount_fen", "label": "退款差额", "display": "30元"}},
                "orchestration": {"requested": "refund_investigation", "effective": "refund_investigation"}}

    def body(self, **changes):
        return {**CONDITIONS, "session_id": "investigation-session", "request_id": "investigate-001", **changes}

    def post(self, client, **changes):
        return client.post("/api/analysis/investigate", headers=self.headers, json=self.body(**changes))

    def terminal(self, client, task_id, headers=None):
        for _ in range(200):
            response = client.get("/api/tasks/" + task_id, headers=headers or self.headers)
            self.assertEqual(response.status_code, 200)
            task = response.json()
            if task["status"] not in {"queued", "running", "cancelling"}:
                return task
            time.sleep(.005)
        self.fail("offline task did not reach terminal state")

    def test_actual_task_conditions_identity_export_and_repeat_request(self):
        with TestClient(create_app(self.settings, self.engine)) as client:
            accepted = self.post(client)
            self.assertEqual(accepted.status_code, 202)
            task = self.terminal(client, accepted.json()["id"])
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["execution_mode"], "refund_investigation")
            self.assertEqual(self.calls[0]["access_role"], "merchant")
            self.assertEqual(self.calls[0]["shop_id"], "S1")
            self.assertEqual(self.calls[0]["period_conditions"], task["analysis_conditions"])
            self.assertIsNone(self.calls[0]["condition_state"])
            self.assertEqual(self.calls[0]["history"], [])
            self.assertEqual(self.calls[0]["historical_evidence"], [])
            again = self.post(client)
            self.assertEqual(again.status_code, 202)
            self.assertEqual(again.json()["id"], task["id"])
            self.assertEqual(len(self.calls), 1)
            export = client.get("/api/tasks/" + task["id"] + "/export", headers=self.headers)
            self.assertEqual(export.status_code, 200)
            self.assertIn("attachment", export.headers["content-disposition"])
            saved = export.json()
            self.assertEqual(saved["analysis_conditions"], task["analysis_conditions"])
            self.assertEqual(saved["result"]["analysis_evidence"], task["analysis_evidence"])
            self.assertEqual(saved["result"]["fixed_answer"], task["fixed_answer"])
            self.assertNotIn("offline-not-real", export.text)

    def test_same_request_id_with_changed_conditions_refuses_old_or_new_execution(self):
        with TestClient(create_app(self.settings, self.engine)) as client:
            first = self.post(client).json()
            self.terminal(client, first["id"])
            for change in ({"refund_before": "2026-11-04T00:00:00+08:00"},
                           {"first_from": "2026-09-30T00:00:00+08:00"},
                           {"session_id": "different-session"}):
                self.assertEqual(self.post(client, **change).status_code, 409)
            self.assertEqual(len(self.calls), 1)

    def test_other_account_same_shop_other_role_cannot_view_cancel_or_export(self):
        with TestClient(create_app(self.settings, self.engine)) as client:
            task = self.post(client).json()
            self.terminal(client, task["id"])
            for headers in ({"Authorization": "Bearer offline-two", "X-Access-Mode": "merchant"},
                            {"Authorization": "Bearer offline-peer", "X-Access-Mode": "merchant"},
                            {"Authorization": "Bearer offline-one", "X-Access-Mode": "buyer"}):
                for suffix in ("", "/export"):
                    self.assertEqual(client.get("/api/tasks/" + task["id"] + suffix, headers=headers).status_code, 404)
                self.assertEqual(client.post("/api/tasks/" + task["id"] + "/cancel", headers=headers).status_code, 404)
                self.assertEqual(client.get("/api/tasks", headers=headers).json(), [])
            self.assertEqual(len(self.calls), 1)

    def test_buyer_scope_comes_from_authorization_not_form(self):
        self.headers["X-Access-Mode"] = "buyer"
        with TestClient(create_app(self.settings, self.engine)) as client:
            task = self.post(client).json()
            self.terminal(client, task["id"])
            self.assertEqual(self.calls[0]["buyer_id"], "B1")
            self.assertNotIn("shop_id", self.calls[0])
            self.assertNotIn("access_role", self.calls[0])
            self.assertEqual(self.post(client, buyer_id="B404").status_code, 422)

    def test_invalid_input_or_unauthorized_request_creates_no_task(self):
        with TestClient(create_app(self.settings, self.engine)) as client:
            self.assertEqual(client.post("/api/analysis/investigate", json=self.body()).status_code, 401)
            self.assertEqual(client.post("/api/analysis/investigate", json=self.body(),
                headers={"Authorization": "Bearer offline-two", "X-Access-Mode": "buyer"}).status_code, 401)
            for changes in ({"shop_id": "S2"}, {"facts": {}}, {"message": "override"}, {"execution_mode": "single"},
                            {"first_from": "2026-10-01"}, {"first_before": "2026-10-20T00:00:00+08:00"},
                            {"refund_before": "2026-10-31T00:00:00+08:00"}):
                self.assertEqual(self.post(client, **changes).status_code, 422)
            self.assertEqual(self.calls, [])
            self.assertEqual(client.get("/api/tasks", headers=self.headers).json(), [])

    def test_running_cancel_reaches_same_task_no_duplicate_submit(self):
        self.release.clear()
        with TestClient(create_app(self.settings, self.engine)) as client:
            try:
                task = self.post(client).json()
                self.assertTrue(self.started.wait(1))
                running = client.get("/api/tasks/" + task["id"], headers=self.headers).json()
                self.assertEqual(running["status"], "running")
                self.assertEqual(self.post(client).json()["id"], task["id"])
                self.assertEqual(self.post(client, request_id="investigate-002").status_code, 409)
                cancelled = client.post("/api/tasks/" + task["id"] + "/cancel", headers=self.headers)
                self.assertEqual(cancelled.status_code, 200)
                self.assertEqual(cancelled.json()["status"], "cancelling")
            finally:
                self.release.set()
            self.assertEqual(self.terminal(client, task["id"])["status"], "cancelled")
            self.assertEqual(len(self.calls), 1)

    def test_partial_and_runtime_exception_not_mislabeled_completed(self):
        with TestClient(create_app(self.settings, self.engine)) as client:
            self.status = "partial"
            task = self.terminal(client, self.post(client).json()["id"])
            self.assertEqual(task["status"], "partial")
            self.assertEqual(task["analysis_validation"]["status"], "rejected")
            self.raise_error = True
            second = self.post(client, request_id="investigate-002", session_id="second").json()
            failed = self.terminal(client, second["id"])
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["error"], "RuntimeError")
            self.assertNotIn("offline-private-internal-error", json.dumps(failed))

    def test_model_disabled_quota_and_mode_boundary_do_not_bypass_existing_controls(self):
        with TestClient(create_app(replace(self.settings, model_enabled=False), self.engine)) as client:
            self.assertEqual(self.post(client).status_code, 503)
        with TestClient(create_app(replace(self.settings, max_daily_tasks=1), self.engine)) as client:
            task = self.terminal(client, self.post(client).json()["id"])
            self.assertEqual(self.post(client, request_id="investigate-002", session_id="second").status_code, 429)
            self.assertEqual(client.post("/api/tasks", headers=self.headers, json={"message": "调查", "session_id": "other",
                "request_id": "ask-request-1", "execution_mode": "refund_investigation"}).status_code, 422)
            self.assertEqual(client.post("/api/analysis/tasks", headers=self.headers,
                json=self.body(request_id="period-request-1")).status_code, 409)
            self.assertEqual(task["status"], "completed")
            self.assertEqual(len(self.calls), 1)

    def test_completed_task_survives_app_recreation_without_new_execution(self):
        with TestClient(create_app(self.settings, self.engine)) as client:
            task = self.terminal(client, self.post(client).json()["id"])
        with TestClient(create_app(self.settings, self.engine)) as client:
            restored = client.get("/api/tasks/" + task["id"], headers=self.headers).json()
            self.assertEqual(restored["status"], "completed")
            self.assertEqual(restored["analysis_conditions"], task["analysis_conditions"])
            self.assertEqual(restored["analysis_evidence"], task["analysis_evidence"])
            self.assertEqual(self.post(client).json()["id"], task["id"])
            self.assertEqual(len(self.calls), 1)
