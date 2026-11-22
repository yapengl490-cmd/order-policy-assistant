"""本机填写入口；不回显密钥，不默认联网，不触碰现有应用/数据库配置。"""

import argparse
import getpass
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from shopify_connection import ShopifyConnectionError, ShopifySettings, check_connection, save_private_json

ROOT = Path(__file__).resolve().parent


def run_check(root: Path) -> int:
    record = {
        "status": "connection_failed", "checked_at": datetime.now(timezone.utc).isoformat(),
        "orders_read": False, "customer_data_read": False, "model_called": False,
    }
    code = 1
    try:
        record = check_connection(ShopifySettings.load(root / "local" / "shopify.json"))
        code = 0
    except ShopifyConnectionError as error:
        record["message"] = str(error)
    # 每次保留独立结果，失败也更新最新状态，避免旧成功被误当本次通过。
    save_private_json(root / "local" / "shopify_checks" / (uuid4().hex + ".json"), record)
    save_private_json(root / "local" / "shopify_connection_check.json", record)
    if code:
        print(record["message"])
    else:
        print("连接检查通过：开发店及两项读取权限已确认。")
        print(f"店铺：{record['shop_domain']}；币种：{record['currency']}；时区：{record['timezone']}")
        print(record["notice"])
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="填写Shopify凭据，或单独执行开发店连接检查")
    parser.add_argument("--check", action="store_true", help="只读取已保存配置并检查连接，不读订单/调用模型")
    options = parser.parse_args()
    path = ROOT / "local" / "shopify.json"
    try:
        if options.check:
            return run_check(ROOT)
        if not sys.stdin.isatty():
            print("请在本机终端或双击填写入口运行；不要通过聊天输入密钥。")
            return 2
        print("本次只保存配置，不联网、不改店铺、不调用模型。")
        print("填写本人的Shopify开发店；不要在这里配置正式经营店。")
        if path.exists() or path.is_symlink():
            if input("已有Shopify配置；输入 REPLACE 才替换，其他输入保留退出：").strip() != "REPLACE":
                print("已保留原配置。")
                return 0
        domain = input("店铺域名（xxx.myshopify.com）：")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            client_id = getpass.getpass("Client ID（应用编号，粘贴后按回车，内容不显示）：").strip()
            secret = getpass.getpass("Client secret（应用密钥，粘贴后按回车，内容不显示）：").strip()
        settings = ShopifySettings(domain, client_id, secret)
        settings.save(path)
        print("已保存到项目 local/shopify.json，仅当前本机账号可读写；访问限制不等于加密。")
        print("下一步在本机终端运行 python setup_shopify.py --check。密钥不要发送到聊天。")
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\n已取消，未主动删除原配置。")
        return 2
    except getpass.GetPassWarning:
        print("当前终端不能隐藏输入，已停止；请使用本机终端。")
        return 2
    except ShopifyConnectionError as error:
        print(str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
