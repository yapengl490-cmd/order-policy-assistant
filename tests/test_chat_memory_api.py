"""聊天记忆的真实应用入口，模型用替身；不把离线接口测试算真实提取成功。"""
import json
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app_config import AppSettings
from app_server import create_app
from preference_store import preference_metadata


class ChatMemoryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.access = self.root / "access.json"
        roles = [{"role": "buyer", "buyer_id": "B001"}, {"role": "merchant", "shop_id": "S001"}]
        self.accounts = [{"token": "chat-memory-test-a", "account_id": "account_A001", "roles": roles},
                         {"token": "chat-memory-test-b", "account_id": "account_B001", "roles": roles}]
        self.access.write_text(json.dumps({"accounts": self.accounts}))
        self.engine = create_engine("sqlite://")
        self.memory_engine = create_engine("sqlite:///" + str(self.root / "memory.db"),
            execution_options={"schema_translate_map": {"app_memory": None}})
        preference_metadata.create_all(self.memory_engine)
        self.settings = AppSettings(tokens_file=self.access, runtime_dir=self.root / "runtime",
            model_enabled=True, model_api_key="test-not-real", memory_enabled=True)
        self.calls, self.proposal = [], None
        self.started, self.release = threading.Event(), threading.Event()
        self.block = False

    def tearDown(self):
        self.release.set()
        self.engine.dispose()
        self.memory_engine.dispose()
        self.temp.cleanup()

    def agent(self, settings, engine, message, buyer, history, cancelled, **kwargs):
        self.calls.append(deepcopy(kwargs))
        self.started.set()
        if self.block:
            self.release.wait(3)
        return {"status": "completed", "answer": "业务回答保留", "facts": [{"test": "retained"}],
            "memory_proposal": deepcopy(self.proposal),
            "history": [{"role": "user", "content": message}, {"role": "assistant", "content": "业务回答保留"}]}

    def client(self):
        self.app = create_app(self.settings, self.engine, self.agent, self.memory_engine, self.agent)
        return TestClient(self.app)

    def headers(self, person=0, role="buyer"):
        return {"Authorization": "Bearer " + self.accounts[person]["token"], "X-Access-Mode": role}

    def prefs(self, client, person=0, role="buyer"):
        return client.get("/api/preferences", headers=self.headers(person, role)).json()

    def save(self, client, values, version=0, person=0, role="buyer"):
        response = client.put("/api/preferences", headers=self.headers(person, role),
            json={"values": values, "expected_version": version})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def ask(self, client, message, key="memory-task-001", role="buyer", mode="single", wait=True):
        response = client.post("/api/tasks", headers=self.headers(role=role),
            json={"message": message, "session_id": key, "request_id": key, "execution_mode": mode})
        self.assertEqual(response.status_code, 202, response.text)
        return self.wait(client, response.json()["id"], role) if wait else response.json()

    def wait(self, client, task_id, role="buyer"):
        for _ in range(200):
            task = client.get("/api/tasks/" + task_id, headers=self.headers(role=role)).json()
            if task["status"] not in {"queued", "running", "cancelling"}:
                return task
            time.sleep(.01)
        self.fail("task did not finish")

    def propose(self, quote, values=None, scope="explicit_long_term"):
        self.proposal = {"values": values or {"answer_style": "brief"}, "source_quote": quote, "scope": scope}

    def decide(self, client, task, action="accept", person=0, role="buyer"):
        return client.post("/api/tasks/" + task["id"] + "/memory-decision",
            headers=self.headers(person, role), json={"action": action})

    def test_explicit_merge_frozen_snapshot_new_session_and_no_duplicate_task(self):
        with self.client() as client:
            self.save(client, {"amount_format": "yuan"})
            self.propose("以后简短回答")
            task = self.ask(client, "以后简短回答")
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["memory_update"]["status"], "saved")
            self.assertEqual(task["facts"], [{"test": "retained"}])
            self.assertIn("业务回答保留", task["answer"])
            self.assertIn("已保存买家偏好", task["answer"])
            self.assertEqual(self.calls[0]["preference_snapshot"]["values"], {"amount_format": "yuan"})
            saved = self.prefs(client)
            self.assertEqual(saved["values"], {"amount_format": "yuan", "answer_style": "brief"})
            self.assertEqual(saved["version"], 2)
            self.assertIn(task["id"], saved["source"]["text"])
            self.assertEqual(self.ask(client, "以后简短回答")["id"], task["id"])
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(self.decide(client, task).json()["memory_update"]["status"], "saved")
            self.assertEqual(self.prefs(client)["version"], 2)
            self.proposal = None
            self.ask(client, "你好", "memory-task-002")
            self.assertEqual(self.calls[-1]["preference_snapshot"]["values"], saved["values"])
            export = client.get("/api/tasks/" + task["id"] + "/export", headers=self.headers()).json()
            self.assertEqual(export["preference_snapshot"]["version"], 1)
            self.assertEqual(export["result"]["history"][-1]["content"], task["answer"])

    def test_same_values_do_not_increment_version(self):
        with self.client() as client:
            self.save(client, {"answer_style": "brief"})
            self.propose("以后简短回答")
            task = self.ask(client, "以后简短回答")
            self.assertEqual(task["memory_update"]["status"], "unchanged")
            self.assertEqual(self.prefs(client)["version"], 1)

    def test_suggestion_confirm_is_owned_versioned_and_does_not_call_model(self):
        with self.client() as client:
            self.propose("太长了", scope="suggestion")
            task = self.ask(client, "太长了")
            self.assertEqual(task["memory_update"]["status"], "proposed")
            self.assertEqual(self.prefs(client)["version"], 0)
            self.assertEqual(self.decide(client, task, person=1).status_code, 404)
            self.assertEqual(self.decide(client, task, role="merchant").status_code, 404)
            forged = client.post("/api/tasks/" + task["id"] + "/memory-decision", headers=self.headers(),
                json={"action": "accept", "values": {"amount_format": "fen"}, "expected_version": 0})
            self.assertEqual(forged.status_code, 422)
            self.assertEqual(self.decide(client, task).json()["memory_update"]["status"], "saved")
            self.assertIn("user_confirmed", self.prefs(client)["source"]["text"])
            self.assertEqual(self.decide(client, task).status_code, 200)
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(self.prefs(client)["version"], 1)
            self.assertEqual(self.prefs(client, 1)["version"], 0)
            self.assertEqual(self.prefs(client, role="merchant")["version"], 0)

    def test_dismiss_preserves_preference(self):
        with self.client() as client:
            self.propose("太长了", scope="suggestion")
            task = self.ask(client, "太长了")
            self.assertEqual(self.decide(client, task, "dismiss").json()["memory_update"]["status"], "dismissed")
            self.assertEqual(self.decide(client, task).json()["memory_update"]["status"], "dismissed")
            self.assertEqual(self.prefs(client)["version"], 0)

    def test_stale_suggestion_cannot_restore_deleted_preferences(self):
        with self.client() as client:
            self.save(client, {"amount_format": "yuan"})
            self.propose("太长了", scope="suggestion")
            task = self.ask(client, "太长了")
            self.assertEqual(client.request("DELETE", "/api/preferences", headers=self.headers(),
                json={"expected_version": 1}).status_code, 200)
            self.assertEqual(self.decide(client, task).json()["memory_update"]["status"], "conflict")
            self.assertEqual(self.prefs(client)["values"], {})

    def test_cancelled_task_does_not_write(self):
        self.block = True
        with self.client() as client:
            self.propose("以后简短回答")
            task = self.ask(client, "以后简短回答", wait=False)
            self.assertTrue(self.started.wait(1))
            client.post("/api/tasks/" + task["id"] + "/cancel", headers=self.headers())
            self.release.set()
            task = self.wait(client, task["id"])
            self.assertEqual(task["status"], "cancelled")
            self.assertEqual(task["memory_update"]["status"], "discarded")
            self.assertEqual(self.prefs(client)["version"], 0)

    def test_settings_changed_during_model_call_are_not_overwritten(self):
        self.block = True
        with self.client() as client:
            self.propose("以后简短回答")
            task = self.ask(client, "以后简短回答", wait=False)
            self.assertTrue(self.started.wait(1))
            self.save(client, {"answer_style": "detailed"})
            self.release.set()
            task = self.wait(client, task["id"])
            self.assertEqual(task["memory_update"]["status"], "conflict")
            self.assertEqual(self.prefs(client)["values"], {"answer_style": "detailed"})

    def test_invalid_and_temporary_candidates_do_not_destroy_business_result(self):
        with self.client() as client:
            for index, raw in enumerate(("invalid", {"values": {"buyer_id": "B999"},
                "source_quote": "以后简短回答", "scope": "explicit_long_term"},
                {"values": {"answer_style": "brief"}, "source_quote": "这次简短回答", "scope": "explicit_long_term"})):
                self.proposal = raw
                task = self.ask(client, "以后简短回答。这次简短回答", "invalid-memory-%d" % index)
                self.assertEqual(task["status"], "completed")
                self.assertEqual(task["memory_update"]["status"], "rejected")
                self.assertEqual(task["facts"], [{"test": "retained"}])
            self.assertEqual(self.prefs(client)["version"], 0)

    def test_multi_entry_runs_once_and_preserves_role(self):
        with self.client() as client:
            self.propose("以后金额用分", {"amount_format": "fen"})
            task = self.ask(client, "以后金额用分", role="merchant", mode="multi")
            self.assertEqual(task["memory_update"]["status"], "saved")
            self.assertEqual(task["memory_update"]["role"], "merchant")
            self.assertEqual(self.prefs(client, role="merchant")["values"], {"amount_format": "fen"})
            self.assertEqual(self.prefs(client)["values"], {})
            self.assertEqual(len(self.calls), 1)

    def test_model_omitted_feedback_candidate_gets_confirmation_only(self):
        with self.client() as client:
            self.save(client, {"amount_format": "fen", "answer_style": "detailed"})
            self.proposal = None
            task = self.ask(client, "你刚才的回答太长了，请精简一点。")
            self.assertEqual(task["memory_update"]["status"], "proposed")
            self.assertEqual(task["memory_update"]["candidate_origin"], "program_signal")
            self.assertEqual(self.prefs(client)["version"], 1)
            accepted = self.decide(client, task).json()
            self.assertEqual(accepted["memory_update"]["status"], "saved")
            self.assertEqual(self.prefs(client)["values"], {"amount_format": "fen", "answer_style": "brief"})
            self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
