"""独立内存SQLite与人工向量：验证生命周期约束，不代表正式库/真实模型通过。"""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.exc import OperationalError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from embedding_client import DIMENSION, MODEL
from policy_documents import PolicyDocumentError, load_manifest_files, parse_policy_file
from policy_kb import (metadata, policy_documents, policy_chunks, policy_lifecycle_events,
    PolicyConflictError, import_policy_files, index_policy_embeddings, search_policy_chunks, select_policy)
from policy_lifecycle import correct_policy, get_policy_lifecycle, validate_policy_reference, withdraw_policy

ROOT = Path(__file__).resolve().parents[1]
SCOPE = {"store_id": "S001", "category": "ordinary_shoes", "paid_at": "2026-10-15T10:00:00+08:00"}


class FakeEmbedder:
    source = "offline_test_double"
    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback
    def embed_texts(self, texts):
        self.calls.append(list(texts))
        if self.callback:
            self.callback()
        vector = [1.0] + [0.0] * (DIMENSION - 1)
        return {"model": MODEL, "dimension": DIMENSION, "source": self.source,
                "vectors": [vector for _ in texts]}


class PolicyLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:").execution_options(schema_translate_map={"public": None})
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.paths = load_manifest_files(ROOT / "knowledge/manifest.json")
        import_policy_files(self.engine, self.paths)
        self.original = parse_policy_file(self.paths[0])
        self.identity = self.original["document_id"]
        self.audit = {"expected_checksum": self.original["checksum"], "reason": "录入小时数有误，人工复核", "actor": "local-admin"}

    def replacement(self, *, identity="S001-shoes-2026-10-r2", version="2026-10-v2", changes=()):
        source = self.paths[0].read_text(encoding="utf-8").replace(self.identity, identity).replace(
            self.original["version"], version).replace("168小时", "240小时")
        for before, after in changes:
            source = source.replace(before, after)
        path = Path(self.temp.name) / (identity + ".md")
        path.write_text(source, encoding="utf-8")
        return path

    def rows(self, table):
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(table)).mappings()]

    def withdraw(self):
        return withdraw_policy(self.engine, self.identity, **self.audit)

    def correct(self, path=None):
        return correct_policy(self.engine, self.identity, path or self.replacement(), **self.audit)

    def test_withdraw_stops_rules_and_retrieval_without_deleting_evidence(self):
        before_docs, before_chunks = self.rows(policy_documents), self.rows(policy_chunks)
        frozen = deepcopy(select_policy(self.engine, **SCOPE))
        result = self.withdraw()
        self.assertEqual(result["status"], "withdrawn")
        selected = select_policy(self.engine, **SCOPE)
        self.assertEqual(selected["status"], "withdrawn")
        self.assertNotIn("rules", selected)
        self.assertNotIn("actor", selected["lifecycle"])
        fake = FakeEmbedder()
        found = search_policy_chunks(self.engine, "运费", **SCOPE, mode="hybrid", embedder=fake, allow_test_vectors=True)
        self.assertEqual(found["status"], "withdrawn"); self.assertEqual(found["results"], [])
        self.assertFalse(fake.calls)
        self.assertEqual(before_docs, self.rows(policy_documents)); self.assertEqual(before_chunks, self.rows(policy_chunks))
        self.assertEqual(frozen["rules"]["return_window_hours"], 168)
        self.assertEqual(select_policy(self.engine, **(SCOPE | {"paid_at": "2026-11-15T00:00:00+08:00"}))["payment_version"], "2026-11-v1")

    def test_withdraw_is_idempotent_but_never_rewrites_prior_reason(self):
        self.withdraw()
        self.assertEqual(self.withdraw()["status"], "unchanged")
        with self.assertRaises(PolicyConflictError):
            withdraw_policy(self.engine, self.identity, **(self.audit | {"reason": "改掉原理由"}))
        self.assertEqual(len(self.rows(policy_lifecycle_events)), 1)

    def test_correction_of_active_policy_preserves_original_and_uses_new_rules(self):
        before_docs, before_chunks = self.rows(policy_documents), self.rows(policy_chunks)
        path = self.replacement(); new = parse_policy_file(path)
        result = self.correct(path)
        self.assertEqual(result["embedding_requests"], 0)
        selected = select_policy(self.engine, **SCOPE)
        self.assertEqual(selected["rules"]["return_window_hours"], 240)
        self.assertEqual(selected["lifecycle"]["revision_chain"], [self.identity, new["document_id"]])
        self.assertEqual(self.rows(policy_documents)[:2], before_docs)
        self.assertEqual(self.rows(policy_chunks)[:6], before_chunks)
        self.assertTrue(all(row["embedding"] is None for row in self.rows(policy_chunks) if row["document_id"] == new["document_id"]))
        self.assertEqual(self.correct(path)["status"], "unchanged")
        self.assertEqual(len(self.rows(policy_lifecycle_events)), 1)

    def test_withdraw_then_correct_and_rewithdraw_never_revives_original(self):
        self.withdraw(); path = self.replacement(); replacement = parse_policy_file(path)
        self.correct(path)
        self.assertEqual(select_policy(self.engine, **SCOPE)["rules"]["return_window_hours"], 240)
        withdraw_policy(self.engine, replacement["document_id"], expected_checksum=replacement["checksum"], reason="更正资料也待复核", actor="local-admin")
        selected = select_policy(self.engine, **SCOPE)
        self.assertEqual(selected["status"], "withdrawn")
        self.assertEqual(selected["lifecycle"]["document_id"], replacement["document_id"])
        with self.assertRaises(PolicyConflictError):
            self.withdraw()  # 不能撤回已替代的祖先来间接修改当前稿。

    def test_second_correction_forms_one_chain(self):
        first = self.replacement(); self.correct(first); first_doc = parse_policy_file(first)
        second = self.replacement(identity="S001-shoes-2026-10-r3", version="2026-10-v3", changes=(("240小时", "300小时"),))
        correct_policy(self.engine, first_doc["document_id"], second, expected_checksum=first_doc["checksum"], reason="再次核正", actor="local-admin")
        selected = select_policy(self.engine, **SCOPE)
        self.assertEqual(selected["rules"]["return_window_hours"], 300)
        self.assertEqual(len(selected["lifecycle"]["revision_chain"]), 3)
        with self.assertRaises(PolicyConflictError):
            self.correct(self.replacement(identity="unrelated-branch", version="branch-v1"))

    def test_reference_annotation_is_external_and_detects_stale_frozen_hash(self):
        frozen = deepcopy(select_policy(self.engine, **SCOPE)["rule_citations"])
        self.correct()
        state = validate_policy_reference(self.engine, document_id=self.identity, document_checksum=self.original["checksum"])
        self.assertEqual(state["status"], "corrected")
        self.assertEqual(state["reason"], "policy_reference_superseded")
        self.assertNotIn("actor", state["lifecycle"])
        self.assertEqual(frozen["return_window_hours"]["text"], self.original["rule_citations"]["return_window_hours"]["text"])
        self.assertEqual(validate_policy_reference(self.engine, document_id=self.identity, document_checksum="a"*64)["status"], "invalid_policy")
        self.assertEqual(get_policy_lifecycle(self.engine, ["missing-policy"])["missing-policy"]["status"], "missing")

    def test_incorrect_expected_hash_does_not_write_anything(self):
        for action in (lambda: withdraw_policy(self.engine, self.identity, **(self.audit | {"expected_checksum": "a"*64})),
                       lambda: correct_policy(self.engine, self.identity, self.replacement(), **(self.audit | {"expected_checksum": "a"*64}))):
            with self.assertRaises(PolicyConflictError):
                action()
        self.assertEqual(len(self.rows(policy_documents)), 2); self.assertFalse(self.rows(policy_lifecycle_events))

    def test_correction_does_not_change_scope_period_or_parse_unknown_rules(self):
        changes = [(('S001', 'S002'),), (("2026-11-01T00:00:00+08:00", "2026-12-01T00:00:00+08:00"),),
                   (("定制商品不适用", "定制商品也适用"),)]
        for number, item in enumerate(changes):
            path = self.replacement(identity=f"rejected-{number}", changes=item)
            with self.assertRaises((PolicyConflictError, PolicyDocumentError)):
                self.correct(path)
        self.assertEqual(len(self.rows(policy_documents)), 2); self.assertFalse(self.rows(policy_lifecycle_events))

    def test_regular_import_cannot_bypass_correction_and_withdrawal(self):
        self.withdraw()
        with self.assertRaises(PolicyConflictError):
            import_policy_files(self.engine, [self.replacement()])
        self.assertEqual(import_policy_files(self.engine, [self.paths[0]])["status"], "unchanged")
        self.assertEqual(select_policy(self.engine, **SCOPE)["status"], "withdrawn")

    def test_new_future_period_uses_existing_import_not_correction(self):
        path = self.replacement(identity="S001-shoes-future", version="future-v1",
            changes=(("2026-10-01T00:00:00+08:00", "2026-12-01T00:00:00+08:00"),
                     ("2026-11-01T00:00:00+08:00", "2027-01-01T00:00:00+08:00")))
        with self.assertRaises(PolicyConflictError):
            self.correct(path)
        self.assertEqual(import_policy_files(self.engine, [path])["status"], "imported")
        self.assertEqual(select_policy(self.engine, **SCOPE)["rules"]["return_window_hours"], 168)

    def test_event_write_failure_rolls_back_new_document_and_chunks(self):
        def fail(_conn, _cursor, statement, _params, _context, _many):
            if statement.startswith("INSERT INTO main.policy_lifecycle_events"):
                raise RuntimeError("injected event failure")
        event.listen(self.engine, "before_cursor_execute", fail)
        try:
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.correct()
        finally:
            event.remove(self.engine, "before_cursor_execute", fail)
        self.assertEqual(len(self.rows(policy_documents)), 2); self.assertEqual(len(self.rows(policy_chunks)), 6)
        self.assertEqual(select_policy(self.engine, **SCOPE)["rules"]["return_window_hours"], 168)

    def test_only_current_documents_get_new_vectors_and_old_vectors_remain(self):
        fake = FakeEmbedder(); index_policy_embeddings(self.engine, fake, allow_test_vectors=True)
        old = self.rows(policy_chunks); self.correct(); fake.calls.clear()
        prepared = index_policy_embeddings(self.engine, fake, allow_test_vectors=True)
        self.assertEqual(prepared["indexed_chunks"], 3)
        self.assertEqual(len(fake.calls[0]), 3)
        self.assertEqual(self.rows(policy_chunks)[:6], old)
        fake.calls.clear()
        with self.assertRaises(ValueError):
            index_policy_embeddings(self.engine, fake, [self.identity], allow_test_vectors=True)
        self.assertFalse(fake.calls)

    def test_withdrawal_during_index_response_prevents_entire_vector_write(self):
        fake = FakeEmbedder(callback=self.withdraw)
        with self.assertRaises(PolicyConflictError):
            index_policy_embeddings(self.engine, fake, allow_test_vectors=True)
        self.assertTrue(all(row["embedding"] is None for row in self.rows(policy_chunks)))

    def test_withdrawal_during_query_embedding_discards_old_candidates(self):
        index_policy_embeddings(self.engine, FakeEmbedder(), allow_test_vectors=True)
        fake = FakeEmbedder(callback=self.withdraw)
        result = search_policy_chunks(self.engine, "退货", **SCOPE, mode="hybrid", embedder=fake, allow_test_vectors=True)
        self.assertEqual(result["status"], "withdrawn"); self.assertEqual(result["results"], [])
        self.assertEqual(result["stats"]["embedding_requests"], 1)
        self.assertNotIn("keyword_results", result)

    def test_correction_needs_its_own_index_before_hybrid_can_query(self):
        index_policy_embeddings(self.engine, FakeEmbedder(), allow_test_vectors=True)
        self.correct(); fake = FakeEmbedder()
        result = search_policy_chunks(self.engine, "退货", **SCOPE, mode="hybrid", embedder=fake, allow_test_vectors=True)
        self.assertEqual(result["reason"], "candidate_embeddings_missing_or_stale"); self.assertFalse(fake.calls)
        self.assertEqual(search_policy_chunks(self.engine, "退货", **SCOPE)["payment_version"], "2026-10-v2")

    def test_missing_migration_is_an_error_never_implicit_active(self):
        with self.engine.begin() as connection:
            connection.execute(text("DROP TABLE policy_lifecycle_events"))
        with self.assertRaises(OperationalError):
            select_policy(self.engine, **SCOPE)

    def test_invalid_admin_inputs_reject_before_write(self):
        bad_values = ({"reason": ""}, {"reason": "x"*501}, {"reason": "line\nbreak"},
                      {"actor": "../admin"}, {"actor": "x"*81}, {"expected_checksum": "not-a-checksum"})
        for bad in bad_values:
            with self.assertRaises(ValueError):
                withdraw_policy(self.engine, self.identity, **(self.audit | bad))
        self.assertFalse(self.rows(policy_lifecycle_events))


if __name__ == "__main__":
    unittest.main()
