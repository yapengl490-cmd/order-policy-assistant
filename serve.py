"""只向本机开放的网页入口；订单主库仍为原PostgreSQL。"""
import os
from pathlib import Path
import uvicorn
from app_server import create_app
from app_config import bind_host

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    (root / "local").mkdir(mode=0o700, exist_ok=True)
    app = create_app()
    # 应用取得单实例锁后才登记PID；第二次误启动不会覆盖已有服务记录。
    uvicorn.run(app, host=bind_host(), port=18080, access_log=False, log_level="info")
