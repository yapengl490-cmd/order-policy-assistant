"""独立变化解释入口：可信范围、条件冻结、幂等和取消，不调用外部模型。"""
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from app_config import AppSettings
from app_server import create_app

CONDITIONS = dict(first_from="2026-10-01T00:00:00+08:00", first_before="2026-10-15T00:00:00+08:00",
                  second_from="2026-10-15T00:00:00+08:00", second_before="2026-11-01T00:00:00+08:00",
                  refund_before="2026-11-03T00:00:00+08:00")

class PeriodAnalysisAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.access = self.root / "access.json"
        self.access.write_text(json.dumps({"accounts": [
            {"token": "test-one", "roles": [{"role": "buyer", "buyer_id": "B1"}, {"role": "merchant", "shop_id": "S1"}]},
            {"token": "test-two", "roles": [{"role": "merchant", "shop_id": "S2"}]}]}))
        self.settings = AppSettings(tokens_file=self.access, runtime_dir=self.root/"runtime",
                                    model_enabled=True, model_api_key="test-not-real")
        self.engine = create_engine("sqlite://"); self.addCleanup(self.engine.dispose)
        self.calls=[]; self.started=threading.Event(); self.release=threading.Event(); self.release.set()
        self.headers={"Authorization": "Bearer test-one", "X-Access-Mode": "merchant"}
    def runner(self, settings, engine, message, buyer_id, history, cancelled, **kwargs):
        self.calls.append(dict(message=message, history=history, buyer_id=buyer_id, **kwargs)); self.started.set()
        if not self.release.wait(2): raise TimeoutError()
        return {"status":"cancelled" if cancelled() else "completed", "answer":"测试变化说明", "facts":[],
                "analysis_validation":{"schema":"passed", "semantics":"not_guaranteed"},
                "answer_quality":{"brevity":"passed"}, "history":[]}
    def body(self, **changes):
        return {**CONDITIONS, "request_id":"analysis-001", "session_id":"analysis-session", **changes}
    def post(self, client, **changes):
        return client.post("/api/analysis/tasks", headers=self.headers, json=self.body(**changes))
    def terminal(self, client, task_id):
        for _ in range(200):
            record=client.get("/api/tasks/"+task_id, headers=self.headers).json()
            if record["status"] not in {"queued","running","cancelling"}: return record
            time.sleep(.005)
        self.fail("controlled task did not finish")
    def test_frozen_conditions_trusted_scope_history_export_and_idempotency(self):
        with TestClient(create_app(self.settings,self.engine,period_analysis_agent=self.runner)) as c:
            response=self.post(c); self.assertEqual(response.status_code,202)
            task=self.terminal(c,response.json()["id"])
            self.assertEqual(task["execution_mode"],"period_analysis")
            self.assertEqual(task["status"],"completed")
            self.assertEqual(self.calls[0]["shop_id"],"S1")
            self.assertEqual(self.calls[0]["period_conditions"],task["analysis_conditions"])
            self.assertIsNone(self.calls[0]["condition_state"])
            self.assertEqual(self.calls[0]["history"],[])
            self.assertEqual(self.post(c).json()["id"],task["id"])
            self.assertEqual(len(self.calls),1)
            self.assertEqual(self.post(c,refund_before="2026-11-04T00:00:00+08:00").status_code,409)
            exported=c.get("/api/tasks/"+task["id"]+"/export",headers=self.headers).json()
            self.assertEqual(exported["analysis_conditions"],task["analysis_conditions"])
            other={"Authorization":"Bearer test-two","X-Access-Mode":"merchant"}
            for suffix in ("","/export"):
                self.assertEqual(c.get("/api/tasks/"+task["id"]+suffix,headers=other).status_code,404)
            self.assertEqual(c.get("/api/tasks",headers=other).json(),[])
    def test_bad_fields_and_ranges_stop_before_task_creation(self):
        with TestClient(create_app(self.settings,self.engine,period_analysis_agent=self.runner)) as c:
            self.assertEqual(c.post("/api/analysis/tasks",json=self.body()).status_code,401)
            for change in ({"shop_id":"S2"},{"facts":{}},{"first_from":"2026-10-01"},
                           {"first_before":"2026-10-20T00:00:00+08:00"},
                           {"refund_before":"2026-10-30T00:00:00+08:00"}):
                self.assertEqual(self.post(c,**change).status_code,422)
            self.assertEqual(self.calls,[])
            self.assertEqual(c.get("/api/tasks",headers=self.headers).json(),[])
    def test_cancel_reaches_same_task_and_does_not_resubmit(self):
        self.release.clear()
        with TestClient(create_app(self.settings,self.engine,period_analysis_agent=self.runner)) as c:
            try:
                task=self.post(c).json(); self.assertTrue(self.started.wait(1))
                self.assertEqual(c.post("/api/tasks/"+task["id"]+"/cancel",headers=self.headers).status_code,200)
            finally:self.release.set()
            self.assertEqual(self.terminal(c,task["id"])["status"],"cancelled")
            self.assertEqual(len(self.calls),1)
    def test_normal_chat_cannot_bypass_condition_form(self):
        with TestClient(create_app(self.settings,self.engine,period_analysis_agent=self.runner)) as c:
            data={"message":"比较","session_id":"s","request_id":"request-001","execution_mode":"period_analysis"}
            self.assertEqual(c.post("/api/tasks",json=data,headers=self.headers).status_code,422)
            self.assertEqual(self.calls,[])
    def test_model_disabled_keeps_fixed_comparison_available(self):
        with TestClient(create_app(replace(self.settings,model_enabled=False),self.engine,period_analysis_agent=self.runner)) as c:
            self.assertEqual(self.post(c).status_code,503)
            self.assertEqual(self.calls,[])
    def test_daily_quota_shared_with_other_task_modes(self):
        with TestClient(create_app(replace(self.settings,max_daily_tasks=1),self.engine,period_analysis_agent=self.runner)) as c:
            task=self.post(c).json(); self.terminal(c,task["id"])
            self.assertEqual(self.post(c,request_id="analysis-002",session_id="other").status_code,429)
            self.assertEqual(len(self.calls),1)

if __name__=="__main__":unittest.main()
