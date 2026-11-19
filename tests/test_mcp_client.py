"""官方 Client＋真实 ASGI 协议的离线客户端回归；只用临时库和临时凭据。"""
import asyncio
from contextlib import asynccontextmanager
import json
import unittest
from unittest.mock import patch

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy.exc import SQLAlchemyError

from mcp_order_client import MCPCallError, OrderProtocolClient, URL
from mcp_order_server import create_mcp_app
import test_mcp_server as server_tests

REAL_ASYNC_CLIENT = httpx2.AsyncClient


def leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in leaves(child)]
    return [error]


class MCPClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = server_tests.MCPServerTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.app = create_mcp_app(self.fixture.root, self.fixture.engine)

    def http_client(self, **kwargs):
        return REAL_ASYNC_CLIENT(transport=httpx2.ASGITransport(self.app), **kwargs)

    @asynccontextmanager
    async def environment(self):
        async with self.app.router.lifespan_context(self.app):
            with patch("mcp_order_client.httpx2.AsyncClient", side_effect=self.http_client):
                yield

    async def test_sdk_returns_tool_error_but_wraps_user_exception_on_exit(self):
        """直接验证SDK机制：is_error正常返回，用户在with内抛异常才被TaskGroup包裹。"""
        async with self.app.router.lifespan_context(self.app):
            async with self.http_client(headers={"Authorization": "Bearer " + self.fixture.tokens["buyer:B001"]}) as http:
                caught = None
                try:
                    async with Client(streamable_http_client(URL, http_client=http)) as client:
                        await client.list_tools()
                        result = await client.call_tool("check_order", {"order_id": "O001", "buyer_id": "B002"})
                        self.assertTrue(result.is_error)
                        self.assertEqual(result.structured_content["status"], "invalid_arguments")
                        raise MCPCallError("invalid_arguments")
                except Exception as error:
                    caught = error
                self.assertIsInstance(caught, ExceptionGroup)
                business = [error for error in leaves(caught) if isinstance(error, MCPCallError)]
                self.assertEqual([error.code for error in business], ["invalid_arguments"])

    async def test_client_preserves_invalid_arguments_and_database_error(self):
        async with self.environment():
            client = OrderProtocolClient("buyer:B001", root=self.fixture.root)
            with self.assertRaises(MCPCallError) as caught:
                await client._request({"order_id": "O001", "buyer_id": "B002"})
            self.assertEqual(caught.exception.code, "invalid_arguments")
            self.assertTrue(client.last_trace["is_error"])
            with patch("mcp_order_server.check_order", side_effect=SQLAlchemyError("TEST_SECRET_MUST_STAY_LOCAL")):
                with self.assertRaises(MCPCallError) as caught:
                    await client._request({"order_id": "O001"})
            self.assertEqual(caught.exception.code, "database_error")
            self.assertNotIn("TEST_SECRET", json.dumps(client.last_trace))

    async def test_concurrent_official_clients_have_real_request_authorization(self):
        async with self.environment():
            buyer_one = OrderProtocolClient("buyer:B001", root=self.fixture.root)
            buyer_two = OrderProtocolClient("buyer:B002", root=self.fixture.root)
            merchant = OrderProtocolClient("merchant:S001", root=self.fixture.root)
            own, cross = await asyncio.gather(
                buyer_one._request({"order_id": "O001", "refund_before": "2026-11-03T00:00:00+08:00"}),
                buyer_two._request({"order_id": "O001", "refund_before": "2026-11-03T00:00:00+08:00"}))
            self.assertEqual(own["net_amount_fen"], 8500)
            self.assertEqual(cross["status"], "not_found")
            self.assertNotIn("paid_amount_fen", cross)
            own_shop, other_shop = await asyncio.gather(
                merchant._request({"order_id": "O002"}), buyer_one._request({"order_id": "O002"}))
            self.assertEqual(own_shop["shop_id"], "S001")
            self.assertEqual(other_shop["status"], "not_found")
            audit = [json.loads(line) for line in (self.fixture.root / "local/mcp/events.jsonl").read_text().splitlines()]
            self.assertEqual({row["scope_key"] for row in audit}, {"buyer:B001", "buyer:B002", "merchant:S001"})


if __name__ == "__main__":
    unittest.main()
