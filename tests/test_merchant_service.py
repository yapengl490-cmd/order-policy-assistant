"""助手商家范围回归：独立样本；可选真实PG随机模式，不改原业务记录。"""

import os
import unittest
import uuid
from datetime import datetime, timezone

from sqlalchemy import create_engine, delete, event, insert, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from merchant_service import check_shop_order, list_shop_orders, order_shops, shop_metadata, shops, summary_shop_orders
from models import Base, Order, Refund


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _sqlite_time(target, context):
    if context.session.bind.dialect.name == "sqlite":
        field = "paid_at" if isinstance(target, Order) else "refunded_at"
        value = getattr(target, field)
        if value.tzinfo is None:
            setattr(target, field, value.replace(tzinfo=timezone.utc))


class MerchantServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        target = os.environ.get("TEST_DATABASE_URL")
        cls.schema = "merchant_test_" + uuid.uuid4().hex
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
        event.listen(Order, "load", _sqlite_time)
        event.listen(Refund, "load", _sqlite_time)
        Base.metadata.create_all(cls.engine)
        shop_metadata.create_all(cls.engine)

    @classmethod
    def tearDownClass(cls):
        event.remove(Order, "load", _sqlite_time)
        event.remove(Refund, "load", _sqlite_time)
        if cls.engine.dialect.name == "postgresql":
            with cls.base_engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.base_engine.dispose()

    def setUp(self):
        with Session(self.engine) as session, session.begin():
            for table in (order_shops, Refund, Order, shops):
                session.execute(delete(table))
            session.execute(insert(shops), [{"shop_id": "S001", "name": "测试一店"}, {"shop_id": "S002", "name": "测试二店"}])
            session.add_all([
                Order(order_id="O001", buyer_id="B001", paid_at=utc("2026-10-15T00:00:00"), paid_amount_fen=12500),
                Order(order_id="O002", buyer_id="B002", paid_at=utc("2026-10-16T00:00:00"), paid_amount_fen=12500),
                Order(order_id="O003", buyer_id="B003", paid_at=utc("2026-10-17T00:00:00"), paid_amount_fen=50000),
                Order(order_id="O004", buyer_id="B001", paid_at=utc("2026-10-18T00:00:00"), paid_amount_fen=9999),
            ])
            session.flush()
            session.execute(insert(order_shops), [
                {"order_id": "O001", "shop_id": "S001"}, {"order_id": "O002", "shop_id": "S001"},
                {"order_id": "O003", "shop_id": "S002"},
            ])
            session.add_all([
                Refund(refund_id="R001", order_id="O001", refunded_at=utc("2026-10-20T00:00:00"), refund_amount_fen=2500),
                Refund(refund_id="R002", order_id="O001", refunded_at=utc("2026-11-02T00:00:00"), refund_amount_fen=1500),
                Refund(refund_id="R003", order_id="O003", refunded_at=utc("2026-10-20T00:00:00"), refund_amount_fen=100),
            ])
        self.start = utc("2026-10-01T00:00:00")
        self.end = utc("2026-11-01T00:00:00")
        self.cutoff = utc("2026-11-03T00:00:00")

    def test_shop_can_read_different_buyers_within_same_shop(self):
        first = check_shop_order(self.engine, "O001", "S001", self.cutoff)
        second = check_shop_order(self.engine, "O002", "S001", self.cutoff)
        self.assertEqual((first["status"], second["status"]), ("ok", "ok"))
        self.assertEqual((first["buyer_id"], second["buyer_id"]), ("B001", "B002"))
        self.assertEqual((first["shop_id"], second["shop_id"]), ("S001", "S001"))
        self.assertEqual(second["refund_amount_fen"], 0)

    def test_other_shop_missing_and_unmapped_orders_are_not_found(self):
        results = [check_shop_order(self.engine, order_id, "S001", self.cutoff) for order_id in ("O003", "O404", "O004")]
        results.append(check_shop_order(self.engine, "O001", "S404", self.cutoff))
        for result in results:
            self.assertEqual(result["status"], "not_found")
            self.assertNotIn("buyer_id", result)
            self.assertNotIn("paid_amount_fen", result)
            self.assertIn("shop_id", result)
        self.assertEqual(len({result["message"] for result in results}), 1)

    def test_summary_preserves_equal_price_orders_once_each(self):
        result = summary_shop_orders(self.engine, "S001", self.start, self.end, self.cutoff)
        self.assertEqual((result["paid_amount_fen"], result["refund_amount_fen"], result["net_amount_fen"]), (25000, 4000, 21000))
        self.assertEqual(result["order_count"], 2)
        self.assertEqual([row["order_id"] for row in result["orders"]], ["O001", "O002"])
        self.assertTrue(all(row["shop_id"] == "S001" for row in result["orders"]))

    def test_listing_filters_shop_and_preserves_limit(self):
        result = list_shop_orders(self.engine, "S001", 1)
        self.assertTrue(result["has_more"])
        self.assertEqual([row["order_id"] for row in result["orders"]], ["O002"])
        self.assertEqual([row["order_id"] for row in list_shop_orders(self.engine, "S002")["orders"]], ["O003"])
        self.assertEqual(list_shop_orders(self.engine, "S404")["orders"], [])

    def test_cross_month_and_exact_cutoff(self):
        nov = check_shop_order(self.engine, "O001", "S001", self.end)
        exact = check_shop_order(self.engine, "O001", "S001", utc("2026-10-20T00:00:00"))
        self.assertEqual((nov["refund_amount_fen"], nov["net_amount_fen"]), (2500, 10000))
        self.assertEqual(exact["refund_amount_fen"], 0)

    def test_cutoff_before_payment_has_scope_no_net(self):
        result = check_shop_order(self.engine, "O001", "S001", self.start)
        self.assertEqual((result["status"], result["shop_id"]), ("not_yet_paid", "S001"))
        self.assertNotIn("net_amount_fen", result)

    def test_summary_unknown_shop_is_zero_not_all_orders(self):
        result = summary_shop_orders(self.engine, "S404", self.start, self.end, self.cutoff)
        self.assertEqual((result["status"], result["shop_id"], result["order_count"], result["net_amount_fen"]), ("ok", "S404", 0, 0))

    def test_shop_value_is_parameter_not_sql(self):
        result = list_shop_orders(self.engine, "S001' OR 1=1 --")
        self.assertEqual(result["orders"], [])

    def test_input_constraints(self):
        for limit in (0, 51, True, "2"):
            with self.assertRaises(ValueError):
                list_shop_orders(self.engine, "S001", limit)
        with self.assertRaises(ValueError):
            check_shop_order(self.engine, "O001", "", self.cutoff)
        with self.assertRaises(ValueError):
            check_shop_order(self.engine, "O001", "S001", datetime(2026, 11, 3))
        with self.assertRaises(ValueError):
            summary_shop_orders(self.engine, "S001", self.start, self.cutoff, self.end)

    def test_bad_amount_stops_summary_and_keeps_scope(self):
        with Session(self.engine) as session, session.begin():
            session.add(Refund(refund_id="BAD", order_id="O001", refunded_at=self.end, refund_amount_fen=20000))
        result = summary_shop_orders(self.engine, "S001", self.start, self.end, self.cutoff)
        self.assertEqual((result["status"], result["shop_id"]), ("data_error", "S001"))
        self.assertNotIn("net_amount_fen", result)

    def test_limit_does_not_return_partial_total(self):
        with Session(self.engine) as session, session.begin():
            session.add_all([Refund(refund_id=f"M{i}", order_id="O001", refunded_at=self.end, refund_amount_fen=1) for i in range(199)])
        result = check_shop_order(self.engine, "O001", "S001", self.cutoff)
        self.assertEqual((result["status"], result["shop_id"]), ("too_many_refunds", "S001"))
        self.assertNotIn("net_amount_fen", result)

    def test_missing_schema_raises_not_empty_success(self):
        broken = create_engine("sqlite://")
        try:
            with self.assertRaises(SQLAlchemyError):
                check_shop_order(broken, "O001", "S001", self.cutoff)
        finally:
            broken.dispose()

    def test_one_statement_for_order_and_refunds(self):
        statements = []
        def observe(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(self.engine, "before_cursor_execute", observe)
        try:
            check_shop_order(self.engine, "O001", "S001", self.cutoff)
        finally:
            event.remove(self.engine, "before_cursor_execute", observe)
        business_reads = [sql for sql in statements if "LEFT OUTER JOIN" in sql.upper()]
        self.assertEqual(len(business_reads), 1)


if __name__ == "__main__":
    unittest.main()
