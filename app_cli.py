"""命令行复用同一退款业务；原main.py保留本人B002/O001读取对照。"""
import argparse
from datetime import datetime
import json
from database import get_engine
from refund_service import check_order
from sqlalchemy.exc import SQLAlchemyError


def main():
    parser = argparse.ArgumentParser(description="本机合成数据核查；仅本地教学模拟身份，不是平台登录")
    parser.add_argument("order_id")
    parser.add_argument("--buyer", default="B001")
    parser.add_argument("--before", required=True, help="带时区的退款截止，如2026-11-03T00:00:00+08:00")
    args = parser.parse_args()
    engine = None
    try:
        cutoff = datetime.fromisoformat(args.before)
        engine = get_engine()
        result = check_order(engine, args.order_id, args.buyer, cutoff)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in {"ok", "not_found"} else 1
    except ValueError as error:
        print("参数或配置错误：", error)
        return 2
    except SQLAlchemyError as error:
        print("数据库读取失败，未完成核查：", type(error).__name__)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
