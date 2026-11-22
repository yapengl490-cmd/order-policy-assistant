"""本机 MCP 专用随机凭据；静态 Bearer 演示，不是 OAuth 或公开账号系统。"""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parent
# 允许项不是账号授权：新范围仍须同时有access角色绑定及独立协议凭据。
ALLOWED_SCOPE_KEYS = frozenset({"buyer:B001", "buyer:B002", "merchant:S001", "merchant:S_UCI_DEMO", "merchant:S_BATCH_1000"})


def _authorized_scopes(root: Path) -> set[str]:
    try:
        data = json.loads((root / "local/access.json").read_text(encoding="utf-8"))
        scopes = set()
        for account in data["accounts"]:
            for role in account.get("roles", [{"role": "buyer", "buyer_id": account.get("buyer_id")} ]):
                kind = role.get("role")
                identity = role.get("buyer_id" if kind == "buyer" else "shop_id")
                key = f"{kind}:{identity}"
                if key in ALLOWED_SCOPE_KEYS:
                    scopes.add(key)
        return scopes
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("无法读取本机预授权角色配置；未展示凭据") from None


def _read_credentials(root: Path) -> list[dict]:
    try:
        data = json.loads((root / "local/mcp_access.json").read_text(encoding="utf-8"))
        rows = data["credentials"]
        if data.get("scope") != "local_mcp_demo" or not isinstance(rows, list) or not rows:
            raise ValueError
        seen_keys, seen_tokens = set(), set()
        for row in rows:
            key, token = row["scope_key"], row["token"]
            if (set(row) != {"scope_key", "token"} or key not in ALLOWED_SCOPE_KEYS
                    or not isinstance(token, str) or not token.isascii() or len(token) < 32
                    or key in seen_keys or token in seen_tokens):
                raise ValueError
            seen_keys.add(key)
            seen_tokens.add(token)
        return rows
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("MCP 专用凭据缺失或格式错误；请检查本机配置，未展示凭据") from None


def prepare_mcp_credentials(root: Path = ROOT) -> dict:
    """从现有合法角色生成独立凭据；已有文件完整保留，不默默扩权或换密钥。"""
    root = Path(root)
    scopes = _authorized_scopes(root)
    if not scopes:
        raise ValueError("没有可用于本机 MCP 的预授权角色")
    path = root / "local/mcp_access.json"
    if path.exists():
        rows = _read_credentials(root)
        return {"created": False, "scope_keys": sorted(row["scope_key"] for row in rows if row["scope_key"] in scopes)}
    payload = {"scope": "local_mcp_demo", "credentials": [
        {"scope_key": key, "token": secrets.token_urlsafe(32)} for key in sorted(scopes)]}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    return {"created": True, "scope_keys": sorted(scopes)}


def load_mcp_credential(scope_key: str, root: Path = ROOT) -> str:
    """可信后端选择范围；模型参数和前端请求体不能调用此函数来申请身份。"""
    root = Path(root)
    if scope_key not in _authorized_scopes(root):
        raise ValueError("该范围未获本机预授权")
    for row in _read_credentials(root):
        if row["scope_key"] == scope_key:
            return row["token"]
    raise ValueError("该范围尚无 MCP 专用凭据")


def authorize_mcp_token(token: str, root: Path = ROOT) -> dict | None:
    """每次请求读取专用凭据与现行角色；不缓存全局当前身份。"""
    if not isinstance(token, str) or not token or not token.isascii():
        return None
    root = Path(root)
    try:
        scopes = _authorized_scopes(root)
        for row in _read_credentials(root):
            if hmac.compare_digest(token, row["token"]) and row["scope_key"] in scopes:
                role, identity = row["scope_key"].split(":", 1)
                return {"role": role, "key": row["scope_key"],
                        "buyer_id" if role == "buyer" else "shop_id": identity}
    except ValueError:
        return None
    return None


if __name__ == "__main__":
    result = prepare_mcp_credentials()
    print("MCP 专用凭据已准备；已有凭据未覆盖，未展示密钥。")
    print("可用范围：" + ", ".join(result["scope_keys"]))
