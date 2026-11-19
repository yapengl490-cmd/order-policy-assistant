"""无模型调用的内存SQL硬边界与三类正确性；不访问正式数据库。"""
import copy
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import unittest
from snapshot_sql import snapshot_from_summary,execute_snapshot_sql,SnapshotError

VALID_SQL='SELECT COUNT(*) AS order_count, COALESCE(SUM(paid_amount_fen),0) AS paid_amount_fen, COALESCE(SUM(refund_amount_fen),0) AS refund_amount_fen, COALESCE(SUM(net_amount_fen),0) AS net_amount_fen FROM order_facts'
CONDITIONS={"paid_from":"2026-10-01T00:00:00+00:00","paid_before":"2026-11-01T00:00:00+00:00","refund_before":"2026-11-03T00:00:00+00:00"}
SUMMARY={"status":"ok","order_count":2,"paid_amount_fen":20000,"refund_amount_fen":3000,"net_amount_fen":17000,"orders":[
    {"order_id":"O_A","paid_at":"2026-10-10T00:00:00+00:00","paid_amount_fen":10000,"refund_amount_fen":1000,"net_amount_fen":9000,"refund_count":1},
    {"order_id":"O_B","paid_at":"2026-10-20T00:00:00+00:00","paid_amount_fen":10000,"refund_amount_fen":2000,"net_amount_fen":8000,"refund_count":1}]}


class SnapshotSQLTests(unittest.TestCase):
    def setUp(self):self.snapshot=snapshot_from_summary(copy.deepcopy(SUMMARY),CONDITIONS)

    def test_valid_summary_all_three_checks_and_immutable_source(self):
        got=execute_snapshot_sql(self.snapshot,VALID_SQL)
        self.assertEqual(got["status"],"ok")
        self.assertEqual([got[k]for k in ("syntax","execution","metric")],["passed"]*3)
        self.assertEqual(got["actual"],self.snapshot.baseline())
        with self.assertRaises(FrozenInstanceError):self.snapshot.rows=()
        original=copy.deepcopy(SUMMARY);frozen=snapshot_from_summary(original,CONDITIONS)
        original["orders"][0]["paid_amount_fen"]=1
        self.assertEqual(frozen.rows[0][2],10000)

    def test_zero_orders_need_coalesce(self):
        empty={"status":"ok","orders":[],"order_count":0,"paid_amount_fen":0,"refund_amount_fen":0,"net_amount_fen":0}
        snapshot=snapshot_from_summary(empty,CONDITIONS)
        self.assertEqual(execute_snapshot_sql(snapshot,VALID_SQL)["status"],"ok")

    def test_distinct_money_and_extra_filter_are_metric_errors_not_syntax_errors(self):
        for sql in [VALID_SQL.replace('SUM(paid_amount_fen)','SUM(DISTINCT paid_amount_fen)'), VALID_SQL+" WHERE order_id='O_A'"]:
            got=execute_snapshot_sql(self.snapshot,sql)
            self.assertEqual((got["syntax"],got["execution"],got["metric"]),("passed","passed","failed"))
            self.assertFalse(got["repairable"])

    def test_no_writes_pragmas_attach_catalog_or_extension(self):
        with tempfile.TemporaryDirectory()as directory:
            path=Path(directory)/"should_not_exist.sqlite"
            statements=["DELETE FROM order_facts","UPDATE order_facts SET paid_amount_fen=0", "DROP TABLE order_facts",
                "CREATE TABLE other(x)", "CREATE TEMP TABLE other(x)","PRAGMA query_only=OFF",
                f"ATTACH DATABASE '{path}' AS stolen","SELECT * FROM sqlite_master","SELECT rowid FROM order_facts",
                "SELECT load_extension('/tmp/not_allowed') FROM order_facts", "SELECT randomblob(9999999) FROM order_facts"]
            for sql in statements:
                with self.subTest(sql=sql):
                    got=execute_snapshot_sql(self.snapshot,sql)
                    self.assertEqual(got["status"],"rejected")
                    self.assertEqual(got["error"],"sql_policy_denied")
                    self.assertFalse(got["repairable"])
            self.assertFalse(path.exists())
        self.assertEqual(execute_snapshot_sql(self.snapshot,VALID_SQL)["actual"],self.snapshot.baseline())

    def test_single_statement_and_snapshot_read_required(self):
        got=execute_snapshot_sql(self.snapshot,VALID_SQL+"; SELECT 1")
        self.assertNotEqual(got["status"],"ok");self.assertFalse(got["repairable"])
        self.assertEqual(execute_snapshot_sql(self.snapshot,"SELECT 2 AS order_count,20000 AS paid_amount_fen,3000 AS refund_amount_fen,17000 AS net_amount_fen")["error"],"authorized_snapshot_read_required")

    def test_one_repairable_class_reference_or_syntax(self):
        for sql in [VALID_SQL.replace('paid_amount_fen','not_a_column'),"SELECT FROM order_facts"]:
            got=execute_snapshot_sql(self.snapshot,sql)
            self.assertEqual(got["status"],"error");self.assertTrue(got["repairable"])
            self.assertEqual(got["syntax"],"passed"if "not_a_column"in sql else"failed")

    def test_row_byte_time_and_vm_limits(self):
        self.assertEqual(execute_snapshot_sql(self.snapshot,'SELECT * FROM order_facts',max_rows=1)["error"],"result_row_limit")
        self.assertEqual(execute_snapshot_sql(self.snapshot,VALID_SQL,max_bytes=10)["error"],"result_byte_limit")
        self.assertEqual(execute_snapshot_sql(self.snapshot,VALID_SQL,max_seconds=0)["status"],"limited")
        expensive="SELECT COUNT(*) FROM order_facts a CROSS JOIN order_facts b CROSS JOIN order_facts c CROSS JOIN order_facts d CROSS JOIN order_facts e CROSS JOIN order_facts f CROSS JOIN order_facts g CROSS JOIN order_facts h"
        got=execute_snapshot_sql(self.snapshot,expensive,max_vm_steps=100)
        self.assertEqual(got["status"],"limited")

    def test_cancel_before_and_during_and_callback_failure(self):
        self.assertEqual(execute_snapshot_sql(self.snapshot,VALID_SQL,should_cancel=lambda:True)["status"],"cancelled")
        counter=[0]
        def cancel():counter[0]+=1;return counter[0]>1
        self.assertEqual(execute_snapshot_sql(self.snapshot,VALID_SQL,should_cancel=cancel)["status"],"cancelled")
        def broken():raise RuntimeError('test')
        self.assertEqual(execute_snapshot_sql(self.snapshot,VALID_SQL,should_cancel=broken)["status"],"cancelled")

    def test_input_limits_and_invalid_snapshot(self):
        for sql in ['',None,'x'*8193,'SELECT \ud800','SELECT\x00']:
            self.assertEqual(execute_snapshot_sql(self.snapshot,sql)["status"],"rejected")
        bad=copy.deepcopy(SUMMARY);bad['orders'][1]['order_id']='O_A'
        with self.assertRaises(SnapshotError):snapshot_from_summary(bad,CONDITIONS)
        bad=copy.deepcopy(SUMMARY);bad['paid_amount_fen']=3
        with self.assertRaises(SnapshotError):snapshot_from_summary(bad,CONDITIONS)

    def test_result_wrong_shape_is_not_verified(self):
        got=execute_snapshot_sql(self.snapshot,'SELECT COUNT(*) AS count FROM order_facts')
        self.assertEqual(got['execution'],'passed');self.assertEqual(got['metric'],'failed')
