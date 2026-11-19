"""受控原文/规则绑定的离线检查；不调用外部服务。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from policy_documents import parse_policy_file, parse_policy_text, load_manifest_files, PolicyDocumentError
ROOT = Path(__file__).resolve().parents[1]


class PolicyDocumentTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / "knowledge/2026-10-shoes.md"
        self.source = self.path.read_text()
        self.original_sources = {path: path.read_bytes() for path in (ROOT/"knowledge").glob("*-shoes.md")}

    def tearDown(self):
        for path, original in self.original_sources.items():
            self.assertEqual(path.read_bytes(), original, "测试不能修改受控知识原文")

    def test_rules_come_from_complete_body_and_citations_keep_exceptions(self):
        doc = parse_policy_file(self.path)
        self.assertEqual(doc["rules"]["return_window_hours"], 168)
        self.assertEqual(doc["rules"]["return_window_basis"], "delivered_at")
        self.assertEqual(doc["rules"]["return_window_boundary"], "start_inclusive_end_exclusive")
        self.assertEqual(len(doc["chunks"]), 3)
        self.assertFalse(doc["rules"]["self_report_verified"])
        citation = doc["rule_citations"]["return_window_hours"]
        self.assertIn("定制商品不适用", citation["text"])
        self.assertIn("未穿着且吊牌完好", citation["text"])
        lines = self.source.splitlines()
        self.assertEqual("\n".join(lines[citation["line_start"]-1:citation["line_end"]]), citation["text"])
        self.assertEqual(doc["checksum"], hashlib.sha256(self.source.encode()).hexdigest())

    def test_new_window_is_derived_from_body_not_version_name(self):
        changed = self.source.replace("168小时", "240小时")
        doc = parse_policy_text(changed)
        self.assertEqual(doc["rules"]["return_window_hours"], 240)
        self.assertEqual(doc["version"], "2026-10-v1")
        self.assertNotEqual(doc["checksum"], parse_policy_text(self.source)["checksum"])

    def test_partial_window_change_missing_exception_or_unrecognized_policy_rejected(self):
        mutations = [self.source.replace("满168小时", "满336小时"),
            self.source.replace("收货时刻", "支付时刻"),
            self.source.replace("不包含满168小时", "包含满168小时"),
            self.source.replace("例外：定制商品不适用本条退货资格。", ""),
            self.source.replace("未穿着且吊牌完好", "未穿着或吊牌完好"),
            self.source.replace("买家承担", "商家承担"), self.source + "额外政策：所有人都能退。\n",
            self.source.replace('"source_kind": "synthetic_demo"', '"source_kind": "real_platform"'),
            self.source.replace("适用店铺：S001", "适用店铺：S002")]
        for source in mutations:
            with self.subTest(source=source[-70:]), self.assertRaises(PolicyDocumentError):
                parse_policy_text(source)

    def test_mutated_copy_does_not_edit_controlled_original(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad-window.md"
            path.write_text(self.source.replace("收货时刻", "支付时刻"))
            with self.assertRaises(PolicyDocumentError):
                parse_policy_file(path)
        self.assertEqual(self.path.read_text(), self.source)

    def test_long_clause_is_rejected_not_truncated(self):
        with self.assertRaises(PolicyDocumentError):
            parse_policy_text(self.source + "很长的额外条款" * 1000)

    def test_metadata_cannot_be_second_rules_truth(self):
        source = self.source.replace('"title":', '"return_window_hours": 336,\n  "title":')
        with self.assertRaises(PolicyDocumentError):
            parse_policy_text(source)
        with self.assertRaises(PolicyDocumentError):
            parse_policy_text(self.source.replace('"store_id": "S001",', '"store_id": "S001", "store_id": "S002",'))

    def test_manifest_checks_fingerprint_and_keeps_versions(self):
        paths = load_manifest_files(ROOT / "knowledge/manifest.json")
        self.assertEqual([parse_policy_file(p)["rules"]["return_window_hours"] for p in paths], [168, 336])
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder); (base / "p.md").write_text(self.source)
            manifest = {"schema_version": 1, "source_kind": "synthetic_demo", "files": [{"path": "p.md", "sha256": "0"*64}]}
            (base / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(PolicyDocumentError):
                load_manifest_files(base / "manifest.json")


if __name__ == "__main__":
    unittest.main()
