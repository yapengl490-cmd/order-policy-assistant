"""只读来源查询：区分当前金额快照与稍后读取的导入来源元数据。

旧记录没有登记来源时明确 unknown，不用现有样本编号猜来源。来源表尚未
迁移或不可读时只标不可用，不将正确的金额变成零或虚构一个批次。
"""
from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, MetaData, Table, Text, and_, inspect, or_, select
from sqlalchemy.exc import SQLAlchemyError

metadata = MetaData(schema="public")
row_sources = Table("import_row_sources", metadata,
    Column("record_kind", Text), Column("record_id", Text), Column("batch_sha256", Text))
batches = Table("import_batches", metadata,
    Column("batch_sha256", Text), Column("source", Text), Column("version", Text),
    Column("imported_at", DateTime(timezone=True)))
MAX_RECORDS = 200
MAX_LINKS = 400


def provenance_table_available(connection):
    """测试模式的 schema 映射与正式 public 使用同一逻辑。"""
    schema = connection.get_execution_options().get("schema_translate_map", {}).get("public", "public")
    return inspect(connection).has_table("import_row_sources", schema=schema)


def read_provenance(session, references):
    references = sorted(set(references))
    selected = references[:MAX_RECORDS]
    result = {"status": "unknown", "observed_at": datetime.now(timezone.utc).isoformat(),
              "basis": "import_metadata_read_after_business_query",
              "notice": "来源为导入时的声明，不证明外部平台状态；未登记历史行显示unknown。",
              "requested_records": len(references), "returned_records": len(selected),
              "truncated": len(references) > len(selected), "records": [], "batches": []}
    if not selected:
        result["status"] = "not_applicable"
        return result
    try:
        connection = session.connection()
        if not provenance_table_available(connection):
            result.update(status="unavailable", reason="provenance_not_migrated")
            return result
        predicates = []
        for kind in sorted({kind for kind, _ in selected}):
            predicates.append(and_(row_sources.c.record_kind == kind,
                row_sources.c.record_id.in_([identity for k, identity in selected if k == kind])))
        statement = (select(row_sources.c.record_kind, row_sources.c.record_id,
            batches.c.batch_sha256, batches.c.source, batches.c.version, batches.c.imported_at)
            .join(batches, batches.c.batch_sha256 == row_sources.c.batch_sha256)
            .where(or_(*predicates))
            .order_by(row_sources.c.record_kind, row_sources.c.record_id, batches.c.batch_sha256)
            .limit(MAX_LINKS + 1))
        rows = connection.execute(statement).mappings().all()
        result["source_links_truncated"] = len(rows) > MAX_LINKS
        by_record = {ref: [] for ref in selected}; by_batch = {}
        for row in rows[:MAX_LINKS]:
            by_record[(row["record_kind"], row["record_id"])].append(row["batch_sha256"])
            imported_at = row["imported_at"]
            by_batch[row["batch_sha256"]] = {"batch_sha256": row["batch_sha256"],
                "source": row["source"], "version": row["version"],
                "imported_at": imported_at.isoformat() if imported_at else None}
        result["records"] = [{"kind": kind, "record_id": identity,
            "status": "recorded" if values else ("not_shown" if result["source_links_truncated"] else "unknown"),
            "batch_sha256": values} for (kind, identity), values in by_record.items()]
        result["batches"] = list(by_batch.values())
        known = sum(bool(values) for values in by_record.values())
        result["status"] = ("recorded" if known == len(selected) and not result["truncated"]
                            and not result["source_links_truncated"] else "partial" if known else "unknown")
        return result
    except SQLAlchemyError:
        # 不能把不能读取来源误报为「确认没有来源」。
        result.update(status="unavailable", reason="provenance_read_failed")
        return result
