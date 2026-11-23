"""Offline request contracts: errors still count, no hidden retries or repairs."""
from copy import deepcopy
import hashlib
from types import SimpleNamespace
import unittest

import httpx

from model_request import decode_unique_json, request_model_once


class ModelRequestTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(model_timeout_seconds=30, model_base_url="https://model.example.invalid/")
        self.payload = {"model": "offline-test", "messages": [{"role": "user", "content": "synthetic facts"}]}
        self.usage = {"model_requests": 2, "tool_calls": 1, "input_tokens": 7,
                      "output_tokens": 3, "total_tokens": 10, "unmetered_requests": 1}
        self.sent, self.lifecycle = [], []

    def request(self, response, *, remaining=5, limit=1024):
        def send(request):
            self.sent.append(request)
            if isinstance(response, Exception):
                raise response
            return response
        def begin(event):
            self.lifecycle.append(("begin", deepcopy(self.usage)))
            event["started_at"] = "offline-start"
            return "clock-marker"
        def end(event, stamp):
            self.assertEqual(stamp, "clock-marker")
            self.lifecycle.append(("end", deepcopy(self.usage)))
            event["finished_at"] = "offline-end"
        return request_model_once(self.settings, self.payload, secret="offline-only",
            transport=httpx.MockTransport(send), remaining_seconds=lambda: remaining,
            usage=self.usage, request_number=3, begin=begin, end=end, max_response_bytes=limit)

    def test_success_caps_timeout_and_preserves_caller_counters_and_snapshots(self):
        source = {"usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
                  "choices": [{"message": {"content": "saved output"}}]}
        body, event = self.request(httpx.Response(200, json=source))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(str(self.sent[0].url), "https://model.example.invalid/chat/completions")
        self.assertEqual(self.sent[0].extensions["timeout"]["read"], 5)
        self.assertEqual(self.usage, {"model_requests": 3, "tool_calls": 1, "input_tokens": 11,
                                     "output_tokens": 5, "total_tokens": 16, "unmetered_requests": 1})
        # Attempts are counted before trace starts; usage is collected after trace ends.
        for phase, counters in self.lifecycle:
            self.assertEqual(counters["model_requests"], 3, phase)
            self.assertEqual(counters["unmetered_requests"], 2, phase)
            self.assertEqual(counters["total_tokens"], 10, phase)
        self.assertEqual(event["request_number"], 3)
        self.payload["messages"][0]["content"] = "later change"
        body["choices"][0]["message"]["content"] = "later change"
        self.assertEqual(event["request"]["messages"][0]["content"], "synthetic facts")
        self.assertEqual(event["response"], source)

    def test_model_timeout_cap_and_minimum_timeout(self):
        for remaining, expected in [(90, 30), (0.0001, .001)]:
            with self.subTest(remaining=remaining):
                self.request(httpx.Response(200, json={}), remaining=remaining)
                self.assertEqual(self.sent[-1].extensions["timeout"]["read"], expected)

    def test_http_error_body_usage_is_counted_without_retry(self):
        body, event = self.request(httpx.Response(503, json={"error": "temporary failure",
            "usage": {"prompt_tokens": 4, "completion_tokens": 0, "total_tokens": 4}}))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(body["error"], "temporary failure")
        self.assertEqual(event["http_status"], 503)
        self.assertEqual(event["error_type"], "HTTPStatusError")
        self.assertEqual(event["error_category"], "model_request_failed")
        self.assertEqual(self.usage["total_tokens"], 14)
        self.assertEqual(self.usage["unmetered_requests"], 1)
        self.assertEqual(event["finished_at"], "offline-end")

    def test_timeout_has_one_attempt_and_finished_trace_but_unknown_usage(self):
        body, event = self.request(httpx.ReadTimeout("controlled timeout with offline-only"))
        self.assertIsNone(body)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(event["error_type"], "ReadTimeout")
        self.assertEqual(event["error_category"], "model_request_failed")
        self.assertEqual(event["finished_at"], "offline-end")
        self.assertNotIn("offline-only", str(event))
        self.assertEqual(self.usage["unmetered_requests"], 2)
        self.assertEqual(self.usage["total_tokens"], 10)

    def test_oversize_body_is_hashed_not_parsed_including_on_http_error(self):
        raw = b'{"usage":{"total_tokens":20}}'
        body, event = self.request(httpx.Response(503, content=raw), limit=len(raw)-1)
        self.assertIsNone(body)
        self.assertEqual(event["error_category"], "response_size_limit")
        self.assertEqual(event["response_bytes"], len(raw))
        self.assertEqual(event["response_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertNotIn("response", event)
        self.assertNotIn("raw_response", event)
        self.assertEqual(self.usage["total_tokens"], 10)
        self.assertEqual(self.usage["unmetered_requests"], 2)
        body, event = self.request(httpx.Response(200, content=raw), limit=len(raw))
        self.assertEqual(body["usage"]["total_tokens"], 20)
        self.assertNotIn("error_category", event)

    def test_malformed_body_is_retained_and_does_not_invent_usage(self):
        for status in [200, 503]:
            with self.subTest(status=status):
                body, event = self.request(httpx.Response(status, text="not json"))
                self.assertIsNone(body)
                self.assertEqual(event["raw_response"], "not json")
                self.assertEqual(self.usage["total_tokens"], 10)
                self.assertEqual("error_category" in event, status == 503)
        self.assertEqual(len(self.sent), 2)

    def test_missing_or_invalid_total_cannot_clear_unknown_usage(self):
        for total in [None, True, -1, "6", 6.0]:
            with self.subTest(total=total):
                before = deepcopy(self.usage)
                self.request(httpx.Response(200, json={"usage": {
                    "prompt_tokens": 4, "completion_tokens": 2, "total_tokens": total}}))
                self.assertEqual(self.usage["total_tokens"], before["total_tokens"])
                self.assertEqual(self.usage["input_tokens"], before["input_tokens"])
                self.assertEqual(self.usage["unmetered_requests"], before["unmetered_requests"]+1)
        for body in [{}, {"usage": None}, {"usage": []}, []]:
            before = self.usage["unmetered_requests"]
            self.request(httpx.Response(200, json=body))
            self.assertEqual(self.usage["unmetered_requests"], before+1)

    def test_valid_total_does_not_accept_boolean_or_negative_component_usage(self):
        self.request(httpx.Response(200, json={"usage": {
            "prompt_tokens": True, "completion_tokens": -2, "total_tokens": 0}}))
        self.assertEqual(self.usage["unmetered_requests"], 1)
        self.assertEqual((self.usage["input_tokens"], self.usage["output_tokens"], self.usage["total_tokens"]), (7, 3, 10))

    def test_redirect_is_not_followed(self):
        _, event = self.request(httpx.Response(307, headers={"Location": "https://other.example.invalid"}))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(event["http_status"], 307)
        self.assertEqual(event["error_category"], "model_request_failed")

    def test_unexpected_error_propagates_after_trace_finalization(self):
        with self.assertRaisesRegex(RuntimeError, "programming failure"):
            self.request(RuntimeError("programming failure"))
        self.assertEqual([item[0] for item in self.lifecycle], ["begin", "end"])
        self.assertEqual(self.usage["unmetered_requests"], 2)


class AnalysisRequestIntegrationTests(unittest.TestCase):
    def test_http_failure_usage_reaches_both_runtime_results_without_repair(self):
        # Local imports avoid collecting the imported TestCase classes a second time.
        import test_period_analysis_runtime as period_fixtures
        import test_refund_investigation_runtime as investigation_fixtures

        for fixture_class in (period_fixtures.PeriodAnalysisRuntimeTests,
                              investigation_fixtures.RefundInvestigationRuntimeTests):
            with self.subTest(runtime=fixture_class.__name__):
                fixture = fixture_class()
                try:
                    fixture.setUp()
                    original_facts = deepcopy(fixture.result)
                    got = fixture.run_case(lambda context: httpx.Response(503, json={
                        "error": "offline controlled failure",
                        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                    }))
                    self.assertEqual(got["status"], "partial")
                    self.assertTrue(got["fixed_answer"])
                    self.assertIn(got["fixed_answer"], got["answer"])
                    self.assertEqual(got["facts"][0]["result"], original_facts)
                    self.assertEqual(fixture.result, original_facts)
                    self.assertEqual(len(fixture.requests), 1)
                    self.assertEqual(len(fixture.loads), 1)
                    self.assertFalse(got["analysis_validation"]["repair_attempted"])
                    self.assertEqual(got["analysis_validation"]["attempts"], [])
                    self.assertEqual(got["usage"], {"model_requests": 1, "tool_calls": 1,
                        "input_tokens": 11, "output_tokens": 7, "total_tokens": 18,
                        "unmetered_requests": 0})
                    self.assertEqual(got["budget"]["used"]["model_requests"], 1)
                    event = got["trace"][-1]
                    self.assertEqual(event["http_status"], 503)
                    self.assertEqual(event["error_category"], "model_request_failed")
                    self.assertEqual(event["response"]["usage"]["total_tokens"], 18)
                    self.assertIn("finished_at", event)
                finally:
                    fixture.doCleanups()


class UniqueJsonTests(unittest.TestCase):
    def test_rejects_repeated_keys_including_nested_objects_and_escape_alias(self):
        for source in ['{"x":1,"x":2}', '{"x":[{"y":1,"y":2}]}', '{"x":1,"\\u0078":2}']:
            with self.subTest(source=source), self.assertRaises(ValueError):
                decode_unique_json(source)

    def test_decode_preserves_values_leaving_business_schema_validation_to_caller(self):
        self.assertEqual(decode_unique_json('{"x":[1,false,null],"y":{"x":2}}'),
                         {"x": [1, False, None], "y": {"x": 2}})
        self.assertEqual(decode_unique_json('[1,2]'), [1, 2])
        with self.assertRaises(ValueError):
            decode_unique_json('not json')


if __name__ == "__main__":
    unittest.main()
