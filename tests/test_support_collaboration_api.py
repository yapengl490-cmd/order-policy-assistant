"""实际HTTP→身份→状态→协作→SQLite→导出；仅模型HTTP为离线替身。

这些是助手开发回归，不是新真实模型调用、正式PG或本人运行证据。
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app_config import AppSettings
from app_server import create_app
from buyer_support_service import support_metadata, order_support_facts
from merchant_service import shop_metadata, order_shops
from models import Base, Order
from multi_agent_runtime import run_multi_agent
from policy_kb import metadata as policy_metadata, import_policy_files


ROOT = Path(__file__).resolve().parents[1]
QUESTION = ('核查订单 O_SUPPORT_OK 能否退货、退货运费由谁承担？未穿过，吊牌完好，'
            '原因是尺码不合适，判断时间2026-11-18T21:00:00+08:00。')


class SupportCollaborationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='support-collaboration-http-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        access = root / 'access.json'
        access.write_text(json.dumps({'accounts': [
            {'buyer_id': 'B001', 'token': 'offline-buyer-one'},
            {'buyer_id': 'B002', 'token': 'offline-buyer-two'}]}))
        self.settings = AppSettings(tokens_file=access, runtime_dir=root/'runtime',
            model_enabled=True, model_api_key='offline-never-sent',
            model_base_url='https://offline.invalid', model_name='no-network-model')
        self.engine = create_engine('sqlite+pysqlite:///:memory:',
            connect_args={'check_same_thread': False}, poolclass=StaticPool
            ).execution_options(schema_translate_map={'public': None})
        self.addCleanup(self.engine.dispose)
        for metadata in (Base.metadata, shop_metadata, support_metadata, policy_metadata):
            metadata.create_all(self.engine)
        business = json.loads((ROOT/'samples/support/business.json').read_text())
        source = json.loads((ROOT/'samples/support/support_facts.json').read_text())
        order = next(x for x in business['orders'] if x['order_id'] == 'O_SUPPORT_OK')
        fact = next(x for x in source['order_support_facts'] if x['order_id'] == 'O_SUPPORT_OK')
        with self.engine.begin() as connection:
            connection.execute(Order.__table__.insert().values(
                **{k: order[k] for k in ('order_id','buyer_id','paid_amount_fen')},
                paid_at=datetime.fromisoformat(order['paid_at']).astimezone(timezone.utc)))
            connection.execute(order_shops.insert().values(order_id=order['order_id'], shop_id='S001'))
            connection.execute(order_support_facts.insert().values(
                **{k: fact[k] for k in ('order_id','product_category','is_custom','source')},
                delivered_at=datetime.fromisoformat(fact['delivered_at']).astimezone(timezone.utc),
                recorded_at=datetime.fromisoformat(fact['recorded_at']).astimezone(timezone.utc),
                source_kind='synthetic_demo', source_version=source['metadata']['version']))
        import_policy_files(self.engine, [ROOT/'knowledge/2026-10-shoes.md', ROOT/'knowledge/2026-11-shoes.md'])
        self.requests = []
        self.fail_policy = False
        self.reject_followup = False
        self.block_first = False
        self.started, self.release = threading.Event(), threading.Event()
        self.transport = httpx.MockTransport(self.model_http)
        for name in ('httpx.HTTPTransport.handle_request', 'socket.create_connection'):
            block = patch(name, side_effect=AssertionError('external network forbidden'))
            block.start(); self.addCleanup(block.stop)

    def model_http(self, request):
        self.assertEqual(request.url.host, 'offline.invalid')
        data = json.loads(request.content); self.requests.append(data)
        offered = [x['function']['name'] for x in data.get('tools', [])]
        tools = [x for x in data['messages'] if x.get('role') == 'tool']
        if self.block_first and len(self.requests) == 1:
            self.started.set()
            if not self.release.wait(3):
                raise AssertionError('test did not release model response')
        if self.fail_policy and 'review_frozen_policy' in offered:
            return httpx.Response(503, json={'error': 'offline policy model failure'})
        # 仅请求实际被允许的工具，不自行生成业务结果。
        if self.reject_followup and 'lookup_policy_evidence' in offered:
            reply = {'role':'assistant','content':None,'tool_calls':[{
                'id':f'test-tool-{len(self.requests)}','type':'function',
                'function':{'name':'lookup_policy_evidence','arguments':'{}'}}]}
            reason = 'tool_calls'
        elif not tools:
            name = next((x for x in ('read_order_facts', 'review_frozen_policy') if x in offered), None)
            self.assertIsNotNone(name, offered)
            reply = {'role':'assistant','content':None,'tool_calls':[{
                'id':f'test-tool-{len(self.requests)}','type':'function',
                'function':{'name':name,'arguments':'{}'}}]}
            reason = 'tool_calls'
        else:
            # 补查结果不取代原政策合同；从当前消息中读取最后一个真实合同。
            result = next(json.loads(item['content']) for item in reversed(tools)
                          if 'support_contract' in json.loads(item['content']))
            if 'result' in result and isinstance(result['result'], dict):
                result = result['result']
            # 模型实际收到的是压缩资料和完整合同；按合同选择，不能从引用指针重建原文。
            contract = result.get('support_contract')
            self.assertIsInstance(contract,dict)
            catalog = contract['claims']
            body = {'schema_version':contract['schema_version'],
                'status':contract['status'],
                'decision':contract['decision'],
                'claims':[{'id':identity,'citation_ids':claim['citation_ids']}
                          for identity,claim in catalog.items() if claim['required']]}
            reply = {'role':'assistant','content':json.dumps(body,ensure_ascii=False)}
            reason = 'stop'
        return httpx.Response(200, json={'choices':[{'finish_reason':reason,'message':reply}],
            'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}})

    def auth(self, second=False):
        return {'Authorization': 'Bearer offline-buyer-' + ('two' if second else 'one')}

    def multi(self, *args, **kwargs):
        if self.reject_followup:
            from buyer_support_service import get_support_context
            from support_runtime import review_support_context
            def review():
                result = review_support_context(self.engine,
                    get_support_context(self.engine,'O_SUPPORT_OK',buyer_id='B001'),
                    state=kwargs['condition_state'],keyword_only=True)
                # 仅离线故障注入：已有权威原文但首次解释性召回为空。
                result['retrieval']['results'] = []
                return result
            kwargs['tool_handlers'] = {'review_frozen_policy':review,
                'lookup_policy_evidence':lambda:{'status':'rejected',
                    'reason':'followup_source_changed_or_outside_handoff',
                    'results':[],'decision_changed':False}}
        return run_multi_agent(*args, transport=self.transport, **kwargs)

    @contextmanager
    def client(self):
        with TestClient(create_app(self.settings,self.engine,multi_agent=self.multi)) as client:
            yield client

    def submit(self, client, message=QUESTION, second=False, request='support-collab-001'):
        response = client.post('/api/tasks',headers=self.auth(second),json={
            'message':message,'request_id':request,'session_id':'support-collab-chat','execution_mode':'multi'})
        self.assertEqual(response.status_code,202,response.text)
        return response.json()

    def terminal(self, client, task_id, second=False):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            response=client.get('/api/tasks/'+task_id,headers=self.auth(second))
            self.assertEqual(response.status_code,200,response.text)
            task=response.json()
            if task['status'] not in {'queued','running','cancelling'}:
                return task
            threading.Event().wait(.005)
        self.fail('task not terminal')

    def test_real_http_dependency_facts_policy_history_export_and_idempotency(self):
        with self.client() as client:
            task=self.terminal(client,self.submit(client)['id'])
            self.assertEqual(task['status'],'completed',task)
            self.assertEqual(task['orchestration']['effective'],'dependent')
            self.assertEqual([b['name'] for b in task['orchestration']['branches']],['order_facts','policy'])
            handoff=task['orchestration']['handoff']
            self.assertEqual(handoff['order_id'],'O_SUPPORT_OK');self.assertEqual(len(handoff['sha256']),64)
            support=next(f['result'] for f in task['facts'] if 'eligibility' in f['result'])
            self.assertEqual(support['eligibility'],'eligible_with_self_report')
            self.assertEqual(support['shipping']['payer'],'buyer')
            self.assertEqual(support['policy_version'],'2026-11-v1')
            self.assertEqual(datetime.fromisoformat(support['return_window']['before']),datetime.fromisoformat('2026-12-01T09:00:00+08:00'))
            self.assertFalse(support['user_claims']['verified'])
            self.assertTrue(task['support_validation']['passed'])
            policy_request=next(p for p in self.requests if any(t['function']['name']=='review_frozen_policy' for t in p.get('tools',[])))
            self.assertIn('O_SUPPORT_OK',json.dumps(policy_request,ensure_ascii=False))
            self.assertIn(handoff['sha256'],json.dumps(policy_request,ensure_ascii=False))
            exported=client.get('/api/tasks/'+task['id']+'/export',headers=self.auth())
            self.assertEqual(exported.status_code,200)
            result=exported.json()['result']
            for key in ('answer','facts','orchestration','usage'):
                self.assertEqual(result[key],task[key])
            recent=client.get('/api/tasks',headers=self.auth()).json()
            self.assertEqual(recent[0]['id'],task['id'])
            previous=len(self.requests)
            self.assertEqual(self.submit(client)['id'],task['id'])
            self.assertEqual(len(self.requests),previous)
            self.assertEqual(client.get('/api/tasks/'+task['id'],headers=self.auth(True)).status_code,404)
            self.assertEqual(client.get('/api/tasks/'+task['id']+'/export',headers=self.auth(True)).status_code,404)

    def test_other_buyer_cannot_feed_policy_role_another_order(self):
        with self.client() as client:
            task=self.terminal(client,self.submit(client,second=True)['id'],second=True)
            self.assertNotIn('10000',json.dumps(task,ensure_ascii=False))
            self.assertNotIn('2026-11-v1',json.dumps(task,ensure_ascii=False))
            self.assertFalse(any(t['function']['name']=='review_frozen_policy'
                                 for p in self.requests for t in p.get('tools',[])))
            fact=task['facts'][0]['result']
            self.assertEqual(fact['status'],'not_found');self.assertNotIn('order',fact)

    def test_policy_model_failure_retains_order_without_completed_answer(self):
        self.fail_policy=True
        with self.client() as client:
            task=self.terminal(client,self.submit(client)['id'])
            self.assertEqual(task['status'],'partial',task)
            self.assertTrue(task['facts'])
            self.assertEqual(task['facts'][0]['result']['order_id'],'O_SUPPORT_OK')
            self.assertFalse(any(f['result'].get('eligibility')=='eligible_with_self_report' for f in task['facts']))
            self.assertLessEqual(task['usage']['model_requests'],4)

    def test_http_cancellation_prevents_next_dependency_external_call(self):
        self.block_first=True
        with self.client() as client:
            pending=self.submit(client)
            self.assertTrue(self.started.wait(2))
            response=client.post('/api/tasks/'+pending['id']+'/cancel',headers=self.auth())
            self.assertEqual(response.status_code,200)
            self.release.set()
            task=self.terminal(client,pending['id'])
            self.assertEqual(task['status'],'cancelled')
            self.assertEqual(len(self.requests),1)
            self.assertEqual(task['usage']['tool_calls'],0)

    def test_rejected_supplement_is_partial_and_visible_while_primary_facts_survive(self):
        self.reject_followup=True
        with self.client() as client:
            task=self.terminal(client,self.submit(client)['id'])
            self.assertEqual(task['status'],'partial',task.get('orchestration'))
            self.assertEqual(task['orchestration']['followup']['attempts'],1)
            self.assertEqual(task['orchestration']['followup']['status'],'rejected')
            self.assertIn('补查',task['answer'])
            self.assertTrue(any(f['result'].get('eligibility')=='eligible_with_self_report' for f in task['facts']))
            self.assertEqual(task['usage']['tool_calls'],3)

