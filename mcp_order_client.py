"""后端的协议客户端：发现一个获准工具，再经真实HTTP调用原业务服务。

凭据按后端已经认证的范围读取，既不属于模型参数，也不写入调用轨迹。
普通同步工作线程用asyncio.run管理一次连接；没有失败后偷偷改成本地函数的回退。
"""
import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_credentials import load_mcp_credential
from app_config import mcp_endpoint

URL = "http://127.0.0.1:18081/mcp"


class MCPCallError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class OrderProtocolClient:
    def __init__(self, scope_key, *, root=None, url=None, deadline_seconds=None):
        configured = mcp_endpoint()
        url = configured if url is None else url
        if url != configured:
            raise ValueError("协议地址必须是当前受控部署服务")
        self.root = Path(root) if root else Path(__file__).resolve().parent
        self.scope_key, self.url = scope_key, url
        self.last_trace = {}
        self.deadline_seconds = deadline_seconds

    async def _request(self, arguments=None):
        request_id = uuid.uuid4().hex
        token = load_mcp_credential(self.scope_key, root=self.root)
        started = time.monotonic()
        self.last_trace = {"transport": "streamable-http", "request_id": request_id,
                           "operation": "tools/list" if arguments is None else "tools/call",
                           "endpoint": self.url, "tool": "check_order"}
        try:
            async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token,
                    "X-Request-ID": request_id}, timeout=10, trust_env=False,
                    follow_redirects=False) as http:
                async with Client(streamable_http_client(self.url, http_client=http),
                                  read_timeout_seconds=12) as client:
                    listed = await client.list_tools()
                    tools = [tool for tool in listed.tools if tool.name == "check_order"]
                    self.last_trace.update(protocol_version=client.protocol_version,
                        server_name=client.server_info.name if client.server_info else None,
                        discovered_tools=[t.name for t in listed.tools])
                    if len(tools) == 1:
                        tool = tools[0]
                        self.last_trace["schema_sha256"] = hashlib.sha256(json.dumps(tool.input_schema,sort_keys=True).encode()).hexdigest()
                        if arguments is not None:
                            result = await client.call_tool("check_order", arguments)
                            self.last_trace["is_error"] = result.is_error
                            if isinstance(result.meta, dict):
                                self.last_trace["server_request_id"] = result.meta.get("request_id")
                            body = result.structured_content
            # 先正常退出协议传输的任务组，再解释业务拒绝；否则本地异常会被
            # SDK的ExceptionGroup包装，错误参数会被误归类为网络断开。
            if len(tools) != 1:
                raise MCPCallError("mcp_expected_tool_missing")
            if arguments is None:
                return {"type": "function", "function": {"name": tool.name,
                    "description": tool.description, "parameters": tool.input_schema}}
            if result.is_error:
                code = body.get("status", "mcp_tool_error") if isinstance(body, dict) else "mcp_tool_error"
                raise MCPCallError(code if code in {"invalid_arguments", "unknown_tool", "database_error", "busy", "audit_error", "execution_error"} else "mcp_tool_error")
            if not isinstance(body, dict) or not isinstance(body.get("status"), str):
                raise MCPCallError("mcp_invalid_result")
            self.last_trace["business_status"] = body["status"]
            self.last_trace["result_sha256"] = hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            return body
        except MCPCallError:
            raise
        except Exception as exc:
            self.last_trace["error_type"] = type(exc).__name__
            # 异常原文可能携带请求头；只对调用方暴露固定错误类别。
            raise MCPCallError("mcp_transport_failed") from None
        finally:
            self.last_trace["elapsed_seconds"] = round(time.monotonic() - started, 6)

    async def _bounded_request(self, arguments=None):
        if self.deadline_seconds is None:
            return await self._request(arguments)
        remaining = self.deadline_seconds()
        if remaining <= 0:
            raise TimeoutError("task deadline reached before protocol request")
        try:
            # 限制发现、握手、业务调用及清理的总等待；不等于撤销服务端已执行查询。
            return await asyncio.wait_for(self._request(arguments), timeout=remaining)
        except TimeoutError:
            self.last_trace["error_type"] = "TaskDeadlineExceeded"
            raise

    def discover(self):
        return asyncio.run(self._bounded_request())

    def check_order(self, **arguments):
        return asyncio.run(self._bounded_request(arguments))
