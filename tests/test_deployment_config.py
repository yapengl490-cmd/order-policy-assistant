"""容器显式配置不放宽任意协议地址；默认本机行为保持。"""
import os
import unittest
from unittest.mock import patch
from app_config import deployment_mode,bind_host,mcp_endpoint
from mcp_order_client import OrderProtocolClient
class DeploymentConfigTests(unittest.TestCase):
    def test_default_stays_loopback(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(bind_host(),'127.0.0.1');self.assertEqual(mcp_endpoint(),'http://127.0.0.1:18081/mcp')
    def test_container_has_one_fixed_internal_endpoint(self):
        with patch.dict(os.environ,{'DATUS_DEPLOYMENT':'container'},clear=True):
            self.assertEqual(bind_host(),'0.0.0.0');self.assertEqual(OrderProtocolClient('buyer:B001').url,'http://mcp:18081/mcp')
            with self.assertRaises(ValueError):OrderProtocolClient('buyer:B001',url='http://example.com/mcp')
    def test_malformed_mode_and_overridden_endpoint_stop(self):
        for env in [{'DATUS_DEPLOYMENT':'public'},{'MCP_URL':'http://example.com/mcp'},
                    {'DATUS_DEPLOYMENT':'container','MCP_URL':'http://127.0.0.1:18081/mcp'}]:
            with patch.dict(os.environ,env,clear=True),self.assertRaises(ValueError):mcp_endpoint()
