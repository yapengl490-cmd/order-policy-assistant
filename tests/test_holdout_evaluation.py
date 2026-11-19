"""评判器离线单元测试：人工假记录，仅验证判分机制，绝非真实模型验收。"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from holdout_evaluation import (DIMENSIONS, _amounts, _subset, aggregate_runs,
    apply_manual_review, evaluate_record, load_suite, public_runs, record_sha256,
    verify_commitment, preflight_suite, _preflight_data_fingerprint)


def sample():
    fact = {"tool": "check_order", "arguments": {"order_id": "T_ONLY", "refund_before": "2026-01-04T00:00:00+08:00"},
            "result": {"status": "ok", "order_id": "T_ONLY", "paid_amount_fen": 4500,
                       "refund_amount_fen": 1200, "net_amount_fen": 3300}}
    case = {"id": "UNIT", "role": "buyer", "request": {"message": "测试问题", "execution_mode": "single"},
            "resource_limits": {"model_requests": 4, "tool_calls": 6},
            "private_expectations": {"statuses": ["completed"], "required_facts": [deepcopy(fact)],
                "allowed_order_ids": ["T_ONLY"], "answer_money_fen": [4500, 1200, 3300],
                "allowed_answer_money_fen": [4500, 1200, 3300], "max_answer_nonspace_chars": 100}}
    stamp = {"started_at": "2026-01-05T00:00:00+08:00", "finished_at": "2026-01-05T00:00:01+08:00", "elapsed_seconds": 1.0}
    record = {"id": "unit-1", "status": "completed", "answer": "支付45元，退款12元，净支出33元。", "result": {
        "facts": [deepcopy(fact)], "usage": {"model_requests": 2, "tool_calls": 1, "total_tokens": 100, "unmetered_requests": 0},
        "budget": {"elapsed_seconds": 3.2},
        "trace": [{"stage": "model", "request": {"messages": []}, "response": {}, **stamp},
                  {"stage": "tool", **stamp},
                  {"stage": "model", "request": {"messages": []}, "response": {}, **stamp}]}}
    return case, record


class HoldoutEvaluationTests(unittest.TestCase):
    def test_frozen_manifest_and_sources_match(self):
        self.assertEqual(verify_commitment()["status"], "passed")
        suite = load_suite()
        self.assertEqual(len(suite["cases"]), 6)
        self.assertEqual(len(public_runs(suite)), 12)

    def test_public_input_excludes_private_fields_and_does_not_mutate_suite(self):
        suite = {"cases": [sample()[0]]}
        exposed = public_runs(suite)
        self.assertEqual(set(exposed[0]), {"case_id", "repeat", "role", "request"})
        self.assertNotIn("private_expectations", json.dumps(exposed))
        exposed[0]["request"]["message"] = "changed"
        self.assertEqual(suite["cases"][0]["request"]["message"], "测试问题")
        for repeats in (0, 3, True):
            with self.assertRaises(ValueError):
                public_runs(suite, repeats)

    def test_complete_record_and_chinese_money_labels(self):
        case, record = sample()
        result = evaluate_record(case, record)
        self.assertEqual(result["decision"], "passed", result)
        self.assertEqual(set(result["dimensions"]), set(DIMENSIONS))
        self.assertEqual([int(v) for v in _amounts("实付45元、退款1200分。2026-01-01，耗时3分钟")], [4500, 1200])

    def test_timezone_equivalence_and_missing_condition_do_not_pass(self):
        self.assertTrue(_subset({"refund_before": "2026-01-03T16:00:00Z"}, {"refund_before": "2026-01-04T00:00:00+08:00"}))
        self.assertFalse(_subset({"refund_before": "2026-01-04T00:00:00"}, {"refund_before": "2026-01-04T00:00:00+08:00"}))
        case, record = sample()
        record["result"]["facts"][0]["arguments"]["refund_before"] = "2026-01-05T00:00:00+08:00"
        self.assertEqual(evaluate_record(case, record)["dimensions"]["conditions"]["status"], "failed")

    def test_correct_numbers_under_wrong_labels_are_rejected(self):
        case, record = sample()
        record["answer"] = "支付45元，退款33元，净支出12元。"
        report = evaluate_record(case, record)
        self.assertEqual(report["dimensions"]["answer_fidelity"]["status"], "failed")

    def test_missing_units_or_ambiguous_labels_require_review(self):
        case, record = sample()
        record["answer"] = "三项金额分别是45、12、33。"
        self.assertEqual(evaluate_record(case, record)["decision"], "needs_review")

    def test_fabricated_number_and_wrong_fact_fail(self):
        case, record = sample()
        record["answer"] += "另有999元。"
        self.assertEqual(evaluate_record(case, record)["decision"], "failed")
        record = sample()[1]
        record["result"]["facts"][0]["result"]["net_amount_fen"] = 4500
        self.assertEqual(evaluate_record(case, record)["dimensions"]["facts"]["status"], "failed")

    def test_cross_scope_record_is_hard_failure(self):
        case, record = sample()
        record["result"]["facts"].append({"tool": "list_orders", "result": {"orders": [{"order_id": "OUTSIDE"}]}})
        report = evaluate_record(case, record)
        self.assertTrue(report["security_failure"])
        self.assertEqual(report["decision"], "failed")

    def test_not_found_response_cannot_leak_details(self):
        case, record = sample()
        case["private_expectations"] = {"statuses": ["completed"], "deny_business_details": True, "max_answer_nonspace_chars": 100}
        record["answer"] = "当前范围未找到该订单。"
        record["result"]["facts"][0]["result"] = {"status": "not_found", "order_id": "T_ONLY", "buyer_id": "OTHER"}
        self.assertTrue(evaluate_record(case, record)["security_failure"])

    def test_citation_requires_actual_source_and_version(self):
        case, record = sample()
        case["private_expectations"]["citations"] = {"DEMO-TEST-v1": "1.0"}
        record["answer"] += "依据DEMO-TEST-v1版本1.0。"
        self.assertEqual(evaluate_record(case, record)["dimensions"]["citations"]["status"], "failed")
        record["result"]["facts"][0]["result"]["policies"] = [
            {"citation_id": "DEMO-TEST-v1", "version": "1.0", "text": "原文", "source": "单元测试"}]
        self.assertEqual(evaluate_record(case, record)["dimensions"]["citations"]["status"], "passed")

    def test_brevity_counts_entire_answer_and_preserves_raw_record(self):
        case, record = sample()
        record["answer"] += "重复内容" * 30
        original = deepcopy(record)
        report = evaluate_record(case, record)
        self.assertEqual(report["dimensions"]["brevity"]["status"], "failed")
        self.assertEqual(record, original)
        self.assertGreater(report["metrics"]["answer_nonspace_chars"], report["metrics"]["answer_han_chars"])

    def test_trace_calls_and_resources_are_independently_checked(self):
        case, record = sample()
        record["result"]["usage"]["tool_calls"] = 7
        report = evaluate_record(case, record)
        self.assertEqual(report["dimensions"]["resources"]["status"], "failed")
        self.assertEqual(report["dimensions"]["trace"]["status"], "failed")

    def test_clarification_must_not_have_tool_or_fabricated_amount(self):
        case, record = sample()
        case["private_expectations"] = {"statuses": ["clarification"], "no_tools": True, "max_answer_nonspace_chars": 100}
        record["status"] = "clarification"
        record["answer"] = "请补退款截止时刻。"
        record["result"].update(facts=[], trace=[], usage={"model_requests": 0, "tool_calls": 0})
        self.assertEqual(evaluate_record(case, record)["decision"], "passed")
        record["answer"] += "目前净额33元。"
        self.assertEqual(evaluate_record(case, record)["decision"], "failed")

    def test_comparison_checks_actual_fact_not_answer_only(self):
        case, record = sample()
        case["private_expectations"]["comparison"] = {"first": {"net_amount_fen": 3300},
            "second": {"net_amount_fen": 5000}, "delta": {"net_amount_fen": 1700},
            "conditions": {"refund_before": "2026-02-01T00:00:00+08:00"}, "reason_status": "unknown"}
        self.assertEqual(evaluate_record(case, record)["dimensions"]["facts"]["status"], "failed")
        record["result"]["comparison"] = deepcopy(case["private_expectations"]["comparison"])
        self.assertEqual(evaluate_record(case, record)["dimensions"]["facts"]["status"], "passed")

    def test_repeated_statistics_require_different_tasks_and_fingerprints(self):
        case, record = sample()
        suite = {"version": "unit", "scope": "unit", "cases": [case]}
        first = {"case_id": "UNIT", "repeat": 1, "record": record,
                 "config_fingerprint": "a" * 64, "data_fingerprint": "b" * 64}
        second = deepcopy(first); second["repeat"] = 2; second["record"]["id"] = "unit-2"
        result = aggregate_runs(suite, [first, second])
        self.assertEqual(result["decision"], "needs_review")
        self.assertEqual(len(result["manual_review_missing"]), 2)
        self.assertEqual(result["cases"]["UNIT"]["metrics"]["model_requests"]["mean"], 2)
        self.assertEqual(aggregate_runs(suite, [first])["decision"], "incomplete")
        second["record"]["id"] = "unit-1"
        self.assertTrue(aggregate_runs(suite, [first, second])["integrity_issues"])

    def test_security_failure_cannot_be_averaged_away(self):
        case, record = sample()
        suite = {"version": "unit", "scope": "unit", "cases": [case]}
        runs = [{"case_id": "UNIT", "repeat": i, "record": deepcopy(record),
                 "config_fingerprint": "a" * 64, "data_fingerprint": "b" * 64} for i in (1, 2)]
        runs[1]["record"]["id"] = "unit-2"
        runs[1]["record"]["result"]["facts"][0]["result"]["order_id"] = "OUTSIDE"
        report = aggregate_runs(suite, runs)
        self.assertEqual(report["decision"], "failed")
        self.assertEqual(report["security_failures"], [{"case_id": "UNIT", "repeat": 2}])

    def test_manual_review_bound_to_raw_record_and_cannot_hide_hard_failure(self):
        case, record = sample()
        case["private_expectations"]["needs_semantic_review"] = True
        automatic = evaluate_record(case, record)
        manual = {"reviewer": "unit-test", "reviewed_at": "2026-01-05T00:00:02Z", "record_sha256": record_sha256(record),
                  "dimensions": {"answer_fidelity": {"status": "passed", "reason": "单元测试语义", "evidence": "unit-only"}}}
        resolved = apply_manual_review(automatic, record, manual)
        self.assertEqual(resolved["decision"], "passed")
        self.assertEqual(resolved["automatic_decision"], "needs_review")
        record["answer"] += "偷偷更改"
        self.assertIn("manual_review_error", apply_manual_review(automatic, record, manual))
        record["result"]["facts"][0]["result"]["order_id"] = "OUTSIDE"
        manual["record_sha256"] = record_sha256(record)
        self.assertEqual(apply_manual_review(evaluate_record(case, record), record, manual)["decision"], "failed")

    def test_human_can_find_semantic_failure_even_when_rules_pass(self):
        case, record = sample()
        automatic = evaluate_record(case, record)
        self.assertEqual(automatic["decision"], "passed")
        manual = {"reviewer": "unit-test", "reviewed_at": "2026-01-05T00:00:02Z", "record_sha256": record_sha256(record),
                  "dimensions": {"answer_fidelity": {"status": "failed", "reason": "规则外语义错误", "evidence": "unit-only"}}}
        reviewed = apply_manual_review(automatic, record, manual)
        self.assertEqual(reviewed["decision"], "failed")
        self.assertEqual(reviewed["automatic_decision"], "passed")

    def test_invalid_record_is_reported_not_crashed(self):
        case, _ = sample()
        self.assertEqual(evaluate_record(case, None)["decision"], "failed")

    def test_preflight_is_explicit_readonly_and_detects_drift(self):
        suite = load_suite()
        required = [f for c in suite["cases"] for f in c["private_expectations"].get("required_facts", [])]
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value.execute.return_value.first.return_value = ("O_DEMO_003", "B_DEMO_003", "S002")
        comparison = next(c["private_expectations"]["comparison"] for c in suite["cases"] if "comparison" in c["private_expectations"])
        calls = 0
        def lookup(_engine, oid, _scope, cutoff):
            nonlocal calls
            calls += 1
            value = deepcopy(next(f["result"] for f in required if f["tool"] == "check_order"
                                  and f["arguments"]["order_id"] == oid
                                  and f["arguments"]["refund_before"] == cutoff.isoformat()))
            value["provenance"] = {"status": "unknown", "observed_at": f"2026-01-05T00:00:{calls:02d}+00:00"}
            return value
        with patch("refund_service.check_order", side_effect=lookup), patch("merchant_service.check_shop_order", side_effect=lookup), \
             patch("sales_analysis.compare_periods", return_value=comparison), patch("agent_runtime.httpx.Client", side_effect=AssertionError("预检不能调用网络")):
            first = preflight_suite(suite, engine)
            second = preflight_suite(suite, engine)
            self.assertEqual(first["status"], "passed", first)
            self.assertEqual(first["data_fingerprint"], second["data_fingerprint"])
            engine.begin.assert_not_called()
            engine.connect.return_value.__enter__.return_value.execute.return_value.first.return_value = None
            self.assertEqual(preflight_suite(suite, engine)["status"], "failed")

    def test_preflight_observation_time_changes_do_not_change_data_fingerprint(self):
        before = [{"result": {"paid_amount_fen": 4500,
            "provenance": {"observed_at": "2026-01-05T00:00:00Z", "status": "unknown"}}},
            {"comparison": {"provenance": {"observed_at": "2026-01-05T00:00:00Z", "batches": []}}}]
        after = deepcopy(before)
        after[0]["result"]["provenance"]["observed_at"] = "2026-01-05T00:00:01Z"
        after[1]["comparison"]["provenance"]["observed_at"] = "2026-01-05T00:00:02Z"
        # 旧实现直接hash完整观察对象，会把纯读取时刻变化误当数据变化。
        self.assertNotEqual(record_sha256(before), record_sha256(after))
        self.assertEqual(_preflight_data_fingerprint(before), _preflight_data_fingerprint(after))
        self.assertIn("observed_at", before[0]["result"]["provenance"])
        self.assertIn("observed_at", after[1]["comparison"]["provenance"])

    def test_preflight_keeps_money_business_times_provenance_versions_and_scope(self):
        original = [{"result": {"paid_amount_fen": 4500, "paid_at": "2026-01-01T00:00:00Z",
            "refunds": [{"refunded_at": "2026-01-02T00:00:00Z"}], "shop_id": "S_UNIT",
            "observed_at": "unrelated-observation-must-remain",
            "provenance": {"observed_at": "2026-01-05T00:00:00Z", "batches": [
                {"version": "v1", "imported_at": "2026-01-03T00:00:00Z", "batch_sha256": "a" * 64}]}}},
            {"cross_scope_fixture_identity": ["ORDER", "BUYER", "SHOP"]}]
        edits = [
            lambda v: v[0]["result"].update(paid_amount_fen=4501),
            lambda v: v[0]["result"].update(paid_at="2026-01-01T00:00:01Z"),
            lambda v: v[0]["result"]["refunds"][0].update(refunded_at="2026-01-02T00:00:01Z"),
            lambda v: v[0]["result"]["provenance"]["batches"][0].update(version="v2"),
            lambda v: v[0]["result"]["provenance"]["batches"][0].update(imported_at="2026-01-03T00:00:01Z"),
            lambda v: v[0]["result"]["provenance"]["batches"][0].update(batch_sha256="b" * 64),
            lambda v: v[0]["result"].update(shop_id="S_OTHER"),
            lambda v: v[1].update(cross_scope_fixture_identity=["ORDER", "BUYER", "OTHER"]),
            lambda v: v[0]["result"].update(observed_at="not-provenance-must-still-affect-hash"),
        ]
        expected = _preflight_data_fingerprint(original)
        for index, edit in enumerate(edits):
            with self.subTest(index=index):
                changed = deepcopy(original)
                edit(changed)
                self.assertNotEqual(_preflight_data_fingerprint(changed), expected)


if __name__ == "__main__":
    unittest.main()
