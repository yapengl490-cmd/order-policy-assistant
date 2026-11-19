"""助手回归：默认独立内存库；TEST_DATABASE_URL 可启用真实 PostgreSQL 临时模式。

真实数据库测试只创建/删除随机命名的 refund_test_* 模式，不修改 public 业务表。
SQLite 测试将已知 UTC 样本的时区补回，用于逻辑回归，不能代替 PostgreSQL 验证。
"""

import json
import os
import unittest
import uuid
from datetime import datetime, timezone

from sqlalchemy import create_engine, delete, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from models import Base, Order, Refund
from refund_service import check_order, list_orders, summary_orders


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


class RefundServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        target = os.environ.get("TEST_DATABASE_URL")
        cls.schema = "refund_test_" + uuid.uuid4().hex
        if target:
            cls.base_engine = create_engine(target, hide_parameters=True)
            if cls.base_engine.dialect.name != "postgresql":
                raise RuntimeError("TEST_DATABASE_URL只接受PostgreSQL测试连接")
            with cls.base_engine.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
            cls.engine = cls.base_engine.execution_options(schema_translate_map={"public": cls.schema})
        else:
            cls.base_engine = create_engine("sqlite://")
            event.listen(cls.base_engine, "connect", lambda conn, _: conn.execute("ATTACH DATABASE ':memory:' AS public"))
            cls.engine = cls.base_engine
        Base.metadata.create_all(cls.engine)

    @classmethod
    def tearDownClass(cls):
        if cls.engine.dialect.name == "postgresql":
            with cls.base_engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.base_engine.dispose()

    def setUp(self):
        with Session(self.engine) as session, session.begin():
            session.execute(delete(Refund))
            session.execute(delete(Order))
            session.add(Order(order_id="O001", buyer_id="B001", paid_at=utc("2026-10-15T02:00:00"), paid_amount_fen=12500))
            session.add_all([
                Refund(refund_id="R001", order_id="O001", refunded_at=utc("2026-10-20T01:00:00"), refund_amount_fen=2500),
                Refund(refund_id="R002", order_id="O001", refunded_at=utc("2026-11-02T07:00:00"), refund_amount_fen=1500),
            ])
        self.cutoff = utc("2026-11-03T00:00:00")

    def add_order(self, order_id="O002", buyer_id="B001", amount=12500, paid_at=None):
        with Session(self.engine) as session, session.begin():
            session.add(Order(order_id=order_id, buyer_id=buyer_id,
                              paid_at=paid_at or utc("2026-10-16T00:00:00"), paid_amount_fen=amount))

    def add_refund(self, refund_id, order_id, amount, refunded_at=None):
        with Session(self.engine) as session, session.begin():
            session.add(Refund(refund_id=refund_id, order_id=order_id,
                               refunded_at=refunded_at or utc("2026-10-20T02:00:00"), refund_amount_fen=amount))

    def test_two_refunds_and_cross_month(self):
        result = check_order(self.engine, "O001", "B001", self.cutoff)
        self.assertEqual((result["paid_amount_fen"], result["refund_amount_fen"], result["net_amount_fen"]), (12500, 4000, 8500))
        self.assertEqual([row["refund_id"] for row in result["refunds"]], ["R001", "R002"])
        json.dumps(result)

    def test_cutoff_change(self):
        result = check_order(self.engine, "O001", "B001", utc("2026-11-01T00:00:00"))
        self.assertEqual((result["refund_amount_fen"], result["net_amount_fen"]), (2500, 10000))

    def test_exact_refund_cutoff_excluded(self):
        result = check_order(self.engine, "O001", "B001", utc("2026-10-20T01:00:00"))
        self.assertEqual(result["refund_amount_fen"], 0)
        self.assertEqual(result["refunds"], [])

    def test_no_refunds_does_not_hide_order(self):
        self.add_order()
        result = check_order(self.engine, "O002", "B001", self.cutoff)
        self.assertEqual((result["status"], result["refund_amount_fen"], result["net_amount_fen"]), ("ok", 0, 12500))

    def test_missing_and_foreign_same_status(self):
        missing = check_order(self.engine, "O404", "B001", self.cutoff)
        foreign = check_order(self.engine, "O001", "B002", self.cutoff)
        self.assertEqual(missing["status"], "not_found")
        self.assertEqual(missing["message"], foreign["message"])
        self.assertNotIn("buyer_id", foreign)
        self.assertNotIn("paid_amount_fen", foreign)

    def test_cutoff_at_or_before_payment(self):
        for cutoff in (utc("2026-10-15T02:00:00"), utc("2026-10-01T00:00:00")):
            result = check_order(self.engine, "O001", "B001", cutoff)
            self.assertEqual(result["status"], "not_yet_paid")
            self.assertNotIn("net_amount_fen", result)

    def test_empty_or_malformed_identifier_rejected(self):
        for invalid in ("", "  ", "O001\n", None, "a" * 129):
            with self.assertRaises(ValueError):
                check_order(self.engine, invalid, "B001", self.cutoff)
        with self.assertRaises(ValueError):
            check_order(self.engine, "O001", "", self.cutoff)

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValueError):
            check_order(self.engine, "O001", "B001", datetime(2026, 11, 3))

    def test_timezone_equivalence(self):
        a = check_order(self.engine, "O001", "B001", datetime.fromisoformat("2026-11-03T00:00:00+08:00"))
        b = check_order(self.engine, "O001", "B001", utc("2026-11-02T16:00:00"))
        # 两次请求的来源观察时间不同；时区等价应比较业务口径与其余来源内容。
        a["provenance"].pop("observed_at")
        b["provenance"].pop("observed_at")
        self.assertEqual(a, b)

    def test_negative_payment_rejected(self):
        self.add_order(amount=-1)
        result = check_order(self.engine, "O002", "B001", self.cutoff)
        self.assertEqual(result["status"], "data_error")
        self.assertNotIn("net_amount_fen", result)

    def test_invalid_refund_values_rejected(self):
        self.add_refund("BAD", "O001", 0)
        self.assertEqual(check_order(self.engine, "O001", "B001", self.cutoff)["status"], "data_error")

    def test_refund_before_payment_rejected(self):
        self.add_refund("BAD", "O001", 10, utc("2026-10-01T00:00:00"))
        self.assertEqual(check_order(self.engine, "O001", "B001", self.cutoff)["status"], "data_error")

    def test_over_refund_rejected(self):
        self.add_refund("BAD", "O001", 10000)
        result = check_order(self.engine, "O001", "B001", self.cutoff)
        self.assertEqual(result["status"], "data_error")
        self.assertNotIn("net_amount_fen", result)

    def test_more_than_200_refunds_never_partial_total(self):
        with Session(self.engine) as session, session.begin():
            session.add_all([Refund(refund_id=f"M{i}", order_id="O001", refunded_at=utc("2026-10-21T00:00:00"), refund_amount_fen=1) for i in range(199)])
        result = check_order(self.engine, "O001", "B001", self.cutoff)
        self.assertEqual(result["status"], "too_many_refunds")
        self.assertNotIn("net_amount_fen", result)

    def test_order_listing_scope_limit_and_order(self):
        self.add_order()
        self.add_order("O003", "B002")
        result = list_orders(self.engine, "B001", 1)
        self.assertEqual([order["order_id"] for order in result["orders"]], ["O002"])
        self.assertTrue(result["has_more"])
        self.assertEqual(len(list_orders(self.engine, "B001", 10)["orders"]), 2)
        for invalid in (0, 51, True, "1"):
            with self.assertRaises(ValueError):
                list_orders(self.engine, "B001", invalid)

    def test_summary_same_price_distinct_orders_and_other_buyer(self):
        self.add_order()
        self.add_order("O003", "B002")
        result = summary_orders(self.engine, "B001", utc("2026-10-01T00:00:00"), utc("2026-11-01T00:00:00"), self.cutoff)
        self.assertEqual((result["order_count"], result["paid_amount_fen"], result["refund_amount_fen"], result["net_amount_fen"]), (2, 25000, 4000, 21000))

    def test_summary_payment_boundaries(self):
        self.add_order("AT_START", paid_at=utc("2026-10-01T00:00:00"))
        self.add_order("AT_END", paid_at=utc("2026-11-01T00:00:00"))
        result = summary_orders(self.engine, "B001", utc("2026-10-01T00:00:00"), utc("2026-11-01T00:00:00"), self.cutoff)
        self.assertEqual([order["order_id"] for order in result["orders"]], ["AT_START", "O001"])

    def test_summary_no_orders_is_valid_zero(self):
        result = summary_orders(self.engine, "B404", utc("2026-10-01T00:00:00"), utc("2026-11-01T00:00:00"), self.cutoff)
        self.assertEqual((result["status"], result["order_count"], result["net_amount_fen"]), ("ok", 0, 0))

    def test_summary_rejects_invalid_ranges(self):
        with self.assertRaises(ValueError):
            summary_orders(self.engine, "B001", self.cutoff, self.cutoff, self.cutoff)
        with self.assertRaises(ValueError):
            summary_orders(self.engine, "B001", utc("2026-10-01T00:00:00"), self.cutoff, utc("2026-11-01T00:00:00"))

    def test_summary_data_error_not_partial_success(self):
        self.add_order(amount=-1)
        result = summary_orders(self.engine, "B001", utc("2026-10-01T00:00:00"), utc("2026-11-01T00:00:00"), self.cutoff)
        self.assertEqual(result["status"], "data_error")
        self.assertNotIn("net_amount_fen", result)

    def test_query_error_not_not_found(self):
        broken = create_engine("sqlite://")
        try:
            with self.assertRaises(SQLAlchemyError):
                check_order(broken, "O001", "B001", self.cutoff)
        finally:
            broken.dispose()

    def test_single_statement_consistent_snapshot(self):
        statements = []
        def observe(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(self.engine, "before_cursor_execute", observe)
        try:
            check_order(self.engine, "O001", "B001", self.cutoff)
        finally:
            event.remove(self.engine, "before_cursor_execute", observe)
        business_reads = [sql for sql in statements if "LEFT OUTER JOIN" in sql.upper()]
        self.assertEqual(len(business_reads), 1)


# SQLite不保存tzinfo；只在测试引擎对已知UTC样本作适配，不修改业务代码。
def _sqlite_restore_utc(target, context):
    if context.session.bind.dialect.name == "sqlite":
        name = "paid_at" if isinstance(target, Order) else "refunded_at"
        value = getattr(target, name)
        if value.tzinfo is None:
            setattr(target, name, value.replace(tzinfo=timezone.utc))


event.listen(Order, "load", _sqlite_restore_utc)
event.listen(Refund, "load", _sqlite_restore_utc)


if __name__ == "__main__":
    unittest.main()
