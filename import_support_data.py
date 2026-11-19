"""管理员显式导入合成售后补充事实；不写订单金额、不创建账号、不自动迁移。

先用既有import_data导入规范化订单与退款，再执行本模块。两步不是同一事务；
本步整批事务、同编号同内容跳过/冲突拒绝，失败后保留第一步并可安全重试。
"""
import argparse
from datetime import timezone
import hashlib
import json
from pathlib import Path

from sqlalchemy import MetaData, Table, select
from sqlalchemy.exc import SQLAlchemyError

from import_data import (ImportDataError, ImportConflictError, ImportExecutionError,
                         ImportValidationError, _text, _timestamp, _db_time,
                         load_payload, local_admin_engine)

KINDS = {"order_support_facts": "order_id", "refund_reason_records": "evidence_id"}
FIELDS = {
    "order_support_facts": {"order_id", "product_category", "delivered_at", "is_custom", "source", "recorded_at"},
    "refund_reason_records": {"evidence_id", "refund_id", "reason_code", "verification_status", "recorded_at", "source", "note"},
}
REASONS = {"size", "preference", "merchant_responsibility", "other", "unknown"}


def prepare_support_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {"metadata", *KINDS}:
        raise ImportValidationError("售后导入需要metadata/order_support_facts/refund_reason_records")
    meta = payload["metadata"]
    if not isinstance(meta, dict) or set(meta) != {"source", "version", "data_kind"} or meta["data_kind"] != "synthetic_demo":
        raise ImportValidationError("本入口仅接收明确标注synthetic_demo的独立合成资料")
    _text(meta["source"], 500, "来源"); _text(meta["version"], 80, "版本")
    try:
        data = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError):
        raise ImportValidationError("售后输入必须为合法JSON") from None
    if len(data) > 2*1024*1024 or any(not isinstance(payload[k], list) for k in KINDS):
        raise ImportValidationError("售后资料过大或记录组不是数组")
    if sum(len(payload[k]) for k in KINDS) > 1000:
        raise ImportValidationError("单批售后资料最多1000行")
    records = {}
    for kind, key in KINDS.items():
        rows = {}
        for item in payload[kind]:
            if not isinstance(item, dict) or set(item) != FIELDS[kind]:
                raise ImportValidationError(f"{kind}字段不符合约定")
            row = dict(item)
            for field in (key, "refund_id"):
                if field in row: _text(row[field], 128, field)
            _text(row["source"], 500, "记录来源")
            row["recorded_at"] = _timestamp(row["recorded_at"], "recorded_at")
            row.update(source_kind="synthetic_demo", source_version=meta["version"])
            if kind == "order_support_facts":
                if row["product_category"] is not None: _text(row["product_category"], 80, "商品类别")
                if row["is_custom"] is not None and type(row["is_custom"]) is not bool:
                    raise ImportValidationError("is_custom须为布尔或null")
                if row["delivered_at"] is not None:
                    row["delivered_at"] = _timestamp(row["delivered_at"], "delivered_at")
                    if row["recorded_at"] < row["delivered_at"]:
                        raise ImportValidationError("记录时间不能早于已记录收货时间")
            else:
                if (not isinstance(row["reason_code"], str) or row["reason_code"] not in REASONS
                        or not isinstance(row["verification_status"], str)
                        or row["verification_status"] not in {"verified", "buyer_reported", "unverified"}):
                    raise ImportValidationError("原因或核实状态不符合有限约定")
                if not isinstance(row["note"], str) or len(row["note"]) > 1000 or any(ord(c) < 32 for c in row["note"]):
                    raise ImportValidationError("原因说明须为不含控制字符的短文本")
            if row[key] in rows:
                raise ImportValidationError("售后资料含重复编号")
            rows[row[key]] = row
        records[kind] = rows
    return records, hashlib.sha256(data).hexdigest()


def import_support_payload(engine, payload, *, allowed_shop_id):
    """管理员限定目标店铺；所有引用先核真实订单归属，整批不覆盖旧资料。"""
    _text(allowed_shop_id, 128, "本次获准演示店铺")
    records, digest = prepare_support_payload(payload)
    if engine.dialect.name not in {"postgresql", "sqlite"}:
        raise ImportValidationError("仅支持PostgreSQL；SQLite仅供独立测试")
    try:
        with engine.connect() as connection:
            if engine.dialect.name == "postgresql":
                connection = connection.execution_options(isolation_level="SERIALIZABLE")
            with connection.begin():
                schema = connection.get_execution_options().get("schema_translate_map", {}).get("public", "public")
                meta = MetaData()
                tables = {name: Table(name, meta, schema=schema, autoload_with=connection)
                          for name in (*KINDS, "orders", "refunds", "order_shops")}
                orders, refunds, shops = (tables[k] for k in ("orders", "refunds", "order_shops"))
                refund_ids = {row["refund_id"] for row in records["refund_reason_records"].values()}
                refund_rows = {r["refund_id"]: r for r in connection.execute(select(refunds).where(refunds.c.refund_id.in_(refund_ids))).mappings()} if refund_ids else {}
                if set(refund_rows) != refund_ids:
                    raise ImportValidationError("原因记录引用未找到的成功退款，整批拒绝")
                order_ids = set(records["order_support_facts"]) | {r["order_id"] for r in refund_rows.values()}
                owned = {r["order_id"]: r for r in connection.execute(select(orders)
                    .join(shops, shops.c.order_id == orders.c.order_id)
                    .where(orders.c.order_id.in_(order_ids), shops.c.shop_id == allowed_shop_id)).mappings()} if order_ids else {}
                if set(owned) != order_ids:
                    raise ImportValidationError("存在不属于本次获准店铺、缺归属或不存在的订单，整批拒绝")
                for identity, row in records["order_support_facts"].items():
                    paid = _db_time(owned[identity]["paid_at"], engine.dialect.name)
                    if row["recorded_at"] < paid or (row["delivered_at"] is not None and row["delivered_at"] < paid):
                        raise ImportValidationError("补充记录或收货时间早于支付时间")
                for row in records["refund_reason_records"].values():
                    if row["recorded_at"] < _db_time(refund_rows[row["refund_id"]]["refunded_at"], engine.dialect.name):
                        raise ImportValidationError("本入口的退款后核对记录不能早于退款时间")
                additions, skipped = {}, {}
                for kind, key in KINDS.items():
                    old = {r[key]: r for r in connection.execute(select(tables[kind]).where(tables[kind].c[key].in_(records[kind]))).mappings()} if records[kind] else {}
                    additions[kind] = []
                    for identity, row in records[kind].items():
                        if identity not in old:
                            additions[kind].append(row)
                            continue
                        for field, value in row.items():
                            prior = old[identity][field]
                            if field.endswith("_at") and prior is not None:
                                prior = _db_time(prior, engine.dialect.name)
                            if prior != value:
                                raise ImportConflictError("同编号售后资料内容冲突，整批拒绝且不覆盖")
                    skipped[kind] = len(old)
                for kind, rows in additions.items():
                    if rows: connection.execute(tables[kind].insert(), rows)
                counts = {kind: len(rows) for kind, rows in additions.items()}
                return {"status": "imported" if any(counts.values()) else "already_imported",
                        "shop_id": allowed_shop_id, "batch_sha256": digest, "inserted_counts": counts,
                        "skipped_counts": skipped, "source_proof": "declared_only", "data_kind": "synthetic_demo"}
    except ImportDataError:
        raise
    except SQLAlchemyError:
        raise ImportExecutionError("售后资料数据库操作失败，整批未提交；请核对迁移、权限或并发冲突") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="管理员合成售后资料导入；默认只核对文件，不接触数据库")
    parser.add_argument("json_file", type=Path)
    parser.add_argument("--shop-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--env-file", type=Path, default=Path(__file__).resolve().parent/".env")
    args = parser.parse_args(argv)
    engine = None
    try:
        payload = load_payload(args.json_file)
        rows, digest = prepare_support_payload(payload)
        _text(args.shop_id, 128, "目标店铺")
        if not args.apply:
            print(json.dumps({"status": "file_validated_only", "shop_id": args.shop_id,
                "counts": {k: len(v) for k,v in rows.items()}, "batch_sha256": digest,
                "database_accessed": False, "ownership_not_yet_checked": True}, ensure_ascii=False))
            return 0
        engine = local_admin_engine(args.env_file)
        print(json.dumps(import_support_payload(engine, payload, allowed_shop_id=args.shop_id), ensure_ascii=False))
        return 0
    except ImportDataError as exc:
        print(f"售后导入未完成：{exc}")
        return 1
    finally:
        if engine is not None: engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
