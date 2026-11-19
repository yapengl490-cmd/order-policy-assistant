"""独立本机 Streamable HTTP MCP 服务；只读订单工具和每请求可信授权。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
import fcntl
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_runtime import CheckOrderArgs
from app_config import bind_host, deployment_mode
from database import get_engine
from mcp_credentials import ROOT, authorize_mcp_token
from merchant_service import check_shop_order
from refund_service import check_order

HOST, PORT = bind_host(), 18081
SERVICE_NAME = "datus-order-mcp"
MAX_BODY_BYTES = 16 * 1024
ALLOWED_HOSTS = {"127.0.0.1:18081", "localhost:18081"}
ALLOWED_ORIGINS = {"http://127.0.0.1:18081", "http://localhost:18081"}
if deployment_mode() == "container":
    ALLOWED_HOSTS.add("mcp:18081")
    ALLOWED_ORIGINS.add("http://mcp:18081")


class LocalAuthorizationMiddleware:
    def __init__(self, app, root):
        self.app, self.root = app, root

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        if headers.get("host") not in ALLOWED_HOSTS or (headers.get("origin") is not None and headers["origin"] not in ALLOWED_ORIGINS):
            return await JSONResponse({"detail": "不允许的本机 Host 或 Origin"}, 403)(scope, receive, send)
        if scope["path"].startswith("/mcp"):
            authorization = headers.get("authorization", "")
            scheme, _, token = authorization.partition(" ")
            access = authorize_mcp_token(token, self.root) if scheme.lower() == "bearer" else None
            if access is None:
                return await JSONResponse({"detail": "需要有效的 MCP 专用本机凭据"}, 401,
                                          headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
            # request.scope.state 随本次请求传播，禁止用进程全局变量记录当前用户。
            scope.setdefault("state", {})["mcp_access"] = access
        await self.app(scope, receive, send)


class AuditLog:
    def __init__(self, directory):
        self.path = directory / "events.jsonl"
        self.lock = threading.Lock()

    def append(self, event):
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        with self.lock:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())


def create_mcp_app(root: Path = ROOT, engine=None):
    root = Path(root)
    own_engine = engine is None
    engine = engine if engine is not None else get_engine()
    directory = root / "local/mcp"
    audit = AuditLog(directory)
    app = None

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(
            name="check_order", description="按服务端预授权买家或店铺范围核查单笔订单；只读，不接受身份参数。",
            input_schema=CheckOrderArgs.model_json_schema(),
            annotations=types.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))])

    async def call_tool(ctx, params):
        started = time.monotonic()
        access = ctx.request.scope.get("state", {}).get("mcp_access") if ctx.request is not None else None
        normalized, acquired = None, False
        try:
            if access is None:
                result, is_error = {"status": "unauthorized", "message": "本次请求缺少可信授权"}, True
            elif params.name != "check_order":
                result, is_error = {"status": "unknown_tool", "message": "仅提供 check_order 工具"}, True
            else:
                args = CheckOrderArgs.model_validate(params.arguments or {})
                normalized = args.model_dump()
                try:
                    app.state.tool_slots.acquire_nowait()
                    acquired = True
                except anyio.WouldBlock:
                    result, is_error = {"status": "busy", "message": "当前已有两个订单核查，请稍后重试"}, True
                else:
                    cutoff = datetime.fromisoformat(args.refund_before) if args.refund_before is not None else None
                    function = (partial(check_order, engine, args.order_id, access["buyer_id"], cutoff)
                                if access["role"] == "buyer" else partial(check_shop_order, engine, args.order_id, access["shop_id"], cutoff))
                    result = await anyio.to_thread.run_sync(function, abandon_on_cancel=False)
                    is_error = result.get("status") not in {"ok", "not_found"}
        except (ValidationError, ValueError):
            result, is_error = {"status": "invalid_arguments", "message": "工具参数无效；仅允许订单编号和带时区的退款截止"}, True
        except SQLAlchemyError:
            result, is_error = {"status": "database_error", "message": "数据库读取未完成，未得出订单结论"}, True
        except Exception:
            result, is_error = {"status": "execution_error", "message": "工具执行未完成，请按请求编号核查"}, True
        finally:
            if acquired:
                app.state.tool_slots.release()
        text = json.dumps(result, ensure_ascii=False, sort_keys=True)
        raw_id = str(ctx.request_id)
        # 协议序号在每次新连接中可能重复；另存关联编号串起应用和服务审计。
        incoming = ctx.request.headers.get("x-request-id", "") if ctx.request is not None else ""
        request_id = incoming if re.fullmatch(r"[a-zA-Z0-9_-]{8,64}", incoming) else uuid.uuid4().hex
        event = {"request_id": request_id, "tool": params.name if params.name == "check_order" else "unknown",
                 "rpc_id": raw_id[:64],
                 "arguments": normalized, "scope_key": access["key"] if access else None,
                 "status": result["status"], "result_sha256": hashlib.sha256(text.encode()).hexdigest(),
                 "duration_ms": round((time.monotonic() - started) * 1000, 3),
                 "protocol": ctx.protocol_version, "recorded_at": time.time()}
        try:
            audit.append(event)
        except OSError:
            result, is_error = {"status": "audit_error", "message": "工具审计记录未完成；本次结果未交付"}, True
            text = json.dumps(result, ensure_ascii=False)
        return types.CallToolResult(content=[types.TextContent(text=text)], structured_content=result, is_error=is_error,
                                    _meta={"request_id": request_id, "protocol": ctx.protocol_version})

    async def health(request):
        return JSONResponse({"service": SERVICE_NAME, "status": "ok"})

    server = Server(SERVICE_NAME, version="1.0.0", on_list_tools=list_tools, on_call_tool=call_tool)
    app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, max_request_body_size=MAX_BODY_BYTES,
        transport_security=TransportSecuritySettings(allowed_hosts=sorted(ALLOWED_HOSTS), allowed_origins=sorted(ALLOWED_ORIGINS)),
        custom_starlette_routes=[Route("/health", health)], host=HOST)
    sdk_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = (directory / "server.lock").open("a")
        pid_file, locked = directory / "server.pid", False
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            pid_file.write_text(str(os.getpid()), encoding="ascii")
            application.state.tool_slots = anyio.Semaphore(2)
            async with sdk_lifespan(application):
                yield
        finally:
            if own_engine:
                engine.dispose()
            if locked and pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
                pid_file.unlink()
            handle.close()

    app.router.lifespan_context = lifespan
    app.add_middleware(LocalAuthorizationMiddleware, root=root)
    return app


def main():
    import uvicorn
    uvicorn.run(create_mcp_app(), host=HOST, port=PORT, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
