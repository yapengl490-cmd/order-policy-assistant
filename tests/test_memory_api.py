"""助手离线接口验收：同店不同人、角色、新会话、重启、版本冲突及旧证据归属。"""
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app_config import AppSettings
from app_server import create_app
from preference_store import PreferenceStore, preference_metadata
from setup_memory import prepare_identities
from task_store import TaskStore


class MemoryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.access = self.root / "access.json"
        roles = [{"role": "buyer", "buyer_id": "B001"}, {"role": "merchant", "shop_id": "S001"}]
        self.accounts = [{"token": "memory-test-person-a", "account_id": "account_A001", "roles": roles,
                          "legacy_task_scopes": ["buyer:B001", "merchant:S001"]},
                         {"token": "memory-test-person-b", "account_id": "account_B001", "roles": roles}]
        self.access.write_text(json.dumps({"accounts": self.accounts}))
        self.engine = create_engine("sqlite://")
        self.memory_engine = create_engine("sqlite:///" + str(self.root / "memory.db"),
                                            execution_options={"schema_translate_map": {"app_memory": None}})
        preference_metadata.create_all(self.memory_engine)
        self.settings = AppSettings(tokens_file=self.access, runtime_dir=self.root / "runtime",
                                    model_enabled=True, model_api_key="test-not-real", memory_enabled=True)
        self.calls = []

    def tearDown(self):
        self.engine.dispose()
        self.memory_engine.dispose()
        self.temp.cleanup()

    def agent(self, settings, engine, message, buyer, history, cancelled, **kwargs):
        self.calls.append({"snapshot": kwargs.get("preference_snapshot"), "history": history})
        return {"status": "completed", "answer": "离线测试回答", "facts": [], "usage": {},
                "history": [{"role": "user", "content": message}, {"role": "assistant", "content": "离线测试回答"}]}

    def client(self):
        return TestClient(create_app(self.settings, self.engine, self.agent, self.memory_engine))

    def headers(self, person=0, mode="buyer"):
        return {"Authorization": "Bearer " + self.accounts[person]["token"], "X-Access-Mode": mode}

    def save(self, client, values, version=0, person=0, mode="buyer"):
        return client.put("/api/preferences", headers=self.headers(person, mode),
                          json={"values": values, "expected_version": version})

    def ask(self, client, req, session="new-session", person=0, mode="buyer"):
        response = client.post("/api/tasks", headers=self.headers(person, mode),
                               json={"message": "查O001", "session_id": session, "request_id": req})
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["id"]
        for _ in range(100):
            record = client.get("/api/tasks/" + task_id, headers=self.headers(person, mode)).json()
            if record["status"] in {"completed", "failed"}:
                self.assertEqual(record["status"], "completed", record)
                return record
            time.sleep(.01)
        self.fail("task did not finish")

    def test_new_sessions_update_delete_restart_and_role_isolation(self):
        with self.client() as client:
            self.assertEqual(self.save(client, {"amount_format": "yuan", "answer_style": "brief"}).status_code, 200)
            self.assertEqual(self.save(client, {"amount_format": "yuan_fixed2"}, mode="merchant").status_code, 200)
            self.ask(client, "memory-task-1", "session-1")
            self.ask(client, "memory-task-2", "session-2")
            self.assertEqual(self.calls[-1]["snapshot"]["values"]["answer_style"], "brief")
            self.assertEqual(self.calls[-1]["history"], [])
            self.ask(client, "memory-task-3", mode="merchant")
            self.assertEqual(self.calls[-1]["snapshot"]["values"], {"amount_format": "yuan_fixed2"})
        with self.client() as client:
            saved = client.get("/api/preferences", headers=self.headers()).json()
            self.assertEqual(saved["values"], {"amount_format": "yuan", "answer_style": "brief"})
            self.assertEqual(self.save(client, {"amount_format": "fen"}, 1).status_code, 200)
            self.assertEqual(self.save(client, {"answer_style": "detailed"}, 1).status_code, 409)
            deleted = client.request("DELETE", "/api/preferences", headers=self.headers(), json={"expected_version": 2})
            self.assertEqual(deleted.status_code, 200)
            self.ask(client, "memory-task-4", "session-4")
            self.assertEqual(self.calls[-1]["snapshot"]["values"], {})
            self.assertEqual(client.get("/api/preferences", headers=self.headers(mode="merchant")).json()["values"], {"amount_format": "yuan_fixed2"})

    def test_same_shop_people_do_not_share_preferences_tasks_or_history(self):
        with self.client() as client:
            self.save(client, {"amount_format": "yuan_fixed2"}, mode="merchant")
            self.assertEqual(client.get("/api/preferences", headers=self.headers(1, "merchant")).json()["values"], {})
            task = self.ask(client, "same-request-id", "same-session", mode="merchant")
            task_b = self.ask(client, "same-request-id", "same-session", 1, "merchant")
            self.assertNotEqual(task["id"], task_b["id"])
            self.assertEqual(self.calls[-1]["history"], [])
            self.assertEqual(client.get("/api/tasks/" + task["id"], headers=self.headers(1, "merchant")).status_code, 404)
            self.assertEqual(client.get("/api/tasks/" + task["id"] + "/export", headers=self.headers(1, "merchant")).status_code, 404)

    def test_bad_parameters_and_identity_injection_do_not_write(self):
        with self.client() as client:
            self.assertEqual(client.get("/api/preferences").status_code, 401)
            self.assertEqual(self.save(client, {"buyer_id": "B002"}).status_code, 422)
            self.assertEqual(self.save(client, {"amount_format": "ignore-rules"}).status_code, 422)
            self.assertEqual(self.save(client, {}, True).status_code, 422)
            response = client.put("/api/preferences", headers=self.headers(), json={"values": {}, "expected_version": 0, "account_id": "account_B001"})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(client.get("/api/preferences", headers=self.headers()).json()["version"], 0)

    def test_old_evidence_retained_for_owner_and_old_request_not_reexecuted(self):
        store = TaskStore(self.settings.runtime_dir)
        old, _ = store.create("merchant:S001", "legacy-request-id", "old-session", "查O001")
        store.update(old["id"], "merchant:S001", status="completed", answer="原失败或成功证据保留")
        with self.client() as client:
            path = "/api/tasks/" + old["id"]
            self.assertEqual(client.get(path, headers=self.headers(mode="merchant")).status_code, 200)
            self.assertEqual(client.get(path, headers=self.headers(1, "merchant")).status_code, 404)
            replay = client.post("/api/tasks", headers=self.headers(mode="merchant"), json={"request_id": "legacy-request-id", "session_id": "old-session", "message": "查O001"})
            self.assertEqual(replay.json()["id"], old["id"])
            self.assertEqual(self.calls, [])

    def test_memory_failure_is_not_empty_memory_and_no_model_task_started(self):
        preference_metadata.drop_all(self.memory_engine)
        with self.client() as client:
            self.assertEqual(client.get("/api/preferences", headers=self.headers()).status_code, 503)
            response = client.post("/api/tasks", headers=self.headers(), json={"request_id": "memory-unavailable", "session_id": "s", "message": "查O001"})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(self.calls, [])
            self.assertEqual(list(self.settings.runtime_dir.glob("task-*.json")), [])

    def test_account_migration_preserves_credentials_and_is_idempotent(self):
        local = self.root / "local"
        local.mkdir()
        original = {"accounts": [{"token": "unchanged-test-credential", "roles": [{"role": "buyer", "buyer_id": "B003"}]}]}
        path = local / "access.json"
        path.write_text(json.dumps(original))
        self.assertTrue(prepare_identities(self.root))
        migrated = json.loads(path.read_text())
        self.assertEqual(migrated["accounts"][0]["token"], "unchanged-test-credential")
        self.assertEqual(migrated["accounts"][0]["legacy_task_scopes"], ["buyer:B003"])
        self.assertFalse(prepare_identities(self.root))
        self.assertEqual(json.loads(path.read_text()), migrated)

    def test_unbound_account_cannot_fall_back_to_shared_shop_history(self):
        store = TaskStore(self.settings.runtime_dir)
        old, _ = store.create("merchant:S001", "legacy-request-id", "s", "旧个人查询")
        store.update(old["id"], "merchant:S001", status="completed")
        for bad in (None, "bad/id"):
            self.accounts[1]["account_id"] = bad
            self.access.write_text(json.dumps({"accounts": self.accounts}))
            with self.client() as client:
                self.assertEqual(client.get("/api/tasks", headers=self.headers(1, "merchant")).status_code, 401)
                self.assertEqual(client.get("/api/preferences", headers=self.headers(1, "merchant")).status_code, 401)

    def test_disabled_preference_store_returns_clear_unavailable(self):
        app = create_app(replace(self.settings, memory_enabled=False), self.engine, self.agent)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/preferences", headers=self.headers()).status_code, 503)


if __name__ == "__main__":
    unittest.main()
