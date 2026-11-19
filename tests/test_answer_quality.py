"""长度仅是一项观察维度，不能伪装事实正确；始终保留原文。"""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import httpx

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from answer_quality import measure_answer,brief_instruction,compact_instruction
from agent_runtime import run_agent

class AnswerQualityTests(unittest.TestCase):
    def test_digits_and_punctuation_count_but_whitespace_does_not(self):
        got=measure_answer("净额 85.00 元。\n",{"answer_style":"brief"})
        self.assertEqual(got["non_whitespace_characters"],9)
        self.assertEqual(got["brevity"],"passed")
        self.assertFalse(got["text_truncated"])
        self.assertEqual(got["facts_and_citations"],"requires_separate_verification")
    def test_excess_is_reported_not_removed_and_detailed_not_penalized(self):
        answer="测"*261
        self.assertEqual(measure_answer(answer,{"answer_style":"brief"})["brevity"],"failed")
        self.assertEqual(measure_answer(answer,{"answer_style":"detailed"})["brevity"],"not_required")
        self.assertEqual(len(answer),261)
    def test_prompt_only_applies_to_requested_brief_mode(self):
        self.assertEqual(brief_instruction({}),"")
        self.assertIn("150",brief_instruction({"answer_style":"brief"},"policy"))
        self.assertIn("引用ID和版本",brief_instruction({"answer_style":"brief"},"policy"))

    def test_compact_default_and_brief_keep_conditions_and_unknown_source_without_new_limit(self):
        for values in ({},{"answer_style":"standard"},{"answer_style":"brief"}):
            prompt=compact_instruction(values)
            for phrase in ("同一截止只出现一次", "不同时复述UTC与北京时间", "不抄字段英文", "不在开头和结尾重复",
                           "不主动添加‘本次未涉及政策’", "来源unknown须说明来源未确认", "不省略用户明确要求的明细"):
                self.assertIn(phrase,prompt)
            self.assertNotIn("不超过",prompt)
        self.assertEqual(compact_instruction({"answer_style":"detailed"}),"")
        self.assertIn("本轮明确要求详细解释",compact_instruction({}))

    def test_runtime_sends_general_compaction_and_preserves_original_answer_without_retry(self):
        for style in ("standard","brief","detailed"):
            with self.subTest(style=style):
                requests=[];long_answer="所需金额、范围和来源须核对。"*30
                def send(request):
                    body=json.loads(request.content);requests.append(body)
                    if len(requests)==1:
                        message={"role":"assistant","content":None,"tool_calls":[{"id":"read-one","type":"function",
                            "function":{"name":"check_order","arguments":json.dumps({"order_id":"INV-91"})}}]}
                    else:
                        self.assertEqual(len(requests),2,"不能为了缩短回答额外请求模型")
                        message={"role":"assistant","content":json.dumps({"status":"completed","answer":long_answer})}
                    return httpx.Response(200,json={"choices":[{"message":message,"finish_reason":"tool_calls" if len(requests)==1 else "stop"}],
                        "usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}})
                settings=SimpleNamespace(model_api_key="offline-key",model_base_url="https://offline.invalid",model_name="fake",
                    max_model_calls=4,max_tool_calls=6,model_timeout_seconds=30,max_output_tokens=1200)
                values={"answer_style":style}
                snapshot={"role":"buyer","values":values,"version":1,"updated_at":"2026-11-18T00:00:00Z","source":{"kind":"user_settings"}}
                result=run_agent(settings,None,"核查订单INV-91","B-test",[],lambda:False,
                    preference_snapshot=snapshot,transport=httpx.MockTransport(send),tool_handlers={"check_order":lambda **args:{
                        "status":"ok","buyer_id":"B-test","order_id":"INV-91","paid_amount_fen":6700,"refund_amount_fen":1200,"net_amount_fen":5500}})
                self.assertEqual(result["answer"],long_answer);self.assertEqual(result["usage"]["model_requests"],2)
                self.assertEqual(result["facts"][0]["result"]["net_amount_fen"],5500)
                self.assertFalse(result["answer_quality"]["text_truncated"])
                self.assertEqual(result["answer_quality"]["brevity"],"failed" if style=="brief" else "not_required")
                for request in requests:
                    system=request["messages"][0]["content"]
                    if style=="detailed":self.assertNotIn("回答组织：",system)
                    else:self.assertIn(compact_instruction(values),system)
                    if style=="brief":self.assertIn(brief_instruction(values),system)

if __name__=="__main__":unittest.main()
