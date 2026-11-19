"""应用配置与本机测试凭证；模型只收到业务问题，不收到这些密钥。"""

import hmac
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class AppSettings:
    model_api_key: str = ""
    model_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-flash"
    max_model_calls: int = 4
    max_tool_calls: int = 6
    model_timeout_seconds: float = 30
    max_output_tokens: int = 1200
    # 整条任务共享截止时间；已发出的远端请求仍需响应/超时收尾。
    task_timeout_seconds: float = 90
    runtime_dir: Path = ROOT / "local" / "runtime"
    tokens_file: Path = ROOT / "local" / "access.json"
    max_concurrent_tasks: int = 2
    max_daily_tasks: int = 30
    model_enabled: bool = False
    # 独立协议服务；普通工具隔离测试默认关闭，正式入口load时按本机配置启用。
    mcp_enabled: bool = False
    memory_enabled: bool = False

    @classmethod
    def load(cls):
        # 应用额外配置独立于原数据库 .env；环境变量可覆盖，但不复制旧工程密钥。
        config = {**dotenv_values(ROOT / ".env"), **dotenv_values(ROOT / "local" / "app.env"), **os.environ}
        key = config.get("DEEPSEEK_API_KEY", "")
        key_file = config.get("MODEL_KEY_FILE")
        if not key and key_file:
            key = dotenv_values(Path(key_file).expanduser()).get("DEEPSEEK_API_KEY", "")
        base = config.get("MODEL_BASE_URL", "https://api.deepseek.com").rstrip("/")
        if base != "https://api.deepseek.com":
            raise ValueError("本版仅配置已授权的 DeepSeek 官方地址；更换服务需单独核对数据和费用范围")
        try:
            daily = int(config.get("MAX_DAILY_TASKS", "30"))
        except (TypeError, ValueError):
            raise ValueError("MAX_DAILY_TASKS须为1—100整数") from None
        if not 1 <= daily <= 100:
            raise ValueError("MAX_DAILY_TASKS须为1—100整数")
        return cls(
            max_daily_tasks=daily,
            model_api_key=key,
            model_base_url=base,
            model_name=config.get("MODEL_NAME", "deepseek-flash"),
            model_enabled=config.get("MODEL_ENABLED", "0") == "1",
            mcp_enabled=config.get("MCP_ENABLED", "1") == "1",
            memory_enabled=config.get("MEMORY_ENABLED", "0") == "1",
        )


def authenticate(token: str, settings: AppSettings) -> str | None:
    """服务器将随机凭证映射为买家；不接受请求体自报 buyer_id。"""
    if not token or not token.isascii() or not settings.tokens_file.is_file():
        return None
    data = json.loads(settings.tokens_file.read_text(encoding="utf-8"))
    for item in data.get("accounts", []):
        if hmac.compare_digest(token, item["token"]):
            return item["buyer_id"]
    return None


def authorize(token: str, mode: str, settings: AppSettings) -> dict | None:
    """同一凭证可有多个预授权角色；请求只能选角色，不能自定买家或店铺。"""
    if not token or not token.isascii() or not settings.tokens_file.is_file():
        return None
    data = json.loads(settings.tokens_file.read_text(encoding="utf-8"))
    for account in data.get("accounts", []):
        if not hmac.compare_digest(token, account["token"]):
            continue
        roles = account.get("roles", [{"role": "buyer", "buyer_id": account.get("buyer_id")}])
        for role in roles:
            if role["role"] != mode:
                continue
            if mode == "buyer" and role.get("buyer_id"):
                scope = {**role, "key": "buyer:" + role["buyer_id"]}
            if mode == "merchant" and role.get("shop_id"):
                scope = {**role, "key": "merchant:" + role["shop_id"]}
            elif mode != "buyer" or not role.get("buyer_id"):
                continue
            # 账号识别人，角色/店铺限定业务权限；同店员工不能共用个人记忆或任务。
            account_id = account.get("account_id")
            if isinstance(account_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,64}", account_id):
                if sum(item.get("account_id") == account_id for item in data["accounts"]) != 1:
                    return None
                scope["account_id"] = account_id
                scope["task_key"] = scope["key"] + ":account:" + account_id
                # 只为迁移前已有账号保留其旧任务；不重写原任务/证据。
                scope["legacy_task_scope"] = scope["key"] if scope["key"] in account.get("legacy_task_scopes", []) else None
            elif settings.memory_enabled:
                # 启用个人记忆后，不允许缺失身份的账号退回店铺共用任务空间。
                return None
            return scope
    return None


def deployment_mode():
    """容器内监听与Mac本机监听显式区分；服务仍由Compose只映射到宿主环回地址。"""
    mode = os.environ.get("DATUS_DEPLOYMENT", "local")
    if mode not in {"local", "container"}:
        raise ValueError("DATUS_DEPLOYMENT 仅允许 local 或 container")
    return mode


def bind_host():
    return "0.0.0.0" if deployment_mode() == "container" else "127.0.0.1"


def mcp_endpoint():
    expected = "http://mcp:18081/mcp" if deployment_mode() == "container" else "http://127.0.0.1:18081/mcp"
    configured = os.environ.get("MCP_URL", expected)
    if configured != expected:
        raise ValueError("协议地址不属于当前受控部署模式")
    return configured
