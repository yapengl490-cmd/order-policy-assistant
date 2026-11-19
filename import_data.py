"""管理员离线导入规范化样本；普通查询、模型和网页入口不调用本模块。

旧 JSON 顶层 metadata/buyers/orders/refunds 保持兼容；可成组追加 shops/order_shops，另可独立追加 order_channels。
金额必须是整数分，时间
必须带时区；orders.status 只接受 paid，refunds.status 只接受 succeeded。
这些状态是输入契约，不代表本程序验证了外部支付平台的真实状态。
现有业务表以“已支付/成功退款”为含义，因此校验后不另写 status 列。

先由管理员显式执行 sql/003_import_batches.sql；本模块从不自动建表。
同编号同内容跳过，不同内容拒绝；整批及批次记录在同一事务内提交。
"""

import argparse
import hashlib
import json
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Engine, MetaData, Table, URL, create_engine, func, select
from provenance import provenance_table_available
from sqlalchemy.exc import SQLAlchemyError

MAX_ROWS = 1000
MAX_BYTES = 2 * 1024 * 1024
MAX_FEN = 2**63 - 1
BASE_KINDS = ("buyers", "orders", "refunds")
KINDS = (*BASE_KINDS, "shops", "order_shops", "order_channels")
KEYS = {"buyers": "buyer_id", "orders": "order_id", "refunds": "refund_id",
        "shops": "shop_id", "order_shops": "order_id", "order_channels": "order_id"}
FIELDS = {
    "buyers": {"buyer_id", "display_name"},
    "shops": {"shop_id", "name"},
    "order_shops": {"order_id", "shop_id"},
    "order_channels": {"order_id", "channel"},
    "orders": {"order_id", "buyer_id", "paid_at", "paid_amount_fen", "status"},
    "refunds": {"refund_id", "order_id", "refunded_at", "refund_amount_fen", "status"},
}


class ImportDataError(Exception):
    """可直接显示的脱敏错误；不包含数据库连接串或原始驱动异常。"""


class ImportValidationError(ImportDataError):
    pass


class ImportConflictError(ImportDataError):
    pass


class ImportExecutionError(ImportDataError):
    pass


@dataclass
class PreparedBatch:
    metadata: dict
    records: dict
    input_counts: dict
    sha256: str


def _fields(value, expected, location):
    if not isinstance(value, dict) or set(value) != expected:
        raise ImportValidationError(f"{location} 的字段必须为：{', '.join(sorted(expected))}")


def _text(value, maximum, location):
    if (not isinstance(value, str) or not value or value.strip() != value
            or len(value) > maximum or any(ord(c) < 32 for c in value)):
        raise ImportValidationError(f"{location} 必须是无首尾空白、无控制字符的 1—{maximum} 字符文本")
    return value


def _money(value, positive, location):
    if type(value) is not int or not (int(positive) <= value <= MAX_FEN):
        raise ImportValidationError(f"{location} 必须是{'正' if positive else '非负'}整数分，不能用布尔值或小数")
    return value


def _timestamp(value, location):
    if not isinstance(value, str) or len(value) > 64:
        raise ImportValidationError(f"{location} 必须是带时区的时间文本")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ImportValidationError(f"{location} 必须是有效的带时区时间") from None


def prepare_payload(payload) -> PreparedBatch:
    """仅检查内存中的整批输入，不访问数据库。哈希基于规范 JSON 编码。"""
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ImportValidationError("输入必须是合法 JSON 对象") from None
    if len(encoded) > MAX_BYTES:
        raise ImportValidationError("导入内容不能超过 2 MiB")
    if (not isinstance(payload, dict) or not {"metadata", *BASE_KINDS} <= set(payload)
            or not set(payload) <= {"metadata", *KINDS}
            or ("shops" in payload) != ("order_shops" in payload)):
        raise ImportValidationError("顶层须含metadata/buyers/orders/refunds；shops与order_shops须成组，可另含order_channels")
    kinds = tuple(kind for kind in KINDS if kind in payload)
    meta = payload["metadata"]
    _fields(meta, {"source", "version", "currency", "normalized", "data_kind"}, "metadata")
    _text(meta["source"], 200, "metadata.source")
    _text(meta["version"], 80, "metadata.version")
    if meta["currency"] != "CNY" or meta["normalized"] is not True or meta["data_kind"] != "normalized_sample":
        raise ImportValidationError("metadata 必须声明 CNY、normalized=true、data_kind=normalized_sample")
    if any(not isinstance(payload[kind], list) for kind in kinds):
        raise ImportValidationError("各记录组都必须是数组")
    counts = {kind: len(payload[kind]) for kind in kinds}
    if sum(counts.values()) > MAX_ROWS:
        raise ImportValidationError("一批各类记录合计不能超过 1000 行")
    records = {}
    for kind in kinds:
        unique = {}
        for index, item in enumerate(payload[kind]):
            location = f"{kind}[{index}]"
            _fields(item, FIELDS[kind], location)
            row = {}
            for field, value in item.items():
                if field.endswith("_id"):
                    row[field] = _text(value, 64, f"{location}.{field}")
                elif field in {"display_name", "name", "channel"}:
                    row[field] = _text(value, 100, f"{location}.{field}")
                elif field.endswith("_at"):
                    row[field] = _timestamp(value, f"{location}.{field}")
                elif field.endswith("_fen"):
                    row[field] = _money(value, kind == "refunds", f"{location}.{field}")
                elif value != {"orders": "paid", "refunds": "succeeded"}[kind]:
                    raise ImportValidationError(f"{location}.status 必须明确为 {'paid' if kind == 'orders' else 'succeeded'}")
            identity = row[KEYS[kind]]
            if identity in unique and unique[identity] != row:
                raise ImportConflictError(f"本批 {kind} 存在同编号不同内容：{identity}")
            unique[identity] = row
        records[kind] = unique
    return PreparedBatch(dict(meta), records, counts, hashlib.sha256(encoded).hexdigest())


def _load_rows(connection, table, key, identities):
    if not identities:
        return {}
    rows = connection.execute(select(table).where(table.c[key].in_(sorted(identities)))).mappings()
    return {row[key]: dict(row) for row in rows}


def _db_time(value, dialect):
    # SQLite 临时测试不保存时区；测试写入前已统一 UTC。正式 PostgreSQL 必须保留时区。
    if not isinstance(value, datetime):
        raise ImportConflictError("数据库时间类型与导入契约不一致")
    if value.tzinfo is None:
        if dialect != "sqlite":
            raise ImportConflictError("数据库时间缺少时区，已停止导入")
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _same_row(existing, incoming, dialect):
    return all((_db_time(existing[key], dialect) if key.endswith("_at") else existing[key]) == value
               for key, value in incoming.items())


def import_payload(engine: Engine, payload: dict) -> dict:
    """一次事务导入；不覆盖已有事实，也不连接任何外部业务平台。

    PostgreSQL 使用可串行化事务避免并发导入分别通过累计退款检查。
    并发冲突时整批失败，由管理员确认后重试；这里不自动无限重试。
    SQLite 分支仅用于独立临时测试，不代表已验证 PostgreSQL 的并发行为。
    """
    batch = prepare_payload(payload)
    kinds = tuple(batch.records)
    if engine.dialect.name not in {"postgresql", "sqlite"}:
        raise ImportValidationError("当前导入只支持 PostgreSQL，SQLite 仅用于临时测试")
    try:
        with engine.connect() as connection:
            if engine.dialect.name == "postgresql":
                connection = connection.execution_options(isolation_level="SERIALIZABLE")
            with connection.begin():
                metadata = MetaData()
                schema = connection.get_execution_options().get("schema_translate_map", {}).get("public", "public")
                tables = {name: Table(name, metadata, schema=schema, autoload_with=connection)
                          for name in (*kinds, "import_batches")}
                records = batch.records
                buyer_ids = set(records["buyers"]) | {x["buyer_id"] for x in records["orders"].values()}
                order_ids = (set(records["orders"]) | {x["order_id"] for x in records["refunds"].values()}
                             | set(records.get("order_shops", {})) | set(records.get("order_channels", {})))
                existing = {
                    "buyers": _load_rows(connection, tables["buyers"], "buyer_id", buyer_ids),
                    "orders": _load_rows(connection, tables["orders"], "order_id", order_ids),
                    "refunds": _load_rows(connection, tables["refunds"], "refund_id", set(records["refunds"])),
                }
                if "shops" in kinds:
                    shop_ids = set(records["shops"]) | {x["shop_id"] for x in records["order_shops"].values()}
                    existing["shops"] = _load_rows(connection, tables["shops"], "shop_id", shop_ids)
                    existing["order_shops"] = _load_rows(connection, tables["order_shops"], "order_id", set(records["order_shops"]))
                if "order_channels" in kinds:
                    existing["order_channels"] = _load_rows(connection, tables["order_channels"], "order_id", set(records["order_channels"]))
                additions = {kind: [] for kind in kinds}
                for kind in kinds:
                    for identity, row in records[kind].items():
                        old = existing[kind].get(identity)
                        if old is None:
                            additions[kind].append(row)
                        elif not _same_row(old, row, engine.dialect.name):
                            raise ImportConflictError(f"数据库已有同编号不同内容的 {kind}：{identity}；未覆盖")
                buyers = {**existing["buyers"], **records["buyers"]}
                orders = {**existing["orders"], **records["orders"]}
                for row in records["orders"].values():
                    if row["buyer_id"] not in buyers:
                        raise ImportValidationError(f"订单引用了不存在的买家：{row['order_id']}")
                for row in records["refunds"].values():
                    order = orders.get(row["order_id"])
                    if order is None:
                        raise ImportValidationError(f"退款引用了不存在的订单：{row['refund_id']}")
                    if row["refunded_at"] < _db_time(order["paid_at"], engine.dialect.name):
                        raise ImportValidationError(f"退款时间早于支付时间：{row['refund_id']}")
                if "shops" in kinds:
                    shops = {**existing["shops"], **records["shops"]}
                    for row in records["order_shops"].values():
                        if row["order_id"] not in orders or row["shop_id"] not in shops:
                            raise ImportValidationError(f"店铺归属引用不存在的订单或店铺：{row['order_id']}")
                for row in records.get("order_channels", {}).values():
                    if row["order_id"] not in orders:
                        raise ImportValidationError(f"渠道记录引用不存在的订单：{row['order_id']}")
                refunds = tables["refunds"]
                totals = dict(connection.execute(
                    select(refunds.c.order_id, func.sum(refunds.c.refund_amount_fen))
                    .where(refunds.c.order_id.in_(sorted(order_ids))).group_by(refunds.c.order_id)
                ).all()) if order_ids else {}
                for row in additions["refunds"]:
                    totals[row["order_id"]] = totals.get(row["order_id"], 0) + row["refund_amount_fen"]
                for identity in order_ids:
                    if totals.get(identity, 0) > orders[identity]["paid_amount_fen"]:
                        raise ImportValidationError(f"累计成功退款超过支付金额：{identity}")
                inserted = {kind: len(additions[kind]) for kind in kinds}
                skipped = {kind: batch.input_counts[kind] - inserted[kind] for kind in kinds}
                batches = tables["import_batches"]
                prior = connection.execute(select(batches.c.batch_sha256)
                                           .where(batches.c.batch_sha256 == batch.sha256)).first()
                if prior and any(inserted.values()):
                    raise ImportConflictError("批次已登记但部分事实记录缺失，需人工核查；未自动补写")
                if not prior:
                    for kind in kinds:
                        if additions[kind]:
                            connection.execute(tables[kind].insert(), additions[kind])
                    connection.execute(batches.insert().values(
                        batch_sha256=batch.sha256, source=batch.metadata["source"],
                        version=batch.metadata["version"], currency="CNY",
                        row_count=sum(batch.input_counts.values()), inserted_counts=inserted,
                        skipped_counts=skipped, imported_at=datetime.now(timezone.utc),
                    ))
                # 同内容的新批次也是该行的声明来源；不猜旧行属于哪个历史批次。
                lineage = "unavailable"
                if provenance_table_available(connection):
                    sources = Table("import_row_sources", metadata, schema=schema, autoload_with=connection)
                    existing_links = set(connection.execute(select(sources.c.record_kind, sources.c.record_id)
                        .where(sources.c.batch_sha256 == batch.sha256)).all())
                    links = [{"record_kind": kind, "record_id": identity, "batch_sha256": batch.sha256}
                             for kind in kinds for identity in records[kind]
                             if (kind, identity) not in existing_links]
                    if links:
                        connection.execute(sources.insert(), links)
                    lineage = "recorded"
                result = {"status": "already_imported" if prior else "imported", "provenance": lineage,
                          "batch_sha256": batch.sha256, "source": batch.metadata["source"],
                          "version": batch.metadata["version"], "row_count": sum(batch.input_counts.values()),
                          "inserted_counts": inserted, "skipped_counts": skipped}
            return result
    except ImportDataError:
        raise
    except SQLAlchemyError:
        raise ImportExecutionError("导入数据库操作失败，整批未提交；请检查初始化、权限或并发冲突后再处理") from None


def load_payload(path: Path) -> dict:
    """读取有大小上限的 UTF-8 JSON；重复字段名拒绝，避免后值悄悄覆盖前值。"""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ImportValidationError("JSON 含重复字段名，已拒绝导入")
            result[key] = value
        return result
    try:
        with path.open("rb") as reader:
            data = reader.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ImportValidationError("导入文件不能超过 2 MiB")
        return json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise ImportValidationError("无法读取有效的 UTF-8 JSON 文件") from None


def local_admin_engine(env_path: Path) -> Engine:
    """只读本机 .env 所需字段，不执行 shell、不展开变量、不接受其他数据库目标。"""
    wanted = {"DB_HOST", "DB_PORT", "DB_NAME", "POSTGRES_PASSWORD"}
    config = {}
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[7:]
            key, separator, value = line.partition("=")
            key = key.strip()
            if not separator or key not in wanted:
                continue
            value = value.strip()
            if value.startswith(("'", '"')):
                parts = shlex.split(value, comments=True, posix=True)
                if len(parts) != 1:
                    raise ValueError
                value = parts[0]
            else:
                value = value.partition(" #")[0].strip()
            if key in config:
                raise ValueError
            config[key] = value
    except (OSError, UnicodeError, ValueError):
        raise ImportValidationError("无法读取本机管理员配置；不要在聊天中提供密码") from None
    if (config.get("DB_HOST"), config.get("DB_PORT"), config.get("DB_NAME")) != (
            "127.0.0.1", "15432", "datus_core") or not config.get("POSTGRES_PASSWORD"):
        raise ImportValidationError("离线导入仅允许本项目 127.0.0.1:15432/datus_core 的管理员配置")
    return create_engine(URL.create("postgresql+psycopg", username="postgres",
                                   password=config["POSTGRES_PASSWORD"], host="127.0.0.1",
                                   port=15432, database="datus_core"),
                         connect_args={"connect_timeout": 5,
                                       "options": "-c timezone=UTC -c statement_timeout=10000"},
                         hide_parameters=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="管理员本机离线导入规范化样本；不会自动创建表")
    parser.add_argument("json_file", type=Path)
    parser.add_argument("--env-file", type=Path, default=Path(__file__).resolve().parent / ".env")
    args = parser.parse_args(argv)
    engine = None
    try:
        payload = load_payload(args.json_file)
        prepare_payload(payload)  # 先拒绝格式错误，避免为了无效输入连接管理员数据库。
        engine = local_admin_engine(args.env_file)
        result = import_payload(engine, payload)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ImportDataError as error:
        print(f"导入未完成：{error}")
        return 2 if isinstance(error, (ImportValidationError, ImportConflictError)) else 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
