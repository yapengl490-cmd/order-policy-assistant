"""小型受控政策库：先按可信店铺/品类/支付时刻过滤，再比较检索方法。

PostgreSQL保存原文和完整条款；JSON向量配Python精确余弦，无新数据库或扩展。
来源解析后的规则每次由原文重建，库中不保存第二套可独立改写的业务规则。
调用者必须先查真实授权订单，本模块的store/category参数不是身份认证入口。
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import math
import re
import time

from sqlalchemy import (CheckConstraint, Column, DateTime, ForeignKey, Integer, JSON, MetaData, Table, Text,
                        UniqueConstraint, and_, insert, select, text, update)
from sqlalchemy.exc import IntegrityError

from embedding_client import DIMENSION, MODEL, EmbeddingError, validate_vector
from policy_documents import PolicyDocumentError, aware_time, parse_policy_file, parse_policy_text

metadata = MetaData(schema="public")
policy_documents = Table("policy_documents", metadata,
    Column("document_id", Text, primary_key=True), Column("store_id", Text, nullable=False),
    Column("category", Text, nullable=False), Column("version", Text, nullable=False),
    Column("title", Text, nullable=False), Column("source_kind", Text, nullable=False),
    Column("effective_from", DateTime(timezone=True), nullable=False),
    Column("effective_before", DateTime(timezone=True), nullable=False),
    Column("checksum", Text, nullable=False), Column("source_path", Text, nullable=False),
    Column("source_text", Text, nullable=False), Column("imported_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("store_id", "category", "version"))
policy_chunks = Table("policy_chunks", metadata,
    Column("chunk_id", Text, primary_key=True),
    Column("document_id", Text, ForeignKey("public.policy_documents.document_id"), nullable=False),
    Column("section", Text, nullable=False), Column("line_start", Integer, nullable=False),
    Column("line_end", Integer, nullable=False), Column("text", Text, nullable=False),
    Column("checksum", Text, nullable=False), Column("embedding", JSON),
    Column("embedding_model", Text), Column("embedding_dimension", Integer),
    Column("embedding_source", Text), Column("embedding_text_checksum", Text),
    Column("embedded_at", DateTime(timezone=True)), UniqueConstraint("document_id", "section"))
# 生命周期属于同一份元数据；测试 create_all 与正式 011 迁移使用同一结构。
# 事件追加保存，原政策/原分块不覆盖；应用读账号不能更改这些事件。
policy_lifecycle_events = Table("policy_lifecycle_events", metadata,
    Column("event_id", Text, primary_key=True),
    Column("document_id", Text, ForeignKey("public.policy_documents.document_id"), nullable=False),
    Column("event_index", Integer, nullable=False), Column("action", Text, nullable=False),
    Column("replacement_document_id", Text, ForeignKey("public.policy_documents.document_id")),
    Column("document_checksum", Text, nullable=False), Column("reason", Text, nullable=False),
    Column("actor", Text, nullable=False), Column("recorded_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("document_id", "event_index"), UniqueConstraint("replacement_document_id"),
    CheckConstraint("event_index > 0"),
    CheckConstraint("(action = 'withdrawn' AND replacement_document_id IS NULL) OR "
                    "(action = 'corrected' AND replacement_document_id IS NOT NULL)"))


class PolicyConflictError(ValueError):
    pass


def _db_time(value, connection):
    # SQLite测试库只存本模块规范化的UTC值；正式PG必须返回带时区值。
    if connection.dialect.name == "sqlite" and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return aware_time(value)


def _lock_scopes(connection, scopes):
    if connection.dialect.name == "postgresql":
        for scope in sorted(set(scopes)):
            key = int.from_bytes(hashlib.sha256(("policy:" + scope[0] + ":" + scope[1]).encode()).digest()[:8], "big", signed=True)
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _lifecycle_states(connection, document_ids):
    """事件表必须已迁移；数据库错误不降级成“政策仍有效”。"""
    states = {identity: {"status": "active", "document_id": identity, "event_index": 0}
              for identity in document_ids}
    if not states:
        return states
    rows = connection.execute(select(policy_lifecycle_events).where(
        policy_lifecycle_events.c.document_id.in_(states)).order_by(
            policy_lifecycle_events.c.document_id, policy_lifecycle_events.c.event_index)).mappings()
    for row in rows:
        previous = states[row["document_id"]]
        if (row["event_index"] != previous["event_index"] + 1
                or previous["status"] == "corrected"
                or (previous["status"] == "withdrawn" and row["action"] != "corrected")):
            raise PolicyConflictError("政策生命周期事件不连续或状态冲突")
        states[row["document_id"]] = {**dict(row), "status": row["action"],
            "recorded_at": _db_time(row["recorded_at"], connection).isoformat()}
    return states


def _insert_document(connection, doc):
    fields = {key: doc[key] for key in ("document_id", "store_id", "category", "version", "title",
                                        "source_kind", "checksum", "source_path", "source_text")}
    connection.execute(insert(policy_documents).values(**fields,
        effective_from=aware_time(doc["effective_from"]), effective_before=aware_time(doc["effective_before"]),
        imported_at=datetime.now(timezone.utc)))
    connection.execute(insert(policy_chunks), [{**chunk, "document_id": doc["document_id"]} for chunk in doc["chunks"]])


def import_policy_files(engine, paths):
    """先解析整批，再一个事务写入；同ID同内容幂等，冲突从不覆盖旧版本。"""
    if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= 20:
        raise PolicyDocumentError("一次导入须为1至20个文件")
    documents = [parse_policy_file(path) for path in paths]
    imported, skipped = [], []
    try:
        with engine.begin() as connection:
            _lock_scopes(connection, [(d["store_id"], d["category"]) for d in documents])
            for doc in documents:
                existing = connection.execute(select(policy_documents).where(policy_documents.c.document_id == doc["document_id"])).mappings().first()
                if existing:
                    if existing["checksum"] != doc["checksum"] or existing["source_text"] != doc["source_text"]:
                        raise PolicyConflictError("同政策编号内容冲突，未覆盖")
                    # 幂等也验证既有条款未被损坏，不能用重复导入掩盖缺块。
                    if _bundle(connection, existing, aware_time(doc["effective_from"]))["status"] != "ok":
                        raise PolicyConflictError("已存政策或条款不完整，未静默修补")
                    skipped.append(doc["document_id"])
                    continue
                start, end = aware_time(doc["effective_from"]), aware_time(doc["effective_before"])
                matching = connection.execute(select(policy_documents).where(
                    policy_documents.c.store_id == doc["store_id"], policy_documents.c.category == doc["category"])).mappings().all()
                for row in matching:
                    if row["version"] == doc["version"] or start < _db_time(row["effective_before"], connection) and _db_time(row["effective_from"], connection) < end:
                        raise PolicyConflictError("同店同品类的版本标签冲突或适用时间重叠，整批未导入")
                _insert_document(connection, doc)
                imported.append(doc["document_id"])
    except IntegrityError:
        raise PolicyConflictError("数据库唯一性/关联约束拒绝，整批未导入") from None
    return {"status": "imported" if imported else "unchanged", "imported": imported, "unchanged": skipped,
            "embedding_requests": 0, "source_kind": "synthetic_demo"}


def _bundle(connection, row, paid_at):
    try:
        doc = parse_policy_text(row["source_text"], source_path=row["source_path"])
        fields = ("document_id", "store_id", "category", "version", "title", "source_kind", "checksum")
        if any(doc[key] != row[key] for key in fields):
            raise PolicyDocumentError("元数据与原文不一致")
        if any(aware_time(doc[key]) != _db_time(row[key], connection) for key in ("effective_from", "effective_before")):
            raise PolicyDocumentError("时间元数据与原文不一致")
        chunks = connection.execute(select(policy_chunks).where(policy_chunks.c.document_id == doc["document_id"])).mappings().all()
        by_id = {chunk["chunk_id"]: chunk for chunk in chunks}
        if len(by_id) != len(doc["chunks"]):
            raise PolicyDocumentError("分块数量不符")
        for expected in doc["chunks"]:
            actual = by_id.get(expected["chunk_id"], {})
            if any(actual.get(key) != value for key, value in expected.items()):
                raise PolicyDocumentError("分块原文/行号/指纹不符")
    except (PolicyDocumentError, TypeError, ValueError):
        return {"status": "invalid_policy", "reason": "stored_content_mismatch"}
    return {"status": "ok", "payment_version": doc["version"],
            "applicability": {"store_id": doc["store_id"], "category": doc["category"], "paid_at": paid_at.isoformat(),
                "effective_from": doc["effective_from"], "effective_before": doc["effective_before"], "selection_basis": "order_paid_at"},
            "document": {key: value for key, value in doc.items() if key not in {"rules", "rule_citations", "chunks"}},
            "rules": doc["rules"], "rule_citations": doc["rule_citations"], "chunks": doc["chunks"]}


def select_policy(engine, *, store_id, category, paid_at):
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value) for value in (store_id, category)):
        raise ValueError("可信店铺/品类标识无效")
    paid_at = aware_time(paid_at, "paid_at")
    applicability = {"store_id": store_id, "category": category, "paid_at": paid_at.isoformat(), "selection_basis": "order_paid_at"}
    with engine.connect() as connection:
        rows = connection.execute(select(policy_documents).where(policy_documents.c.store_id == store_id,
            policy_documents.c.category == category, policy_documents.c.effective_from <= paid_at,
            policy_documents.c.effective_before > paid_at)).mappings().all()
        if not rows:
            return {"status": "insufficient_evidence", "reason": "no_applicable_policy", "applicability": applicability,
                    "notice": "未找到支付时适用的受控政策，不使用其他版本补猜"}
        states = _lifecycle_states(connection, [row["document_id"] for row in rows])
        by_id = {row["document_id"]: row for row in rows}
        replacements = {identity: state["replacement_document_id"] for identity, state in states.items()
                        if state["status"] == "corrected"}
        roots = set(by_id) - set(replacements.values())
        if len(roots) != 1 or any(identity not in by_id for identity in replacements.values()):
            return {"status": "conflict", "reason": "overlapping_policy_versions", "applicability": applicability}
        identity = next(iter(roots)); chain = []
        while identity not in chain:
            chain.append(identity)
            if identity not in replacements:
                break
            identity = replacements[identity]
        if len(set(chain)) != len(rows) or identity in replacements:
            return {"status": "conflict", "reason": "invalid_policy_revision_chain", "applicability": applicability}
        first = by_id[chain[0]]
        if any(_db_time(row[key], connection) != _db_time(first[key], connection)
               for row in rows for key in ("effective_from", "effective_before")):
            return {"status": "conflict", "reason": "revision_period_mismatch", "applicability": applicability}
        if any(state.get("document_checksum", by_id[key]["checksum"]) != by_id[key]["checksum"]
               for key, state in states.items()):
            return {"status": "invalid_policy", "reason": "lifecycle_checksum_mismatch", "applicability": applicability}
        current = by_id[identity]
        state = states[identity]
        lifecycle = {"status": state["status"], "document_id": identity, "version": current["version"],
            "document_checksum": current["checksum"], "reason": state.get("reason"),
            "recorded_at": state.get("recorded_at"), "replacement_document_id": state.get("replacement_document_id"),
            "revision_chain": chain}
        if lifecycle["status"] == "withdrawn":
            return {"status": "withdrawn", "reason": "applicable_policy_withdrawn", "applicability": applicability,
                    "lifecycle": lifecycle, "notice": "该支付时段的政策依据已撤回，暂无可信更正版；不能据此判断售后，不回退其他版本。"}
        bundle = _bundle(connection, current, paid_at)
        return {**bundle, "lifecycle": lifecycle}


def _ready_embedding(row, source):
    if (row["embedding_model"] != MODEL or row["embedding_dimension"] != DIMENSION
            or row["embedding_text_checksum"] != row["checksum"] or row["embedding_source"] != source):
        return False
    try:
        validate_vector(row["embedding"])
        return True
    except EmbeddingError:
        return False


def index_policy_embeddings(engine, embedder, document_ids=None, *, allow_test_vectors=False):
    """显式准备小索引：请求在事务外，最终校对内容后整批写入，不让旧向量配新文本。"""
    source = getattr(embedder, "source", None)
    if source != "siliconflow_api" and not allow_test_vectors:
        raise ValueError("生产索引不接受离线替身向量")
    if document_ids is not None and (not isinstance(document_ids, (list, tuple)) or not 1 <= len(document_ids) <= 20):
        raise ValueError("明确文档列表须为1至20个")
    # 历史原文和旧向量保留供审计，但只为当前有效修订准备索引。
    statement = select(policy_chunks).where(~policy_chunks.c.document_id.in_(
        select(policy_lifecycle_events.c.document_id))).order_by(policy_chunks.c.chunk_id)
    if document_ids is not None:
        statement = statement.where(policy_chunks.c.document_id.in_(document_ids))
    with engine.connect() as connection:
        rows = connection.execute(statement.limit(33)).mappings().all()
        # 每次索引前重校原文和分块，不允许替被损坏的条款生成向量。
        doc_ids = {row["document_id"] for row in rows}
        for identity in doc_ids:
            doc = connection.execute(select(policy_documents).where(policy_documents.c.document_id == identity)).mappings().one()
            if _bundle(connection, doc, _db_time(doc["effective_from"], connection))["status"] != "ok":
                raise PolicyConflictError("原文/条款校验失败，未调用嵌入服务")
    if len(rows) > 32:
        raise ValueError("小索引一次最多32块，请显式缩小文档列表")
    if document_ids is not None and set(document_ids) != {row["document_id"] for row in rows}:
        raise ValueError("所选文档不存在、已撤回/替代或缺失完整分块")
    pending = [dict(row) for row in rows if not _ready_embedding(row, source)]
    if not pending:
        return {"status": "unchanged", "indexed_chunks": 0, "embedding_requests": 0, "source": source}
    with engine.connect() as connection:
        if any(state["status"] != "active" for state in _lifecycle_states(connection, doc_ids).values()):
            raise PolicyConflictError("索引调用前政策已撤回或替代，未调用嵌入服务")
    result = embedder.embed_texts([row["text"] for row in pending])
    if result.get("model") != MODEL or result.get("dimension") != DIMENSION or result.get("source") != source or len(result.get("vectors", [])) != len(pending):
        raise EmbeddingError("index_response_mismatch")
    vectors = [validate_vector(vector) for vector in result["vectors"]]
    with engine.begin() as connection:
        documents = connection.execute(select(policy_documents).where(policy_documents.c.document_id.in_(doc_ids))).mappings().all()
        _lock_scopes(connection, [(doc["store_id"], doc["category"]) for doc in documents])
        if any(state["status"] != "active" for state in _lifecycle_states(connection, doc_ids).values()):
            raise PolicyConflictError("嵌入请求期间政策被撤回或替代，整批索引未写入")
        for row, vector in zip(pending, vectors):
            # 乐观内容校验避免请求期间文档被改后仍写入旧向量；全部处于同一事务。
            changed = connection.execute(update(policy_chunks).where(policy_chunks.c.chunk_id == row["chunk_id"],
                policy_chunks.c.checksum == row["checksum"], policy_chunks.c.text == row["text"]).values(
                embedding=vector, embedding_model=MODEL, embedding_dimension=DIMENSION, embedding_source=source,
                embedding_text_checksum=row["checksum"], embedded_at=datetime.now(timezone.utc)))
            if changed.rowcount != 1:
                raise PolicyConflictError("嵌入请求期间条款变化，整批索引未写入")
    return {"status": "indexed", "indexed_chunks": len(pending), "embedding_requests": 1,
            "source": source, "model": MODEL, "dimension": DIMENSION,
            "stats": dict(getattr(embedder, "last_stats", result.get("stats", {})))}


def _terms(text_value):
    terms = set(re.findall(r"[a-z0-9_]+", text_value.lower()))
    for word in re.findall(r"[\u3400-\u9fff]+", text_value):
        terms.update(word[i:i + 2] for i in range(len(word) - 1))
        if len(word) == 1:
            terms.add(word)
    return terms


def _cosine(first, second):
    return sum(a * b for a, b in zip(first, second)) / math.sqrt(sum(x*x for x in first) * sum(x*x for x in second))


def _hybrid_order(chunks, fused_scores, cosine_scores):
    """融合分相同时复用语义相似度；两者仍相同才按编号保证结果稳定。

    不把余弦值加到融合分中、不修改主排序；不因条款编号字母序判相关性。
    """
    return sorted(chunks, key=lambda chunk: (-fused_scores[chunk["chunk_id"]],
        -cosine_scores[chunk["chunk_id"]], chunk["chunk_id"]))


def search_policy_chunks(engine, query, *, store_id, category, paid_at, mode="keyword", embedder=None, limit=4, allow_test_vectors=False):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 1000 or type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("问题须为1至1000字符，limit须为1至10")
    if mode not in {"keyword", "hybrid"}:
        raise ValueError("仅支持keyword或hybrid；默认没有重排序")
    started = time.monotonic()
    bundle = select_policy(engine, store_id=store_id, category=category, paid_at=paid_at)
    stats = {"candidate_count": 0, "embedding_requests": 0, "reranker": "not_enabled", "comparison_basis": "same_filtered_candidates"}
    if bundle["status"] != "ok":
        return {"status": bundle["status"], "reason": bundle.get("reason"), "results": [], "stats": stats,
                "lifecycle": bundle.get("lifecycle"), "notice": bundle.get("notice")}
    chunks = bundle["chunks"]
    candidate_ids = sorted(chunk["chunk_id"] for chunk in chunks)
    stats.update(candidate_count=len(chunks), candidate_ids=candidate_ids, filter_basis=bundle["applicability"],
                 candidate_sha256=hashlib.sha256("\n".join(candidate_ids).encode()).hexdigest())
    keyword_started = time.monotonic()
    terms = _terms(query)
    scores = {chunk["chunk_id"]: len(terms & _terms(chunk["text"])) / max(1, len(terms)) for chunk in chunks}
    lexical = sorted(chunks, key=lambda chunk: (-scores[chunk["chunk_id"]], chunk["chunk_id"]))
    stats["keyword_seconds"] = round(time.monotonic() - keyword_started, 6)
    def cite(chunk, score):
        return {**chunk, "citation_id": chunk["chunk_id"], "document_id": bundle["document"]["document_id"],
            "document_checksum": bundle["document"]["checksum"], "version": bundle["payment_version"],
            "source_path": bundle["document"]["source_path"],
            "store_id": store_id, "category": category, "source_kind": "synthetic_demo", "score": score}
    keyword_results = [cite(chunk, scores[chunk["chunk_id"]]) for chunk in lexical if scores[chunk["chunk_id"]] > 0]
    common = {"mode": mode, "payment_version": bundle["payment_version"], "applicability": bundle["applicability"],
              "keyword_results": keyword_results[:limit], "stats": stats}
    if mode == "keyword":
        stats["total_seconds"] = round(time.monotonic() - started, 6)
        return {**common, "status": "ok" if keyword_results else "insufficient_evidence", "results": keyword_results[:limit]}
    source = getattr(embedder, "source", None)
    if embedder is None or (source != "siliconflow_api" and not allow_test_vectors):
        return {**common, "status": "embedding_unavailable", "reason": "real_embedding_client_required", "results": []}
    with engine.connect() as connection:
        vectors = {row["chunk_id"]: row for row in connection.execute(select(policy_chunks).where(policy_chunks.c.chunk_id.in_(candidate_ids))).mappings()}
    if any(chunk["chunk_id"] not in vectors or not _ready_embedding(vectors[chunk["chunk_id"]], source)
           or vectors[chunk["chunk_id"]]["checksum"] != chunk["checksum"]
           or vectors[chunk["chunk_id"]]["text"] != chunk["text"] for chunk in chunks):
        return {**common, "status": "embedding_unavailable", "reason": "candidate_embeddings_missing_or_stale", "results": []}
    try:
        embedded = embedder.embed_texts([query])
        if embedded.get("model") != MODEL or embedded.get("source") != source or len(embedded.get("vectors", [])) != 1:
            raise EmbeddingError("query_response_mismatch")
        query_vector = validate_vector(embedded["vectors"][0])
    except EmbeddingError as error:
        stats.update(embedding_requests=1, embedding_error=error.code, embedding_stats=dict(getattr(embedder, "last_stats", {})))
        return {**common, "status": "embedding_failed", "reason": error.code, "results": []}
    # 外部嵌入等待期间可能完成撤回/更正；不能把旧候选当成新查询的有效依据。
    latest = select_policy(engine, store_id=store_id, category=category, paid_at=paid_at)
    if latest["status"] != "ok" or latest["document"]["checksum"] != bundle["document"]["checksum"]:
        stats["embedding_requests"] = 1
        return {"status": latest["status"] if latest["status"] != "ok" else "insufficient_evidence",
                "reason": latest.get("reason", "policy_revision_changed_during_retrieval"),
                "lifecycle": latest.get("lifecycle"), "notice": latest.get("notice"), "results": [], "stats": stats}
    vector_started = time.monotonic()
    cosine = {identity: _cosine(query_vector, validate_vector(row["embedding"])) for identity, row in vectors.items()}
    vector_order = sorted(chunks, key=lambda chunk: (-cosine[chunk["chunk_id"]], chunk["chunk_id"]))
    stats.update(embedding_requests=1, embedding_source=source, embedding_stats=dict(getattr(embedder, "last_stats", {})),
                 vector_seconds=round(time.monotonic() - vector_started, 6))
    fusion_started = time.monotonic()
    # Reciprocal Rank Fusion（倒数排名融合）避免直接相加不同量纲分数。
    keyword_rank = {row["chunk_id"]: i for i, row in enumerate(keyword_results, 1)}
    vector_rank = {row["chunk_id"]: i for i, row in enumerate(vector_order, 1) if cosine[row["chunk_id"]] > 0}
    fused = {identity: (1/(60+keyword_rank[identity]) if identity in keyword_rank else 0)
             + (1/(60+vector_rank[identity]) if identity in vector_rank else 0) for identity in candidate_ids}
    hybrid = _hybrid_order(chunks, fused, cosine)
    hybrid_results = [cite(chunk, fused[chunk["chunk_id"]]) for chunk in hybrid if fused[chunk["chunk_id"]] > 0]
    stats.update(hybrid_tie_break="cosine_then_chunk_id", hybrid_seconds=round(time.monotonic() - fusion_started, 6), total_seconds=round(time.monotonic() - started, 6))
    return {**common, "status": "ok" if hybrid_results else "insufficient_evidence", "results": hybrid_results[:limit],
            "vector_results": [cite(chunk, cosine[chunk["chunk_id"]]) for chunk in vector_order[:limit]],
            "score_details": [{"chunk_id": identity, "keyword": scores[identity], "cosine": cosine[identity], "hybrid_rrf": fused[identity]} for identity in candidate_ids]}
