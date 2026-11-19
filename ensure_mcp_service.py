"""本机协议服务便捷入口：核身份、启动、等待就绪，不停止占端口的其他服务。"""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
HEALTH = "http://127.0.0.1:18081/health"


def health():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(HEALTH, timeout=2) as response:
            value = json.load(response)
        return value
    except Exception:
        return None


def ensure():
    from mcp_credentials import prepare_mcp_credentials
    prepare_mcp_credentials(root=ROOT)
    result = health()
    if result and result.get("service") == "datus-order-mcp":
        print("协议服务已运行，复用现有服务。")
        return
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", 18081)) == 0:
            raise RuntimeError("18081端口已有服务但未通过本项目健康检查，未覆盖或停止它。")
    folder = ROOT / "local" / "mcp"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = folder / "service.log"
    with log.open("ab") as out:
        log.chmod(0o600)
        child = subprocess.Popen([sys.executable, str(ROOT / "mcp_order_server.py")],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
    for _ in range(80):
        result = health()
        if result and result.get("service") == "datus-order-mcp":
            print("协议服务已就绪：http://127.0.0.1:18081/mcp（请求需要专用凭据）。")
            return
        if child.poll() is not None:
            raise RuntimeError("协议服务未启动，请查看local/mcp/service.log；未修改数据库。")
        time.sleep(0.25)
    raise RuntimeError("协议服务启动未在等待时间内就绪；请查看local/mcp/service.log。")


if __name__ == "__main__":
    try:
        ensure()
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(1)
