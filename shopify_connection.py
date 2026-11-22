"""Shopify 接入第一步：仅验证开发店、应用安装和权限，不读取订单或客户。

设置入口传入本机凭据，固定查询店铺元信息；令牌仅存在内存。
当前不接模型、不同步业务库，也不证明订单/退款/政策业务已通过。
"""

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

API_VERSION = "2026-10"
REQUIRED_SCOPES = frozenset({"read_orders", "read_legal_policies"})
CONNECTION_QUERY = """query OrderPolicyConnectionCheck {
  shop { myshopifyDomain currencyCode ianaTimezone plan { partnerDevelopment } }
  currentAppInstallation { accessScopes { handle } }
}"""


class ShopifyConnectionError(ValueError):
    """仅输出预定义错误；不把服务端原文、请求头或凭据带入错误日志。"""


def normalize_shop_domain(value: str) -> str:
    if not isinstance(value, str):
        raise ShopifyConnectionError("店铺域名必须是 xxx.myshopify.com。")
    domain = value.strip().lower()
    if domain.startswith("https://"):
        domain = domain[8:]
    domain = domain.removesuffix("/")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.myshopify\.com", domain):
        raise ShopifyConnectionError("请填 xxx.myshopify.com；不能填开发控制台、后台管理页或其他网址。")
    return domain


@dataclass(frozen=True)
class ShopifySettings:
    shop_domain: str
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "shop_domain", normalize_shop_domain(self.shop_domain))
        for value in (self.client_id, self.client_secret):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,512}", value):
                raise ShopifyConnectionError("应用编号或密钥格式不完整；请在本机重新复制，不要发送到聊天。")

    @classmethod
    def load(cls, path: Path):
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 8192:
                raise ShopifyConnectionError("本机凭据文件权限或格式不合要求；请重新运行填写入口。")
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or set(data) != {
                    "shop_domain", "client_id", "client_secret", "api_version", "mode"}
                    or data["mode"] != "development" or data["api_version"] != API_VERSION):
                raise ShopifyConnectionError("本机凭据配置不是本轮开发店配置；未发出请求。")
            return cls(data["shop_domain"], data["client_id"], data["client_secret"])
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ShopifyConnectionError("未找到可读的本机Shopify配置，请先运行填写入口。") from None

    def save(self, path: Path):
        save_private_json(path, {
            "shop_domain": self.shop_domain, "client_id": self.client_id,
            "client_secret": self.client_secret, "api_version": API_VERSION, "mode": "development",
        })


def save_private_json(path: Path, data: dict):
    """0600 是本机访问限制，不是静态加密；原子替换避免半份配置。"""
    temporary = None
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise ShopifyConnectionError("本机配置位置不能是符号链接；未保存。")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".shopify-", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise ShopifyConnectionError("本机文件保存失败，原配置未主动删除。") from None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def _response_json(response: httpx.Response, stage: str):
    if response.is_redirect:
        raise ShopifyConnectionError("服务返回重定向，已停止；不会把凭据发送到其他地址。")
    if response.status_code in {401, 403}:
        raise ShopifyConnectionError(f"{stage}未获授权：核对应用已安装、同一组织及本机凭据。")
    if response.status_code == 429:
        raise ShopifyConnectionError("Shopify请求频率受限，本次未自动重试；稍后再检查。")
    if not response.is_success:
        raise ShopifyConnectionError(f"{stage}失败（HTTP {response.status_code}）；服务端原文未输出。")
    if len(response.content) > 262144:
        raise ShopifyConnectionError("接口响应超出本次小型连接检查范围，已停止。")
    try:
        data = response.json()
    except (ValueError, UnicodeError):
        raise ShopifyConnectionError("接口未返回预期数据格式，已停止。") from None
    if not isinstance(data, dict):
        raise ShopifyConnectionError("接口返回结构不符合预期，已停止。")
    return data


def check_connection(settings: ShopifySettings, *, transport=None) -> dict:
    """最多两次HTTPS请求，无自动重试；测试可注入离线传输替身。"""
    origin = "https://" + settings.shop_domain
    try:
        with httpx.Client(timeout=15.0, follow_redirects=False, trust_env=False, transport=transport) as client:
            token_response = client.post(origin + "/admin/oauth/access_token", data={
                "grant_type": "client_credentials", "client_id": settings.client_id,
                "client_secret": settings.client_secret,
            })
            token_data = _response_json(token_response, "应用认证")
            token = token_data.get("access_token")
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,2048}", token):
                raise ShopifyConnectionError("认证未返回可用令牌；检查应用安装与凭据。")
            response = client.post(origin + f"/admin/api/{API_VERSION}/graphql.json",
                                   headers={"X-Shopify-Access-Token": token},
                                   json={"query": CONNECTION_QUERY})
            data = _response_json(response, "店铺连接检查")
            actual_version = response.headers.get("X-Shopify-API-Version", API_VERSION)
            if actual_version != API_VERSION:
                raise ShopifyConnectionError("实际接口版本与本轮配置不一致，已停止。")
    except httpx.TimeoutException:
        raise ShopifyConnectionError("连接检查超时，未自动重试；请检查网络后重试。") from None
    except httpx.HTTPError:
        raise ShopifyConnectionError("连接Shopify失败；请检查网络，未输出请求头或密钥。") from None

    # GraphQL 可在 HTTP 200 下返回 errors，不能把传输成功当成查询成功。
    if data.get("errors"):
        raise ShopifyConnectionError("店铺接口返回查询/权限错误；核对应用版本及安装权限。")
    try:
        shop = data["data"]["shop"]
        if shop["myshopifyDomain"] != settings.shop_domain:
            raise ShopifyConnectionError("返回店铺与配置不一致，已停止。")
        if shop["plan"]["partnerDevelopment"] is not True:
            raise ShopifyConnectionError("未确认是开发测试店，已停止；本入口不读取正式经营订单。")
        raw_scopes = data["data"]["currentAppInstallation"]["accessScopes"]
        if not isinstance(raw_scopes, list):
            raise TypeError
        scopes = {item["handle"] for item in raw_scopes}
        if any(not isinstance(s, str) or not re.fullmatch(r"[a-z0-9_]{1,100}", s) for s in scopes):
            raise TypeError
        missing = REQUIRED_SCOPES - scopes
        if missing:
            raise ShopifyConnectionError("当前安装缺少读取权限：" + ", ".join(sorted(missing)) + "；更新版本后重新批准权限。")
        if scopes - REQUIRED_SCOPES:
            raise ShopifyConnectionError("当前安装包含本轮两项读取权限之外的权限；请先核对最小授权。")
        currency, zone = shop["currencyCode"], shop["ianaTimezone"]
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise TypeError
        if not isinstance(zone, str) or not re.fullmatch(r"[A-Za-z0-9_+./-]{1,100}", zone):
            raise TypeError
    except (KeyError, TypeError, AttributeError):
        raise ShopifyConnectionError("店铺或安装信息不完整，未标记连接通过。") from None
    return {
        "status": "connection_verified", "shop_domain": settings.shop_domain,
        "api_version": API_VERSION, "development_store": True,
        "currency": currency, "timezone": zone, "scopes": sorted(scopes),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "orders_read": False, "customer_data_read": False, "model_called": False,
        "notice": "仅连接、开发店标记与安装权限通过；订单/退款/政策业务尚未验证。",
    }
