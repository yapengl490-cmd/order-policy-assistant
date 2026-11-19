"""准备本机演示访问凭证；已存在则不覆盖，不设置公开注册或真实商家账号。"""
import json
import os
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parent


def prepare():
    local = ROOT / "local"
    local.mkdir(mode=0o700, exist_ok=True)
    path = local / "access.json"
    if not path.exists():
        data = {"scope": "synthetic_local_demo", "accounts": [
            {"label": "本人演示账号", "token": secrets.token_urlsafe(32), "roles": [
                {"role": "buyer", "buyer_id": "B001"},
                {"role": "merchant", "shop_id": "S001"},
            ]},
            {"label": "隔离测试买家", "token": secrets.token_urlsafe(32), "roles": [
                {"role": "buyer", "buyer_id": "B002"},
            ]},
        ]}
        with path.open("x", encoding="utf-8") as file:
            os.chmod(path, 0o600)
            json.dump(data, file, ensure_ascii=False, indent=2)
        print("已生成本机测试凭证；未展示其内容。本人演示账号具有买家B001和商家S001两个只读角色。")
    else:
        print("本机凭证已存在，保留原配置。")
    from mcp_credentials import prepare_mcp_credentials
    prepare_mcp_credentials(root=ROOT)
    print("本机协议服务凭据已准备，原凭据保留；内容不显示。")
    env = local / "app.env"
    if not env.exists():
        env.write_text("# 本机配置，不提交。沿用已授权DeepSeek密钥文件，实际调用前核余额。\nMODEL_ENABLED=0\nMODEL_NAME=deepseek-flash\nMODEL_BASE_URL=https://api.deepseek.com\nMODEL_KEY_FILE=\n", encoding="utf-8")
        os.chmod(env, 0o600)
        print("已生成local/app.env配置模板，模型默认关闭。")


if __name__ == "__main__":
    prepare()
