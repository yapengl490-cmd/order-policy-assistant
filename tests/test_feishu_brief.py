"""只测文本组装和隔离平台替身；不连接飞书、模型或真实收据。"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from answer_quality import measure_answer
from feishu_bridge import BridgeConfig, FeishuBridge, render_reply
from test_feishu_bridge import FakeApi, FakeSender


def task(style="brief", amount_format="yuan", **changes):
    return {"id": "test-task", "status": "completed", "answer": "已退40元，净额85元；截止11月3日，不含当天。",
            "memory": {"status": "loaded", "stored_values": {},
                       "effective_values": {"answer_style": style, "amount_format": amount_format}},
            "facts": [{"tool": "check_order", "result": {
                "status": "ok", "order_id": "O001", "paid_amount_fen": 12500,
                "refund_amount_fen": 4000, "net_amount_fen": 8500,
                "currency": "CNY", "money_unit": "fen", "refund_before": "2026-11-03T00:00:00+08:00",
                "provenance": {"status": "recorded", "batches": [{"source": "synthetic", "version": "1"}]},
                "refunds": [{"refund_id": "R-DETAIL-MARKER", "refund_amount_fen": 4000}]}}], **changes}


class BriefReplyTests(unittest.TestCase):
    def test_brief_keeps_full_answer_amount_cutoff_reference_without_raw_json(self):
        record = task(); before = deepcopy(record)
        text = render_reply(record, "buyer")
        for phrase in (record["answer"], "任务：test-task", "处理状态：completed", "买家·本人订单",
                       "支付125元", "已退40元", "净额85元", "2026-11-03T00:00:00+08:00（不含）",
                       "导入来源：已登记", "网页", "导出"):
            self.assertIn(phrase, text)
        self.assertNotIn('"refunds"', text); self.assertNotIn("R-DETAIL-MARKER", text)
        self.assertEqual(record, before)

    def test_only_effective_preference_applies_current_override_not_stored_value(self):
        record = task("detailed")
        record["memory"]["stored_values"] = {"answer_style": "brief"}
        self.assertIn('"refunds"', render_reply(record, "buyer"))
        record["memory"]["stored_values"] = {"answer_style": "detailed"}
        record["memory"]["effective_values"]["answer_style"] = "brief"
        self.assertNotIn('"refunds"', render_reply(record, "buyer"))

    def test_no_truncation_even_if_answer_itself_fails_brevity_quality(self):
        record = task(answer="证据不足。" * 3000 + "保留最后一句。")
        record["answer_quality"] = measure_answer(record["answer"], record["memory"]["effective_values"])
        before = deepcopy(record); text = render_reply(record, "buyer")
        self.assertIn(record["answer"], text); self.assertIn("保留最后一句。", text)
        self.assertNotIn("展示已截短", text); self.assertEqual(record, before)
        self.assertEqual(record["answer_quality"]["brevity"], "failed")
        self.assertEqual(record["answer_quality"]["non_whitespace_characters"], len(record["answer"]))
        self.assertGreater(len(text), record["answer_quality"]["non_whitespace_characters"])

    def test_normal_and_legacy_rendering_are_byte_for_byte_unchanged(self):
        for style in ("standard", "detailed", None):
            record = task(style)
            if style is None: record.pop("memory")
            lines = ["商家·授权店铺只读模式", "任务：test-task\n处理状态：completed", record["answer"],
                     "程序实际读取结果（金额以这些事实为准）："]
            lines.extend(json.dumps(f, ensure_ascii=False, indent=2) for f in record["facts"])
            self.assertEqual(render_reply(record, "merchant"), "\n\n".join(lines))

    def test_amount_format_honors_fen_fixed_yuan_and_large_exact_integers(self):
        self.assertIn("支付12500分", render_reply(task(amount_format="fen"), "merchant"))
        self.assertIn("净额85.00元", render_reply(task(amount_format="yuan_fixed2"), "buyer"))
        record = task(); result = record["facts"][0]["result"]
        result.update(paid_amount_fen=9007199254740993, refund_amount_fen=1, net_amount_fen=9007199254740992)
        text = render_reply(record, "buyer")
        self.assertIn("支付90071992547409.93元", text); self.assertIn("已退0.01元", text)

    def test_not_found_never_adds_amounts_or_claims_global_nonexistence(self):
        record = task(answer="当前范围没有可用于回答的记录。")
        record["facts"][0]["result"].update(status="not_found", paid_amount_fen=98765,
            refund_amount_fen=87654, net_amount_fen=11111, message="这笔订单不存在")
        text = render_reply(record, "buyer")
        self.assertIn("当前授权范围未取得可用记录", text)
        for fragment in ("98765", "987.65", "87654", "11111", "这笔订单不存在", "支付", "净额"):
            self.assertNotIn(fragment, text)
        self.assertIn("退款截止", text)

    def test_error_incomplete_and_inconsistent_money_do_not_become_zero_or_success(self):
        for patch_value in ({"status": "data_error"}, {"net_amount_fen": 0},
                            {"paid_amount_fen": True}, {"currency": "USD"}, {"money_unit": "yuan"}):
            record = task(answer="金额待核对。")
            record["facts"][0]["result"].update(patch_value)
            text = render_reply(record, "buyer")
            self.assertIn("未取得可核对的完整金额", text); self.assertNotIn("支付125", text)

    def test_summary_preserves_two_payment_boundaries_and_refund_cutoff(self):
        record = task(amount_format="fen"); fact = record["facts"][0]
        fact["tool"] = "summary_orders"; fact["result"].update(paid_from="2026-10-01T00:00:00Z", paid_before="2026-11-01T00:00:00Z")
        text = render_reply(record, "merchant")
        self.assertIn("区间汇总", text); self.assertIn("支付范围 [2026-10-01T00:00:00Z, 2026-11-01T00:00:00Z)", text)
        self.assertIn("净额8500分", text); self.assertIn("退款截止", text)

    def test_policy_keeps_citation_ids_date_and_demo_scope_not_full_documents(self):
        record = task(facts=[{"tool": "search_policies", "result": {
            "status": "ok", "scope": "synthetic_demo", "as_of": "2026-11-18",
            "policies": [{"citation_id": "DEMO-REFUND-v1", "version": "1", "text": "LONG-POLICY-MARKER"},
                         {"citation_id": "DEMO-SUPPORT-v2", "version": "2"}]}}])
        text = render_reply(record, "buyer")
        for fragment in ("DEMO-REFUND-v1", "DEMO-SUPPORT-v2", "2026-11-18", "合成演示政策", "版本与原文"):
            self.assertIn(fragment, text)
        self.assertNotIn("LONG-POLICY-MARKER", text)
        record["facts"][0]["result"]["status"] = "insufficient_evidence"
        self.assertIn("未取得足够政策依据", render_reply(record, "buyer"))

    def test_unknown_tool_only_type_and_complete_evidence_pointer_no_guessed_summary(self):
        record = task(answer="请核对原始依据。", facts=[{"tool": "new_tool", "result": {"paid_amount_fen": 12345, "payload": "RAW-MARKER"}}])
        text = render_reply(record, "buyer")
        self.assertIn("new_tool", text); self.assertIn("网页任务与导出", text)
        self.assertNotIn("12345", text); self.assertNotIn("RAW-MARKER", text)

    def test_account_scope_still_checked_and_old_delivery_receipts_never_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); now = 1800000000
            config = BridgeConfig("test-app", "test-secret", "allowed-user", "allowed-chat", "test-local", root/"state", root/"old.lock")
            api, sender = FakeApi(), FakeSender([{"status": "failed"}, {"status": "sent", "message_id": "sent-test"}])
            bridge = FeishuBridge(config, api, sender, clock=lambda: now, sleep=lambda _: None)
            event = {"event_id": "event-1", "message_id": "message-1", "sender_id": "allowed-user", "conversation_id": "allowed-chat", "chat_type": "p2p", "message_type": "text", "text": "查询订单", "created_at": now}
            self.assertIsNone(bridge.handle({**event, "sender_id": "not-allowed"}))
            self.assertEqual(api.posts, []); self.assertEqual(sender.calls, [])
            key = bridge.handle(event); first_body = bridge._load(key)["reply_text"]
            self.assertIn('"paid_amount_fen"', first_body)
            api.tasks[bridge._load(key)["task_id"]]["memory"] = task()["memory"]
            with patch("feishu_bridge.render_reply", side_effect=AssertionError("must not rerender stored reply")):
                bridge.handle(event)
            record = bridge._load(key)
            self.assertEqual(record["reply_text"], first_body); self.assertEqual(sender.calls[0], sender.calls[1])
            before = deepcopy(record); calls = len(sender.calls); bridge.handle(event)
            self.assertEqual(bridge._load(key), before); self.assertEqual(len(sender.calls), calls)


if __name__ == "__main__":
    unittest.main()
