"""退款调查助手离线回归：真实临时库＋模型替身，未新增外部请求。"""
import copy
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

import httpx

from execution_budget import ExecutionBudget
from refund_investigation import investigate_refund_increase
from refund_investigation_runtime import run_refund_investigation, _validate_selection, _pending_catalog
from test_buyer_support_service import SupportFixture, ROOT, NOW
from test_period_analysis_runtime import config, response


def selection(context, path="reason_breakdown.verified_size.delta_refund_amount_fen"):
    ref, row = next((ref, row) for ref, row in context["evidence"].items() if row["path"] == path)
    return {"focus": [{"evidence_id": ref, "record_level": row["record_level"]}],
            "pending_check_ids": [next(iter(context["pending_check_options"]))], "reason_status": "unknown"}


class RefundInvestigationRuntimeTests(SupportFixture, unittest.TestCase):
    def setUp(self):
        self.setup_support()
        expected = json.loads((ROOT / "samples/support/expected.json").read_text())["r05"]
        self.conditions = expected["conditions"]
        self.times = {key: datetime.fromisoformat(value) for key, value in self.conditions.items()}
        self.result = investigate_refund_increase(self.engine, {"role": "merchant", "shop_id": "S001"}, **self.times, observed_at=NOW)
        self.requests, self.loads = [], []

    def run_case(self, make=None, *, cfg=None, result=None, budget=None, cancel=lambda: False,
                 preference=None, real_tool=False, conditions=None, history=None):
        def send(request):
            payload = json.loads(request.content)
            self.requests.append(payload)
            context = json.loads(payload["messages"][1]["content"])
            if isinstance(make, Exception):
                raise make
            data = make(context) if make is not None else response(json.dumps(selection(context), ensure_ascii=False))
            return data if isinstance(data, httpx.Response) else httpx.Response(200, json=data)
        def read(**kwargs):
            self.loads.append(kwargs)
            if isinstance(result, Exception):
                raise result
            return copy.deepcopy(self.result if result is None else result)
        return run_refund_investigation(cfg or config(), self.engine if real_tool else None,
            "两期退款增加是否有尺码原因记录支持？", "unused", history or [], cancel,
            access_role="merchant", shop_id="S001", period_conditions=self.conditions if conditions is None else conditions,
            preference_snapshot=preference, shared_budget=budget, transport=httpx.MockTransport(send),
            tool_handlers=None if real_tool else {"investigate_refund_increase": read})

    def test_real_temporary_query_matches_frozen_numbers_without_mutation(self):
        before = self.support_snapshot()
        got = self.run_case(real_tool=True)
        self.assertEqual(got["status"], "completed")
        self.assertEqual(got["facts"][0]["result"]["total_refund_delta_fen"], 3000)
        self.assertEqual(got["facts"][0]["result"]["verified_size_delta_fen"], 2000)
        for word in ("30元", "20元", "核实状态是来源声明", "不能证明尺码导致整体退款增加", "模型选择的证据重点"):
            self.assertIn(word, got["answer"])
        self.assertEqual(got["usage"]["model_requests"], 1)
        self.assertEqual(got["usage"]["tool_calls"], 1)
        self.assertEqual(got["usage"]["total_tokens"], 60)
        self.assertEqual(self.support_snapshot(), before)
        self.assertEqual(self.fixture.all_snapshot(), self.business_snapshot)
        self.assertFalse(got["answer_semantics_verified"])
        self.assertFalse(got["analysis_validation"]["semantic_guarantee"])

    def test_model_input_has_aggregates_not_identity_rows_notes_or_history(self):
        source = copy.deepcopy(self.result)
        source["refund_evidence"][0]["evidence"][0]["note"] = "PRIVATE_NOTE"
        got = self.run_case(result=source, history=[{"role": "user", "content": "PRIVATE_HISTORY"}, {"role": "assistant", "content": "PRIVATE_REPLY"}])
        sent = json.dumps(self.requests, ensure_ascii=False)
        for forbidden in ("buyer_id", "shop_id", "refund_id", "order_id", "R_SUPPORT", "O_SUPPORT", "PRIVATE_NOTE", "PRIVATE_HISTORY", "PRIVATE_REPLY"):
            self.assertNotIn(forbidden, sent)
        self.assertNotIn("sk-offline-analysis-secret", json.dumps(got))
        self.assertEqual(got["analysis_validation"]["snapshot_sha256"], got["facts"][0]["result_snapshot_sha256"])

    def test_wrong_level_can_only_be_fixed_by_second_model_output(self):
        outputs = []
        def make(context):
            data = selection(context, "reason_breakdown.buyer_reported_size.second.refund_amount_fen")
            if not outputs:
                data["focus"][0]["record_level"] = "source_declared_verified"
            content = json.dumps(data)
            outputs.append(content)
            return response(content)
        got = self.run_case(make)
        self.assertEqual(got["status"], "completed")
        self.assertIn("后期买家自述尺码原因退款金额：5元", got["answer"])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.loads), 1)
        self.assertEqual(self.requests[0]["messages"], self.requests[1]["messages"][:2])
        self.assertEqual(self.requests[1]["messages"][2]["content"], outputs[0])
        self.assertEqual([x["accepted"] for x in got["analysis_validation"]["attempts"]], [False, True])
        self.assertEqual([x["response"]["choices"][0]["message"]["content"] for x in got["trace"] if x["stage"] == "model"], outputs)
        self.assertEqual(got["budget"]["used"]["model_requests"], 2)
        self.assertEqual(got["budget"]["used"]["reserved_output_tokens"], 2400)
        self.assertEqual(got["usage"]["total_tokens"], 120)

    def test_unknown_reference_twice_falls_back_partial_no_third_request(self):
        def make(context):
            data = selection(context); data["focus"][0]["evidence_id"] = "WRONG"
            return response(json.dumps(data))
        got = self.run_case(make)
        self.assertEqual(got["status"], "partial")
        self.assertIn(got["fixed_answer"], got["answer"])
        self.assertIn("未通过或未完成", got["answer"])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.loads), 1)
        self.assertIn("invalid_evidence_reference", got["analysis_validation"]["reasons"])
        self.assertNotIn("WRONG", got["answer"])
        self.assertIn("WRONG", json.dumps(got["trace"]))

    def test_free_numeric_or_causal_answer_not_silently_rewritten_as_valid(self):
        def extra(context):
            data = selection(context); data["answer"] = "尺码导致退款增加999元"
            return data
        def causal(context):
            data = selection(context); data["pending_checks"] = ["需检查，因为尺码导致退款增加"]
            return data
        def assertion(context):
            data = selection(context); data["reason_status"] = "verified"
            return data
        for make in (extra, causal, assertion):
            before = len(self.requests)
            got = self.run_case(lambda c: response(json.dumps(make(c), ensure_ascii=False)))
            self.assertEqual(got["status"], "partial")
            self.assertEqual(len(self.requests) - before, 1)
            self.assertIn(got["fixed_answer"], got["answer"])
            self.assertFalse(got["analysis_validation"]["repair_attempted"])

    def test_duplicate_ref_malformed_truncated_and_tool_request_do_not_execute_tools(self):
        def tool(c):
            data = response(json.dumps(selection(c)))
            data["choices"][0]["message"]["tool_calls"] = [{"function": {"name": "refund_now"}}]
            return data
        for make in (lambda c: response("bad-json"), lambda c: response(json.dumps(selection(c)), "length"), tool,
                     lambda c: response('{"focus":[],"focus":[],"pending_checks":[],"reason_status":"unknown"}')):
            before = len(self.requests)
            got = self.run_case(make)
            self.assertEqual(got["status"], "partial")
            self.assertEqual(len(self.requests) - before, 1)
            self.assertEqual(got["usage"]["tool_calls"], 1)

    def test_scope_failure_inconsistent_totals_and_query_error_never_call_model(self):
        scope = copy.deepcopy(self.result); scope["scope"] = {"role": "merchant", "shop_id": "OTHER"}
        totals = copy.deepcopy(self.result); totals["total_refund_delta_fen"] += 1
        level = copy.deepcopy(self.result); level["unverified_or_missing_amount_fen"]["second"] = 0
        for result in (scope, totals, level, RuntimeError("offline failed"), {"status": "reason_data_unavailable"}):
            got = self.run_case(result=result)
            self.assertEqual(got["status"], "failed")
            self.assertEqual(got["facts"], [])
            self.assertIn("没有把失败写成零退款", got["answer"])
            self.assertEqual(got["usage"]["model_requests"], 0)
        self.assertEqual(self.requests, [])

    def test_disabled_model_and_http_failure_preserve_fixed_facts(self):
        got = self.run_case(cfg=config(model_enabled=False))
        self.assertEqual(got["status"], "partial")
        self.assertEqual(got["usage"]["model_requests"], 0)
        self.assertTrue(got["fixed_answer"])
        for make in (httpx.ReadTimeout("offline"), lambda c: httpx.Response(503, json={"error": "test"})):
            before = len(self.requests)
            got = self.run_case(make)
            self.assertEqual(got["status"], "partial")
            self.assertEqual(len(self.requests) - before, 1)
            self.assertEqual(got["usage"]["unmetered_requests"], 1)
            self.assertIn(got["fixed_answer"], got["answer"])

    def test_shared_budget_not_reset_for_repair_and_time_cancel_stop_before_next_call(self):
        cfg = config(max_model_calls=3)
        budget = ExecutionBudget(cfg, lambda: False)
        budget.reserve_model(1200); budget.reserve_model(1200)
        def bad(context):
            data = selection(context); data["focus"][0]["evidence_id"] = "UNKNOWN"
            return response(json.dumps(data))
        got = self.run_case(bad, cfg=cfg, budget=budget)
        self.assertEqual(got["status"], "limited")
        self.assertEqual(got["usage"]["model_requests"], 1)
        self.assertEqual(got["budget"]["used"]["model_requests"], 3)
        for mode in ("cancel", "deadline"):
            cancelled, clock = [False], [0]
            shared = ExecutionBudget(config(), lambda: cancelled[0], clock=lambda: clock[0])
            def check(*args):
                result = _validate_selection(*args)
                if mode == "cancel": cancelled[0] = True
                else: clock[0] = 91
                return result
            before = len(self.requests)
            with patch("refund_investigation_runtime._validate_selection", side_effect=check):
                got = self.run_case(bad, budget=shared)
            self.assertEqual(got["status"], "cancelled" if mode == "cancel" else "limited")
            self.assertEqual(len(self.requests) - before, 1)
            self.assertEqual(got["budget"]["used"]["model_requests"], 1)
            self.assertFalse(got["analysis_validation"]["repair_attempted"])

    def test_cancel_before_query_or_invalid_conditions_do_not_call_model(self):
        got = self.run_case(cancel=lambda: True)
        self.assertEqual(got["status"], "cancelled")
        self.assertEqual(self.loads, [])
        got = self.run_case(conditions={})
        self.assertEqual(got["status"], "clarification")
        self.assertEqual(self.loads, [])
        self.assertEqual(self.requests, [])

    def test_preferences_control_rendered_units_without_mutating_snapshot(self):
        pref = {"role": "merchant", "values": {"amount_format": "fen", "answer_style": "brief"},
                "version": 1, "updated_at": NOW.astimezone(timezone.utc).isoformat(), "source": {"kind": "user_settings"}}
        before = copy.deepcopy(pref)
        got = self.run_case(preference=pref)
        self.assertEqual(got["status"], "completed")
        self.assertIn("3000分", got["answer"])
        self.assertIn("2000分", got["answer"])
        self.assertEqual(pref, before)
        self.assertFalse(got["answer_quality"]["text_truncated"])

    def test_repair_wait_uses_remaining_deadline_and_has_no_third_network_call(self):
        clock, timeouts = [0], []
        budget = ExecutionBudget(config(), lambda: False, clock=lambda: clock[0])
        def send(request):
            timeouts.append(request.extensions["timeout"]["read"])
            context = json.loads(json.loads(request.content)["messages"][1]["content"])
            if len(timeouts) == 1:
                clock[0] = 85
                data = selection(context); data["focus"][0]["record_level"] = "incorrect_level"
                return httpx.Response(200, json=response(json.dumps(data)))
            raise httpx.ReadTimeout("offline repair timeout")
        got = run_refund_investigation(config(), None, "核对退款记录", "unused", [], lambda: False,
            access_role="merchant", shop_id="S001", period_conditions=self.conditions, shared_budget=budget,
            transport=httpx.MockTransport(send), tool_handlers={"investigate_refund_increase": lambda **kw: self.result})
        self.assertEqual(timeouts, [30, 5])
        self.assertEqual(got["status"], "partial")
        self.assertEqual(got["usage"]["model_requests"], 2)
        self.assertEqual(got["usage"]["unmetered_requests"], 1)
        self.assertEqual(got["trace"][-1]["error_type"], "ReadTimeout")
        self.assertIn(got["fixed_answer"], got["answer"])

    def test_duplicate_focus_and_unknown_id_still_rejected_without_dropping_them(self):
        def make(context):
            data = selection(context); data["focus"] *= 2
            return response(json.dumps(data))
        got = self.run_case(make)
        self.assertEqual(got["status"], "partial")
        self.assertIn("invalid_evidence_reference", got["analysis_validation"]["reasons"])

        self.assertEqual(len(self.requests), 2)
        for event in got["trace"]:
            if event["stage"] == "model":
                self.assertEqual(len(json.loads(event["response"]["choices"][0]["message"]["content"])["focus"]), 2)

    def test_pending_text_assertion_repro_is_rejected_and_raw_preserved(self):
        assertion='需补充记录，尺码不合适是退款增加的主要原因。'
        for legacy in (True,False):
            def make(context):
                data=selection(context)
                if legacy:
                    del data['pending_check_ids'];data['pending_checks']=[assertion]
                else:
                    data['pending_check_ids']=[assertion]
                return response(json.dumps(data,ensure_ascii=False))
            got=self.run_case(make)
            self.assertEqual(got['status'],'partial')
            self.assertNotIn(assertion,got['answer'])
            self.assertIn(assertion,json.dumps(got['trace'],ensure_ascii=False))
            self.assertEqual(got['usage']['model_requests'],1)

    def test_pending_options_only_include_present_evidence_categories(self):
        got=self.run_case(real_tool=True)
        options=got['investigation_pending_options']
        self.assertEqual(set(options),{'missing_reason','buyer_reports','declared_verification'})
        self.assertEqual(got['analysis_validation']['checks'][-1],'fact_bound_pending_ids')
        for option in options.values():
            self.assertTrue(option['evidence_ids'])
            for ref in option['evidence_ids']:
                self.assertGreater(got['analysis_evidence'][ref]['value'],0)
                self.assertIn(got['analysis_evidence'][ref]['period'],{'first','second'})
        sent=json.loads(self.requests[-1]['messages'][1]['content'])
        self.assertEqual(sent['pending_check_options'],options)
        self.assertIn(options['missing_reason']['text'],got['answer'])

    def test_unknown_duplicate_and_nonstring_pending_ids_are_rejected(self):
        for ids in (['conflicting_reasons'],['missing_reason','missing_reason'],[{'text':'需核对'}],[]):
            def make(context):
                data=selection(context);data['pending_check_ids']=ids
                return response(json.dumps(data,ensure_ascii=False))
            got=self.run_case(make)
            self.assertEqual(got['status'],'partial')
            self.assertFalse(got['analysis_validation']['repair_attempted'])

    def test_zero_refund_scope_does_not_invent_missing_reason_records(self):
        evidence={'I1':{'path':'comparison.first.refund_amount_fen','period':'first','value':0},
                  'I2':{'path':'comparison.second.refund_amount_fen','period':'second','value':0},
                  'I3':{'path':'reason_breakdown.missing_reason.first.refund_amount_fen','period':'first','value':0}}
        options=_pending_catalog(evidence)
        self.assertEqual(list(options),['query_scope'])
        self.assertEqual(options['query_scope']['evidence_ids'],['I1','I2'])
        evidence['I3']['value']=1
        options=_pending_catalog(evidence)
        self.assertEqual(list(options),['missing_reason'])

    def test_two_allowed_pending_ids_render_program_text_without_extra_request(self):
        def make(context):
            data=selection(context);data['pending_check_ids']=['missing_reason','buyer_reports']
            return response(json.dumps(data))
        got=self.run_case(make)
        self.assertEqual(got['status'],'completed')
        self.assertEqual(got['usage']['model_requests'],1)
        self.assertEqual(got['usage']['tool_calls'],1)
        for key in ('missing_reason','buyer_reports'):
            self.assertIn(got['investigation_pending_options'][key]['text'],got['answer'])
