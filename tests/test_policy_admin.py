"""管理员CLI离线检查：工厂替身和临时SQLite，不连接正式PG或任何模型。"""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event, func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import policy_admin
from policy_documents import load_manifest_files, parse_policy_file
from policy_kb import (metadata, policy_documents, policy_lifecycle_events,
                       import_policy_files, select_policy)

ROOT = Path(__file__).resolve().parents[1]
SCOPE = {"store_id": "S001", "category": "ordinary_shoes", "paid_at": "2026-10-15T10:00:00+08:00"}


class PolicyAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = load_manifest_files(ROOT / "knowledge/manifest.json")
        self.original = parse_policy_file(self.paths[0])
        self.identity = self.original["document_id"]
        self.engine = create_engine("sqlite+pysqlite:///" + str(Path(self.temp.name) / "policy-test.db")).execution_options(
            schema_translate_map={"public": None})
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        import_policy_files(self.engine, self.paths)
        self.replacement = Path(self.temp.name) / "corrected.md"
        self.replacement.write_text(self.paths[0].read_text(encoding="utf-8").replace(
            self.identity, "S001-shoes-2026-10-r2").replace(self.original["version"], "2026-10-v2").replace(
                "168小时", "240小时"), encoding="utf-8")

    def args(self, action="withdraw", *, checksum=None, actor="local-admin"):
        return [action, self.identity, "--expected-checksum", checksum or self.original["checksum"],
                "--reason", "录入错误待核正", "--actor", actor]

    def run_cli(self, args, **factory_options):
        output = io.StringIO()
        with patch.object(policy_admin, "local_admin_engine", **factory_options) as factory, redirect_stdout(output):
            status = policy_admin.main(args)
        return status, output.getvalue(), factory

    def count(self, table):
        with self.engine.connect() as connection:
            return connection.scalar(select(func.count()).select_from(table))

    def test_withdraw_preview_never_calls_connection_factory_or_mutation(self):
        with patch.object(policy_admin, "withdraw_policy") as withdraw:
            status, output, factory = self.run_cli(self.args(), side_effect=AssertionError("preview attempted database access"))
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)["status"], "preview_only_no_database_connection")
        factory.assert_not_called(); withdraw.assert_not_called()
        self.assertEqual(self.count(policy_lifecycle_events), 0)

    def test_correct_preview_reads_material_but_does_not_connect_or_claim_scope_passed(self):
        with patch.object(policy_admin, "correct_policy") as correct:
            status, output, factory = self.run_cli(self.args("correct") + ["--replacement", str(self.replacement)],
                side_effect=AssertionError("preview attempted database access"))
        report = json.loads(output)
        self.assertEqual(status, 0)
        self.assertEqual(report["replacement_document_id"], "S001-shoes-2026-10-r2")
        self.assertEqual(report["database_scope_and_version_validation"], "performed_in_apply_transaction")
        self.assertNotIn("source_text", report)
        factory.assert_not_called(); correct.assert_not_called()

    def test_apply_is_required_to_dispatch_real_withdraw_and_disposes_engine(self):
        with patch.object(self.engine, "dispose", wraps=self.engine.dispose) as dispose:
            status, output, factory = self.run_cli(self.args() + ["--apply"], return_value=self.engine)
        self.assertEqual(status, 0); self.assertEqual(json.loads(output)["status"], "withdrawn")
        factory.assert_called_once_with(ROOT / ".env")
        dispose.assert_called_once_with()
        self.assertEqual(select_policy(self.engine, **SCOPE)["status"], "withdrawn")
        self.assertEqual(self.count(policy_lifecycle_events), 1)

    def test_apply_correction_uses_real_transaction_and_new_document(self):
        status, output, factory = self.run_cli(self.args("correct") + ["--replacement", str(self.replacement), "--apply"],
                                              return_value=self.engine)
        self.assertEqual(status, 0); self.assertEqual(json.loads(output)["status"], "corrected")
        self.assertEqual(select_policy(self.engine, **SCOPE)["rules"]["return_window_hours"], 240)
        self.assertEqual(self.count(policy_documents), 3); self.assertEqual(self.count(policy_lifecycle_events), 1)

    def test_well_formed_wrong_checksum_fails_without_mutation(self):
        for action, extra in (("withdraw", []), ("correct", ["--replacement", str(self.replacement)])):
            with self.subTest(action=action):
                status, output, factory = self.run_cli(self.args(action, checksum="a"*64) + extra + ["--apply"],
                                                       return_value=self.engine)
                self.assertEqual(status, 2)
                self.assertIn("PolicyConflictError", output)
        self.assertEqual(self.count(policy_documents), 2); self.assertEqual(self.count(policy_lifecycle_events), 0)
        self.assertEqual(select_policy(self.engine, **SCOPE)["rules"]["return_window_hours"], 168)

    def test_invalid_arguments_stop_before_database_factory(self):
        cases = [self.args(checksum="bad") + ["--apply"], self.args(actor="../postgres") + ["--apply"],
                 self.args("correct") + ["--apply"],
                 self.args() + ["--replacement", str(self.replacement), "--apply"]]
        for args in cases:
            with self.subTest(args=args):
                status, output, factory = self.run_cli(args, side_effect=AssertionError("invalid arguments connected"))
                self.assertEqual(status, 2)
                factory.assert_not_called()

    def test_missing_or_invalid_replacement_stops_before_database_factory(self):
        invalid = Path(self.temp.name) / "invalid.md"
        invalid.write_text("不是受控政策", encoding="utf-8")
        for path in (invalid, Path(self.temp.name) / "missing.md"):
            status, output, factory = self.run_cli(self.args("correct") + ["--replacement", str(path), "--apply"],
                side_effect=AssertionError("invalid material connected"))
            self.assertEqual(status, 2); factory.assert_not_called()
            self.assertNotIn(str(path), output)

    def test_parser_errors_cannot_reach_database(self):
        with patch.object(policy_admin, "local_admin_engine") as factory, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                policy_admin.main(self.args() + ["--database", "other-target"])
        self.assertEqual(caught.exception.code, 2); factory.assert_not_called()

    def test_database_exception_text_is_not_exposed_and_engine_is_disposed(self):
        secret_marker = "private://user:do-not-print@database"
        with patch.object(policy_admin, "withdraw_policy", side_effect=RuntimeError(secret_marker)), \
                patch.object(self.engine, "dispose", wraps=self.engine.dispose) as dispose:
            status, output, factory = self.run_cli(self.args() + ["--apply"], return_value=self.engine)
        self.assertEqual(status, 2); self.assertIn("RuntimeError", output)
        self.assertNotIn(secret_marker, output); dispose.assert_called_once_with()

    def test_actor_cannot_bypass_missing_admin_configuration(self):
        # actor 字符串没有连接参数入口；真实管理员工厂失败仍停止。
        status, output, factory = self.run_cli(self.args(actor="postgres") + ["--apply"],
            side_effect=PermissionError("private administrator credential unavailable"))
        self.assertEqual(status, 2); factory.assert_called_once_with(ROOT / ".env")
        self.assertNotIn("private administrator credential unavailable", output)
        self.assertEqual(self.count(policy_lifecycle_events), 0)

    def test_actor_cannot_make_readonly_database_writable(self):
        # SQLite query_only 只验证未绕过数据库拒绝，不冒充PG角色权限验收。
        def readonly(connection, _record):
            connection.execute("PRAGMA query_only=ON")
        event.listen(self.engine, "connect", readonly)
        self.engine.dispose()
        try:
            status, output, factory = self.run_cli(self.args(actor="postgres") + ["--apply"], return_value=self.engine)
            self.assertEqual(status, 2); self.assertIn("OperationalError", output)
            self.assertEqual(self.count(policy_lifecycle_events), 0)
            self.assertEqual(select_policy(self.engine, **SCOPE)["rules"]["return_window_hours"], 168)
        finally:
            event.remove(self.engine, "connect", readonly)
            self.engine.dispose()


if __name__ == "__main__":
    unittest.main()
