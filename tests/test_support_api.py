"""实际应用任务入口的售后条件继承/模式/隔离，模型及业务用替身。"""
import json
from pathlib import Path
import tempfile
import time
import unittest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from app_config import AppSettings
from app_server import create_app

class SupportAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);p=Path(self.tmp.name)
        access=p/'access.json';access.write_text(json.dumps({'accounts':[{'buyer_id':'B001','token':'fake-one'},{'buyer_id':'B002','token':'fake-two'}]}))
        self.cfg=AppSettings(tokens_file=access,runtime_dir=p/'runtime',model_enabled=True,model_api_key='fake-not-sent')
        self.engine=create_engine('sqlite://');self.addCleanup(self.engine.dispose)
        self.headers={'Authorization':'Bearer fake-one'};self.calls=[]
    def runner(self,settings,engine,message,buyer,history,cancelled,**kw):
        self.calls.append((buyer,kw['condition_state']))
        state=kw['condition_state'];state['support_missing']=[] if state['claims'] else ['is_unworn','tags_intact']
        return {'status':'completed' if state['claims'] else 'clarification','answer':'测试售后回复','condition_state':state,
                'facts':[{'tool':'review_after_sales','result':{'status':'ok'}}]}
    def terminal(self,c,task):
        for _ in range(100):
            row=c.get('/api/tasks/'+task['id'],headers=self.headers).json()
            if row['status'] not in {'queued','running','cancelling'}:return row
            time.sleep(.01)
        self.fail('task not finished')
    def ask(self,c,message,request,mode='single',session='support-chat'):
        response=c.post('/api/tasks',headers=self.headers,json={'message':message,'request_id':request,'session_id':session,'execution_mode':mode})
        self.assertEqual(response.status_code,202);return self.terminal(c,response.json())
    def test_followup_saved_and_exported_with_same_order_and_claim_source(self):
        with TestClient(create_app(self.cfg,self.engine,agent=self.runner)) as c:
            first=self.ask(c,'O_SUPPORT_OK能退货吗','request-001')
            self.assertEqual(first['status'],'clarification');self.assertEqual(first['conditions']['support_missing'],['is_unworn','tags_intact'])
            second=self.ask(c,'没穿过，吊牌完好','request-002')
            self.assertEqual(second['status'],'completed');self.assertEqual(self.calls[-1][1]['confirmed']['order_id']['value'],'O_SUPPORT_OK')
            self.assertEqual(self.calls[-1][0],'B001')
            exported=c.get('/api/tasks/'+second['id']+'/export',headers=self.headers).json()
            self.assertEqual(exported['result']['condition_state']['claims']['is_unworn']['value'],True)
            self.assertEqual(c.get('/api/tasks/'+second['id'],headers={'Authorization':'Bearer fake-two'}).status_code,404)
    def test_multi_selection_explicitly_records_same_order_single_execution(self):
        def forbidden(*args,**kwargs):self.fail('old multi branches must not split new support business')
        with TestClient(create_app(self.cfg,self.engine,agent=self.runner,multi_agent=forbidden)) as c:
            task=self.ask(c,'O_SUPPORT_OK能退货吗','request-003',mode='multi')
            self.assertEqual(task['orchestration']['requested'],'multi');self.assertEqual(task['orchestration']['effective'],'single')
