"""助手离线业务测试：独立SQLite夹具，不访问正式PG、模型或政策向量服务。"""
import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy import select
import test_import_provenance as fixtures
from buyer_support_service import (check_buyer_support, evaluate_buyer_support,
    get_support_context, order_support_facts, refund_reason_records, support_metadata)
from import_data import import_payload, ImportConflictError, ImportValidationError
from import_support_data import import_support_payload, prepare_support_payload

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.fromisoformat("2026-11-18T08:00:00+08:00")
CLAIMS = {"is_unworn": True, "tags_intact": True, "reason": "size"}


def policy(_engine, *, store_id, category, paid_at):
    november = paid_at >= datetime.fromisoformat("2026-11-01T00:00:00+08:00")
    return {"status": "ok", "applicability": {"store_id": store_id, "category": category,
        "paid_at": paid_at.isoformat(), "effective_from": "2026-11-01T00:00:00+08:00" if november else "2026-10-01T00:00:00+08:00",
        "effective_before": "2026-12-01T00:00:00+08:00" if november else "2026-11-01T00:00:00+08:00", "selection_basis": "order_paid_at"},
        "payment_version": "nov-v2" if november else "oct-v1", "rules": {"return_window_hours": 336 if november else 168,
        "return_window_basis": "delivered_at", "return_window_boundary": "start_inclusive_end_exclusive",
        "allowed_categories": ["ordinary_shoes"], "custom_excluded": True, "requires_unworn": True, "requires_tags": True,
        "shipping_by_reason": {"size": "buyer", "preference": "buyer", "merchant_responsibility": "verified_record_required"}},
        "rule_citations": {"return_window_hours": {"chunk_id": "fixture-window", "text": "独立测试原文：收货后窗口上界不含。"}}}


class SupportFixture:
    def setup_support(self):
        self.fixture = fixtures.ImportProvenanceTests(); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.engine = self.fixture.engine
        support_metadata.create_all(self.engine)
        initial = {"metadata": {"source": "独立旧订单夹具", "version": "test", "currency": "CNY", "normalized": True, "data_kind": "normalized_sample"},
            "buyers": [{"buyer_id": "B001", "display_name": "测试买家"}], "orders": [{"order_id": "O001", "buyer_id": "B001", "paid_at": "2026-10-15T10:00:00+08:00", "paid_amount_fen": 12500, "status": "paid"}],
            "refunds": [], "shops": [{"shop_id": "S001", "name": "测试店铺"}], "order_shops": [{"order_id": "O001", "shop_id": "S001"}]}
        import_payload(self.engine, initial)
        self.business = json.loads((ROOT/"samples/support/business.json").read_text())
        self.payload = json.loads((ROOT/"samples/support/support_facts.json").read_text())
        import_payload(self.engine, self.business)
        self.business_snapshot = self.fixture.all_snapshot()
        import_support_payload(self.engine, self.payload, allowed_shop_id="S001")

    def support_snapshot(self):
        with self.engine.connect() as c:
            return tuple(tuple(tuple(r) for r in c.execute(select(t).order_by(*t.primary_key.columns)))
                         for t in (order_support_facts, refund_reason_records))


class BuyerSupportTests(SupportFixture, unittest.TestCase):
    def setUp(self): self.setup_support()

    def check(self, order="O_SUPPORT_OK", claims=None, **kwargs):
        return check_buyer_support(self.engine, order, buyer_id="B001", claims=CLAIMS if claims is None else claims,
                                   now=NOW, policy_selector=policy, **kwargs)

    def test_frozen_normal_old_version_expired_missing_and_custom(self):
        expected = json.loads((ROOT/"samples/support/expected.json").read_text())["r06"]
        for identity in ("O_SUPPORT_OK", "O001", "O_SUPPORT_UNKNOWN", "O_SUPPORT_CUSTOM"):
            got = self.check(identity)
            for key, value in expected[identity].items():
                self.assertEqual(got["shipping"]["payer"] if key=="payer" else got[key], value)
        self.assertEqual(self.check()["policy_version"], "nov-v2")
        self.assertEqual(self.check("O001")["policy_version"], "oct-v1")
        self.assertEqual(self.fixture.all_snapshot(), self.business_snapshot)

    def test_trusted_identity_and_original_facts_never_from_claims(self):
        for params in ({"buyer_id":"B404"}, {"shop_id":"S404"}):
            got=get_support_context(self.engine,"O_SUPPORT_OK",**params)
            self.assertEqual(got["status"],"not_found"); self.assertNotIn("trusted_facts",got);self.assertNotIn("order",got)
        for claims in ({"buyer_id":"B001"},{"delivered_at":NOW.isoformat()},{"is_custom":False}):
            with self.assertRaises(ValueError):self.check(claims=claims)
        for params in ({},{"buyer_id":"B001","shop_id":"S001"}):
            with self.assertRaises(ValueError):get_support_context(self.engine,"O001",**params)

    def test_single_eligibility_does_not_demand_shipping_reason(self):
        got=self.check(claims={"is_unworn":True,"tags_intact":True},topics=["eligibility"])
        self.assertEqual(got["status"],"ok");self.assertEqual(got["shipping"]["status"],"not_requested")
        self.assertEqual(got["missing"],[])

    def test_missing_self_report_and_negative_self_report_are_distinct(self):
        got=self.check(claims={});self.assertEqual(set(got["missing"]),{"is_unworn","tags_intact"})
        for field,code in (("is_unworn","user_reports_worn"),("tags_intact","user_reports_tags_missing")):
            got=self.check(claims={**CLAIMS,field:False});self.assertEqual(got["eligibility"],"ineligible")
            self.assertIn(code,got["failed_conditions"]);self.assertFalse(got["user_claims"]["verified"])

    def test_single_shipping_answers_conditional_rule_without_examining_unasked_eligibility(self):
        got=self.check("O_SUPPORT_UNKNOWN",claims={"reason":"size"},topics=["shipping"])
        self.assertEqual(got["status"],"ok");self.assertEqual(got["eligibility"],"not_requested")
        self.assertEqual(got["shipping"]["payer"],"buyer");self.assertFalse(got["shipping"]["verified"])

    def test_policy_with_different_window_basis_or_inclusive_end_not_silently_reinterpreted(self):
        context=get_support_context(self.engine,"O_SUPPORT_OK",buyer_id="B001")
        bundle=policy(None,store_id="S001",category="ordinary_shoes",paid_at=datetime.fromisoformat(context["order"]["paid_at"]))
        for key,value in (("return_window_basis","paid_at"),("return_window_boundary","end_inclusive")):
            changed=copy.deepcopy(bundle);changed["rules"][key]=value
            self.assertEqual(evaluate_buyer_support(context,changed,claims=CLAIMS,now=NOW)["status"],"insufficient_evidence")

    def test_shipping_reason_missing_or_merchant_responsibility_not_invented(self):
        got=self.check(claims={"is_unworn":True,"tags_intact":True})
        self.assertEqual(got["eligibility"],"eligible_with_self_report");self.assertEqual(got["missing"],["reason"])
        got=self.check(claims={**CLAIMS,"reason":"merchant_responsibility"})
        self.assertEqual(got["status"],"insufficient_evidence");self.assertIsNone(got["shipping"]["payer"])
        self.assertEqual(got["shipping"]["status"],"needs_verified_responsibility")

    def test_half_open_window_one_microsecond_before_and_at_boundary(self):
        context=get_support_context(self.engine,"O_SUPPORT_OK",buyer_id="B001")
        bundle=policy(None,store_id="S001",category="ordinary_shoes",paid_at=datetime.fromisoformat(context["order"]["paid_at"]))
        deadline=datetime.fromisoformat(context["trusted_facts"]["delivered_at"])+timedelta(hours=336)
        for current,eligible in ((deadline-timedelta(microseconds=1),True),(deadline,False),(deadline+timedelta(seconds=1),False)):
            got=evaluate_buyer_support(context,bundle,claims=CLAIMS,now=current)
            self.assertEqual(got["eligibility"]=="eligible_with_self_report",eligible)

    def test_future_record_delivery_and_impossible_sequence_are_data_error(self):
        original=get_support_context(self.engine,"O_SUPPORT_OK",buyer_id="B001")
        bundle=policy(None,store_id="S001",category="ordinary_shoes",paid_at=datetime.fromisoformat(original["order"]["paid_at"]))
        for field,value in (("recorded_at","2026-11-19T00:00:00Z"),("delivered_at","2026-11-19T00:00:00Z"),("delivered_at","2026-10-01T00:00:00Z")):
            c=copy.deepcopy(original);c["trusted_facts"][field]=value
            self.assertEqual(evaluate_buyer_support(c,bundle,claims=CLAIMS,now=NOW)["status"],"data_error")

    def test_wrong_store_version_and_no_policy_do_not_guess(self):
        context=get_support_context(self.engine,"O_SUPPORT_OK",buyer_id="B001")
        bundle=policy(None,store_id="S001",category="ordinary_shoes",paid_at=datetime.fromisoformat(context["order"]["paid_at"]))
        for changes in ({"store_id":"S_OTHER"},{"category":"food"},{"effective_before":"2026-11-01T00:00:00+08:00"}):
            wrong=copy.deepcopy(bundle);wrong["applicability"].update(changes)
            self.assertEqual(evaluate_buyer_support(context,wrong,claims=CLAIMS,now=NOW)["status"],"insufficient_evidence")
        self.assertEqual(evaluate_buyer_support(context,{"status":"insufficient_evidence"},claims=CLAIMS,now=NOW)["status"],"insufficient_evidence")

    def test_unknown_metadata_and_missing_table_are_not_success(self):
        with self.engine.begin() as c:c.execute(order_support_facts.update().where(order_support_facts.c.order_id=="O_SUPPORT_OK").values(product_category=None,is_custom=None))
        got=self.check();self.assertEqual(set(got["missing"]),{"product_category"})
        with self.engine.begin() as c:order_support_facts.drop(c)
        self.assertEqual(self.check()["status"],"support_data_unavailable")

    def test_known_exclusion_not_blocked_by_unnecessary_missing_fact(self):
        with self.engine.begin() as c:
            c.execute(order_support_facts.update().where(order_support_facts.c.order_id=="O_SUPPORT_CUSTOM").values(delivered_at=None))
            c.execute(order_support_facts.update().where(order_support_facts.c.order_id=="O001").values(is_custom=None))
        custom=self.check("O_SUPPORT_CUSTOM",claims={})
        self.assertEqual(custom["eligibility"],"ineligible");self.assertEqual(custom["failed_conditions"],["custom_product_excluded"])
        expired=self.check("O001",claims={})
        self.assertEqual(expired["eligibility"],"ineligible");self.assertEqual(expired["failed_conditions"],["return_window_expired"])

    def test_only_program_identity_selects_payment_policy(self):
        seen=[]
        def selector(engine,**kw):seen.append(kw);return policy(engine,**kw)
        got=check_buyer_support(self.engine,"O_SUPPORT_OK",shop_id="S001",claims=CLAIMS,now=NOW,policy_selector=selector)
        self.assertEqual(got["status"],"ok");self.assertEqual(seen[0]["store_id"],"S001")
        self.assertEqual(seen[0]["category"],"ordinary_shoes");self.assertEqual(seen[0]["paid_at"],datetime.fromisoformat("2026-11-11T10:00:00+08:00"))
        self.assertEqual(got["source_proof"],"declared_only");self.assertFalse(got["refund_execution"])

    def test_claim_types_topics_and_aware_clock_required(self):
        for claims in ({"is_unworn":"true"},{"tags_intact":1},{"reason":"nonsense"}):
            with self.assertRaises(ValueError):self.check(claims=claims)
        for topics in ([],["eligibility","eligibility"],["admin"],"shipping"):
            with self.assertRaises(ValueError):self.check(topics=topics)
        with self.assertRaises(ValueError):check_buyer_support(self.engine,"O001",buyer_id="B001",now=datetime(2026,11,18))


class SupportImportTests(SupportFixture, unittest.TestCase):
    def setUp(self):self.setup_support()

    def test_repeat_is_idempotent_preserves_business_and_source(self):
        before=self.support_snapshot()
        got=import_support_payload(self.engine,self.payload,allowed_shop_id="S001")
        self.assertEqual(got["status"],"already_imported");self.assertFalse(any(got["inserted_counts"].values()))
        self.assertEqual(self.support_snapshot(),before);self.assertEqual(self.fixture.all_snapshot(),self.business_snapshot)

    def test_conflict_refuses_whole_batch_even_if_new_row_would_be_valid(self):
        p=copy.deepcopy(self.payload);p["order_support_facts"][0]["is_custom"]=True
        extra=copy.deepcopy(p["refund_reason_records"][0]);extra["evidence_id"]="NEW-EVIDENCE";p["refund_reason_records"].append(extra)
        before=self.support_snapshot()
        with self.assertRaises(ImportConflictError):import_support_payload(self.engine,p,allowed_shop_id="S001")
        self.assertEqual(self.support_snapshot(),before)

    def test_wrong_shop_missing_order_or_refund_refuses_all(self):
        before=self.support_snapshot()
        for p,shop in ((self.payload,"S_OTHER"),({**copy.deepcopy(self.payload),"order_support_facts":[{**self.payload["order_support_facts"][0],"order_id":"MISSING"}]},"S001"),
                       ({**copy.deepcopy(self.payload),"refund_reason_records":[{**self.payload["refund_reason_records"][0],"refund_id":"MISSING"}]},"S001")):
            with self.assertRaises(ImportValidationError):import_support_payload(self.engine,p,allowed_shop_id=shop)
        self.assertEqual(self.support_snapshot(),before)

    def test_invalid_metadata_duplicate_boolean_time_and_reason_are_rejected(self):
        mutations=[lambda p:p["metadata"].update(data_kind="real"),lambda p:p["order_support_facts"][0].update(is_custom=1),
            lambda p:p["order_support_facts"][0].update(delivered_at="2026-10-20"),lambda p:p["refund_reason_records"][0].update(reason_code="invented"),
            lambda p:p["refund_reason_records"].append(copy.deepcopy(p["refund_reason_records"][0]))]
        for mutate in mutations:
            p=copy.deepcopy(self.payload);mutate(p)
            with self.assertRaises(ImportValidationError):prepare_support_payload(p)

    def test_impossible_source_sequence_rejected_before_any_write(self):
        for kind,field,value in (("order_support_facts","delivered_at","2026-01-01T00:00:00Z"),
                                ("refund_reason_records","recorded_at","2026-01-01T00:00:00Z")):
            p=copy.deepcopy(self.payload);p[kind][0][field]=value;before=self.support_snapshot()
            with self.assertRaises(ImportValidationError):import_support_payload(self.engine,p,allowed_shop_id="S001")
            self.assertEqual(self.support_snapshot(),before)
