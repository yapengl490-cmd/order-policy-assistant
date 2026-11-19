"""能力预检先于模型；不启动Docker，不联网，不下载镜像。"""
from datetime import datetime,timezone
from types import SimpleNamespace as NS
from unittest.mock import patch
import os
import subprocess
import unittest
from app_config import AppSettings
from condition_state import reconcile_conditions
from program_executor import program_execution_capability,ProgramError
from programmatic_runtime import run_programmatic_agent

class CapabilityTests(unittest.TestCase):
    def test_container_is_rejected_without_docker_access(self):
        with patch.dict(os.environ,{'DATUS_DEPLOYMENT':'container'}),patch('program_executor.subprocess.run') as run:
            r=program_execution_capability();self.assertFalse(r['available']);self.assertEqual(r['reason'],'program_host_required');run.assert_not_called()
    def probe(self,results):
        with patch.dict(os.environ,{'DATUS_DEPLOYMENT':'local'}),patch('program_executor._docker_binary',return_value='/fake/docker'),patch('program_executor.subprocess.run',side_effect=results) as run:
            result=program_execution_capability();return result,run.call_args_list
    def test_engine_off_and_image_missing_have_distinct_reasons(self):
        result,calls=self.probe([NS(returncode=1,stdout='',stderr='DO_NOT_EXPOSE')])
        self.assertEqual(result['reason'],'docker_engine_unavailable');self.assertEqual(len(calls),1);self.assertNotIn('DO_NOT_EXPOSE',str(result))
        result,calls=self.probe([NS(returncode=0,stdout='29'),NS(returncode=1,stdout='')])
        self.assertEqual(result['reason'],'program_image_unavailable');self.assertEqual(len(calls),2)
        self.assertTrue(all('pull' not in call.args[0] for call in calls))
    def test_available_and_timeout(self):
        result,calls=self.probe([NS(returncode=0,stdout='29'),NS(returncode=0,stdout='linux/arm64\n')])
        self.assertTrue(result['available']);self.assertEqual(calls[0].kwargs['timeout'],3)
        result,_=self.probe([subprocess.TimeoutExpired('docker',3)])
        self.assertEqual(result['reason'],'docker_probe_failed')
    def test_missing_binary_fails_closed(self):
        with patch.dict(os.environ,{'DATUS_DEPLOYMENT':'local'}),patch('program_executor._docker_binary',side_effect=ProgramError('missing','x')):
            self.assertEqual(program_execution_capability()['reason'],'docker_binary_unavailable')
    def test_runtime_unavailable_stops_before_model_or_tools(self):
        question='列出我店最近2笔订单并逐笔核查，退款截止2026年11月3日零点'
        state=reconcile_conditions(question,None,datetime(2026,11,18,tzinfo=timezone.utc),'turn')
        unavailable={'available':False,'reason':'program_host_required','message':'没有受信宿主执行入口，本次未调用模型。','image':'fixed'}
        with patch('programmatic_runtime.program_execution_capability',return_value=unavailable),patch('programmatic_runtime.httpx.Client',side_effect=AssertionError('no paid call')):
            r=run_programmatic_agent(AppSettings(model_api_key='unused'),None,question,'unused',[],lambda:False,access_role='merchant',shop_id='S001',condition_state=state)
        self.assertEqual(r['status'],'failed');self.assertEqual(r['usage']['model_requests'],0);self.assertEqual(r['usage']['tool_calls'],0)
        self.assertEqual(r['program_capability']['reason'],'program_host_required')
if __name__=='__main__':unittest.main()
