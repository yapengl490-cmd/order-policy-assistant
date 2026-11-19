"""本项目首次准备读取账号的辅助脚本；日常查询不调用此文件。"""

from pathlib import Path

import psycopg
from dotenv import dotenv_values
from psycopg import sql


def main() -> int:
    config = dotenv_values(Path(__file__).resolve().parent / ".env")
    required = ("POSTGRES_PASSWORD", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
    if any(not config.get(key) for key in required):
        print("初始化配置缺失；请按 .env.example 在本机准备，不要在聊天发送密码。")
        return 2
    # 限定当前本地练习库，避免把此辅助脚本意外用于其他服务。
    if (config["DB_HOST"], config["DB_PORT"], config["DB_NAME"], config["DB_USER"]) != (
        "127.0.0.1", "15432", "datus_core", "datus_reader"
    ):
        print("配置不是本项目的本地初始化目标，已停止。")
        return 2
    try:
        with psycopg.connect(
            host=config["DB_HOST"], port=config["DB_PORT"], dbname=config["DB_NAME"],
            user="postgres", password=config["POSTGRES_PASSWORD"], connect_timeout=5,
        ) as connection:
            exists = connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (config["DB_USER"],)
            ).fetchone()
            if exists:
                print("读取账号已存在，本脚本未更改账号、密码或权限；正常查询无需再次初始化。")
                return 0
            # 标识符和密码按各自类型转义，不通过字符串拼接生成语句。
            role = sql.Identifier(config["DB_USER"])
            connection.execute(sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
            ).format(role, sql.Literal(config["DB_PASSWORD"])))
            connection.execute(sql.SQL("GRANT CONNECT ON DATABASE datus_core TO {}").format(role))
            connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
            connection.execute(sql.SQL(
                "GRANT SELECT ON TABLE public.buyers, public.orders, public.refunds TO {}"
            ).format(role))
    except psycopg.Error as error:
        print(f"读取账号准备失败（{type(error).__name__}）；未输出密码，请核对本机服务和配置。")
        return 1
    print("已准备 datus_reader：可读取当前三张业务表，未授予业务表写入权限。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
