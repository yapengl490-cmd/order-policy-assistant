"""Meaningful counterexamples for the bounded evaluator; no model/database/secret access."""
from copy import deepcopy
import hashlib
import json
import unittest

from task_evaluation import evaluate_case, load_cases, model_input


def order_result(cutoff="2026-11-03T00:00:00+08:00", changed=False):
    refunds = [{"refund_id": "R001", "refunded_at": "2026-10-20T10:00:00+08:00",
                "refund_amount_fen": 2500}]
    if not changed:
        refunds.append({"refund_id": "R002", "refunded_at": "2026-11-02T10:00:00+08:00",
                        "refund_amount_fen": 1500})
    return {"status": "completed",
            "answer": "支付125元，退款25元，净支出100元。" if changed else "支付125元，退款40元，净支出85元。",
            "facts": [{"tool": "check_order",
                       "arguments": {"order_id": "O001", "refund_before": cutoff},
                       "result": {"status": "ok", "order_id": "O001", "refund_before": cutoff,
                                  "paid_amount_fen": 12500, "refund_amount_fen": 2500 if changed else 4000,
                                  "net_amount_fen": 10000 if changed else 8500,
                                  "refund_count": len(refunds), "refunds": refunds}}],
            "usage": {"tool_calls": 1}, "trace": [],
            "evidence_status": "tool_results_available", "answer_semantics_verified": False}


class TaskEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_cases()
        cls.cases = {case["id"]: case for case in cls.bundle["cases"]}

    def judge(self, case_id, result):
        return evaluate_case(self.cases[case_id], result)

    def test_six_cases_and_input_does_not_expose_expectations_or_scope(self):
        self.assertEqual(len(self.cases), 6)
        case = self.cases["buyer_order_normal"]
        visible = model_input(case)
        self.assertEqual(set(visible), {"message", "history"})
        self.assertNotIn("private_expectations", visible)
        self.assertNotIn("trusted_scope", visible)
        visible["history"].append({"role": "user", "content": "mutation"})
        self.assertEqual(case["input"]["history"], [])

    def test_real_amounts_pass_but_fake_999_answer_fails_independent_dimension(self):
        result = order_result()
        good = self.judge("buyer_order_normal", result)
        self.assertTrue(good["passed"], good)
        result["answer"] = "支付125元，退款40元，净支出999元。"
        bad = self.judge("buyer_order_normal", result)
        self.assertFalse(bad["passed"])
        self.assertTrue(bad["dimensions"]["program_facts"]["passed"])
        self.assertFalse(bad["dimensions"]["answer_money"]["passed"])

    def test_followup_requires_actual_previous_history(self):
        case = self.cases["buyer_cutoff_changed"]
        with self.assertRaises(ValueError):
            model_input(case)
        prior = {"history": [{"role": "user", "content": "previous actual question"},
                             {"role": "assistant", "content": "previous actual answer"}]}
        visible = model_input(case, prior)
        self.assertEqual(visible["history"], prior["history"])
        visible["history"][0]["content"] = "changed"
        self.assertEqual(prior["history"][0]["content"], "previous actual question")

    def test_program_money_error_is_not_excused_by_correct_answer(self):
        result = order_result()
        result["facts"][0]["result"]["net_amount_fen"] = 99900
        report = self.judge("buyer_order_normal", result)
        self.assertFalse(report["dimensions"]["program_facts"]["passed"])
        self.assertTrue(report["dimensions"]["answer_money"]["passed"])
        self.assertFalse(report["passed"])

    def test_cutoff_followup_must_requery_new_conditions(self):
        fresh = order_result("2026-11-01T00:00:00+08:00", changed=True)
        self.assertTrue(self.judge("buyer_cutoff_changed", fresh)["passed"])
        stale = order_result()
        stale["answer"] = fresh["answer"]
        report = self.judge("buyer_cutoff_changed", stale)
        self.assertFalse(report["dimensions"]["conditions"]["passed"])
        self.assertFalse(report["dimensions"]["program_facts"]["passed"])

    def test_equivalent_timezone_and_fen_units_pass(self):
        result = order_result("2026-11-02T16:00:00+00:00")
        result["answer"] = "支付金额12500分，退款合计4000分，净额8500分。"
        self.assertTrue(self.judge("buyer_order_normal", result)["passed"])

    def test_clarification_requires_no_call_and_no_false_success(self):
        result = {"status": "clarification", "answer": "请补充退款截止时间。",
                  "facts": [], "usage": {"tool_calls": 0}, "trace": [],
                  "evidence_status": "no_tool_evidence"}
        self.assertTrue(self.judge("summary_missing_cutoff", result)["passed"])
        result["usage"]["tool_calls"] = 1
        self.assertFalse(self.judge("summary_missing_cutoff", result)["dimensions"]["conditions"]["passed"])
        result["usage"]["tool_calls"] = 0
        result["answer"] += "已查到支付125元、退款40元、净额85元。"
        report = self.judge("summary_missing_cutoff", result)
        self.assertFalse(report["dimensions"]["evidence_claims"]["passed"])

    def test_completed_without_tool_facts_cannot_pass(self):
        result = order_result()
        result["facts"] = []
        result["evidence_status"] = "no_tool_evidence"
        report = self.judge("buyer_order_normal", result)
        self.assertFalse(report["passed"])
        self.assertFalse(report["dimensions"]["evidence_claims"]["passed"])

    def test_summary_after_clarification_checks_order_set_and_scope(self):
        args = {"paid_from": "2026-10-01T00:00:00+08:00",
                "paid_before": "2026-11-01T00:00:00+08:00",
                "refund_before": "2026-11-03T00:00:00+08:00"}
        result = {"status": "completed", "answer": "支付125元，退款40元，净额85元。",
                  "facts": [{"tool": "summary_orders", "arguments": args,
                             "result": {"status": "ok", **args, "order_count": 1,
                                        "paid_amount_fen": 12500, "refund_amount_fen": 4000,
                                        "net_amount_fen": 8500, "orders": [{"order_id": "O001"}]}}],
                  "usage": {"tool_calls": 1}, "evidence_status": "tool_results_available"}
        self.assertTrue(self.judge("summary_after_clarification", result)["passed"])
        result["facts"][0]["result"]["orders"].append({"order_id": "O_DEMO_003"})
        report = self.judge("summary_after_clarification", result)
        self.assertFalse(report["dimensions"]["access_scope"]["passed"])
        self.assertFalse(report["passed"])

    def test_merchant_not_found_is_allowed_but_hidden_details_fail(self):
        result = {"status": "completed", "answer": "当前授权店铺范围内未找到O_DEMO_003。",
                  "facts": [{"tool": "check_order",
                             "arguments": {"order_id": "O_DEMO_003", "refund_before": "2026-11-03T00:00:00+08:00"},
                             "result": {"status": "not_found", "order_id": "O_DEMO_003"}}],
                  "evidence_status": "tool_results_available", "usage": {"tool_calls": 1}}
        self.assertTrue(self.judge("merchant_outside_shop", result)["passed"])
        result["facts"][0]["result"]["paid_amount_fen"] = 18000
        report = self.judge("merchant_outside_shop", result)
        self.assertFalse(report["dimensions"]["access_scope"]["passed"])
        self.assertFalse(report["passed"])

    def test_policy_citation_and_no_real_eligibility_claim(self):
        result = {"status": "completed",
                  "answer": "依据合成演示资料DEMO-SUPPORT-2026-v1（版本1.0），没有真实售后资格依据，无法判断是否可退。",
                  "facts": [{"tool": "search_policies", "arguments": {"query": "退货"},
                             "result": {"status": "ok", "scope": "synthetic_demo",
                                        "policies": [{"citation_id": "DEMO-SUPPORT-2026-v1", "version": "1.0",
                                                      "source": "助手合成演示", "text": "没有真实退款申请资格依据。"}]}}],
                  "evidence_status": "tool_results_available", "usage": {"tool_calls": 1}}
        self.assertTrue(self.judge("policy_insufficient_eligibility", result)["passed"])
        fabricated = deepcopy(result)
        fabricated["answer"] += "你可以申请七天无理由退货。"
        self.assertFalse(self.judge("policy_insufficient_eligibility", fabricated)["dimensions"]["evidence_claims"]["passed"])
        wrong_citation = deepcopy(result)
        wrong_citation["answer"] += "另见DEMO-FAKE-2026-v9（版本9.0）。"
        self.assertFalse(self.judge("policy_insufficient_eligibility", wrong_citation)["dimensions"]["answer_citations"]["passed"])
        no_version = deepcopy(result)
        no_version["answer"] = no_version["answer"].replace("（版本1.0）", "")
        self.assertFalse(self.judge("policy_insufficient_eligibility", no_version)["dimensions"]["answer_citations"]["passed"])

    def test_wrong_termination_or_malformed_result_fails(self):
        result = order_result()
        result["status"] = "running"
        self.assertFalse(self.judge("buyer_order_normal", result)["dimensions"]["termination"]["passed"])
        self.assertFalse(self.judge("buyer_order_normal", None)["passed"])
        result = order_result()
        result["facts"] = [{"tool": "check_order", "result": None}]
        self.assertFalse(self.judge("buyer_order_normal", result)["passed"])

    def test_task_envelope_uses_outer_terminal_state_and_does_not_mutate(self):
        inner = order_result()
        original = deepcopy(inner)
        report = self.judge("buyer_order_normal", {"status": "failed", "answer": inner["answer"], "result": inner})
        self.assertFalse(report["dimensions"]["termination"]["passed"])
        self.assertEqual(inner, original)

    def test_contradictory_amounts_and_float_facts_rejected(self):
        result = order_result()
        result["answer"] += "净额999元。"
        self.assertFalse(self.judge("buyer_order_normal", result)["dimensions"]["answer_money"]["passed"])
        result = order_result()
        result["facts"][0]["result"]["net_amount_fen"] = 8500.0
        self.assertFalse(self.judge("buyer_order_normal", result)["dimensions"]["program_facts"]["passed"])

    def test_refund_count_is_not_amount_but_explicit_total_is(self):
        result = order_result()
        result["answer"] = "支付125元。成功退款：共 2 笔，合计 4000 分（40元）。净额85元。"
        self.assertTrue(self.judge("buyer_order_normal", result)["passed"])
        result["answer"] = result["answer"].replace("4000", "4100")
        self.assertFalse(self.judge("buyer_order_normal", result)["dimensions"]["answer_money"]["passed"])

    def test_old_new_comparison_needs_real_history_not_old_assistant_prose(self):
        result = order_result("2026-11-01T00:00:00+08:00", changed=True)
        result["answer"] = "支付125元。退款由4000分变为2500分，净额从8500分变成10000分。"
        result["history"] = [{"role": "assistant", "content": "以前退款4000分净额8500分"}]
        unknown = self.judge("buyer_cutoff_changed", result)
        self.assertEqual(unknown["decision"], "needs_review")
        self.assertFalse(unknown["passed"])
        self.assertEqual(unknown["dimensions"]["answer_money"]["errors"], [])
        fact = deepcopy(order_result()["facts"][0])
        fact["result_snapshot_sha256"] = hashlib.sha256(json.dumps(fact["result"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        result["historical_evidence"] = [{"evidence_kind": "historical_tool_snapshot", "is_current_database_fact": False,
            "source_task_id": "a"*32, "task_completed_at": "2026-11-17T12:00:00+08:00", "facts": [fact]}]
        self.assertTrue(self.judge("buyer_cutoff_changed", result)["passed"])
        wrong_old = deepcopy(result);wrong_old["answer"] = wrong_old["answer"].replace("4000", "9999")
        self.assertEqual(self.judge("buyer_cutoff_changed", wrong_old)["decision"], "failed")
        wrong_new = deepcopy(result);wrong_new["answer"] = wrong_new["answer"].replace("2500", "9999")
        self.assertEqual(self.judge("buyer_cutoff_changed", wrong_new)["decision"], "failed")
        result["answer"] += "本轮净额其实999元。"
        self.assertEqual(self.judge("buyer_cutoff_changed", result)["decision"], "failed")

    def test_tampered_history_checksum_does_not_clear_review(self):
        result = order_result("2026-11-01T00:00:00+08:00", changed=True)
        result["answer"] += "退款由40元变为25元。"
        fact=deepcopy(order_result()["facts"][0]);fact["result_snapshot_sha256"]="fake"
        result["historical_evidence"]=[{"evidence_kind":"historical_tool_snapshot","is_current_database_fact":False,
            "source_task_id":"a"*32,"task_completed_at":"2026-11-17T12:00:00+08:00","facts":[fact]}]
        self.assertTrue(self.judge("buyer_cutoff_changed", result)["needs_review"])
        fact["result"]["order_id"]="ANOTHER_ORDER"
        fact["result_snapshot_sha256"]=hashlib.sha256(json.dumps(fact["result"],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        self.assertTrue(self.judge("buyer_cutoff_changed", result)["needs_review"])
        result["historical_evidence"]=None
        self.assertTrue(self.judge("buyer_cutoff_changed", result)["needs_review"])

    def test_unrecognized_amount_expression_is_review_not_success(self):
        result = order_result();result["answer"] = "支付一百二十五元，退款四十元，净额八十五元。"
        report=self.judge("buyer_order_normal", result)
        self.assertEqual(report["decision"], "needs_review")
        self.assertFalse(report["passed"])

    def test_denial_is_not_promise_but_positive_promise_still_fails(self):
        case=deepcopy(self.cases["policy_insufficient_eligibility"])
        result={"status":"completed","answer":"合成演示DEMO-SUPPORT-2026-v1（版本1.0）证据不足，无法确认资格。",
            "facts":[{"tool":"search_policies","arguments":{"query":"退货"},"result":{"status":"ok","scope":"synthetic_demo",
                "policies":[{"citation_id":"DEMO-SUPPORT-2026-v1","version":"1.0","source":"演示","text":"没有资格依据"}]}}],
            "evidence_status":"tool_results_available"}
        denied=deepcopy(result);denied["answer"] += "我不能承诺退款资格或到账时间。"
        self.assertTrue(evaluate_case(case,denied)["passed"])
        promised=deepcopy(result);promised["answer"] += "我保证退款到账。"
        self.assertEqual(evaluate_case(case,promised)["decision"],"failed")
        mixed=deepcopy(result);mixed["answer"] += "我不能保证退款，但我保证到账。"
        self.assertEqual(evaluate_case(case,mixed)["decision"],"failed")
        for phrase in ('商家说“保证退款到账”。','我并非不能承诺退款。','我可能承诺退款。'):
            uncertain=deepcopy(result);uncertain["answer"] += phrase
            report=evaluate_case(case,uncertain)
            self.assertTrue(report["needs_review"],phrase)
            self.assertFalse(report["passed"],phrase)

    def test_access_dimension_has_noncredential_name(self):
        dimensions=self.judge("buyer_order_normal",order_result())["dimensions"]
        self.assertIn("access_scope",dimensions)
        self.assertNotIn("authorization",dimensions)


if __name__ == "__main__":
    unittest.main()

