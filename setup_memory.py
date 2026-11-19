"""只为本机已有项目准备个人账号标识及偏好存储；不改业务数据/原查询权限。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import secrets
import uuid

import psycopg
from psycopg import sql
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent


def private_write(path: Path, text: str):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as file:
        os.chmod(temp, 0o600)
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    temp.replace(path)


def prepare_identities(root=ROOT):
    """稳定账号不依赖可轮换的登录密钥；既有任务只授权原账号查看。"""
    path = root / "local/access.json"
    original = path.read_text(encoding="utf-8")
    data = json.loads(original)
    accounts = data["accounts"]
    scopes = []
    for account in accounts:
        roles = account.get("roles", [{"role": "buyer", "buyer_id": account.get("buyer_id")}])
        scopes.append([r["role"] + ":" + r[field] for r in roles
                       for field in ["buyer_id" if r["role"] == "buyer" else "shop_id"] if r.get(field)])
    changed = False
    for account, owned in zip(accounts, scopes):
        if not account.get("account_id"):
            if any(sum(scope in row for row in scopes) != 1 for scope in owned):
                raise ValueError("多个旧账号共用范围，需要先确认旧任务归属，未自动分配")
            account["account_id"] = "acct_" + uuid.uuid4().hex
            account["legacy_task_scopes"] = owned
            changed = True
    ids = [a["account_id"] for a in accounts]
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", value) for value in ids):
        raise ValueError("已有账号编号格式不符，未启用记忆或覆盖身份")
    if len(ids) != len(set(ids)):
        raise ValueError("账号编号重复，未更改")
    if changed:
        backup = root / "local/access.before-memory.json"
        if not backup.exists():
            private_write(backup, original)
        private_write(path, json.dumps(data, ensure_ascii=False, indent=2))
    return changed


def prepare(root=ROOT):
    config = dotenv_values(root / ".env")
    if (config.get("DB_HOST"), config.get("DB_PORT"), config.get("DB_NAME")) != ("127.0.0.1", "15432", "datus_core"):
        raise ValueError("仅允许准备本项目本机数据库")
    runtime = root / "local/runtime"
    if any(json.loads(p.read_text())["status"] in {"running", "queued", "cancelling"}
           for p in runtime.glob("task-*.json")):
        raise ValueError("仍有运行任务，请待其结束再准备记忆存储")
    path = root / "local/memory.env"
    if not path.exists():
        private_write(path, "# 偏好专用账号，不提交/不在聊天展示。\nMEMORY_DB_USER=datus_memory\nMEMORY_DB_PASSWORD="
                      + secrets.token_urlsafe(36) + "\n")
    memory_config = dotenv_values(path)
    if memory_config.get("MEMORY_DB_USER") != "datus_memory" or not memory_config.get("MEMORY_DB_PASSWORD"):
        raise ValueError("偏好账号配置不符，保留原配置并停止")
    with psycopg.connect(host=config["DB_HOST"], port=config["DB_PORT"], dbname=config["DB_NAME"],
                         user="postgres", password=config["POSTGRES_PASSWORD"], connect_timeout=5) as connection:
        connection.execute((root / "sql/006_preferences.sql").read_text())
        columns = dict(connection.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='app_memory' AND table_name='preferences'").fetchall())
        expected = {"account_id": "character varying", "role": "character varying", "values": "jsonb",
                    "version": "bigint", "updated_at": "timestamp with time zone", "source": "jsonb"}
        if columns != expected:
            raise ValueError("已有偏好表结构不符，未覆盖原表")
        constraints = {row[0] for row in connection.execute("SELECT conname FROM pg_constraint WHERE conrelid='app_memory.preferences'::regclass").fetchall()}
        if not {"preferences_pkey", "preferences_account_id_valid", "preferences_role_valid",
                "preferences_version_nonnegative", "preferences_values_valid", "preferences_source_valid"} <= constraints:
            raise ValueError("已有偏好表约束不完整，未自动覆盖")
        exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname='datus_memory'").fetchone()
        if not exists:
            connection.execute(sql.SQL("CREATE ROLE datus_memory LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT").format(sql.Literal(memory_config["MEMORY_DB_PASSWORD"])))
        flags = connection.execute("SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname='datus_memory'").fetchone()
        memberships = connection.execute("SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname='datus_memory')").fetchone()
        if any(flags) or memberships:
            raise ValueError("已有同名账号权限范围异常，未扩展或重置")
        connection.execute("GRANT CONNECT ON DATABASE datus_core TO datus_memory")
        connection.execute("GRANT USAGE ON SCHEMA app_memory TO datus_memory")
        connection.execute("GRANT SELECT, INSERT, UPDATE ON app_memory.preferences TO datus_memory")
    # 已有同名账号不重设密码，先验证本机保存的配置确实可用。
    with psycopg.connect(host=config["DB_HOST"], port=config["DB_PORT"], dbname=config["DB_NAME"],
                         user="datus_memory", password=memory_config["MEMORY_DB_PASSWORD"], connect_timeout=5) as connection:
        connection.execute("SELECT version FROM app_memory.preferences LIMIT 1")
    prepare_identities(root)
    app_path = root / "local/app.env"
    original = app_path.read_text()
    lines = [line for line in original.splitlines() if not line.startswith("MEMORY_ENABLED=")]
    private_write(app_path, "\n".join(lines) + "\nMEMORY_ENABLED=1\n")
    print("偏好表和专用连接已准备；原订单账号仍只读，原凭证和业务数据保留。")


if __name__ == "__main__":
    try:
        prepare()
    except Exception as error:
        print(f"记忆准备未完成（{type(error).__name__}）；未输出凭据，请核对本机配置及数据库。")
        raise SystemExit(1)
