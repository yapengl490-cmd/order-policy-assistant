"""公开演示准备的独立SQLite/临时配置验收；不连接正式库、不创建真实账号。"""
import copy
from datetime import datetime
import io
import json
import os
from pathlib import Path
import shutil
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from sqlalchemy import Column, ForeignKey, Table, Text, select
import test_import_provenance as fixtures
from app_config import AppSettings, authorize
from import_data import ImportConflictError, import_payload
from mcp_credentials import authorize_mcp_token, load_mcp_credential
from prepare_public_demo import (ACCOUNT_ID, DemoPrepareError, SHOP_ID, SCOPE_KEY,
                                 build_demo_payload, main, prepare_demo)
from sales_analysis import compare_periods

SOURCE = Path(__file__).resolve().parents[1]


class PublicDemoTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ImportProvenanceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.root = self.fixture.engine, self.fixture.directory
        meta = self.fixture.batches.metadata
        self.channels = Table("order_channels", meta,
            Column("order_id", Text, ForeignKey("public.orders.order_id"), primary_key=True),
            Column("channel", Text, nullable=False))
        meta.create_all(self.engine)
        shutil.copytree(SOURCE / "samples/uci", self.root / "samples/uci")
        (self.root / "local").mkdir(mode=0o700)
        self.original = {"scope": "synthetic_local_demo", "extra": "保留原元信息", "accounts": [
            {"account_id": "original_account", "token": "a" * 43, "label": "旧本人账号",
             "roles": [{"role": "buyer", "buyer_id": "B001"}, {"role": "merchant", "shop_id": "S001"}],
             "legacy_task_scopes": ["buyer:B001", "merchant:S001"]}]}
        self.original_mcp = {"scope": "local_mcp_demo", "credentials": [
            {"scope_key": "buyer:B001", "token": "b" * 43},
            {"scope_key": "merchant:S001", "token": "c" * 43}]}
        self.write("access.json", self.original)
        self.write("mcp_access.json", self.original_mcp)
        old = self.fixture.extended()
        old["shops"][0]["shop_id"] = "S001"
        old["order_shops"][0]["shop_id"] = "S001"
        import_payload(self.engine, old)
        self.old_payload = old
        self.expected = json.loads((SOURCE / "samples/uci/uci_demo_comparison_expected.json").read_text())

    def write(self, name, value):
        path = self.root / "local" / name
        path.write_text(json.dumps(value, ensure_ascii=False))
        os.chmod(path, 0o600)

    def private(self, name):
        return json.loads((self.root / "local" / name).read_text())

    def snapshot(self):
        with self.engine.connect() as connection:
            return {t.name: [dict(row) for row in connection.execute(select(t).order_by(*t.primary_key.columns)).mappings()]
                    for t in (self.fixture.buyers, self.fixture.orders, self.fixture.refunds,
                              self.fixture.shops, self.fixture.mappings, self.channels,
                              self.fixture.batches, self.fixture.sources)}

    def test_full_prepare_has_distinct_identity_mcp_and_no_old_data_changes(self):
        before = self.snapshot()
        result = prepare_demo(self.engine, self.root)
        self.assertEqual(result["status"], "ready")
        access = self.private("access.json")
        self.assertEqual(access["accounts"][:-1], self.original["accounts"])
        self.assertEqual(access["extra"], self.original["extra"])
        account = access["accounts"][-1]
        self.assertEqual(account["account_id"], ACCOUNT_ID)
        settings = AppSettings(tokens_file=self.root / "local/access.json", memory_enabled=True)
        scope = authorize(account["token"], "merchant", settings)
        self.assertEqual(scope["shop_id"], SHOP_ID)
        self.assertIn(ACCOUNT_ID, scope["task_key"])
        self.assertIsNone(scope["legacy_task_scope"])
        self.assertIsNone(authorize(account["token"], "buyer", settings))
        self.assertEqual(authorize("a" * 43, "merchant", settings)["shop_id"], "S001")
        mcp_token = load_mcp_credential(SCOPE_KEY, self.root)
        self.assertNotEqual(mcp_token, account["token"])
        self.assertEqual(authorize_mcp_token(mcp_token, self.root)["shop_id"], SHOP_ID)
        self.assertIsNone(authorize_mcp_token(account["token"], self.root))
        self.assertEqual(self.private("mcp_access.json")["credentials"][:-1], self.original_mcp["credentials"])
        after = self.snapshot()
        for table, rows in before.items():
            for row in rows:
                self.assertIn(row, after[table])
        for path in (self.root / "local").glob("*.json"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(account["token"], json.dumps(result))
        self.assertNotIn(mcp_token, json.dumps(result))

    def test_repeat_preserves_database_and_credentials_exactly(self):
        prepare_demo(self.engine, self.root)
        before = self.snapshot()
        files = {p.name: p.read_bytes() for p in (self.root / "local").glob("*.json")}
        repeated = prepare_demo(self.engine, self.root)
        self.assertEqual(repeated["import"]["status"], "already_imported")
        self.assertFalse(repeated["account_created"])
        self.assertFalse(repeated["mcp_credential_created"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / "local").glob("*.json")}, files)

    def test_original_projection_unchanged_and_frozen_comparison_matches(self):
        original = (self.root / "samples/uci/uci_projection.json").read_bytes()
        payload = build_demo_payload(self.root)
        source = json.loads(original)
        for key in ("buyers", "orders", "refunds", "shops", "order_shops"):
            self.assertEqual(payload[key], source[key])
        prepare_demo(self.engine, self.root)
        periods = [datetime.fromisoformat(v) for v in self.expected["conditions"].values()]
        got = compare_periods(self.engine, {"role": "merchant", "shop_id": SHOP_ID}, *periods)
        for key in ("first", "second", "delta", "reason_status"):
            self.assertEqual(got[key], self.expected[key])
        self.assertEqual({row["channel"]: row["contribution_net_fen"] for row in got["channels"]},
                         self.expected["channel_net_delta_fen"])
        self.assertEqual(got["provenance"]["status"], "recorded")
        self.assertTrue(all("合成" in row["source"] for row in got["provenance"]["batches"]))
        self.assertEqual((self.root / "samples/uci/uci_projection.json").read_bytes(), original)
        old = compare_periods(self.engine, {"role": "merchant", "shop_id": "S001"}, *periods)
        self.assertFalse(any(row["order_id"].startswith("O_PUBLIC_SYNTH_") for row in old["order_evidence"]))

    def test_conflicting_order_refuses_all_changes_and_no_account_is_bound(self):
        payload = build_demo_payload(self.root)
        payload["orders"][0]["paid_amount_fen"] += 1
        import_payload(self.engine, payload)
        before = self.snapshot()
        with self.assertRaises(ImportConflictError):
            prepare_demo(self.engine, self.root)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.private("access.json"), self.original)
        self.assertEqual(self.private("mcp_access.json"), self.original_mcp)

    def test_wrong_account_binding_is_rejected_before_import(self):
        for kind in ("same_id_wrong_role", "other_account_scope"):
            account = {"account_id": ACCOUNT_ID if kind == "same_id_wrong_role" else "other_demo",
                       "token": "z" * 43, "roles": [{"role": "merchant", "shop_id": "S001" if kind == "same_id_wrong_role" else SHOP_ID}]}
            payload = copy.deepcopy(self.original)
            payload["accounts"].append(account)
            self.write("access.json", payload)
            before = self.snapshot()
            with self.assertRaises(DemoPrepareError):
                prepare_demo(self.engine, self.root)
            self.assertEqual(before, self.snapshot())
            self.assertEqual(self.private("access.json"), payload)

    def test_permission_constant_alone_does_not_authorize_scope(self):
        with self.assertRaises(ValueError):
            load_mcp_credential(SCOPE_KEY, self.root)
        self.assertIsNone(authorize_mcp_token("unregistered" * 4, self.root))

    def test_failure_between_stores_has_no_account_and_resumes_without_reset(self):
        import prepare_public_demo as module
        real = module._replace_if_unchanged
        def fail_access(path, expected, data):
            if path.name == "access.json":
                raise OSError("controlled")
            return real(path, expected, data)
        with patch.object(module, "_replace_if_unchanged", side_effect=fail_access):
            with self.assertRaises(OSError):
                prepare_demo(self.engine, self.root)
        pending = self.private("mcp_access.json")["credentials"][-1]
        self.assertEqual(self.private("access.json"), self.original)
        self.assertIsNone(authorize_mcp_token(pending["token"], self.root))
        result = prepare_demo(self.engine, self.root)
        self.assertEqual(result["import"]["status"], "already_imported")
        self.assertFalse(result["mcp_credential_created"])
        self.assertEqual(load_mcp_credential(SCOPE_KEY, self.root), pending["token"])

    def test_missing_migration_and_bad_channel_are_refused_without_import(self):
        with self.engine.begin() as connection:
            self.channels.drop(connection)
        with patch("prepare_public_demo.import_payload") as importer:
            with self.assertRaises(DemoPrepareError):
                prepare_demo(self.engine, self.root)
            importer.assert_not_called()
        path = self.root / "samples/uci/uci_demo_channels.json"
        channels = json.loads(path.read_text())
        channels["order_channels"][0]["channel"] = "淘宝官方"
        path.write_text(json.dumps(channels))
        with self.assertRaises(DemoPrepareError):
            build_demo_payload(self.root)

    def test_private_symlink_and_duplicate_keys_refused(self):
        path = self.root / "local/access.json"
        path.unlink()
        path.symlink_to(self.root / "local/mcp_access.json")
        with self.assertRaises(DemoPrepareError):
            prepare_demo(self.engine, self.root)
        path.unlink()
        path.write_text('{"accounts":[],"accounts":[]}')
        with self.assertRaises(DemoPrepareError):
            prepare_demo(self.engine, self.root)

    def test_plan_only_never_reads_private_config_or_connects_database(self):
        output = io.StringIO()
        with patch("prepare_public_demo.build_demo_payload", return_value=build_demo_payload(self.root)), \
             patch("prepare_public_demo.local_admin_engine") as connect, redirect_stdout(output):
            self.assertEqual(main([]), 0)
        connect.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["status"], "plan_only")
        self.assertEqual(self.private("access.json"), self.original)
