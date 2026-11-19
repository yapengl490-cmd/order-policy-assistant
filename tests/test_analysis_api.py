"""HTTP边界回归；计算函数另有真实PG与单元验证。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from app_config import AppSettings
from app_server import create_app

class AnalysisAPITests(unittest.TestCase):
    def test_identity_does_not_come_from_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/"access.json").write_text(json.dumps({"accounts":[{"token":"test-only-token", "roles":[{"role":"buyer","buyer_id":"B1"},{"role":"merchant","shop_id":"S1"}]}]}))
            settings=AppSettings(tokens_file=root/"access.json",runtime_dir=root/"runtime")
            engine=create_engine("sqlite://")
            body={"first_from":"2026-10-01T00:00:00+08:00","first_before":"2026-10-15T00:00:00+08:00","second_from":"2026-10-15T00:00:00+08:00","second_before":"2026-11-01T00:00:00+08:00","refund_before":"2026-11-03T00:00:00+08:00"}
            with patch("app_server.compare_periods",return_value={"status":"ok"}) as calculate,TestClient(create_app(settings,engine)) as client:
                self.assertEqual(client.post("/api/analysis/compare",json=body).status_code,401)
                for role,field,scope in (("buyer","buyer_id","B1"),("merchant","shop_id","S1")):
                    headers={"Authorization":"Bearer test-only-token","X-Access-Mode":role}
                    self.assertEqual(client.post("/api/analysis/compare",json=body,headers=headers).status_code,200)
                    self.assertEqual(calculate.call_args.args[1][field],scope)
                    self.assertEqual(client.post("/api/analysis/compare",json={**body,"shop_id":"other"},headers=headers).status_code,422)
                calculate.side_effect=ValueError("两期不能重叠")
                self.assertEqual(client.post("/api/analysis/compare",json=body,headers=headers).status_code,422)
            engine.dispose()

if __name__=="__main__":unittest.main()
