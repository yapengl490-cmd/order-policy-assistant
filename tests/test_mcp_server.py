"""真实官方 MCP ASGI 协议＋临时数据库/专用凭据；无网络、模型、正式数据。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from mcp_credentials import prepare_mcp_credentials, load_mcp_credential, authorize_mcp_token
from mcp_order_server import create_mcp_app, MAX_BODY_BYTES
from merchant_service import shop_metadata, shops, order_shops
from models import Base, Order, Refund


PROTOCOL = "2025-11-25"


def sqlite_utc(target, context):
    if context.session.bind.dialect.name == "sqlite":
        name = "paid_at" if isinstance(target, Order) else "refunded_at"
        value = getattr(target, name)
        if value.tzinfo is None:
            setattr(target, name, value.replace(tzinfo=timezone.utc))


class MCPServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="datus-mcp-test-")
        self.root = Path(self.temp.name)
        (self.root / "local").mkdir()
        self.access = {"accounts": [
            {"token": "web-token-never-accepted-for-mcp", "roles": [
                {"role": "buyer", "buyer_id": "B001"}, {"role": "merchant", "shop_id": "S001"}]},
            {"token": "web-buyer-two", "roles": [{"role": "buyer", "buyer_id": "B002"}]},
        ]}
        (self.root / "local/access.json").write_text(json.dumps(self.access))
        self.prepared = prepare_mcp_credentials(self.root)
        self.tokens = {key: load_mcp_credential(key, self.root) for key in self.prepared["scope_keys"]}
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(self.engine, "connect", lambda conn, _: conn.execute("ATTACH DATABASE ':memory:' AS public"))
        event.listen(Order, "load", sqlite_utc)
        event.listen(Refund, "load", sqlite_utc)
        Base.metadata.create_all(self.engine)
        shop_metadata.create_all(self.engine)
        with Session(self.engine) as session, session.begin():
            session.add_all([
                Order(order_id="O001", buyer_id="B001", paid_at=datetime(2026, 10, 15, tzinfo=timezone.utc), paid_amount_fen=12500),
                Order(order_id="O002", buyer_id="B002", paid_at=datetime(2026, 10, 16, tzinfo=timezone.utc), paid_amount_fen=25000),
                Order(order_id="O003", buyer_id="B003", paid_at=datetime(2026, 10, 17, tzinfo=timezone.utc), paid_amount_fen=50000),
                Refund(refund_id="R001", order_id="O001", refunded_at=datetime(2026, 10, 20, tzinfo=timezone.utc), refund_amount_fen=2500),
                Refund(refund_id="R002", order_id="O001", refunded_at=datetime(2026, 11, 2, tzinfo=timezone.utc), refund_amount_fen=1500),
            ])
            session.execute(insert(shops), [{"shop_id": "S001", "name": "本店"}, {"shop_id": "S002", "name": "其他店"}])
            session.execute(insert(order_shops), [{"order_id": "O001", "shop_id": "S001"},
                                                {"order_id": "O002", "shop_id": "S001"},
                                                {"order_id": "O003", "shop_id": "S002"}])

    def tearDown(self):
        event.remove(Order, "load", sqlite_utc)
        event.remove(Refund, "load", sqlite_utc)
        self.engine.dispose()
        self.temp.cleanup()

    def headers(self, key="buyer:B001", **extra):
        return {"Authorization": "Bearer " + self.tokens[key], "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": PROTOCOL, **extra}

    @contextmanager
    def client(self):
        with TestClient(create_mcp_app(self.root, self.engine), base_url="http://127.0.0.1:18081") as client:
            yield client

    def rpc(self, client, method, params=None, key="buyer:B001", identifier="test-request"):
        return client.post("/mcp", headers=self.headers(key), json={"jsonrpc": "2.0", "id": identifier,
                                                                      "method": method, "params": params or {}})

    def call(self, client, order="O001", key="buyer:B001", arguments=None, identifier="call-1"):
        return self.rpc(client, "tools/call", {"name": "check_order", "arguments": arguments if arguments is not None else {
            "order_id": order, "refund_before": "2026-11-03T00:00:00+08:00"}}, key, identifier)

    def test_initialize_discover_and_real_order_tool(self):
        with self.client() as client:
            initialized = self.rpc(client, "initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                  "clientInfo": {"name": "offline-test", "version": "1"}})
            self.assertEqual(initialized.status_code, 200, initialized.text)
            self.assertEqual(initialized.json()["result"]["protocolVersion"], PROTOCOL)
            discovered = self.rpc(client, "tools/list")
            self.assertEqual(discovered.status_code, 200, discovered.text)
            tools = discovered.json()["result"]["tools"]
            self.assertEqual([tool["name"] for tool in tools], ["check_order"])
            self.assertFalse(tools[0]["inputSchema"]["additionalProperties"])
            result = self.call(client).json()["result"]
            self.assertFalse(result["isError"])
            self.assertEqual(result["structuredContent"]["net_amount_fen"], 8500)
            self.assertEqual(len(result["structuredContent"]["refunds"]), 2)

    def test_buyer_and_shop_scopes_apply_to_every_request(self):
        with self.client() as client:
            self.assertEqual(self.call(client, "O002").json()["result"]["structuredContent"]["status"], "not_found")
            own = self.call(client, "O002", "buyer:B002").json()["result"]["structuredContent"]
            self.assertEqual(own["paid_amount_fen"], 25000)
            shop = self.call(client, "O002", "merchant:S001").json()["result"]["structuredContent"]
            self.assertEqual(shop["shop_id"], "S001")
            self.assertEqual(self.call(client, "O003", "merchant:S001").json()["result"]["structuredContent"]["status"], "not_found")
            self.assertEqual(self.call(client, "O001", "buyer:B002").json()["result"]["structuredContent"]["status"], "not_found")

    def test_bad_tool_arguments_are_tool_errors_without_database_reads(self):
        with self.client() as client, patch("mcp_order_server.check_order") as query:
            for arguments in ({"order_id": "O001", "buyer_id": "B002"}, {"order_id": "O001", "shop_id": "S001"},
                              {"order_id": 1}, {"order_id": "O001", "refund_before": "2026-11-03T00:00:00"}):
                response = self.call(client, arguments=arguments)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["result"]["isError"])
                self.assertEqual(response.json()["result"]["structuredContent"]["status"], "invalid_arguments")
            response = self.rpc(client, "tools/call", {"name": "execute_sql", "arguments": {"sql": "secret input"}})
            self.assertTrue(response.json()["result"]["isError"])
            self.assertEqual(response.json()["result"]["structuredContent"]["status"], "unknown_tool")
            query.assert_not_called()

    def test_mcp_authentication_separate_from_web_and_health_public(self):
        with self.client() as client:
            for headers in ({}, {"Authorization": "Bearer web-token-never-accepted-for-mcp"}, {"Authorization": "Bearer wrong"}):
                for method in ("GET", "POST", "DELETE"):
                    self.assertEqual(client.request(method, "/mcp", headers=headers).status_code, 401)
            health = client.get("/health")
            self.assertEqual(health.json(), {"service": "datus-order-mcp", "status": "ok"})
            self.assertNotIn("B001", health.text)

    def test_origin_host_and_size_are_restricted(self):
        with self.client() as client:
            for extra in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Host": "evil.example"}):
                self.assertEqual(client.post("/mcp", headers=self.headers(**extra), json={}).status_code, 403)
            response = client.post("/mcp", headers=self.headers(**{"Content-Type": "application/json"}), content=b"x" * (MAX_BODY_BYTES + 1))
            self.assertEqual(response.status_code, 413, response.text)

    def test_credentials_are_not_overwritten_and_revocation_is_effective(self):
        path = self.root / "local/mcp_access.json"
        before = path.read_bytes()
        self.assertFalse(prepare_mcp_credentials(self.root)["created"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(set(self.tokens.values())), 3)
        self.access["accounts"][0]["roles"] = [{"role": "buyer", "buyer_id": "B001"}]
        (self.root / "local/access.json").write_text(json.dumps(self.access))
        self.assertIsNone(authorize_mcp_token(self.tokens["merchant:S001"], self.root))
        with self.assertRaises(ValueError):
            load_mcp_credential("merchant:S001", self.root)

    def test_errors_and_audit_are_sanitized_and_correlated(self):
        with self.client() as client, patch("mcp_order_server.check_order", side_effect=SQLAlchemyError("NEVER_EXPOSE_DB_PASSWORD")):
            response = self.call(client, identifier="audit-case")
            self.assertTrue(response.json()["result"]["isError"])
            self.assertNotIn("NEVER_EXPOSE", response.text)
        log = (self.root / "local/mcp/events.jsonl").read_text()
        row = json.loads(log)
        self.assertEqual(row["rpc_id"], "audit-case")
        self.assertEqual(len(row["request_id"]), 32)
        self.assertEqual(row["scope_key"], "buyer:B001")
        self.assertEqual(row["status"], "database_error")
        self.assertEqual(row["protocol"], PROTOCOL)
        self.assertEqual(len(row["result_sha256"]), 64)
        for secret in [*self.tokens.values(), "NEVER_EXPOSE_DB_PASSWORD", "Authorization"]:
            self.assertNotIn(secret, log)

    def test_two_concurrent_calls_keep_their_scopes_and_third_is_busy(self):
        started, release = threading.Condition(), threading.Event()
        scopes = []
        def blocking(engine, order_id, buyer_id, cutoff):
            with started:
                scopes.append(buyer_id)
                started.notify_all()
            if not release.wait(3):
                raise TimeoutError
            return {"status": "ok", "buyer_id": buyer_id, "order_id": order_id}
        with self.client() as client, patch("mcp_order_server.check_order", side_effect=blocking), ThreadPoolExecutor(max_workers=2) as workers:
            one = workers.submit(self.call, client, "O001", "buyer:B001", None, "first")
            two = workers.submit(self.call, client, "O002", "buyer:B002", None, "second")
            try:
                with started:
                    self.assertTrue(started.wait_for(lambda: len(scopes) == 2, timeout=2))
                busy = self.call(client, identifier="third").json()["result"]
                self.assertEqual(busy["structuredContent"]["status"], "busy")
                self.assertTrue(busy["isError"])
            finally:
                release.set()
            self.assertEqual(one.result().json()["result"]["structuredContent"]["buyer_id"], "B001")
            self.assertEqual(two.result().json()["result"]["structuredContent"]["buyer_id"], "B002")

    def test_process_lock_and_pid_cleanup(self):
        pid = self.root / "local/mcp/server.pid"
        with self.client():
            self.assertTrue(pid.exists())
            with self.assertRaises(BlockingIOError):
                with self.client():
                    pass
            self.assertTrue(pid.exists())
        self.assertFalse(pid.exists())


if __name__ == "__main__":
    unittest.main()
