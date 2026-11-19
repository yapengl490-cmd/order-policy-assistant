"""独立SQLite离线库验证导入/过滤/对照机制，人工向量不证明真实嵌入效果。"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest

from sqlalchemy import create_engine, select, update, func

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from embedding_client import MODEL, DIMENSION, EmbeddingError
from policy_documents import load_manifest_files
from policy_kb import (metadata, policy_documents, policy_chunks, PolicyConflictError,
    import_policy_files, select_policy, index_policy_embeddings, search_policy_chunks, _hybrid_order)

ROOT = Path(__file__).resolve().parents[1]
SCOPE = {"store_id": "S001", "category": "ordinary_shoes", "paid_at": "2026-10-15T10:00:00+08:00"}


class FakeEmbedder:
    source = "offline_test_double"
    def __init__(self):
        self.calls = []
        self.last_stats = {}
        self.before_return = None
    def embed_texts(self, texts):
        self.calls.append(list(texts))
        self.last_stats = {"requests": 1, "inputs": len(texts), "source": self.source, "usage": {}, "elapsed_seconds": .001}
        vectors = []
        for text in texts:
            axis = 1 if "退货运费" in text or "快递费" in text else 0 if "退货资格" in text else 2
            vector = [0.0]*DIMENSION; vector[axis] = 1.0; vectors.append(vector)
        if self.before_return:
            self.before_return()
        return {"model": MODEL, "dimension": DIMENSION, "source": self.source, "vectors": vectors, "stats": self.last_stats}


class PolicyKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:").execution_options(schema_translate_map={"public": None})
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.paths = load_manifest_files(ROOT / "knowledge/manifest.json")
        original_sources = {path: path.read_bytes() for path in self.paths}
        self.addCleanup(lambda: [self.assertEqual(path.read_bytes(), source, "测试不得改写受控原文") for path, source in original_sources.items()])

    def changed_file(self, name, changes):
        source = self.paths[0].read_text()
        for before, after in changes:
            source = source.replace(before, after)
        path = Path(self.temp.name) / name
        path.write_text(source)
        return path

    def count(self, table):
        with self.engine.connect() as conn:
            return conn.scalar(select(func.count()).select_from(table))

    def import_both(self):
        return import_policy_files(self.engine, self.paths)

    def test_real_files_import_idempotent_and_old_versions_remain(self):
        first = self.import_both()
        self.assertEqual(first["status"], "imported")
        self.assertEqual(first["embedding_requests"], 0)
        self.assertEqual(self.import_both()["status"], "unchanged")
        self.assertEqual(self.count(policy_documents), 2)
        self.assertEqual(self.count(policy_chunks), 6)

    def test_paid_at_boundary_and_scope_filter_precede_retrieval(self):
        self.import_both()
        october = select_policy(self.engine, **SCOPE)
        november = select_policy(self.engine, **(SCOPE | {"paid_at": "2026-11-01T00:00:00+08:00"}))
        self.assertEqual(october["rules"]["return_window_hours"], 168)
        self.assertEqual(november["rules"]["return_window_hours"], 336)
        self.assertEqual(november["applicability"]["selection_basis"], "order_paid_at")
        fake = FakeEmbedder()
        for update_scope in ({"store_id": "S002"}, {"category": "other"}, {"paid_at": "2026-12-01T00:00:00+08:00"}):
            result = search_policy_chunks(self.engine, "快递费谁出", **(SCOPE | update_scope), mode="hybrid", embedder=fake, allow_test_vectors=True)
            self.assertEqual(result["status"], "insufficient_evidence")
            self.assertEqual(result["stats"]["candidate_count"], 0)
        self.assertFalse(fake.calls)

    def test_same_id_change_is_rejected_without_overwriting(self):
        self.import_both()
        changed = self.changed_file("changed.md", [("168小时", "240小时")])
        with self.assertRaises(PolicyConflictError):
            import_policy_files(self.engine, [changed])
        self.assertEqual(select_policy(self.engine, **SCOPE)["rules"]["return_window_hours"], 168)

    def test_overlap_rejects_whole_transaction(self):
        overlap = self.changed_file("overlap.md", [("S001-shoes-2026-10", "S001-other"), ("2026-10-v1", "other-v1")])
        with self.assertRaises(PolicyConflictError):
            import_policy_files(self.engine, [self.paths[0], overlap])
        self.assertEqual(self.count(policy_documents), 0)
        self.assertEqual(self.count(policy_chunks), 0)

    def test_corrupted_stored_chunk_or_metadata_cannot_drive_rules(self):
        self.import_both()
        with self.engine.begin() as c:
            c.execute(update(policy_chunks).where(policy_chunks.c.section == "return_eligibility").values(text="只留下可以退货，没有例外"))
        self.assertEqual(select_policy(self.engine, **SCOPE)["status"], "invalid_policy")
        fake = FakeEmbedder()
        with self.assertRaises(PolicyConflictError):
            index_policy_embeddings(self.engine, fake, allow_test_vectors=True)
        self.assertFalse(fake.calls)

    def test_keyword_does_not_call_embedding_and_full_exception_is_retained(self):
        self.import_both()
        fake = FakeEmbedder()
        result = search_policy_chunks(self.engine, "退货资格 未穿着 吊牌完好 定制", **SCOPE, mode="keyword", embedder=fake)
        self.assertFalse(fake.calls)
        self.assertEqual(result["results"][0]["section"], "return_eligibility")
        self.assertIn("定制商品不适用", result["results"][0]["text"])
        self.assertEqual(result["stats"]["reranker"], "not_enabled")

    def test_fake_vector_is_explicit_and_cannot_be_mistaken_for_live(self):
        self.import_both(); fake = FakeEmbedder()
        with self.assertRaises(ValueError):
            index_policy_embeddings(self.engine, fake)
        prepared = index_policy_embeddings(self.engine, fake, allow_test_vectors=True)
        self.assertEqual(prepared["indexed_chunks"], 6)
        self.assertEqual(prepared["source"], "offline_test_double")
        self.assertEqual(index_policy_embeddings(self.engine, fake, allow_test_vectors=True)["embedding_requests"], 0)
        denied = search_policy_chunks(self.engine, "快递费谁出", **SCOPE, mode="hybrid", embedder=fake)
        self.assertEqual(denied["status"], "embedding_unavailable")
        hybrid = search_policy_chunks(self.engine, "快递费谁出", **SCOPE, mode="hybrid", embedder=fake, allow_test_vectors=True)
        keyword = search_policy_chunks(self.engine, "快递费谁出", **SCOPE)
        self.assertEqual(hybrid["results"][0]["section"], "shipping_cost")
        self.assertEqual(hybrid["stats"]["candidate_ids"], keyword["stats"]["candidate_ids"])
        self.assertEqual(hybrid["stats"]["embedding_source"], "offline_test_double")
        self.assertEqual(hybrid["keyword_results"], keyword["results"])
        self.assertEqual(len(hybrid["score_details"]), 3)

    def test_equal_rrf_uses_cosine_before_arbitrary_chunk_identifier(self):
        self.import_both()
        class ContrastingRanks(FakeEmbedder):
            def embed_texts(self, texts):
                result = super().embed_texts(texts)
                vectors = []
                for text in texts:
                    vector = [0.0] * DIMENSION
                    if text == "退货资格" or "## 退货运费" in text:
                        vector[0] = 1.0
                    elif "## 退货资格" in text:
                        vector[0], vector[1] = .6, .8
                    else:
                        vector[1] = 1.0
                    vectors.append(vector)
                result["vectors"] = vectors
                return result
        fake = ContrastingRanks()
        index_policy_embeddings(self.engine, fake, allow_test_vectors=True)
        result = search_policy_chunks(self.engine, "退货资格", **SCOPE,
            mode="hybrid", embedder=fake, allow_test_vectors=True)
        # 人工向量刻意制造互换排名；只验排序规则，不代表这一问真实该答运费。
        scores = {row["chunk_id"].split(":")[-1]: row for row in result["score_details"]}
        self.assertEqual(scores["return_eligibility"]["hybrid_rrf"], scores["shipping_cost"]["hybrid_rrf"])
        self.assertGreater(scores["shipping_cost"]["cosine"], scores["return_eligibility"]["cosine"])
        self.assertEqual(result["results"][0]["section"], "shipping_cost")
        self.assertEqual(len(fake.calls), 2)  # 建索引一次、查询一次；不增加同分补调。

    def test_fusion_primary_score_is_not_overridden_by_cosine(self):
        chunks = [{"chunk_id": "a"}, {"chunk_id": "b"}]
        self.assertEqual([row["chunk_id"] for row in _hybrid_order(chunks,
            {"a": .1, "b": .2}, {"a": .99, "b": .01})], ["b", "a"])

    def test_complete_tie_is_stable_across_input_order(self):
        chunks = [{"chunk_id": "b"}, {"chunk_id": "a"}]
        first = _hybrid_order(chunks, {"a": .1, "b": .1}, {"a": .5, "b": .5})
        second = _hybrid_order(list(reversed(chunks)), {"a": .1, "b": .1}, {"a": .5, "b": .5})
        self.assertEqual(first, second)
        self.assertEqual([row["chunk_id"] for row in first], ["a", "b"])

    def test_missing_or_stale_vectors_do_not_make_paid_query(self):
        self.import_both(); fake = FakeEmbedder()
        result = search_policy_chunks(self.engine, "快递费", **SCOPE, mode="hybrid", embedder=fake, allow_test_vectors=True)
        self.assertEqual(result["reason"], "candidate_embeddings_missing_or_stale")
        self.assertFalse(fake.calls)

    def test_index_content_change_rolls_back_entire_vector_write(self):
        self.import_both(); fake = FakeEmbedder()
        with self.engine.connect() as c:
            identity = c.scalar(select(policy_chunks.c.chunk_id).order_by(policy_chunks.c.chunk_id).offset(1))
        def interfere():
            with self.engine.begin() as c:
                c.execute(update(policy_chunks).where(policy_chunks.c.chunk_id == identity).values(text="期间发生修改"))
        fake.before_return = interfere
        with self.assertRaises(PolicyConflictError):
            index_policy_embeddings(self.engine, fake, allow_test_vectors=True)
        with self.engine.connect() as c:
            self.assertTrue(all(value is None for value in c.scalars(select(policy_chunks.c.embedding))))

    def test_embedding_error_is_not_reported_as_successful_hybrid(self):
        self.import_both(); fake = FakeEmbedder()
        index_policy_embeddings(self.engine, fake, allow_test_vectors=True)
        def fail(_texts):
            raise EmbeddingError("transport_or_json_error")
        fake.embed_texts = fail
        result = search_policy_chunks(self.engine, "退款运费", **SCOPE, mode="hybrid", embedder=fake, allow_test_vectors=True)
        self.assertEqual(result["status"], "embedding_failed")
        self.assertEqual(result["results"], [])
        self.assertIn("keyword_results", result)


if __name__ == "__main__":
    unittest.main()
