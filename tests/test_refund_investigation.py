"""固定真值的退款证据调查；只用独立临时数据库，不调用模型。"""
import copy
from datetime import datetime, timedelta
import json
from unittest.mock import patch
import unittest

from sqlalchemy import select
from buyer_support_service import refund_reason_records
from import_support_data import import_support_payload
from refund_investigation import investigate_refund_increase
from test_buyer_support_service import SupportFixture, ROOT, NOW


class RefundInvestigationTests(SupportFixture, unittest.TestCase):
    def setUp(self):
        self.setup_support()
        self.expected=json.loads((ROOT/"samples/support/expected.json").read_text())["r05"]
        self.times=[datetime.fromisoformat(self.expected["conditions"][key]) for key in
                    ("first_from","first_before","second_from","second_before","refund_before")]

    def run_case(self, access=None, **kwargs):
        return investigate_refund_increase(self.engine,access or {"role":"merchant","shop_id":"S001"},*self.times,observed_at=NOW,**kwargs)

    def extra_reason(self,refund_id="R_SUPPORT_C",reason="size",status="verified",**updates):
        extra=copy.deepcopy(self.payload["refund_reason_records"][0])
        extra.update(evidence_id="EV_EXTRA",refund_id=refund_id,reason_code=reason,verification_status=status,**updates)
        p={"metadata":copy.deepcopy(self.payload["metadata"]),"order_support_facts":[],"refund_reason_records":[extra]}
        return import_support_payload(self.engine,p,allowed_shop_id="S001")

    def test_frozen_totals_evidence_categories_and_no_causal_claim(self):
        for access in ({"role":"merchant","shop_id":"S001"},{"role":"buyer","buyer_id":"B001"}):
            got=self.run_case(access)
            self.assertEqual(got["status"],"ok")
            for period in ("first","second"):self.assertEqual(got["comparison"][period],self.expected[period])
            self.assertEqual(got["total_refund_delta_fen"],3000);self.assertEqual(got["verified_size_delta_fen"],2000)
            buckets={row["bucket"]:row for row in got["reason_breakdown"]}
            self.assertEqual(buckets["buyer_reported_size"]["second"]["refund_amount_fen"],500)
            self.assertEqual(buckets["missing_reason"]["second"]["refund_amount_fen"],1000)
            self.assertEqual(sum(row["delta_refund_amount_fen"]for row in buckets.values()),3000)
            self.assertEqual(got["finding"],"recorded_size_amount_increased")
            self.assertFalse(got["causality_verified"]);self.assertEqual(got["source_proof"],"declared_only")
            self.assertEqual(got["evidence_status"],"partial_reason_coverage")
        self.assertEqual(self.fixture.all_snapshot(),self.business_snapshot)

    def test_cross_shop_and_other_buyer_never_reveal_refund_or_reason(self):
        for access in ({"role":"merchant","shop_id":"S_OTHER"},{"role":"buyer","buyer_id":"B404"}):
            got=self.run_case(access);self.assertEqual(got["status"],"ok");self.assertEqual(got["refund_evidence"],[])
            self.assertEqual(got["total_refund_delta_fen"],0)
            self.assertNotIn("R_SUPPORT",json.dumps(got))

    def test_duplicate_consistent_evidence_does_not_double_count_refund(self):
        self.extra_reason()
        got=self.run_case();self.assertEqual(got["verified_size_delta_fen"],2000);self.assertEqual(got["total_refund_delta_fen"],3000)
        one=next(row for row in got["refund_evidence"]if row["refund_id"]=="R_SUPPORT_C")
        self.assertEqual(len(one["evidence"]),2);self.assertEqual(one["refund_amount_fen"],3000)

    def test_conflicting_records_isolate_amount_and_do_not_choose_preferred_reason(self):
        self.extra_reason(reason="other",status="buyer_reported")
        got=self.run_case();buckets={r["bucket"]:r for r in got["reason_breakdown"]}
        self.assertEqual(buckets["conflicting_reasons"]["second"]["refund_amount_fen"],3000)
        self.assertEqual(got["verified_size_delta_fen"],-1000)
        self.assertEqual(got["finding"],"no_verified_size_increase")

    def test_records_after_observation_do_not_backfill_known_evidence(self):
        self.extra_reason(refund_id="R_SUPPORT_D",recorded_at=(NOW+timedelta(seconds=1)).isoformat())
        got=self.run_case();one=next(r for r in got["refund_evidence"]if r["refund_id"]=="R_SUPPORT_D")
        self.assertEqual(one["evidence_bucket"],"missing_reason");self.assertEqual(one["evidence"],[])

    def test_at_observation_record_included_but_refund_cutoff_excluded(self):
        self.extra_reason(refund_id="R_SUPPORT_D",recorded_at=NOW.isoformat())
        got=self.run_case();one=next(r for r in got["refund_evidence"]if r["refund_id"]=="R_SUPPORT_D")
        self.assertEqual(one["evidence_bucket"],"verified_size")
        # R_SUPPORT_C2恰在新的退款上界，不计入；支付区间结束早于此截止。
        self.times[4]=datetime.fromisoformat("2026-11-17T12:00:00+08:00")
        got=self.run_case();self.assertEqual(got["comparison"]["second"]["refund_amount_fen"],4000)
        self.assertNotIn("R_SUPPORT_C2",{r["refund_id"]for r in got["refund_evidence"]})

    def test_missing_table_and_row_limit_do_not_masquerade_as_zero(self):
        with patch("refund_investigation.MAX_EVIDENCE_ROWS",1):got=self.run_case()
        self.assertEqual(got["status"],"too_many_evidence_rows");self.assertNotIn("reason_breakdown",got)
        with self.engine.begin() as c:refund_reason_records.drop(c)
        got=self.run_case();self.assertEqual(got["status"],"reason_data_unavailable")
        self.assertEqual(got["comparison"]["delta"]["refund_amount_fen"],3000)

    def test_invalid_scope_time_and_future_cutoff_rejected(self):
        for access in ({"role":"admin"},{"role":"merchant"}):
            with self.assertRaises(ValueError):self.run_case(access)
        with self.assertRaises(ValueError):investigate_refund_increase(self.engine,{"role":"merchant","shop_id":"S001"},*self.times,observed_at=datetime(2026,11,18))
        self.times[4]=NOW+timedelta(seconds=1)
        with self.assertRaises(ValueError):self.run_case()

    def test_amount_reason_snapshot_mismatch_refuses_partial_explanation(self):
        import refund_investigation as target
        original=target.compare_periods
        def altered(*args,**kwargs):
            result=original(*args,**kwargs);result["second"]["refund_amount_fen"]+=1;return result
        with patch("refund_investigation.compare_periods",side_effect=altered):got=self.run_case()
        self.assertEqual(got["status"],"data_error");self.assertNotIn("verified_size_delta_fen",got)

    def test_database_error_propagates_not_empty_evidence(self):
        with patch("refund_investigation.compare_periods",side_effect=RuntimeError("test database unavailable")):
            with self.assertRaises(RuntimeError):self.run_case()
