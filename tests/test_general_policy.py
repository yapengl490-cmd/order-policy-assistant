"""Offline policy-query coverage with an isolated SQLite library, no orders."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, update

from general_policy import lookup_general_policy, render_general_policy
from policy_documents import MAX_RETURN_WINDOW_HOURS, PolicyDocumentError, parse_policy_text
from policy_kb import metadata, policy_documents, policy_chunks, import_policy_files, select_policy
from buyer_support_service import evaluate_buyer_support


ROOT = Path(__file__).resolve().parents[1]
OCTOBER = datetime.fromisoformat("2026-10-15T10:00:00+08:00")
NOVEMBER = datetime.fromisoformat("2026-11-20T10:00:00+08:00")


class GeneralPolicyTests(unittest.TestCase):
    def setUp(self):
        self.no_network = patch("socket.socket", side_effect=AssertionError("network forbidden in offline test"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)
        self.engine = create_engine("sqlite+pysqlite:///:memory:").execution_options(schema_translate_map={"public": None})
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        self.paths = [ROOT / "knowledge/2026-10-shoes.md", ROOT / "knowledge/2026-11-shoes.md"]
        import_policy_files(self.engine, self.paths)

    def lookup(self, **changes):
        args = {"store_id": "S001", "category": "ordinary_shoes", "policy_at": NOVEMBER}
        return lookup_general_policy(self.engine, **(args | changes))

    def test_general_query_needs_no_order_and_never_claims_order_eligibility(self):
        # This database intentionally has no orders/buyers/order_support_facts.
        result = self.lookup()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["kind"], "general_policy")
        self.assertEqual(result["selection_basis"], "policy_query_time")
        self.assertEqual(result["order_eligibility"], "not_evaluated")
        self.assertFalse(result["refund_execution"])
        self.assertEqual(result["model_requests"], 0)
        self.assertEqual(result["embedding_requests"], 0)
        self.assertNotIn("paid_at", result["applicability"])
        self.assertNotIn("payment_version", result)
        self.assertNotIn("order", result)
        self.assertIn("未核查任何订单", render_general_policy(result))

    def test_general_query_and_order_query_use_distinct_time_semantics(self):
        general = self.lookup(policy_at=NOVEMBER)
        order_policy = select_policy(self.engine, store_id="S001", category="ordinary_shoes", paid_at=OCTOBER)
        self.assertEqual(general["rules"]["return_window_hours"], 336)
        self.assertEqual(order_policy["rules"]["return_window_hours"], 168)
        self.assertEqual(order_policy["applicability"]["selection_basis"], "order_paid_at")
        self.assertNotEqual(general["policy_version"], order_policy["payment_version"])

    def test_exact_policy_boundary_and_equivalent_timezones(self):
        start = datetime.fromisoformat("2026-11-01T00:00:00+08:00")
        self.assertEqual(self.lookup(policy_at=start - timedelta(microseconds=1))["rules"]["return_window_hours"], 168)
        for stamp in (start, datetime.fromisoformat("2026-10-31T16:00:00+00:00")):
            self.assertEqual(self.lookup(policy_at=stamp)["rules"]["return_window_hours"], 336)
        result = self.lookup(policy_at=datetime.fromisoformat("2026-12-01T00:00:00+08:00"))
        self.assertEqual(result["reason"], "no_supported_policy")
        self.assertFalse(result["citations"])

    def test_missing_category_reports_actual_coverage_without_assuming_shoes(self):
        result = self.lookup(category=None)
        self.assertEqual(result["status"], "clarification")
        self.assertEqual(result["missing"], ["category"])
        self.assertEqual(result["supported_categories"], ["ordinary_shoes"])
        self.assertIsNone(result["scope"]["category"])
        self.assertFalse(result["rules"])
        self.assertFalse(result["citations"])
        self.assertIn("无需提供订单号", render_general_policy(result))

    def test_scope_category_and_period_do_not_borrow_other_evidence(self):
        for changes in ({"store_id": "S_OTHER"}, {"category": "t_shirt"},
                        {"policy_at": datetime.fromisoformat("2027-01-01T00:00:00+08:00")},
                        {"store_id": "S_OTHER", "category": None}):
            with self.subTest(changes=changes):
                result = self.lookup(**changes)
                self.assertEqual(result["status"], "insufficient_evidence")
                self.assertEqual(result["reason"], "no_supported_policy")
                self.assertFalse(result["citations"])
                self.assertFalse(result["claims"])
                self.assertNotIn("document", result)
                self.assertNotIn("DEMO-SUPPORT", json.dumps(result))

    def test_unsupported_or_mixed_topics_do_not_substitute_return_policy(self):
        # A sentinel engine proves unknown topics stop before any database use.
        for topics in (["exchange"], ["arrival"], ["repair"], ["compensation"], ["eligibility", "exchange"]):
            with self.subTest(topics=topics):
                result = lookup_general_policy(object(), store_id="S001", category="ordinary_shoes", policy_at=NOVEMBER, topics=topics)
                self.assertEqual(result["status"], "insufficient_evidence")
                self.assertEqual(result["reason"], "unsupported_topic")
                self.assertFalse(result["claims"])

    def test_shipping_question_keeps_own_citation_and_no_personal_payer(self):
        result = self.lookup(topics=["shipping"])
        self.assertEqual([c["id"] for c in result["claims"]], ["scope", "shipping_policy"])
        shipping = result["claims"][-1]
        self.assertEqual(result["citation_index"][shipping["citation_ids"][0]]["section"], "shipping_cost")
        answer = render_general_policy(result)
        self.assertIn("经核实的本次售后责任记录", answer)
        self.assertNotIn("shipping_payer", result)
        self.assertNotIn("小时内", answer)

    def test_return_conditions_keep_exception_and_unverified_boundary(self):
        result = self.lookup(topics=["eligibility"])
        answer = render_general_policy(result)
        for phrase in ("336小时", "未穿着", "吊牌完好", "定制商品不适用", "自述仍需核实", "不含截止"):
            self.assertIn(phrase, answer)
        self.assertNotIn("shipping_policy", [c["id"] for c in result["claims"]])

    def test_each_claim_cites_exact_current_document_and_source_lines(self):
        result = self.lookup()
        lines = result["document"]["source_text"].splitlines()
        for claim in result["claims"]:
            self.assertTrue(claim["citation_ids"])
            for ref in claim["citation_ids"]:
                source = result["citation_index"][ref]
                self.assertEqual(source["document_checksum"], result["document"]["checksum"])
                self.assertEqual(source["version"], result["policy_version"])
                self.assertEqual(source["text"], "\n".join(lines[source["line_start"] - 1:source["line_end"]]))
                self.assertIn("[" + ref + "]", render_general_policy(result))

    def test_corrupted_policy_is_not_rendered_or_advertised_as_coverage(self):
        with self.engine.begin() as c:
            c.execute(update(policy_chunks).where(policy_chunks.c.document_id == "S001-shoes-2026-11").values(text="缺失完整条款"))
        result = self.lookup()
        self.assertEqual(result["status"], "invalid_policy")
        self.assertFalse(result["claims"])
        coverage = self.lookup(category=None)
        self.assertEqual(coverage["reason"], "no_supported_policy")
        self.assertEqual(coverage["supported_categories"], [])

    def test_version_conflict_stops_general_lookup(self):
        with self.engine.begin() as c:
            c.execute(update(policy_documents).where(policy_documents.c.document_id == "S001-shoes-2026-10").values(
                effective_before=datetime.fromisoformat("2026-12-01T00:00:00+08:00")))
        result = self.lookup()
        self.assertEqual(result["status"], "conflict")
        self.assertFalse(result["claims"])
        self.assertIn("版本冲突", render_general_policy(result))

    def test_absent_policy_tables_are_distinct_from_empty_matching_scope(self):
        empty = create_engine("sqlite+pysqlite:///:memory:").execution_options(schema_translate_map={"public": None})
        self.addCleanup(empty.dispose)
        result = lookup_general_policy(empty, store_id="S001", category="ordinary_shoes", policy_at=NOVEMBER)
        self.assertEqual(result["reason"], "policy_data_unavailable")

    def test_result_mutation_does_not_modify_stored_policy(self):
        first = self.lookup()
        first["rules"]["return_window_hours"] = 1
        first["claims"][0]["text"] = "changed"
        first["citation_index"].clear()
        second = self.lookup()
        self.assertEqual(second["rules"]["return_window_hours"], 336)
        self.assertTrue(second["citation_index"])
        self.assertNotEqual(second["claims"][0]["text"], "changed")

    def test_invalid_inputs_fail_before_reading(self):
        for changes in ({"store_id": ""}, {"store_id": None}, {"category": ""},
                        {"policy_at": "2026-11-20"}, {"policy_at": datetime(2026, 11, 20)},
                        {"topics": []}, {"topics": ["shipping", "shipping"]}, {"topics": "shipping"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                lookup_general_policy(object(), **({"store_id": "S001", "category": "ordinary_shoes", "policy_at": NOVEMBER} | changes))


class SharedWindowLimitTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "knowledge/2026-10-shoes.md").read_text()

    def context_and_bundle(self, hours):
        doc = parse_policy_text(self.source.replace("168小时", str(hours) + "小时"))
        delivered = datetime.fromisoformat("2026-10-16T10:00:00+08:00")
        context = {"status": "ok", "order_id": "TEST-ORDER", "scope": {"role": "buyer", "buyer_id": "TEST-BUYER"},
                   "order": {"shop_id": "S001", "paid_at": OCTOBER.isoformat()},
                   "trusted_facts": {"product_category": "ordinary_shoes", "is_custom": False,
                                     "delivered_at": delivered.isoformat(), "recorded_at": delivered.isoformat(), "source_kind": "synthetic_demo"}}
        bundle = {"status": "ok", "payment_version": doc["version"], "rules": doc["rules"], "rule_citations": doc["rule_citations"],
                  "applicability": {"store_id": "S001", "category": "ordinary_shoes", "selection_basis": "order_paid_at",
                                    "effective_from": doc["effective_from"], "effective_before": doc["effective_before"]}}
        return context, bundle, delivered

    def test_parser_and_service_accept_same_long_window_and_exact_boundary(self):
        self.assertEqual(MAX_RETURN_WINDOW_HOURS, 8760)
        for hours in (2161, MAX_RETURN_WINDOW_HOURS):
            context, bundle, delivered = self.context_and_bundle(hours)
            deadline = delivered + timedelta(hours=hours)
            for now, expected in ((deadline - timedelta(microseconds=1), "eligible_with_self_report"), (deadline, "ineligible")):
                with self.subTest(hours=hours, now=now):
                    result = evaluate_buyer_support(context, bundle, claims={"is_unworn": True, "tags_intact": True}, now=now, topics=["eligibility"])
                    self.assertEqual(result["status"], "ok")
                    self.assertEqual(result["eligibility"], expected)
                    self.assertEqual(result["return_window"]["hours"], hours)

    def test_outside_shared_limit_is_rejected_by_parser_and_service(self):
        context, bundle, delivered = self.context_and_bundle(168)
        for hours in (0, MAX_RETURN_WINDOW_HOURS + 1):
            with self.subTest(hours=hours):
                with self.assertRaises(PolicyDocumentError):
                    parse_policy_text(self.source.replace("168小时", str(hours) + "小时"))
                altered = deepcopy(bundle)
                altered["rules"]["return_window_hours"] = hours
                result = evaluate_buyer_support(context, altered, claims={"is_unworn": True, "tags_intact": True}, now=delivered, topics=["eligibility"])
                self.assertEqual(result["status"], "insufficient_evidence")

    def test_existing_168_and_336_hour_rules_remain_unchanged(self):
        for hours in (168, 336):
            context, bundle, delivered = self.context_and_bundle(hours)
            result = evaluate_buyer_support(context, bundle, claims={"is_unworn": True, "tags_intact": True}, now=delivered, topics=["eligibility"])
            self.assertEqual(result["eligibility"], "eligible_with_self_report")
            self.assertEqual(result["return_window"]["hours"], hours)


if __name__ == "__main__":
    unittest.main()
