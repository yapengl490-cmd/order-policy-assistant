"""真实FastAPI鉴权/建号/持久任务＋模型/平台发送替身，不用桥接预期ID充当后端结果。"""
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from app_config import AppSettings
from app_server import create_app
from feishu_bridge import BridgeConfig, FeishuBridge, LocalApi
from task_store import TaskStore

class FeishuAccountIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='feishu-account-'); self.root=Path(self.tmp.name)
        self.access=self.root/'access.json'; self.token='fake-token'; self.account='fake_account_001'
        self.accounts={'accounts':[{'account_id':self.account,'token':self.token,
            'roles':[{'role':'buyer','buyer_id':'B001'},{'role':'merchant','shop_id':'S001'}],
            'legacy_task_scopes':['buyer:B001','merchant:S001']}]}
        self.save_access();self.calls=[];self.sent=[];self.now=time.time()
        self.settings=AppSettings(model_api_key='unused',model_enabled=True,tokens_file=self.access,runtime_dir=self.root/'runtime')
        self.engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
        self.config=BridgeConfig('fake-app','fake-secret','fake-user','fake-chat',self.token,self.root/'feishu',self.root/'old.lock',self.account)
    def tearDown(self):self.engine.dispose();self.tmp.cleanup()
    def save_access(self):self.access.write_text(json.dumps(self.accounts))
    def agent(self,settings,engine,message,buyer_id,history,cancelled,**kwargs):
        self.calls.append(message)
        return {'status':'completed','answer':'模型替身结果','facts':[],'usage':{},'history':[{'role':'user','content':message}]}
    def reply(self,*args):self.sent.append(args);return {'status':'sent','message_id':'fake-delivery'}
    @contextmanager
    def running(self):
        app=create_app(settings=self.settings,engine=self.engine,agent=self.agent)
        with TestClient(app) as client:
            # TestClient本机版本基于httpx2；用真实LocalApi的httpx传输适配，保留正式异常类型。
            def dispatch(request):
                response=client.request(request.method,str(request.url),headers=dict(request.headers),content=request.content)
                return httpx.Response(response.status_code,headers=dict(response.headers),content=response.content,request=request)
            api=LocalApi(self.token,transport=httpx.MockTransport(dispatch))
            sender=type('Sender',(),{'reply':lambda _,*args:self.reply(*args)})()
            b=FeishuBridge(self.config,api,sender,clock=lambda:self.now,sleep=lambda _:time.sleep(.005),max_polls=200)
            try:yield b,api,client,app
            finally:api.close()
    def event(self,n=1):return {'event_id':f'event-{n}','message_id':f'message-{n}','sender_id':'fake-user','conversation_id':'fake-chat','chat_type':'p2p','message_type':'text','text':'介绍助手能力','created_at':self.now}
    def terminal(self,c,task_id,role='buyer'):
        for _ in range(200):
            r=c.get('/api/tasks/'+task_id,headers={'Authorization':'Bearer '+self.token,'X-Access-Mode':role})
            if r.status_code==200 and r.json()['status'] not in {'queued','running','cancelling'}:return r.json()
            time.sleep(.005)
        self.fail('isolated task did not finish')
    def test_real_server_ids_both_roles_and_duplicate_event(self):
        with self.running() as (b,api,c,app):
            for n,role in enumerate(['buyer','merchant'],1):
                state=b.store.read('state.json');state['mode']=role;b.store.save('state.json',state)
                key=b.handle(self.event(n));r=b._load(key)
                scope=('buyer:B001' if role=='buyer' else 'merchant:S001')+':account:'+self.account
                self.assertEqual(r['task_id'],TaskStore.task_id(scope,r['request_id']))
                self.assertEqual((r['state'],r['delivery_status']),('finished','sent'));b.handle(self.event(n))
            self.assertEqual(len(self.calls),2);self.assertEqual(len(self.sent),2)
    def test_unknown_submit_recovers_without_second_model(self):
        with self.running() as (b,api,c,app):
            original=api.submit
            def lost(r):original(r);raise httpx.ReadTimeout('response lost')
            with patch.object(api,'submit',side_effect=lost):key=b.handle(self.event())
            r=b._load(key);self.assertEqual(r['state'],'waiting');self.terminal(c,r['task_id'])
            b.process(key);r=b._load(key)
            self.assertEqual((r['state'],r['delivery_status']),('finished','sent'));self.assertEqual(len(self.calls),1)
            self.assertEqual(r['notice']['status'],'sent')
    def test_old_unconfirmed_receipt_recovers_new_scope_task(self):
        with self.running() as (b,api,c,app):
            key=b.accept(self.event());r=b._load(key);r['task_id']=TaskStore.task_id('buyer:B001',r['request_id']);b._save(r)
            accepted=api.submit(r);self.terminal(c,accepted['id']);self.assertNotEqual(accepted['id'],r['task_id'])
            with patch.object(api,'submit',side_effect=AssertionError('must recover')):b.process(key)
            r=b._load(key);self.assertEqual(r['task_id'],accepted['id']);self.assertEqual(r['delivery_status'],'sent');self.assertEqual(len(self.calls),1)
    def test_existing_legacy_task_retained(self):
        with self.running() as (b,api,c,app):
            key=b.accept(self.event());r=b._load(key)
            old,_=app.state.store.create('buyer:B001',r['request_id'],r['session_id'],r['text'])
            app.state.store.update(old['id'],'buyer:B001',status='completed',answer='旧结果',result={'facts':[]})
            r.update(task_id=old['id'],submitted=True);b._save(r)
            with patch.object(api,'submit',side_effect=AssertionError('must retain old')):b.process(key)
            r=b._load(key);self.assertEqual(r['task_id'],old['id']);self.assertEqual(r['delivery_status'],'sent');self.assertEqual(len(self.calls),0)
    def test_accepted_missing_never_resubmits(self):
        with self.running() as (b,api,c,app):
            key=b.accept(self.event());r=b._load(key);r.update(task_id=TaskStore.task_id('buyer:B001',r['request_id']),submitted=True);b._save(r)
            b.process(key);r=b._load(key);self.assertEqual(r['state'],'waiting');self.assertEqual(len(self.calls),0);self.assertEqual(r['notice']['status'],'sent')
    def test_revoked_token_has_notice_no_business(self):
        with self.running() as (b,api,c,app):
            self.accounts['accounts']=[];self.save_access();key=b.handle(self.event());r=b._load(key)
            self.assertEqual((r['state'],r['delivery_status']),('waiting','not_ready'));self.assertEqual(r['notice']['status'],'sent');self.assertEqual(len(self.calls),0)
    def test_role_mapping_change_is_rejected(self):
        with self.running() as (b,api,c,app):
            self.accounts['accounts'][0]['roles'][1]['shop_id']='S002';self.save_access()
            st=b.store.read('state.json');st['mode']='merchant';b.store.save('state.json',st)
            key=b.handle(self.event());r=b._load(key)
            self.assertEqual(r['last_error'],'BridgeIdentityError');self.assertEqual(len(self.calls),0);self.assertEqual(r['notice']['status'],'sent')
    def test_changed_account_cannot_recover_bound_receipt(self):
        with self.running() as (b,api,c,app):
            original=api.submit
            def lost(r):original(r);raise httpx.ReadTimeout('lost')
            with patch.object(api,'submit',side_effect=lost):key=b.handle(self.event())
            r=b._load(key);self.terminal(c,r['task_id'])
            self.accounts['accounts'][0]['account_id']='fake_account_002';self.save_access();b.config=replace(self.config,account_id='fake_account_002')
            b.process(key);r=b._load(key)
            self.assertEqual(r['last_error'],'BridgeIdentityError');self.assertEqual(len(self.calls),1);self.assertEqual(r['delivery_status'],'not_ready')
    def test_same_id_wrong_message_cannot_be_delivered(self):
        with self.running() as (b,api,c,app):
            original=api.submit
            with patch.object(api,'submit',side_effect=lambda r:original(r)|{'message':'different'}):key=b.handle(self.event())
            r=b._load(key);self.assertEqual(r['last_error'],'BridgeIdentityError');self.assertEqual(r['delivery_status'],'not_ready');self.assertEqual(r['notice']['status'],'sent')

if __name__=='__main__':unittest.main()
