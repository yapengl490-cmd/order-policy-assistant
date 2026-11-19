"""固定人工预期的两期/渠道核查；SQLite只补已知UTC时区，不冒充PG。"""
import copy
from datetime import datetime
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from sqlalchemy import Column, ForeignKey, Table, Text, event
import test_import_provenance as fixtures
from import_data import import_payload, ImportConflictError, ImportValidationError
from sales_analysis import compare_periods

ROOT=Path(__file__).resolve().parent/"fixtures"
PERIODS=[datetime.fromisoformat(x) for x in ["2026-10-01T00:00:00Z","2026-11-01T00:00:00Z",
    "2026-11-01T00:00:00Z","2026-12-01T00:00:00Z","2026-12-03T00:00:00Z"]]


class SalesAnalysisTests(unittest.TestCase):
    def setUp(self):
        # 复用独立临时库夹具，但不继承/重复运行前一套测试。
        self.fixture=fixtures.ImportProvenanceTests()
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.engine=self.fixture.engine
        metadata=self.fixture.batches.metadata
        self.channels=Table("order_channels",metadata,
            Column("order_id",Text,ForeignKey("public.orders.order_id"),primary_key=True),
            Column("channel",Text,nullable=False))
        metadata.create_all(self.engine)
        self.payload=json.loads((ROOT/"channel_comparison.json").read_text())
        self.expected=json.loads((ROOT/"channel_expected.json").read_text())
        import_payload(self.engine,self.payload)

    def compare(self,access=None,periods=None):
        return compare_periods(self.engine,access or {"role":"buyer","buyer_id":"B_ANALYSIS"},*(periods or PERIODS))

    def test_frozen_total_channel_and_source_for_both_roles(self):
        for access in [{"role":"buyer","buyer_id":"B_ANALYSIS"},{"role":"merchant","shop_id":"S_ANALYSIS"}]:
            got=self.compare(access)
            self.assertEqual(got["status"],"ok")
            for key in ("first","second","delta"):self.assertEqual(got[key],self.expected[key])
            by_channel={x["channel"]:x for x in got["channels"]}
            for channel,expect in self.expected["channels"].items():
                self.assertEqual(by_channel[channel]["presence"],expect["presence"])
                self.assertEqual(by_channel[channel]["contribution_net_fen"],expect["net_delta"])
            self.assertEqual(sum(x["contribution_net_fen"]for x in got["channels"]),got["delta"]["net_amount_fen"])
            self.assertEqual(got["provenance"]["status"],"recorded")
            ids={x["record_id"]for x in got["provenance"]["records"]}
            self.assertFalse(ids & set(self.expected["excluded_orders"]+self.expected["excluded_refunds"]))
            self.assertEqual(got["reason_status"],"unknown")
            self.assertFalse(any("percent"in k for row in got["channels"]for k in row))

    def test_other_account_scope_never_contains_primary_orders_or_sources(self):
        got=self.compare({"role":"merchant","shop_id":"S_OTHER"})
        self.assertEqual(got["second"]["paid_amount_fen"],99900)
        self.assertEqual({x["order_id"]for x in got["order_evidence"]},{"X"})
        self.assertEqual({x["record_id"]for x in got["provenance"]["records"]},{"X"})

    def test_zero_results_are_valid_and_have_no_source(self):
        got=self.compare({"role":"buyer","buyer_id":"B_NONE"})
        self.assertEqual(got["status"],"ok")
        self.assertTrue(all(v==0 for v in got["first"].values()))
        self.assertEqual(got["channels"],[])
        self.assertEqual(got["provenance"]["status"],"not_applicable")

    def test_overlap_reverse_empty_timezone_and_cutoff_rejected(self):
        for updates in [{2:"2026-10-20T00:00:00Z"},{0:"2026-11-01T00:00:00Z"},
                        {4:"2026-11-30T00:00:00Z"},{0:"2026-10-01T00:00:00"}]:
            periods=list(PERIODS)
            for i,v in updates.items():periods[i]=datetime.fromisoformat(v)
            with self.assertRaises(ValueError):self.compare(periods=periods)
        with self.assertRaises(ValueError):compare_periods(self.engine,{"role":"admin"},*PERIODS)
        with self.assertRaises(ValueError):compare_periods(self.engine,{"role":"buyer"},*PERIODS)

    def test_unmapped_channel_unknown_not_guessed(self):
        got=self.compare()
        unknown=next(x for x in got["channels"]if x["channel"]=="unknown")
        self.assertEqual(unknown["label"],"未提供渠道")
        self.assertEqual(unknown["first"]["paid_amount_fen"],5000)

    def test_unmigrated_channel_table_keeps_totals_marks_limitation(self):
        with self.engine.begin() as c:self.channels.drop(c)
        got=self.compare()
        self.assertEqual(got["delta"],self.expected["delta"])
        self.assertEqual(got["channel_data_status"],"not_migrated")
        self.assertEqual([x["channel"]for x in got["channels"]],["unknown"])

    def test_two_periods_one_business_query_not_two_independent_reads(self):
        seen=[]
        def record(conn,cursor,statement,parameters,context,many):seen.append(statement)
        event.listen(self.engine,"before_cursor_execute",record)
        try:self.compare()
        finally:event.remove(self.engine,"before_cursor_execute",record)
        self.assertEqual(sum("LEFT OUTER JOIN" in x.upper() for x in seen),1)

    def test_upper_limits_never_return_partial_totals(self):
        for name in ["MAX_SUMMARY_ORDERS","MAX_SUMMARY_ROWS"]:
            with patch('sales_analysis.'+name,1):got=self.compare()
            self.assertIn(got["status"],{"too_many_rows","too_many_orders"})
            self.assertNotIn("delta",got)

    def test_channel_only_extension_and_conflict_do_not_change_facts(self):
        extension={"metadata":dict(self.payload["metadata"],version="channel-add"),"buyers":[],"orders":[],"refunds":[],
                   "order_channels":[{"order_id":"B","channel":"offline"}]}
        result=import_payload(self.engine,extension)
        self.assertEqual(result["inserted_counts"]["order_channels"],1)
        got=self.compare();self.assertEqual(got["delta"],self.expected["delta"])
        extension["order_channels"][0]["channel"]="different"
        with self.assertRaises(ImportConflictError):import_payload(self.engine,extension)
        extension["order_channels"][0]["order_id"]="MISSING"
        with self.assertRaises(ImportValidationError):import_payload(self.engine,extension)

    def test_payment_boundary_placed_in_second_period_once(self):
        # 订单C移至两期相接的边界；其余数据不变，C仍仅计第二期。
        with self.engine.begin() as c:
            c.execute(self.fixture.orders.update().where(self.fixture.orders.c.order_id=="C").values(paid_at=PERIODS[2]))
        got=self.compare();self.assertEqual(got["first"],self.expected["first"])
        self.assertEqual(got["second"],self.expected["second"])
