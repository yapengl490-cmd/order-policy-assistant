"""独立临时 SQLite + public 附加库测试；不读取真实 .env，不连接正式 PostgreSQL。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import (BigInteger, Column, DateTime, ForeignKey, Integer, JSON,
                        MetaData, Table, Text, create_engine, event, func, select)

from import_data import (ImportConflictError, ImportExecutionError, ImportValidationError,
                         MAX_BYTES, import_payload, load_payload, local_admin_engine)

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "import_demo.json"


class ImportDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        public_path = str(self.directory / "public.sqlite3")

        @event.listens_for(self.engine, "connect")
        def attach_public(dbapi_connection, _record):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
            dbapi_connection.execute("ATTACH DATABASE ? AS public", (public_path,))

        metadata = MetaData(schema="public")
        self.buyers = Table("buyers", metadata,
                            Column("buyer_id", Text, primary_key=True),
                            Column("display_name", Text, nullable=False))
        self.orders = Table("orders", metadata,
                            Column("order_id", Text, primary_key=True),
                            Column("buyer_id", Text, ForeignKey("public.buyers.buyer_id"), nullable=False),
                            Column("paid_at", DateTime(timezone=True), nullable=False),
                            Column("paid_amount_fen", BigInteger, nullable=False))
        self.refunds = Table("refunds", metadata,
                             Column("refund_id", Text, primary_key=True),
                             Column("order_id", Text, ForeignKey("public.orders.order_id"), nullable=False),
                             Column("refunded_at", DateTime(timezone=True), nullable=False),
                             Column("refund_amount_fen", BigInteger, nullable=False))
        self.batches = Table("import_batches", metadata,
                             Column("batch_sha256", Text, primary_key=True),
                             Column("source", Text, nullable=False),
                             Column("version", Text, nullable=False),
                             Column("currency", Text, nullable=False),
                             Column("row_count", Integer, nullable=False),
                             Column("inserted_counts", JSON, nullable=False),
                             Column("skipped_counts", JSON, nullable=False),
                             Column("imported_at", DateTime(timezone=True), nullable=False))
        # 仅在测试临时库建表；被测导入模块从不执行建表。
        metadata.create_all(self.engine)
        self.payload = json.loads(SAMPLE.read_text(encoding="utf-8"))

    def snapshot(self):
        with self.engine.connect() as connection:
            return tuple(tuple(tuple(row) for row in connection.execute(select(table)
                         .order_by(next(iter(table.primary_key.columns)))))
                         for table in (self.buyers, self.orders, self.refunds, self.batches))

    def assert_rejected_unchanged(self, payload, exception=ImportValidationError):
        before = self.snapshot()
        with self.assertRaises(exception):
            import_payload(self.engine, payload)
        self.assertEqual(before, self.snapshot())

    def test_success_and_same_batch_idempotent(self):
        first = import_payload(self.engine, self.payload)
        self.assertEqual(first["status"], "imported")
        self.assertEqual(first["inserted_counts"], {"buyers": 1, "orders": 1, "refunds": 2})
        self.assertEqual(first["skipped_counts"], dict.fromkeys(("buyers", "orders", "refunds"), 0))
        before = self.snapshot()
        repeated = import_payload(self.engine, self.payload)
        self.assertEqual(repeated["status"], "already_imported")
        self.assertEqual(repeated["skipped_counts"], {"buyers": 1, "orders": 1, "refunds": 2})
        self.assertEqual(before, self.snapshot())
        with self.engine.connect() as connection:
            row = connection.execute(select(self.batches)).mappings().one()
            self.assertEqual(row["source"], self.payload["metadata"]["source"])
            self.assertEqual(row["version"], self.payload["metadata"]["version"])
            self.assertEqual(row["row_count"], 4)
            self.assertEqual(row["inserted_counts"], first["inserted_counts"])

    def test_equivalent_timezones_and_new_provenance_skip_same_facts(self):
        import_payload(self.engine, self.payload)
        changed = copy.deepcopy(self.payload)
        changed["metadata"]["version"] = "v2"
        changed["orders"][0]["paid_at"] = "2026-11-01T02:00:00Z"
        result = import_payload(self.engine, changed)
        self.assertEqual(result["inserted_counts"], {"buyers": 0, "orders": 0, "refunds": 0})
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(select(func.count()).select_from(self.batches)), 2)

    def test_identical_duplicate_inside_payload_counts_as_skipped(self):
        self.payload["refunds"].append(copy.deepcopy(self.payload["refunds"][0]))
        result = import_payload(self.engine, self.payload)
        self.assertEqual(result["row_count"], 5)
        self.assertEqual(result["inserted_counts"]["refunds"], 2)
        self.assertEqual(result["skipped_counts"]["refunds"], 1)

    def test_conflicting_duplicate_inside_payload_rejected(self):
        other = copy.deepcopy(self.payload["refunds"][0])
        other["refund_amount_fen"] = 1
        self.payload["refunds"].append(other)
        self.assert_rejected_unchanged(self.payload, ImportConflictError)

    def test_existing_id_conflicts_never_overwrite(self):
        import_payload(self.engine, self.payload)
        for kind, field, new_value in (("buyers", "display_name", "另一名称"),
                                       ("orders", "paid_amount_fen", 11000),
                                       ("refunds", "refund_amount_fen", 2100)):
            with self.subTest(kind=kind):
                changed = copy.deepcopy(self.payload)
                changed[kind][0][field] = new_value
                self.assert_rejected_unchanged(changed, ImportConflictError)

    def test_status_required_and_not_inferred(self):
        for kind in ("orders", "refunds"):
            for value in (None, "pending", "cancelled", True):
                with self.subTest(kind=kind, value=value):
                    changed = copy.deepcopy(self.payload)
                    if value is None:
                        del changed[kind][0]["status"]
                    else:
                        changed[kind][0]["status"] = value
                    self.assert_rejected_unchanged(changed)

    def test_integer_money_not_bool_decimal_negative_or_overflow(self):
        for value in (True, 10.5, "100", -1, 2**63):
            with self.subTest(value=value):
                changed = copy.deepcopy(self.payload)
                changed["orders"][0]["paid_amount_fen"] = value
                self.assert_rejected_unchanged(changed)
        self.payload["refunds"][0]["refund_amount_fen"] = 0
        self.assert_rejected_unchanged(self.payload)

    def test_naive_or_invalid_timestamp_rejected(self):
        for value in ("2026-11-01T10:00:00", "not-a-time", 1):
            with self.subTest(value=value):
                changed = copy.deepcopy(self.payload)
                changed["orders"][0]["paid_at"] = value
                self.assert_rejected_unchanged(changed)

    def test_ids_and_unexpected_fields_rejected(self):
        for identity in ("", "B" * 65, " B1", 7, "B\n1"):
            changed = copy.deepcopy(self.payload)
            changed["buyers"][0]["buyer_id"] = identity
            self.assert_rejected_unchanged(changed)
        self.payload["orders"][0]["ignored_field"] = "not allowed"
        self.assert_rejected_unchanged(self.payload)

    def test_metadata_must_declare_currency_and_normalized_sample(self):
        for key, value in (("currency", "USD"), ("normalized", 1),
                           ("data_kind", "production"), ("version", "")):
            changed = copy.deepcopy(self.payload)
            changed["metadata"][key] = value
            self.assert_rejected_unchanged(changed)

    def test_missing_buyer_or_order_rejected(self):
        changed = copy.deepcopy(self.payload)
        changed["orders"][0]["buyer_id"] = "B_MISSING"
        self.assert_rejected_unchanged(changed)
        self.payload["refunds"][0]["order_id"] = "O_MISSING"
        self.assert_rejected_unchanged(self.payload)

    def test_refund_before_payment_rejected(self):
        self.payload["refunds"][0]["refunded_at"] = "2026-10-31T10:00:00+08:00"
        self.assert_rejected_unchanged(self.payload)

    def test_cumulative_refunds_include_existing_database_rows(self):
        import_payload(self.engine, self.payload)
        changed = copy.deepcopy(self.payload)
        changed["metadata"]["version"] = "exceeds-total"
        changed["buyers"] = []
        changed["orders"] = []
        changed["refunds"] = [dict(self.payload["refunds"][0], refund_id="R_EXTRA", refund_amount_fen=7001)]
        self.assert_rejected_unchanged(changed)
        changed["refunds"][0]["refund_amount_fen"] = 7000
        result = import_payload(self.engine, changed)
        self.assertEqual(result["inserted_counts"], {"buyers": 0, "orders": 0, "refunds": 1})

    def test_total_rows_limit_counts_duplicates_too(self):
        self.payload["buyers"] = [self.payload["buyers"][0]] * 1001
        self.assert_rejected_unchanged(self.payload)

    def test_payload_and_file_byte_limits(self):
        changed = copy.deepcopy(self.payload)
        changed["metadata"]["source"] = "a" * (MAX_BYTES + 1)
        self.assert_rejected_unchanged(changed)
        path = self.directory / "oversized.json"
        path.write_bytes(b" " * (MAX_BYTES + 1))
        with self.assertRaises(ImportValidationError):
            load_payload(path)

    def test_database_error_rolls_back_earlier_inserts_and_batch_log(self):
        # 受控数据库故障发生在买家和订单已插入之后，验证真实事务回滚而非仅输入预检。
        with self.engine.begin() as connection:
            connection.exec_driver_sql("""CREATE TRIGGER public.reject_refund BEFORE INSERT ON refunds
                BEGIN SELECT RAISE(ABORT, 'controlled test failure'); END""")
        self.assert_rejected_unchanged(self.payload, ImportExecutionError)

    def test_existing_batch_missing_fact_is_not_falsely_reported_success(self):
        import_payload(self.engine, self.payload)
        with self.engine.begin() as connection:
            connection.execute(self.refunds.delete().where(self.refunds.c.refund_id == "R_IMPORT_DEMO_1"))
        self.assert_rejected_unchanged(self.payload, ImportConflictError)

    def test_missing_batch_table_is_not_created_automatically(self):
        with self.engine.begin() as connection:
            self.batches.drop(connection)
        with self.assertRaises(ImportExecutionError):
            import_payload(self.engine, self.payload)
        with self.engine.connect() as connection:
            self.assertEqual(connection.scalar(select(func.count()).select_from(self.orders)), 0)
            self.assertEqual(connection.scalar(select(func.count()).select_from(self.buyers)), 0)

    def test_json_duplicate_fields_are_rejected(self):
        path = self.directory / "duplicate.json"
        path.write_text('{"metadata":{},"metadata":{}}', encoding="utf-8")
        with self.assertRaisesRegex(ImportValidationError, "重复字段"):
            load_payload(path)

    def test_cli_target_guard_without_connecting(self):
        path = self.directory / "test.env"
        base = "DB_HOST=127.0.0.1\nDB_PORT=15432\nDB_NAME=datus_core\nPOSTGRES_PASSWORD='fake-local-test'\n"
        path.write_text(base, encoding="utf-8")
        engine = local_admin_engine(path)
        self.assertEqual(engine.url.database, "datus_core")
        self.assertEqual(engine.url.username, "postgres")
        engine.dispose()  # 创建 Engine 不建立连接。
        path.write_text(base.replace("15432", "5432"), encoding="utf-8")
        with self.assertRaises(ImportValidationError):
            local_admin_engine(path)


if __name__ == "__main__":
    unittest.main()
