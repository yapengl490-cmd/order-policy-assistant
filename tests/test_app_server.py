"""助手接口/持久任务回归：临时凭证、独立内存库、可控模型替身，无外部调用。"""

import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, insert
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app_config import AppSettings, authenticate
from app_server import create_app
from models import Base, Order, Refund
from merchant_service import order_shops, shop_metadata, shops
from task_store import TaskStore


class ControlledAgent:
    """阻塞由事件精确控制；取消后不执行下一次模拟外部动作。"""

    def __init__(self, blocked=False, fail=False):
        self.condition = threading.Condition()
        self.release = threading.Event()
        if not blocked:
            self.release.set()
        self.calls = []
        self.next_actions = 0
        self.fail = fail

    def __call__(self, settings, engine, message, buyer_id, history, cancelled, **kwargs):
        with self.condition:
            self.calls.append({"message": message, "buyer": buyer_id, "history": history,
                               "access_role": kwargs.get("access_role", "buyer"), "shop_id": kwargs.get("shop_id")})
            self.condition.notify_all()
        if not self.release.wait(3):
            raise TimeoutError("test agent timed out")
        if cancelled():
            return {"status": "cancelled", "answer": "已停止", "facts": [], "usage": {}}
        if self.fail:
            raise RuntimeError("DO_NOT_EXPOSE_TEST_SECRET")
        with self.condition:
            self.next_actions += 1
        return {"status": "completed", "answer": "测试事实：净额8500分", "facts": [{"net_amount_fen": 8500}],
                "usage": {"model_calls": 0}, "history": [{"role": "user", "content": message}]}

    def wait_started(self, count=1):
        with self.condition:
            return self.condition.wait_for(lambda: len(self.calls) >= count, timeout=3)


def _sqlite_utc(target, context):
    if context.session.bind.dialect.name == "sqlite":
        field = "paid_at" if isinstance(target, Order) else "refunded_at"
        value = getattr(target, field)
        if value.tzinfo is None:
            setattr(target, field, value.replace(tzinfo=timezone.utc))


class AppServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="datus-api-test-")
        self.root = Path(self.temp.name)
        self.tokens = {"B001": "test-only-buyer-one-credential", "B002": "test-only-buyer-two-credential"}
        access = self.root / "access.json"
        access.write_text(json.dumps({"accounts": [{"buyer_id": buyer, "token": token} for buyer, token in self.tokens.items()]}))
        access.chmod(0o600)
        self.settings = AppSettings(model_api_key="test-fake-key-not-used", model_enabled=True,
                                    runtime_dir=self.root / "runtime", tokens_file=access)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(self.engine, "connect", lambda conn, _: conn.execute("ATTACH DATABASE ':memory:' AS public"))
        event.listen(Order, "load", _sqlite_utc)
        event.listen(Refund, "load", _sqlite_utc)
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as session, session.begin():
            session.add(Order(order_id="O001", buyer_id="B001", paid_at=datetime(2026, 10, 15, tzinfo=timezone.utc), paid_amount_fen=12500))
            session.add(Refund(refund_id="R001", order_id="O001", refunded_at=datetime(2026, 10, 20, tzinfo=timezone.utc), refund_amount_fen=2500))

    def tearDown(self):
        event.remove(Order, "load", _sqlite_utc)
        event.remove(Refund, "load", _sqlite_utc)
        self.engine.dispose()
        self.temp.cleanup()

    def auth(self, buyer="B001", mode=None):
        headers = {"Authorization": "Bearer " + self.tokens[buyer]}
        if mode is not None:
            headers["X-Access-Mode"] = mode
        return headers

    def enable_merchant(self):
        """同一凭证预授权双角色；B002只有买家角色，M002只授权另一店铺。"""
        self.tokens["M002"] = "test-only-shop-two-credential"
        self.settings.tokens_file.write_text(json.dumps({"accounts": [
            {"token": self.tokens["B001"], "roles": [{"role": "buyer", "buyer_id": "B001"}, {"role": "merchant", "shop_id": "S001"}]},
            {"token": self.tokens["B002"], "roles": [{"role": "buyer", "buyer_id": "B002"}]},
            {"token": self.tokens["M002"], "roles": [{"role": "merchant", "shop_id": "S002"}]},
        ]}))
        shop_metadata.create_all(self.engine)
        with Session(self.engine) as session, session.begin():
            session.execute(insert(shops), [{"shop_id": "S001", "name": "测试一店"}, {"shop_id": "S002", "name": "测试二店"}])
            session.add_all([
                Order(order_id="O002", buyer_id="B002", paid_at=datetime(2026, 10, 16, tzinfo=timezone.utc), paid_amount_fen=12500),
                Order(order_id="O003", buyer_id="B003", paid_at=datetime(2026, 10, 17, tzinfo=timezone.utc), paid_amount_fen=50000),
            ])
            session.flush()
            session.execute(insert(order_shops), [
                {"order_id": "O001", "shop_id": "S001"}, {"order_id": "O002", "shop_id": "S001"},
                {"order_id": "O003", "shop_id": "S002"},
            ])

    @contextmanager
    def client(self, agent=None, settings=None, engine=None):
        agent = agent or ControlledAgent()
        app = create_app(settings=settings or self.settings, engine=engine if engine is not None else self.engine, agent=agent)
        with TestClient(app) as client:
            try:
                yield client, agent, app
            finally:
                agent.release.set()

    def ask(self, client, request_id="request-0001", session_id="session1", buyer="B001", message="查询订单O001", mode=None):
        return client.post("/api/tasks", headers=self.auth(buyer, mode),
                           json={"message": message, "session_id": session_id, "request_id": request_id})

    def terminal(self, client, task_id, buyer="B001", mode=None):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response = client.get(f"/api/tasks/{task_id}", headers=self.auth(buyer, mode))
            self.assertEqual(response.status_code, 200)
            record = response.json()
            if record["status"] not in {"queued", "running", "cancelling"}:
                return record
            threading.Event().wait(0.005)
        self.fail("测试任务没有在3秒内结束")

    def test_unauthenticated_endpoints_reject(self):
        with self.client() as (client, agent, _):
            for path in ("/api/me", "/api/orders", "/api/tasks", "/api/tasks/" + "0" * 32):
                self.assertEqual(client.get(path).status_code, 401)
            response = client.post("/api/tasks", json={"message": "查询", "session_id": "s", "request_id": "request-001"})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(client.get("/api/me", headers={"Authorization": "Bearer B001"}).status_code, 401)
            self.assertEqual(agent.calls, [])

    def test_authenticate_non_ascii_is_unauthorized(self):
        self.assertIsNone(authenticate("中文伪造凭证", self.settings))

    def test_body_cannot_choose_buyer(self):
        with self.client() as (client, agent, _):
            response = client.post("/api/tasks", headers=self.auth(), json={
                "message": "查询", "session_id": "s", "request_id": "request-001", "buyer_id": "B002",
            })
            self.assertEqual(response.status_code, 422)
            self.assertEqual(agent.calls, [])

    def test_order_scope_and_zero_unauthorized_information(self):
        with self.client() as (client, _, __):
            own = client.get("/api/orders/O001", headers=self.auth(), params={"refund_before": "2026-11-03T00:00:00+00:00"})
            self.assertEqual(own.status_code, 200)
            self.assertEqual(own.json()["net_amount_fen"], 10000)
            foreign = client.get("/api/orders/O001", headers=self.auth("B002"))
            absent = client.get("/api/orders/O404", headers=self.auth("B002"))
            self.assertEqual(foreign.json()["status"], "not_found")
            self.assertEqual(foreign.json()["message"], absent.json()["message"])
            self.assertNotIn("paid_amount_fen", foreign.json())
            self.assertEqual(client.get("/api/orders", headers=self.auth("B002")).json()["orders"], [])

    def test_cross_buyer_task_export_and_history_not_visible(self):
        with self.client() as (client, _, __):
            response = self.ask(client)
            task_id = response.json()["id"]
            self.assertEqual(self.terminal(client, task_id)["status"], "completed")
            for suffix in ("", "/export"):
                self.assertEqual(client.get(f"/api/tasks/{task_id}{suffix}", headers=self.auth("B002")).status_code, 404)
            self.assertEqual(client.post(f"/api/tasks/{task_id}/cancel", headers=self.auth("B002")).status_code, 404)
            self.assertEqual(client.get("/api/tasks", headers=self.auth("B002")).json(), [])
            export = client.get(f"/api/tasks/{task_id}/export", headers=self.auth())
            self.assertEqual(export.status_code, 200)
            self.assertIn("attachment", export.headers["content-disposition"])
            self.assertEqual(export.json()["result"]["facts"][0]["net_amount_fen"], 8500)
            self.assertNotIn(self.settings.model_api_key, export.text)
            self.assertNotIn(self.tokens["B001"], export.text)

    def test_duplicate_request_does_not_repeat_and_changed_body_conflicts(self):
        fake = ControlledAgent(blocked=True)
        with self.client(fake) as (client, _, __):
            first = self.ask(client)
            self.assertTrue(fake.wait_started())
            duplicate = self.ask(client)
            self.assertEqual(duplicate.status_code, 202)
            self.assertEqual(duplicate.json()["id"], first.json()["id"])
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(self.ask(client, message="不同问题").status_code, 409)
            self.assertEqual(self.ask(client, session_id="other").status_code, 409)
            fake.release.set()
            self.terminal(client, first.json()["id"])
            again = self.ask(client)
            self.assertEqual(again.json()["status"], "completed")
            self.assertEqual(len(fake.calls), 1)

    def test_same_session_cannot_overlap(self):
        fake = ControlledAgent(blocked=True)
        with self.client(fake) as (client, _, __):
            self.ask(client)
            self.assertTrue(fake.wait_started())
            response = self.ask(client, request_id="request-0002")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(len(fake.calls), 1)

    def test_global_capacity_two_rejects_third_without_queue(self):
        fake = ControlledAgent(blocked=True)
        with self.client(fake) as (client, _, app):
            first = self.ask(client)
            second = self.ask(client, request_id="request-0002", session_id="session2", buyer="B002")
            self.assertEqual((first.status_code, second.status_code), (202, 202))
            self.assertTrue(fake.wait_started(2))
            third = self.ask(client, request_id="request-0003", session_id="session3")
            self.assertEqual(third.status_code, 429)
            self.assertIsNone(app.state.store.get(app.state.store.task_id("buyer:B001", "request-0003"), "buyer:B001"))
            self.assertEqual(len(fake.calls), 2)
            fake.release.set()
            self.terminal(client, first.json()["id"])
            self.terminal(client, second.json()["id"], "B002")
            accepted = self.ask(client, request_id="request-0003", session_id="session3")
            self.assertEqual(accepted.status_code, 202)
            self.terminal(client, accepted.json()["id"])
            self.assertEqual(len(fake.calls), 3)

    def test_cancel_is_request_then_terminal_and_stops_next_action(self):
        fake = ControlledAgent(blocked=True)
        with self.client(fake) as (client, _, __):
            task_id = self.ask(client).json()["id"]
            self.assertTrue(fake.wait_started())
            accepted = client.post(f"/api/tasks/{task_id}/cancel", headers=self.auth())
            self.assertEqual(accepted.json()["status"], "cancelling")
            fake.release.set()
            final = self.terminal(client, task_id)
            self.assertEqual(final["status"], "cancelled")
            self.assertEqual(fake.next_actions, 0)

    def test_cancelling_finished_task_preserves_result(self):
        with self.client() as (client, _, __):
            task_id = self.ask(client).json()["id"]
            original = self.terminal(client, task_id)
            result = client.post(f"/api/tasks/{task_id}/cancel", headers=self.auth()).json()
            self.assertEqual((result["status"], result["answer"]), ("completed", original["answer"]))

    def test_restart_keeps_completed_and_does_not_reexecute(self):
        fake = ControlledAgent()
        with self.client(fake) as (client, _, __):
            task_id = self.ask(client).json()["id"]
            original = self.terminal(client, task_id)
        with self.client(fake) as (client, _, __):
            recovered = client.get(f"/api/tasks/{task_id}", headers=self.auth()).json()
            self.assertEqual((recovered["status"], recovered["answer"]), ("completed", original["answer"]))
            self.assertEqual(self.ask(client).json()["id"], task_id)
            self.assertEqual(len(fake.calls), 1)

    def test_orphan_running_record_marked_interrupted(self):
        store = TaskStore(self.settings.runtime_dir)
        orphan, _ = store.create("buyer:B001", "orphan-0001", "session1", "崩溃前任务")
        store.update(orphan["id"], "buyer:B001", status="running")
        with self.client() as (client, agent, __):
            result = client.get(f"/api/tasks/{orphan['id']}", headers=self.auth()).json()
            self.assertEqual((result["status"], result["error"]), ("interrupted", "service_restarted"))
            self.assertEqual(agent.calls, [])

    def test_disabled_model_does_not_disable_order_core(self):
        with self.client(settings=replace(self.settings, model_enabled=False, model_api_key="")) as (client, agent, __):
            self.assertEqual(self.ask(client).status_code, 503)
            self.assertEqual(client.get("/api/orders/O001", headers=self.auth()).json()["status"], "ok")
            self.assertFalse(client.get("/health").json()["model_enabled"])
            self.assertEqual(agent.calls, [])

    def test_request_errors_differ_from_database_fault(self):
        with self.client() as (client, _, __):
            self.assertEqual(client.get("/api/orders/O001", headers=self.auth(), params={"refund_before": "2026-11-03"}).status_code, 422)
            self.assertEqual(client.get("/api/orders", headers=self.auth(), params={"limit": 21}).status_code, 422)
            self.assertEqual(self.ask(client, message=" ").status_code, 422)
        broken = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        try:
            with self.client(engine=broken) as (client, _, __):
                result = client.get("/api/orders/O001", headers=self.auth())
                self.assertEqual(result.status_code, 503)
                self.assertEqual(result.json()["detail"], "数据库读取失败，未得出查询结论")
                self.assertNotIn("not_found", result.text)
        finally:
            broken.dispose()

    def test_agent_failure_terminal_and_secret_not_exposed(self):
        with self.client(ControlledAgent(fail=True)) as (client, _, __):
            task_id = self.ask(client).json()["id"]
            result = self.terminal(client, task_id)
            self.assertEqual((result["status"], result["error"]), ("failed", "RuntimeError"))
            export = client.get(f"/api/tasks/{task_id}/export", headers=self.auth())
            self.assertNotIn("DO_NOT_EXPOSE_TEST_SECRET", export.text)

    def test_session_history_is_scoped(self):
        with self.client() as (client, agent, __):
            first = self.ask(client, message="仅属于B001的上下文")
            self.terminal(client, first.json()["id"])
            other = self.ask(client, buyer="B002", request_id="request-0002")
            self.terminal(client, other.json()["id"], "B002")
            self.assertEqual(agent.calls[-1]["history"], [])
            followup = self.ask(client, request_id="request-0003", message="补充问题")
            self.terminal(client, followup.json()["id"])
            self.assertEqual(agent.calls[-1]["history"], [{"role": "user", "content": "仅属于B001的上下文"}])

    def test_daily_cap_does_not_block_idempotent_result(self):
        with self.client(settings=replace(self.settings, max_daily_tasks=1)) as (client, agent, __):
            first = self.ask(client)
            self.terminal(client, first.json()["id"])
            self.assertEqual(self.ask(client).status_code, 202)
            self.assertEqual(self.ask(client, request_id="request-0002").status_code, 429)
            self.assertEqual(len(agent.calls), 1)

    def test_service_lock_rejects_second_instance(self):
        with self.client() as (_, __, ___):
            app2 = create_app(self.settings, self.engine, ControlledAgent())
            with self.assertRaises(BlockingIOError):
                with TestClient(app2):
                    pass

    def test_access_mode_requires_pre_authorized_role(self):
        self.enable_merchant()
        with self.client() as (client, agent, __):
            own = client.get("/api/me", headers=self.auth()).json()
            self.assertEqual((own["role"], own["buyer_id"]), ("buyer", "B001"))
            merchant = client.get("/api/me", headers=self.auth(mode="merchant")).json()
            self.assertEqual((merchant["role"], merchant["shop_id"]), ("merchant", "S001"))
            self.assertEqual(client.get("/api/orders", headers=self.auth("B002", "merchant")).status_code, 401)
            self.assertEqual(self.ask(client, buyer="B002", mode="merchant").status_code, 401)
            self.assertEqual(client.get("/api/me", headers=self.auth(mode="admin")).status_code, 401)
            self.assertEqual(client.get("/api/me", headers=self.auth("M002", "buyer")).status_code, 401)
            self.assertEqual(agent.calls, [])

    def test_buyer_own_and_merchant_shop_order_ranges(self):
        self.enable_merchant()
        with self.client() as (client, _, __):
            buyer = client.get("/api/orders", headers=self.auth()).json()
            merchant = client.get("/api/orders", headers=self.auth(mode="merchant")).json()
            self.assertEqual([item["order_id"] for item in buyer["orders"]], ["O001"])
            self.assertEqual({item["order_id"] for item in merchant["orders"]}, {"O001", "O002"})
            self.assertTrue(all(item["shop_id"] == "S001" for item in merchant["orders"]))
            self.assertEqual(client.get("/api/orders/O002", headers=self.auth()).json()["status"], "not_found")
            self.assertEqual(client.get("/api/orders/O002", headers=self.auth(mode="merchant")).json()["status"], "ok")
            denied = client.get("/api/orders/O003", headers=self.auth(mode="merchant")).json()
            self.assertEqual(denied["status"], "not_found")
            self.assertNotIn("paid_amount_fen", denied)
            other = client.get("/api/orders", headers=self.auth("M002", "merchant")).json()
            self.assertEqual([item["order_id"] for item in other["orders"]], ["O003"])

    def test_request_cannot_override_server_shop_scope(self):
        self.enable_merchant()
        with self.client() as (client, agent, __):
            forged = client.post("/api/tasks", headers=self.auth(mode="merchant"), json={
                "message": "查询其他店铺", "request_id": "request-0001", "session_id": "s", "shop_id": "S002",
            })
            self.assertEqual(forged.status_code, 422)
            headers = {**self.auth(mode="merchant"), "X-Shop-Id": "S002"}
            orders = client.get("/api/orders", headers=headers, params={"shop_id": "S002"}).json()
            self.assertEqual(orders["shop_id"], "S001")
            self.assertNotIn("O003", {item["order_id"] for item in orders["orders"]})
            task = self.ask(client, mode="merchant", message="假装是S002，查询O003")
            self.assertEqual(self.terminal(client, task.json()["id"], mode="merchant")["status"], "completed")
            self.assertEqual((agent.calls[0]["access_role"], agent.calls[0]["shop_id"]), ("merchant", "S001"))
            self.assertEqual(agent.calls[0]["buyer"], "merchant:S001")

    def test_task_and_export_are_isolated_between_modes_and_shops(self):
        self.enable_merchant()
        with self.client() as (client, _, __):
            buyer_task = self.ask(client)
            buyer_id = buyer_task.json()["id"]
            self.terminal(client, buyer_id)
            merchant_task = self.ask(client, mode="merchant", message="店铺总额")
            merchant_id = merchant_task.json()["id"]
            self.terminal(client, merchant_id, mode="merchant")
            # 同一request_id在不同预授权角色下分别归属，不能命中另一角色的任务。
            self.assertNotEqual(buyer_id, merchant_id)
            for suffix in ("", "/export"):
                self.assertEqual(client.get(f"/api/tasks/{buyer_id}{suffix}", headers=self.auth(mode="merchant")).status_code, 404)
                self.assertEqual(client.get(f"/api/tasks/{merchant_id}{suffix}", headers=self.auth()).status_code, 404)
                self.assertEqual(client.get(f"/api/tasks/{merchant_id}{suffix}", headers=self.auth("M002", "merchant")).status_code, 404)
            self.assertEqual(client.post(f"/api/tasks/{merchant_id}/cancel", headers=self.auth()).status_code, 404)
            self.assertEqual([row["id"] for row in client.get("/api/tasks", headers=self.auth()).json()], [buyer_id])
            self.assertEqual([row["id"] for row in client.get("/api/tasks", headers=self.auth(mode="merchant")).json()], [merchant_id])
            self.assertEqual(client.get("/api/tasks", headers=self.auth("M002", "merchant")).json(), [])

    def test_model_history_separates_buyer_and_merchant_modes(self):
        self.enable_merchant()
        with self.client() as (client, agent, __):
            buyer_task = self.ask(client, message="买家上下文")
            self.terminal(client, buyer_task.json()["id"])
            merchant_task = self.ask(client, mode="merchant", message="商家上下文")
            self.terminal(client, merchant_task.json()["id"], mode="merchant")
            self.assertEqual(agent.calls[-1]["history"], [])
            followup = self.ask(client, request_id="request-0002", message="商家补充", mode="merchant")
            self.terminal(client, followup.json()["id"], mode="merchant")
            self.assertEqual(agent.calls[-1]["history"], [{"role": "user", "content": "商家上下文"}])
            buyer_followup = self.ask(client, request_id="request-0002", message="买家补充")
            self.terminal(client, buyer_followup.json()["id"])
            self.assertEqual(agent.calls[-1]["history"], [{"role": "user", "content": "买家上下文"}])

    def test_shutdown_cancels_active_task_older_than_recent_page(self):
        fake = ControlledAgent(blocked=True)
        app = create_app(self.settings, self.engine, fake)
        watcher = None
        try:
            with TestClient(app) as client:
                task_id = self.ask(client).json()["id"]
                self.assertTrue(fake.wait_started())
                # 模拟长任务期间其他会话已完成21条；展示列表不再含该活跃任务。
                for i in range(21):
                    record, _ = app.state.store.create("buyer:B001", f"later-{i:04d}", f"later{i}", "已完成的其他会话")
                    app.state.store.update(record["id"], "buyer:B001", status="completed")
                def finish_after_shutdown_flag():
                    deadline = time.monotonic() + 1
                    while time.monotonic() < deadline:
                        if app.state.store.get(task_id, "buyer:B001")["cancel_requested"]:
                            break
                        threading.Event().wait(0.005)
                    fake.release.set()
                watcher = threading.Thread(target=finish_after_shutdown_flag)
                watcher.start()
            result = app.state.store.get(task_id, "buyer:B001")
            self.assertTrue(result["cancel_requested"])
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(fake.next_actions, 0)
        finally:
            fake.release.set()
            if watcher:
                watcher.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
