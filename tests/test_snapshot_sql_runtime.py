"""模型替身+固定快照，不进行付费请求。真实PG冻结单独验证。"""
import copy
from datetime import datetime,timezone
import json
import unittest
import httpx
from types import SimpleNamespace
from condition_state import reconcile_conditions
from execution_budget import ExecutionBudget
from snapshot_sql_runtime import run_snapshot_sql
from test_snapshot_sql import SUMMARY,VALID_SQL


def config(**updates):
    return SimpleNamespace(**({"model_api_key":"sk-offline-snapshot-secret","model_base_url":"https://api.deepseek.com",
        "model_name":"deepseek-flash","max_model_calls":4,"max_tool_calls":6,"max_output_tokens":1200,
        "model_timeout_seconds":30,"task_timeout_seconds":90}|updates))


def reply(sql):
    return {"choices":[{"message":{"role":"assistant","content":json.dumps({"sql":sql})},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":20,"completion_tokens":10,"total_tokens":30}}


def state():
    return reconcile_conditions("汇总支付月份2026年10月，退款截止2026年11月3日零点",None,
        datetime(2026,11,18,tzinfo=timezone.utc),"snapshot_test")


class SnapshotRuntimeTests(unittest.TestCase):
    def run_case(self,replies,*,cfg=None,conditions=None,cancel=lambda:False,pref=None,shared=None):
        self.requests=[];self.loads=[]
        def send(request):
            self.requests.append(json.loads(request.content))
            item=replies.pop(0)
            if isinstance(item,Exception):raise item
            return httpx.Response(200,json=item)
        def load(**args):self.loads.append(args);return copy.deepcopy(SUMMARY)
        return run_snapshot_sql(cfg or config(),None,"汇总支付月份2026年10月，退款截止2026年11月3日零点","B001",[],cancel,
            condition_state=state()if conditions is None else conditions,preference_snapshot=pref,shared_budget=shared,
            transport=httpx.MockTransport(send),tool_handlers={"summary_orders":load})

    def test_valid_sql_actual_schema_no_gold_and_shared_counts(self):
        got=self.run_case([reply(VALID_SQL)])
        self.assertEqual(got['status'],'completed');self.assertEqual(got['usage']['model_requests'],1)
        self.assertEqual(got['usage']['tool_calls'],2);self.assertEqual(got['budget']['used']['model_requests'],1)
        self.assertEqual(got['budget']['used']['tool_calls'],2)
        self.assertEqual(got['sql_validation']['metric'],'passed')
        sent=json.dumps(self.requests)
        self.assertIn('order_facts',sent);self.assertNotIn('17000',sent);self.assertNotIn('baseline',sent)
        self.assertNotIn('sk-offline-snapshot-secret',json.dumps(got))
        self.assertEqual(len(self.loads),1)

    def test_bad_column_repaired_once_same_snapshot(self):
        got=self.run_case([reply(VALID_SQL.replace('paid_amount_fen','wrong_field')),reply(VALID_SQL)])
        self.assertEqual(got['status'],'completed');self.assertEqual(got['usage']['model_requests'],2)
        self.assertEqual(got['usage']['tool_calls'],3);self.assertEqual(len(self.loads),1)
        self.assertIn('sql_reference_error',json.dumps(self.requests[1]))
        hashes={e['snapshot_sha256']for e in got['trace']if e.get('name')=='execute_snapshot_sql'}
        self.assertEqual(len(hashes),1)

    def test_second_bad_sql_stops_no_third_request(self):
        got=self.run_case([reply('SELECT FROM order_facts')]*2)
        self.assertEqual(got['status'],'failed');self.assertEqual(len(self.requests),2)

    def test_policy_and_metric_failure_never_repair(self):
        for sql in ['DROP TABLE order_facts',VALID_SQL+" WHERE order_id='O_A'"]:
            got=self.run_case([reply(sql)])
            self.assertEqual(got['status'],'failed');self.assertEqual(len(self.requests),1)

    def test_clarification_and_cancel_no_model_or_database(self):
        incomplete=state();incomplete.update(needs_clarification=True,clarification='请补时间')
        got=self.run_case([],conditions=incomplete);self.assertEqual(got['status'],'clarification')
        self.assertEqual(self.loads,[]);self.assertEqual(self.requests,[])
        got=self.run_case([],cancel=lambda:True);self.assertEqual(got['status'],'cancelled')
        self.assertEqual(self.loads,[])

    def test_global_and_local_budget_never_reset(self):
        cfg=config();budget=ExecutionBudget(cfg,lambda:False)
        for _ in range(4):budget.reserve_model(1200)
        got=self.run_case([],cfg=cfg,shared=budget)
        self.assertEqual(got['status'],'limited');self.assertEqual(len(self.requests),0)
        got=self.run_case([reply('SELECT FROM order_facts')],cfg=config(max_model_calls=1))
        self.assertEqual(len(self.requests),1);self.assertNotEqual(got['status'],'completed')
        got=self.run_case([reply(VALID_SQL)],cfg=config(max_tool_calls=1))
        self.assertEqual(got['status'],'limited');self.assertEqual(got['usage']['tool_calls'],1)

    def test_model_timeout_unknown_usage_no_network_retry(self):
        got=self.run_case([httpx.ReadTimeout('controlled')])
        event=next(e for e in got['trace'] if e.get('stage')=='model')
        self.assertIn('finished_at',event)
        self.assertGreaterEqual(event['elapsed_seconds'],0)
        self.assertEqual(got['status'],'failed');self.assertEqual(got['usage']['model_requests'],1)
        self.assertEqual(got['usage']['unmetered_requests'],1);self.assertEqual(got['usage']['tool_calls'],1)

    def test_output_preferences_read_snapshot_without_writing(self):
        pref={'role':'buyer','values':{'amount_format':'fen','answer_style':'brief'},'version':1,
            'updated_at':'2026-11-18T00:00:00Z','source':{'kind':'user_settings','text':'test'}}
        original=copy.deepcopy(pref);got=self.run_case([reply(VALID_SQL)],pref=pref)
        self.assertEqual(got['status'],'completed');self.assertIn('17000分',got['answer'])
        self.assertEqual(pref,original);self.assertEqual(got['memory']['effective_values']['amount_format'],'fen')

    def test_unfinished_or_extra_model_fields_not_executed(self):
        item=reply(VALID_SQL);item['choices'][0]['finish_reason']='length'
        got=self.run_case([item]);self.assertEqual(got['status'],'failed');self.assertEqual(got['usage']['tool_calls'],1)

    def test_malformed_message_is_rejected_without_sql(self):
        item=reply(VALID_SQL);item['choices'][0]['message']='not-an-object'
        got=self.run_case([item]);self.assertEqual(got['status'],'failed')
        self.assertEqual(got['usage']['tool_calls'],1)

    def test_cancel_after_model_does_not_execute_sql(self):
        requested=[False]
        def send(request):requested[0]=True;return httpx.Response(200,json=reply(VALID_SQL))
        got=run_snapshot_sql(config(),None,'汇总支付月份2026年10月，退款截止2026年11月3日零点','B001',[],
            lambda:requested[0],condition_state=state(),transport=httpx.MockTransport(send),
            tool_handlers={'summary_orders':lambda **args:copy.deepcopy(SUMMARY)})
        self.assertEqual(got['status'],'cancelled');self.assertEqual(got['usage']['model_requests'],1)
        self.assertEqual(got['usage']['tool_calls'],1)
