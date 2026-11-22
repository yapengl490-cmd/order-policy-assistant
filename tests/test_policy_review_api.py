"""历史政策外部核对的离线 HTTP 验收；不改原任务文件及原导出。

实际 TestClient/认证/TaskStore/政策库，受控原文导入独立 SQLite。
已保存任务属于测试夹具，不冒称模型曾生成此报告；不启动新模型任务。
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from app_config import AppSettings
from app_server import create_app
from policy_kb import metadata, import_policy_files, select_policy, policy_documents, policy_lifecycle_events
from policy_lifecycle import withdraw_policy, correct_policy
from task_store import TaskStore

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = 'S001-shoes-2026-11'
ACTOR = 'private-operator-test'


class PolicyReviewHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='policy-review-http-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        roles = [{'role':'buyer','buyer_id':'B001'}, {'role':'merchant','shop_id':'S001'}]
        self.accounts = [{'account_id':'review_person_A001','token':'test-review-person-a','roles':deepcopy(roles)},
                         {'account_id':'review_person_B001','token':'test-review-person-b','roles':deepcopy(roles)}]
        self.access = self.root/'access.json'
        self.access.write_text(json.dumps({'accounts':self.accounts}))
        self.access.chmod(0o600)
        self.settings = AppSettings(tokens_file=self.access, runtime_dir=self.root/'runtime', memory_enabled=False)
        self.engine = create_engine('sqlite:///'+str(self.root/'policy.db'),
            execution_options={'schema_translate_map':{'public':None}})
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        self.policy_path = ROOT/'knowledge/2026-11-shoes.md'
        import_policy_files(self.engine,[self.policy_path])
        self.bundle = select_policy(self.engine, store_id='S001',category='ordinary_shoes',
                                    paid_at=datetime.fromisoformat('2026-11-15T12:00:00+08:00'))
        self.scope = 'merchant:S001:account:review_person_A001'
        self.store = TaskStore(self.settings.runtime_dir)
        self.model_calls = []
        self.serial = 0
        self.task = self.saved_task({'rule_citations':deepcopy(self.bundle['rule_citations'])})
        self.file = self.settings.runtime_dir/f"task-{self.task['id']}.json"
        self.original_bytes = self.file.read_bytes()
        for target in ('httpx.HTTPTransport.handle_request', 'socket.create_connection'):
            blocker=patch(target,side_effect=AssertionError('external network forbidden'))
            blocker.start();self.addCleanup(blocker.stop)

    def saved_task(self, facts_result):
        self.serial += 1
        record,_=self.store.create(self.scope,f'review-request-{self.serial:04d}',f'session-{self.serial:04d}',
                                  '独立夹具：核对历史政策，不是模型运行结果')
        result={'status':'completed','answer':'独立测试保存的历史答复',
                'facts':[{'tool':'review_after_sales','result':deepcopy(facts_result)}],
                'history':[{'role':'assistant','content':'独立测试保存的历史答复'}],'usage':{}}
        return self.store.update(record['id'],self.scope,status='completed',answer=result['answer'],result=result)

    def headers(self, person=0, role='merchant'):
        return {'Authorization':'Bearer '+self.accounts[person]['token'],'X-Access-Mode':role}

    @contextmanager
    def client(self):
        def forbidden_model(*args,**kwargs):
            self.model_calls.append(True)
            raise AssertionError('review must not create model tasks')
        with TestClient(create_app(self.settings,self.engine,agent=forbidden_model)) as client:
            yield client

    def review(self,client,task=None):
        record=task or self.task
        response=client.get(f"/api/tasks/{record['id']}/policy-status",headers=self.headers())
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def original_export(self,client):
        response=client.get(f"/api/tasks/{self.task['id']}/export",headers=self.headers())
        self.assertEqual(response.status_code,200,response.text)
        return response

    def annotated_export(self,client,task=None):
        record=task or self.task
        response=client.get(f"/api/tasks/{record['id']}/policy-status/export",headers=self.headers())
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('attachment',response.headers.get('content-disposition',''))
        return response.json()

    def withdraw(self):
        return withdraw_policy(self.engine,DOCUMENT,expected_checksum=self.bundle['document']['checksum'],
                               reason='独立测试：原条款已撤回',actor=ACTOR)

    def correct(self):
        text=self.policy_path.read_text().replace(DOCUMENT,DOCUMENT+'-rev2').replace('2026-11-v1','2026-11-v2')
        path=self.root/'replacement.md';path.write_text(text)
        return correct_policy(self.engine,DOCUMENT,path,expected_checksum=self.bundle['document']['checksum'],
                              reason='独立测试：启用同区间更正版',actor=ACTOR)

    def assert_no_write(self,baseline_count=1):
        self.assertEqual(self.file.read_bytes(),self.original_bytes)
        self.assertEqual(len(list(self.settings.runtime_dir.glob('task-*.json'))),baseline_count)
        self.assertEqual(self.model_calls,[])

    def test_clear_withdrawn_corrected_reviews_never_change_original_task_or_export(self):
        with self.client() as client:
            original_response=self.original_export(client)
            self.assertEqual(original_response.json(),self.task)
            clear=self.review(client)
            self.assertEqual(clear['status'],'clear')
            self.assertEqual(clear['sources'][0]['status'],'active')
            public=client.get(f"/api/tasks/{self.task['id']}",headers=self.headers()).json()
            self.assertTrue(public['policy_status_check_available'])
            rows=client.get('/api/tasks',headers=self.headers()).json()
            self.assertTrue(rows[0]['policy_status_check_available'])
            self.withdraw()
            warning=self.review(client)
            self.assertEqual(warning['status'],'warning')
            self.assertEqual(warning['sources'][0]['status'],'withdrawn')
            self.assertIn('已撤回',warning['message'])
            self.assertTrue(warning['original_report_preserved'])
            package=self.annotated_export(client)
            self.assertEqual(package['original_task'],self.task)
            self.assertEqual(package['current_policy_review']['status'],'warning')
            self.assertEqual(self.original_export(client).content,original_response.content)
            self.assert_no_write()
            self.correct()
            corrected=self.review(client)
            self.assertEqual(corrected['status'],'warning')
            self.assertEqual(corrected['sources'][0]['status'],'corrected')
            self.assertEqual(corrected['sources'][0]['replacement_document_id'],DOCUMENT+'-rev2')
            package=self.annotated_export(client)
            self.assertEqual(package['original_task'],self.task)
            self.assertEqual(package['current_policy_review']['sources'][0]['status'],'corrected')
            self.assertEqual(self.original_export(client).content,original_response.content)
            self.assertEqual(client.get(f"/api/tasks/{self.task['id']}",headers=self.headers()).json()['answer'],self.task['answer'])
            self.assert_no_write()

    def test_same_shop_other_account_and_wrong_role_are_rejected_before_policy_read(self):
        with self.client() as client, patch('policy_review.review_task_policies',side_effect=AssertionError('unauthorized read')):
            for suffix in ('policy-status','policy-status/export','export'):
                path=f"/api/tasks/{self.task['id']}/{suffix}"
                with self.subTest(endpoint=suffix):
                    self.assertEqual(client.get(path,headers=self.headers(1)).status_code,404)
                    self.assertEqual(client.get(path,headers=self.headers(0,'buyer')).status_code,404)
                    self.assertEqual(client.get(path).status_code,401)
                    self.assertEqual(client.get(path,headers={'Authorization':'Bearer wrong-token'}).status_code,401)
            self.assert_no_write()

    def test_unknown_and_malformed_task_ids_are_404(self):
        with self.client() as client, patch('policy_review.review_task_policies',side_effect=AssertionError('no task no lookup')):
            for task_id in ('0'*32,'not-a-valid-task'):
                for suffix in ('policy-status','policy-status/export'):
                    self.assertEqual(client.get(f'/api/tasks/{task_id}/{suffix}',headers=self.headers()).status_code,404)
            self.assert_no_write()

    def test_missing_lifecycle_table_is_unavailable_not_clear_and_raw_export_still_works(self):
        policy_lifecycle_events.drop(self.engine)
        with self.client() as client:
            review=self.review(client)
            self.assertEqual(review['status'],'unavailable')
            self.assertEqual(review['sources'],[])
            self.assertIn('error_type',review)
            self.assertIn('不能认定',review['message'])
            package=self.annotated_export(client)
            self.assertEqual(package['current_policy_review']['status'],'unavailable')
            self.assertEqual(package['original_task'],self.task)
            self.assertEqual(self.original_export(client).json(),self.task)
            self.assert_no_write()

    def test_review_does_not_include_operator_or_account_identity(self):
        self.withdraw()
        with self.client() as client:
            value=self.review(client)
            encoded=json.dumps(value,ensure_ascii=False)
            self.assertNotIn(ACTOR,encoded)
            self.assertNotIn('actor',encoded)
            for account in self.accounts:
                self.assertNotIn(account['account_id'],encoded)
                self.assertNotIn(account['token'],encoded)
            package=self.annotated_export(client)
            review_text=json.dumps(package['current_policy_review'],ensure_ascii=False)
            self.assertNotIn(ACTOR,review_text)
            self.assertNotIn(self.accounts[0]['account_id'],review_text)
            self.assert_no_write()

    def test_no_local_policy_not_applicable_without_query_or_new_task(self):
        # Both model prose and traces contain a policy reference, but only tool facts count.
        record=self.saved_task({'platform':'shopify','citation_index':{'remote':{
            'document_id':'remote-policy','source_kind':'shopify','document_checksum':'a'*64}}})
        fake=deepcopy(self.bundle['rule_citations'])
        record=self.store.update(record['id'],self.scope,result={**record['result'],
            'trace':[fake],'history':[{'role':'assistant','content':json.dumps(fake)}]})
        own_file=self.settings.runtime_dir/f"task-{record['id']}.json"
        own_bytes=own_file.read_bytes()
        with self.client() as client, patch('policy_lifecycle.get_policy_lifecycle',side_effect=AssertionError('must skip database')):
            review=self.review(client,record)
            self.assertEqual(review['status'],'not_applicable')
            self.assertEqual(review['sources'],[])
            public=client.get(f"/api/tasks/{record['id']}",headers=self.headers()).json()
            self.assertFalse(public['policy_status_check_available'])
            package=self.annotated_export(client,record)
            self.assertEqual(package['original_task'],record)
            self.assertEqual(package['current_policy_review']['status'],'not_applicable')
            self.assertEqual(own_file.read_bytes(),own_bytes)
            self.assert_no_write(baseline_count=2)

    def test_frozen_source_checksum_mismatch_warns_without_changing_source(self):
        citation=deepcopy(next(iter(self.bundle['rule_citations'].values())))
        citation['document_checksum']='0'*64
        task=self.saved_task({'citation_index':{citation['citation_id']:citation}})
        with self.client() as client:
            review=self.review(client,task)
            self.assertEqual(review['status'],'warning')
            self.assertEqual(review['sources'][0]['status'],'invalid_policy')
            with self.engine.connect() as connection:
                current=connection.execute(select(policy_documents.c.checksum).where(policy_documents.c.document_id==DOCUMENT)).scalar_one()
            self.assertEqual(current,self.bundle['document']['checksum'])
            self.assert_no_write(baseline_count=2)


if __name__=='__main__':
    unittest.main()
