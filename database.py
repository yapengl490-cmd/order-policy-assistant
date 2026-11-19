"""读取本机连接配置并创建连接管理器，不在导入文件时连接数据库。"""

import os
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import URL, Engine, create_engine

ENV_PATH = Path(__file__).resolve().parent / ".env"


def get_engine() -> Engine:
    """由 main 调用；环境变量优先于本项目 .env，缺项时明确停止。"""
    config = {**dotenv_values(ENV_PATH), **os.environ}
    required = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError("缺少数据库配置：" + ", ".join(missing))
    try:
        port = int(config["DB_PORT"])
    except ValueError:
        raise ValueError("DB_PORT 必须是整数端口") from None
    if not 1 <= port <= 65535:
        raise ValueError("DB_PORT 必须在 1—65535 之间")

    # 分字段构造连接地址，避免手拼地址时密码中的特殊字符被误解析。
    url = URL.create(
        "postgresql+psycopg",
        username=config["DB_USER"],
        password=config["DB_PASSWORD"],
        host=config["DB_HOST"],
        port=port,
        database=config["DB_NAME"],
    )
    # Engine 管理连接；真正执行查询时才建立连接。两个超时分别约束连接和单条语句。
    return create_engine(
        url,
        connect_args={
            "connect_timeout": 5,
            "options": "-c timezone=Asia/Shanghai -c statement_timeout=5000",
        },
        hide_parameters=True,
    )


def get_preference_engine() -> Engine:
    """偏好写入使用单独账号；原订单只读连接不增加写权限。"""
    config = {**dotenv_values(ENV_PATH),
              **dotenv_values(ENV_PATH.parent / "local" / "memory.env"), **os.environ}
    required = ("DB_HOST", "DB_PORT", "DB_NAME", "MEMORY_DB_USER", "MEMORY_DB_PASSWORD")
    if any(not config.get(key) for key in required):
        raise ValueError("缺少偏好存储连接配置，请运行本机记忆准备入口")
    port = int(config["DB_PORT"])
    if not 1 <= port <= 65535:
        raise ValueError("数据库端口不合法")
    url = URL.create("postgresql+psycopg", username=config["MEMORY_DB_USER"],
                     password=config["MEMORY_DB_PASSWORD"], host=config["DB_HOST"],
                     port=port, database=config["DB_NAME"])
    return create_engine(url, connect_args={"connect_timeout": 5,
                         "options": "-c timezone=UTC -c statement_timeout=5000"}, hide_parameters=True)
