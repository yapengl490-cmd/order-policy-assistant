"""管理员显式准备独立合成演示；独立账号/店铺，保留旧配置与事实。

默认只检查文件并显示无凭据计划；--apply才导入并绑定。跨数据库/配置文件
无法组成一个事务，因此先原子导入、再协议凭据、最后开放账号。中断可重跑，
不会换密钥或覆盖冲突事实；不把三步说成跨存储原子事务。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile

from sqlalchemy import inspect
from import_data import (ImportDataError, ImportValidationError, import_payload,
                         load_payload, local_admin_engine, prepare_payload)

ROOT = Path(__file__).resolve().parent
SHOP_ID = "S_UCI_DEMO"
SCOPE_KEY = "merchant:" + SHOP_ID
ACCOUNT_ID = "uci_demo_20261118"
ROLES = [{"role": "merchant", "shop_id": SHOP_ID}]
PROJECTION_SHA256 = '94f8dbb094db1f66ca5ae1f881591c017bb0977a54e1fb4542e3a238fd703459'
MAX_PRIVATE_BYTES = 128 * 1024


class DemoPrepareError(ValueError):
    """仅包含可展示的说明，不拼接配置或驱动异常。"""


def build_demo_payload(root: Path = ROOT) -> dict:
    root = Path(root)
    projection = root / "samples/uci/uci_projection.json"
    try:
        if hashlib.sha256(projection.read_bytes()).hexdigest() != PROJECTION_SHA256:
            raise DemoPrepareError("合成演示样例与冻结版本不一致，已停止；先核对来源，不覆盖旧样本")
        payload = load_payload(projection)
        channels = load_payload(root / "samples/uci/uci_demo_channels.json")
        ids = {row["order_id"] for row in payload["orders"]}
        rows = channels["order_channels"]
        mapped = {row["order_id"] for row in rows}
        unmapped = set(channels["unmapped_order_ids"])
        if (channels["kind"] != "synthetic_channel_fixture" or not isinstance(rows, list)
                or len(mapped) != len(rows) or mapped & unmapped or mapped | unmapped != ids
                or any(not row["channel"].startswith("合成") for row in rows)):
            raise DemoPrepareError("合成渠道必须明确标注且恰好对应冻结订单；不得猜测原平台渠道")
        result = copy.deepcopy(payload)
        result["order_channels"] = rows
        result["metadata"]["source"] += "; 渠道为独立合成映射，未提供项保留unknown"
        result["metadata"]["version"] = channels["version"]
        prepare_payload(result)
        return result
    except DemoPrepareError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ImportDataError):
        raise DemoPrepareError("公开素材或渠道映射格式有误，未连接数据库") from None


def _private_json(path: Path) -> tuple[dict, bytes]:
    """拒绝链接/过大/重复键，不回显私密配置内容。"""
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError
            raw = stream.read(MAX_PRIVATE_BYTES + 1)
        if len(raw) > MAX_PRIVATE_BYTES:
            raise ValueError
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        if not isinstance(data, dict):
            raise ValueError
        return data, raw
    except (OSError, ValueError, UnicodeError):
        raise DemoPrepareError("本机账号/协议配置缺失或无效；先准备原应用配置，不展示凭据") from None


def _validate_accounts(data: dict) -> dict | None:
    accounts = data.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise DemoPrepareError("原应用账号配置为空或无效，不创建替代配置")
    seen_tokens, seen_ids = set(), set()
    found = None
    for row in accounts:
        if not isinstance(row, dict):
            raise DemoPrepareError("原账号配置不合法，未改动")
        token, account_id = row.get("token"), row.get("account_id")
        if (not isinstance(token, str) or not token.isascii() or len(token) < 32
                or token in seen_tokens or (account_id is not None and account_id in seen_ids)):
            raise DemoPrepareError("原账号编号或凭据冲突，未改动")
        seen_tokens.add(token)
        if account_id is not None:
            seen_ids.add(account_id)
        roles = row.get("roles", [{"role": "buyer", "buyer_id": row.get("buyer_id")}])
        if not isinstance(roles, list) or any(not isinstance(role, dict) for role in roles):
            raise DemoPrepareError("原账号角色配置不合法，未改动")
        if account_id == ACCOUNT_ID:
            if roles != ROLES or row.get("legacy_task_scopes") or row.get("buyer_id"):
                raise DemoPrepareError("公开演示账号已有不同权限，拒绝覆盖或扩权")
            found = row
        elif any(role.get("shop_id") == SHOP_ID for role in roles):
            raise DemoPrepareError("该演示店铺已绑定其他账号，需先核授权；未修改原绑定")
    return found


def _validate_mcp(data: dict) -> dict | None:
    from mcp_credentials import ALLOWED_SCOPE_KEYS
    rows = data.get("credentials")
    if data.get("scope") != "local_mcp_demo" or not isinstance(rows, list) or not rows:
        raise DemoPrepareError("原协议凭据配置不合法，未改动")
    seen_keys, seen_tokens, found = set(), set(), None
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"scope_key", "token"}:
            raise DemoPrepareError("原协议凭据格式不合法，未改动")
        key, token = row["scope_key"], row["token"]
        if (key not in ALLOWED_SCOPE_KEYS or key in seen_keys or not isinstance(token, str)
                or not token.isascii() or len(token) < 32 or token in seen_tokens):
            raise DemoPrepareError("原协议范围或凭据冲突，未改动")
        seen_keys.add(key)
        seen_tokens.add(token)
        if key == SCOPE_KEY:
            found = row
    return found


def _replace_if_unchanged(path: Path, expected: bytes, data: dict):
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if raw == expected:
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".uci-demo-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if _private_json(path)[1] != expected:
            raise DemoPrepareError("准备期间配置发生其他修改，保留其内容；核对后重试")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _preparation_lock(local: Path):
    if local.is_symlink() or not local.is_dir():
        raise DemoPrepareError("请先准备原应用本机配置目录，不跟随目录链接")
    descriptor = os.open(local / ".public-demo.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DemoPrepareError("另一个公开演示准备正在执行，请结束后再试") from None
        yield
    finally:
        os.close(descriptor)


def prepare_demo(engine, root: Path = ROOT) -> dict:
    """调用者必须已取得此新账号/店铺绑定授权；不启动服务或发起模型请求。"""
    root = Path(root)
    payload = build_demo_payload(root)
    local = root / "local"
    with _preparation_lock(local):
        access_path, mcp_path = local / "access.json", local / "mcp_access.json"
        access, access_raw = _private_json(access_path)
        mcp, mcp_raw = _private_json(mcp_path)
        account, credential = _validate_accounts(access), _validate_mcp(mcp)
        with engine.connect() as connection:
            schema = connection.get_execution_options().get("schema_translate_map", {}).get("public", "public")
            if any(not inspect(connection).has_table(name, schema=schema)
                   for name in ("import_row_sources", "order_channels", "order_shops")):
                raise DemoPrepareError("来源/渠道/店铺迁移未齐，未开始导入；请按恢复步骤准备")
        # 单批包含原投影和单独标注的合成渠道；发生任何事实冲突由原导入器整批回滚。
        imported = import_payload(engine, payload)
        if _private_json(access_path)[1] != access_raw or _private_json(mcp_path)[1] != mcp_raw:
            raise DemoPrepareError("数据已按事务核对，但配置被其他操作更改；未绑定新账号，核对后幂等重试")
        if credential is None:
            mcp["credentials"].append({"scope_key": SCOPE_KEY, "token": secrets.token_urlsafe(32)})
            _replace_if_unchanged(mcp_path, mcp_raw, mcp)
        if account is None:
            access["accounts"].append({"account_id": ACCOUNT_ID, "label": "公开素材独立演示账号",
                                       "token": secrets.token_urlsafe(32), "roles": copy.deepcopy(ROLES)})
            _replace_if_unchanged(access_path, access_raw, access)
        return {"status": "ready", "account_created": account is None,
                "mcp_credential_created": credential is None, "scope": SCOPE_KEY,
                "import": imported, "url": "http://127.0.0.1:18080",
                "credentials_displayed": False, "feishu_binding_changed": False}


def open_demo(root: Path = ROOT):
    """只复制独立账号凭据到本机剪贴板；不写URL、文件或标准输出。"""
    if sys.platform != "darwin":
        raise DemoPrepareError("便捷打开入口仅适用于Mac；其他系统在本机私密配置中查看该独立账号")
    data, _ = _private_json(Path(root) / "local/access.json")
    account = _validate_accounts(data)
    if account is None:
        raise DemoPrepareError("独立演示账号尚未准备；不要使用原账号扩大店铺权限")
    subprocess.run(["/usr/bin/pbcopy"], input=account["token"].encode(), check=True, capture_output=True)
    subprocess.run(["/usr/bin/open", "http://127.0.0.1:18080"], check=True, capture_output=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="公开素材独立只读演示；默认仅核计划，不导入或绑定")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true", help="授权后显式导入并绑定独立演示账号")
    modes.add_argument("--open", action="store_true", help="复制已准备账号到本机剪贴板并打开本机网页")
    args = parser.parse_args(argv)
    engine = None
    try:
        if args.open:
            open_demo()
            print("独立演示凭据已复制；网页先退出旧账号，再粘贴登录并选择商家。使用后复制其他文字清空剪贴板。")
        elif args.apply:
            build_demo_payload()  # 在读取管理员配置前核对冻结素材。
            engine = local_admin_engine(ROOT / ".env")
            print(json.dumps(prepare_demo(engine), ensure_ascii=False, indent=2))
        else:
            payload = build_demo_payload()
            print(json.dumps({"status": "plan_only", "shop_id": SHOP_ID,
                              "order_count": len(payload["orders"]), "channel_rows": len(payload["order_channels"]),
                              "source_kind": "公开发票数值投影＋合成支付/退款/店铺/渠道",
                              "next": "确认独立账号范围后显式运行 --apply；本次没有连接数据库或绑定权限"}, ensure_ascii=False, indent=2))
        return 0
    except (DemoPrepareError, ImportDataError) as error:
        print("演示准备未完成：" + str(error))
        return 2
    except Exception:
        print("演示准备未完成；未显示配置或底层异常。数据与配置非跨存储事务，核对后重跑可接续，不删除旧数据。")
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
