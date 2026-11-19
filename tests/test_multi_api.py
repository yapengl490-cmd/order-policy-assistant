"""真实应用入口与任务文件，模型用替身；不发外部请求、不写正式数据。"""
import json
from pathlib import Path
import tempfile
import time
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app_config import AppSettings
from app_server import create_app
from task_store import TaskStore


class MultiAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        access = root / "access.json"
        access.write_text(json.dumps({"accounts": [
            {"buyer_id": "B001", "token": "fake-buyer-one"},
            {"buyer_id": "B002", "token": "fake-buyer-two"}]}))
        self.settings = AppSettings(model_api_key="fake-not-sent", model_enabled=True,
            tokens_file=access, runtime_dir=root / "runtime")
        self.engine = create_engine("sqlite://")
        self.calls = []

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def runner(self, mode, status="completed"):
        def run(settings, engine, message, buyer, history, cancelled, **kwargs):
            self.calls.append((mode, buyer, kwargs))
            return {"status": status, "answer": "已核金额；政策未完成" if status == "partial" else "核查完成",
                "facts": [{"tool": "check_order", "result": {"status": "ok", "net_amount_fen": 8500}}],
                "usage": {"model_requests": 2}, "budget": {"model_requests": 2},
                "orchestration": {"requested": mode, "effective": mode,
                    "branches": [{"name": "amount", "status": "completed"},
                                 {"name": "policy", "status": "failed"}]}}
        return run

    def app(self, status="completed"):
        return create_app(self.settings, self.engine, agent=self.runner("single"),
                          multi_agent=self.runner("multi", status))

    def headers(self, other=False):
        return {"Authorization": "Bearer " + ("fake-buyer-two" if other else "fake-buyer-one")}

    def submit(self, client, **changes):
        data = {"message": "核查O001并说明退款政策", "session_id": "session-one", "request_id": "request-one"}
        data.update(changes)
        return client.post("/api/tasks", json=data, headers=self.headers())

    def wait(self, client, task_id):
        for _ in range(100):
            record = client.get("/api/tasks/" + task_id, headers=self.headers()).json()
            if record["status"] not in {"queued", "running", "cancelling"}:
                return record
            time.sleep(.01)
        self.fail("task not terminal")

    def test_default_single_and_explicit_multi_dispatch(self):
        with TestClient(self.app()) as client:
            a = self.wait(client, self.submit(client).json()["id"])
            b = self.wait(client, self.submit(client, execution_mode="multi", session_id="session-two", request_id="request-two").json()["id"])
        self.assertEqual([call[0] for call in self.calls], ["single", "multi"])
        self.assertEqual(a["execution_mode"], "single")
        self.assertEqual(b["orchestration"]["effective"], "multi")
        self.assertEqual(self.calls[-1][1], "B001")

    def test_mode_is_part_of_idempotency_and_session(self):
        with TestClient(self.app()) as client:
            self.wait(client, self.submit(client).json()["id"])
            self.assertEqual(self.submit(client, execution_mode="multi").status_code, 409)
            self.assertEqual(self.submit(client, execution_mode="multi", request_id="request-two").status_code, 409)
            self.assertEqual(self.submit(client).status_code, 202)
        self.assertEqual(len(self.calls), 1)

    def test_invalid_mode_does_not_create_task(self):
        with TestClient(self.app()) as client:
            self.assertEqual(self.submit(client, execution_mode="unlimited").status_code, 422)
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.settings.runtime_dir.glob("task-*.json")), [])

    def test_mode_is_in_first_persisted_task_and_survives_restart(self):
        store = TaskStore(self.settings.runtime_dir)
        record, _ = store.create("buyer:B001", "mode-record", "mode-session", "核查", execution_mode="multi")
        persisted = json.loads((self.settings.runtime_dir / ("task-" + record["id"] + ".json")).read_text())
        self.assertEqual(persisted["execution_mode"], "multi")
        reopened = TaskStore(self.settings.runtime_dir)
        self.assertEqual(reopened.get(record["id"], "buyer:B001")["execution_mode"], "multi")
        with self.assertRaises(ValueError):
            reopened.create("buyer:B001", "mode-record", "mode-session", "核查", execution_mode="single")

    def test_partial_persists_exports_and_is_owner_scoped(self):
        with TestClient(self.app("partial")) as client:
            result = self.wait(client, self.submit(client, execution_mode="multi").json()["id"])
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["facts"][0]["result"]["net_amount_fen"], 8500)
            self.assertEqual(client.get("/api/tasks/" + result["id"], headers=self.headers(True)).status_code, 404)
            export = client.get("/api/tasks/" + result["id"] + "/export", headers=self.headers())
            self.assertEqual(export.status_code, 200)
            self.assertEqual(export.json()["result"]["orchestration"]["branches"][1]["status"], "failed")
        with TestClient(self.app()) as client:
            again = client.get("/api/tasks/" + result["id"], headers=self.headers()).json()
            self.assertEqual(again["status"], "partial")
            self.assertEqual(again["execution_mode"], "multi")

    def test_same_mode_follows_existing_condition_state(self):
        with TestClient(self.app()) as client:
            self.wait(client, self.submit(client, execution_mode="multi").json()["id"])
            self.wait(client, self.submit(client, execution_mode="multi", request_id="request-two", message="退款截止改为2026-11-03T00:00:00+08:00").json()["id"])
        self.assertEqual(self.calls[-1][2]["condition_state"]["confirmed"]["order_id"]["value"], "O001")

    def test_batch_modes_dispatch_trusted_scope_and_export(self):
        app=create_app(self.settings,self.engine,agent=self.runner("single"),
            programmatic_agent=self.runner("programmatic"),
            batch_sequential_agent=self.runner("batch_sequential"))
        with TestClient(app) as client:
            for mode in ("programmatic","batch_sequential"):
                record=self.wait(client,self.submit(client,execution_mode=mode,
                    session_id="s-"+mode,request_id="r-"+mode,
                    message="列出最近2笔订单并逐笔核查，退款截止2026-11-03零点").json()["id"])
                self.assertEqual(record["execution_mode"],mode)
                self.assertEqual(record["status"],"completed")
                self.assertEqual(self.calls[-1][0],mode)
                self.assertEqual(self.calls[-1][1],"B001")
                self.assertEqual(client.get("/api/tasks/"+record["id"],headers=self.headers(True)).status_code,404)
                self.assertEqual(client.get("/api/tasks/"+record["id"]+"/export",headers=self.headers()).json()["execution_mode"],mode)
                self.assertEqual(self.submit(client,execution_mode="single",session_id="s-"+mode,
                    request_id="other-"+mode).status_code,409)


if __name__ == "__main__":
    unittest.main()
