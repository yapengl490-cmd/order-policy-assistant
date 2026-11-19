"""聊天偏好提交边界的助手离线测试：真实临时任务文件＋无数据库替身。

故障替身分别模拟提交前失败、提交后回包失败、并发版本变化；不连接真库或模型。
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.exc import SQLAlchemyError

from chat_memory_service import (ChatMemoryService, MemoryDecisionConflict,
                                 TTL_SECONDS, public_memory_update)
from preference_store import PreferenceConflict
from task_store import TaskStore


class FakePreferences:
    """保留版本比较和复制语义，明确标注故障发生在提交前还是提交后。"""
    def __init__(self):
        self.rows = {}
        self.read_calls = []
        self.replace_calls = []
        self.read_error = False
        self.write_fault = None

    def read(self, account_id, role):
        self.read_calls.append((account_id, role))
        if self.read_error:
            raise SQLAlchemyError("offline read failure")
        return deepcopy(self.rows.get((account_id, role), {
            "role": role, "values": {}, "version": 0,
            "source": None, "updated_at": None,
        }))

    def put(self, account_id, role, values, version, source=None):
        self.rows[(account_id, role)] = {
            "role": role, "values": deepcopy(values), "version": version,
            "source": deepcopy(source), "updated_at": "offline-test-time",
        }

    def replace(self, account_id, role, values, expected_version, source):
        self.replace_calls.append((account_id, role, deepcopy(values), expected_version, deepcopy(source)))
        if self.write_fault == "before_commit":
            raise SQLAlchemyError("offline failure before commit")
        if self.write_fault == "concurrent_change":
            self.put(account_id, role, {"amount_format": "fen"}, expected_version + 1,
                     {"kind": "user_settings", "text": "concurrent settings"})
            raise PreferenceConflict("offline competing write")
        actual = self.rows.get((account_id, role), {}).get("version", 0)
        if actual != expected_version:
            raise PreferenceConflict("offline version mismatch")
        self.put(account_id, role, values, expected_version + 1, source)
        if self.write_fault == "after_commit":
            raise SQLAlchemyError("offline commit completed but response failed")
        return self.read(account_id, role)


class ChatMemoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tasks = TaskStore(Path(self.temp.name))
        self.preferences = FakePreferences()
        self.now = 10_000.0
        self.service = ChatMemoryService(self.preferences, self.tasks, clock=lambda: self.now)
        self.access = {"account_id": "test_person_a", "role": "buyer", "buyer_id": "B001"}
        self.scope = "account:test_person_a:buyer"
        self.serial = 0

    def record(self, message="以后回答简短一点", *, values=None, version=0, status="completed"):
        self.serial += 1
        record, _ = self.tasks.create(self.scope, f"request-{self.serial}", "test-session", message)
        snapshot = {"role": "buyer", "values": deepcopy(values or {}), "version": version,
                    "source": {"kind": "user_settings"} if version else None, "updated_at": None}
        return self.tasks.update(record["id"], self.scope, status=status, preference_snapshot=snapshot)

    def result(self, message="以后回答简短一点", *, suggestion=False, status="completed"):
        return {
            "status": status, "answer": "业务结果：订单金额已核对。",
            "memory_proposal": {"values": {"answer_style": "brief"}, "source_quote": message,
                                "scope": "suggestion" if suggestion else "explicit_long_term"},
            "history": [{"role": "user", "content": message},
                        {"role": "assistant", "content": "业务结果：订单金额已核对。"}],
        }

    def latest(self, record):
        return self.tasks.get(record["id"], self.scope)

    def propose(self):
        record = self.record("回答太长了")
        self.service.after_run(record, self.access, self.result("回答太长了", suggestion=True))
        return self.latest(record)

    def test_explicit_merge_preserves_frozen_snapshot_and_input(self):
        self.preferences.put("test_person_a", "buyer", {"amount_format": "yuan"}, 3,
                             {"kind": "user_settings"})
        record = self.record(values={"amount_format": "yuan"}, version=3)
        original_record, result = deepcopy(record), self.result()
        original_result = deepcopy(result)
        saved = self.service.after_run(record, self.access, result)
        row = self.preferences.read("test_person_a", "buyer")
        self.assertEqual(row["values"], {"amount_format": "yuan", "answer_style": "brief"})
        self.assertEqual(row["version"], 4)
        self.assertEqual(saved["memory_update"]["status"], "saved")
        self.assertEqual(record, original_record)
        self.assertEqual(result, original_result)
        self.assertEqual(self.latest(record)["preference_snapshot"], original_record["preference_snapshot"])
        self.assertIn(record["id"], row["source"]["text"])
        self.assertIn("以后回答简短一点", row["source"]["text"])
        self.assertEqual(saved["history"][-1]["content"], saved["answer"])
        self.assertEqual(saved["answer"].count("【偏好状态】"), 1)

    def test_suggestion_waits_for_confirmation(self):
        record = self.propose()
        update = record["result"]["memory_update"]
        self.assertEqual(update["status"], "proposed")
        self.assertEqual(update["expires_at"], self.now + TTL_SECONDS)
        self.assertIn("尚未保存", record["answer"])
        self.assertEqual(self.preferences.replace_calls, [])

    def test_confirm_and_stale_confirmation_replay_write_once(self):
        record = self.propose()
        snapshot = deepcopy(record["preference_snapshot"])
        confirmed = self.service.decide(record, self.access, "accept")
        self.assertEqual(confirmed["result"]["memory_update"]["status"], "saved")
        self.assertIn("user_confirmed", self.preferences.read("test_person_a", "buyer")["source"]["text"])
        # 既检查最新终态重放，也检查并发客户端仍持有旧 proposed 快照的重放。
        self.service.decide(confirmed, self.access, "accept")
        replayed = self.service.decide(record, self.access, "accept")
        self.assertEqual(len(self.preferences.replace_calls), 1)
        self.assertEqual(replayed["result"]["memory_update"]["status"], "saved")
        self.assertEqual(self.latest(record)["preference_snapshot"], snapshot)
        self.assertEqual(replayed["answer"].count("【偏好状态】"), 1)

    def test_dismiss_is_terminal_and_does_not_write(self):
        record = self.propose()
        dismissed = self.service.decide(record, self.access, "dismiss")
        replayed = self.service.decide(dismissed, self.access, "accept")
        self.assertEqual(replayed["result"]["memory_update"]["status"], "dismissed")
        self.assertEqual(self.preferences.replace_calls, [])

    def test_ttl_uses_proposal_deadline_not_task_updated_time(self):
        record = self.propose()
        deadline = record["result"]["memory_update"]["expires_at"]
        self.now = deadline - 1
        self.tasks.update(record["id"], self.scope, error="harmless later task update")
        self.now = deadline
        expired = self.service.decide(self.latest(record), self.access, "accept")
        self.assertEqual(expired["result"]["memory_update"]["status"], "expired")
        self.assertEqual(expired["result"]["memory_update"]["expires_at"], deadline)
        self.assertEqual(self.preferences.replace_calls, [])

    def test_deleted_preferences_cannot_be_resurrected_by_pending_candidate(self):
        record = self.propose()
        self.preferences.put("test_person_a", "buyer", {}, 1, {"kind": "deletion"})
        response = self.service.decide(record, self.access, "accept")
        self.assertEqual(response["result"]["memory_update"]["status"], "conflict")
        self.assertEqual(self.preferences.read("test_person_a", "buyer")["values"], {})
        self.assertEqual(self.preferences.replace_calls, [])

    def test_explicit_request_never_rebases_stale_snapshot(self):
        record = self.record(values={"amount_format": "yuan"}, version=1)
        self.preferences.put("test_person_a", "buyer", {"amount_format": "fen"}, 2,
                             {"kind": "user_settings"})
        result = self.service.after_run(record, self.access, self.result())
        self.assertEqual(result["memory_update"]["status"], "conflict")
        self.assertEqual(self.preferences.read("test_person_a", "buyer")["values"], {"amount_format": "fen"})
        self.assertEqual(self.preferences.replace_calls, [])

    def test_change_between_read_and_replace_is_cas_conflict(self):
        record = self.record()
        self.preferences.write_fault = "concurrent_change"
        result = self.service.after_run(record, self.access, self.result())
        self.assertEqual(result["memory_update"]["status"], "conflict")
        self.assertEqual(self.preferences.read("test_person_a", "buyer")["values"], {"amount_format": "fen"})
        self.assertEqual(len(self.preferences.replace_calls), 1)

    def test_commit_then_raise_recovers_by_read_without_second_write(self):
        record = self.record()
        self.preferences.write_fault = "after_commit"
        result = self.service.after_run(record, self.access, self.result())
        self.assertEqual(result["memory_update"]["status"], "unknown")
        self.assertNotIn("已保存买家偏好", result["answer"])
        self.preferences.write_fault = None
        recovered = self.service.decide(self.latest(record), self.access, "accept")
        self.assertEqual(recovered["result"]["memory_update"]["status"], "saved")
        self.assertEqual(recovered["result"]["memory_update"]["reason"], "confirmed_existing_write")
        self.assertEqual(len(self.preferences.replace_calls), 1)

    def test_raise_before_commit_becomes_not_saved_without_retry(self):
        record = self.record()
        self.preferences.write_fault = "before_commit"
        result = self.service.after_run(record, self.access, self.result())
        self.assertEqual(result["memory_update"]["status"], "unknown")
        self.preferences.write_fault = None
        checked = self.service.decide(self.latest(record), self.access, "accept")
        self.assertEqual(checked["result"]["memory_update"]["status"], "not_saved")
        self.service.decide(checked, self.access, "accept")
        self.assertEqual(len(self.preferences.replace_calls), 1)
        self.assertEqual(self.preferences.read("test_person_a", "buyer")["version"], 0)

    def test_read_failure_unknown_does_not_turn_into_delayed_write(self):
        record = self.record()
        self.preferences.read_error = True
        result = self.service.after_run(record, self.access, self.result())
        self.assertEqual(result["memory_update"]["status"], "unknown")
        still_unknown = self.service.decide(self.latest(record), self.access, "accept")
        self.assertEqual(still_unknown["result"]["memory_update"]["status"], "unknown")
        self.preferences.read_error = False
        checked = self.service.decide(self.latest(record), self.access, "accept")
        self.assertEqual(checked["result"]["memory_update"]["status"], "not_saved")
        self.assertEqual(self.preferences.replace_calls, [])

    def test_recovery_requires_exact_version_values_and_source(self):
        for mismatch in ("source", "values", "version"):
            with self.subTest(mismatch=mismatch):
                self.preferences = FakePreferences()
                self.service.preferences = self.preferences
                record = self.record()
                self.preferences.write_fault = "after_commit"
                self.service.after_run(record, self.access, self.result())
                row = self.preferences.read("test_person_a", "buyer")
                if mismatch == "source":
                    row["source"] = {"kind": "user_settings"}
                elif mismatch == "values":
                    row["values"] = {"answer_style": "detailed"}
                else:
                    row["version"] += 1
                self.preferences.rows[("test_person_a", "buyer")] = row
                response = self.service.decide(self.latest(record), self.access, "accept")
                self.assertEqual(response["result"]["memory_update"]["status"], "conflict")
                self.assertEqual(len(self.preferences.replace_calls), 1)

    def test_same_values_are_unchanged_without_version_increment(self):
        self.preferences.put("test_person_a", "buyer", {"answer_style": "brief"}, 4,
                             {"kind": "user_settings"})
        record = self.record(values={"answer_style": "brief"}, version=4)
        result = self.service.after_run(record, self.access, self.result())
        self.assertEqual(result["memory_update"]["status"], "unchanged")
        self.assertEqual(result["memory_update"]["version"], 4)
        self.assertEqual(self.preferences.replace_calls, [])

    def test_cancelled_or_failed_task_does_not_save_candidate(self):
        for status, cancelled in (("completed", True), ("failed", False), ("cancelled", False),
                                  ("interrupted", False)):
            with self.subTest(status=status, cancelled=cancelled):
                record = self.record()
                result = self.service.after_run(record, self.access, self.result(status=status), cancelled=cancelled)
                self.assertEqual(result["memory_update"]["status"], "discarded")
        self.assertEqual(self.preferences.replace_calls, [])

    def test_pending_task_cannot_be_confirmed_after_cancellation(self):
        record = self.propose()
        cancelled = self.tasks.update(record["id"], self.scope, status="cancelled")
        with self.assertRaises(MemoryDecisionConflict):
            self.service.decide(cancelled, self.access, "accept")
        self.assertEqual(self.preferences.replace_calls, [])

    def test_missing_or_wrong_role_snapshot_is_rejected_without_write(self):
        for snapshot in (None, {"role": "merchant", "values": {}, "version": 0}):
            with self.subTest(snapshot=snapshot):
                record = self.record()
                record["preference_snapshot"] = snapshot
                result = self.service.after_run(record, self.access, self.result())
                self.assertEqual(result["memory_update"]["status"], "rejected")
        self.assertEqual(self.preferences.replace_calls, [])

    def test_unsupported_memory_metadata_does_not_destroy_business_result(self):
        record, result = self.record(), self.result()
        result["memory_proposal"]["values"] = {"buyer_id": "B002"}
        processed = self.service.after_run(record, self.access, result)
        self.assertEqual(processed["status"], "completed")
        self.assertEqual(processed["memory_update"]["status"], "rejected")
        self.assertTrue(processed["answer"].startswith("业务结果：订单金额已核对。"))
        self.assertEqual(self.preferences.replace_calls, [])

    def test_no_proposal_and_no_display_signal_has_no_memory_side_effect(self):
        record, result = self.record("查询O001"), self.result("查询O001")
        result.pop("memory_proposal")
        processed = self.service.after_run(record, self.access, result)
        self.assertIs(processed, result)
        self.assertIsNone(self.latest(record)["result"])
        self.assertEqual(self.preferences.replace_calls, [])

    def test_write_intent_must_persist_before_database_write(self):
        record = self.record()
        with patch.object(self.tasks, "update", side_effect=OSError("offline task file failure")):
            with self.assertRaises(OSError):
                self.service.after_run(record, self.access, self.result())
        self.assertEqual(self.preferences.replace_calls, [])
        self.assertIsNone(self.latest(record)["result"])

    def test_task_file_failure_after_commit_leaves_recoverable_preparing(self):
        record = self.record()
        original_update = self.tasks.update
        calls = []

        def fail_final_write(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("offline final task persistence failure")
            return original_update(*args, **kwargs)

        with patch.object(self.tasks, "update", side_effect=fail_final_write):
            with self.assertRaises(OSError):
                self.service.after_run(record, self.access, self.result())
        pending = self.latest(record)
        self.assertEqual(pending["result"]["memory_update"]["status"], "preparing")
        recovered = self.service.decide(pending, self.access, "accept")
        self.assertEqual(recovered["result"]["memory_update"]["status"], "saved")
        self.assertEqual(len(self.preferences.replace_calls), 1)

    def test_reexecuted_explicit_result_recognizes_same_write(self):
        record = self.record()
        self.service.after_run(record, self.access, self.result())
        replayed = self.service.after_run(record, self.access, self.result())
        self.assertEqual(replayed["memory_update"]["status"], "saved")
        self.assertEqual(len(self.preferences.replace_calls), 1)

    def test_wrong_role_cannot_confirm(self):
        record = self.propose()
        with self.assertRaises(MemoryDecisionConflict):
            self.service.decide(record, {**self.access, "role": "merchant"}, "accept")
        self.assertEqual(self.preferences.replace_calls, [])

    def test_unknown_action_is_not_implicit_acceptance(self):
        record = self.propose()
        for action in ("confirm", "ignore", "", None):
            with self.subTest(action=action), self.assertRaises(MemoryDecisionConflict):
                self.service.decide(record, self.access, action)
        self.assertEqual(self.preferences.replace_calls, [])

    def test_confirm_commit_error_recovers_confirmed_source_without_retry(self):
        record = self.propose()
        self.preferences.write_fault = "after_commit"
        unknown = self.service.decide(record, self.access, "accept")
        self.assertEqual(unknown["result"]["memory_update"]["status"], "unknown")
        self.preferences.write_fault = None
        recovered = self.service.decide(unknown, self.access, "accept")
        self.assertEqual(recovered["result"]["memory_update"]["status"], "saved")
        self.assertIn("user_confirmed", self.preferences.read("test_person_a", "buyer")["source"]["text"])
        self.assertEqual(len(self.preferences.replace_calls), 1)

    def test_public_update_omits_internal_cas_and_write_source(self):
        record = self.propose()
        public = public_memory_update(record["result"])
        self.assertEqual(public["status"], "proposed")
        self.assertNotIn("expected_version", public)
        self.assertNotIn("target_values", public)
        self.assertNotIn("source", public)
        public["values"]["answer_style"] = "detailed"
        self.assertEqual(record["result"]["memory_update"]["values"], {"answer_style": "brief"})


if __name__ == "__main__":
    unittest.main()
