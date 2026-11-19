"""B 单笔核查第一段：用对象映射读取已有订单。助手实现，退款核查待接入。"""

from sqlalchemy import Engine, select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from database import get_engine
from models import Order


def get_order(engine: Engine, order_id: str, current_buyer_id: str) -> dict | None:
    """按订单编号及当前买家读取；无匹配返回 None，数据库故障向外抛出。"""
    # Session 管理本次查询；离开 with 时释放连接，不需要提交只读查询。
    with Session(engine) as session:
        # 买家身份由应用入口提供；两个条件必须同时满足，不能只信订单编号。
        statement = select(Order).where(
            Order.order_id == order_id,
            Order.buyer_id == current_buyer_id,
        )
        order = session.scalar(statement)
        if order is None:
            return None
        # 在会话关闭前取出字段，展示层不需要持有数据库对象或连接。
        return {
            "order_id": order.order_id,
            "buyer_id": order.buyer_id,
            "paid_at": order.paid_at,
            "paid_amount_fen": order.paid_amount_fen,
        }


def main() -> int:
    # 本地测试入口提供模拟身份，不代表已实现登录；不能从聊天自报身份取值。
    current_buyer_id = "B002"
    order_id = "O001"
    engine = None
    try:
        engine = get_engine()
        order = get_order(engine, order_id, current_buyer_id)
    except ValueError as error:
        print(f"配置错误，未执行查询：{error}")
        return 2
    except OperationalError as error:
        if getattr(error.orig, "sqlstate", None) == "57014":
            print("数据库查询超时或被取消，未完成订单读取。")
        else:
            print("数据库连接或执行失败，未完成订单读取；请检查连接配置或数据库状态。")
        return 1
    except SQLAlchemyError as error:
        # 只展示错误类别，不输出可能带有连接信息或参数的原始异常。
        print(f"数据库查询失败，未完成订单读取（{type(error).__name__}）。")
        return 1
    finally:
        if engine is not None:
            engine.dispose()

    if order is None:
        print(f"当前买家范围内未找到订单：{order_id}")
        return 0

    print(f"订单编号：{order['order_id']}")
    print(f"所属买家：{order['buyer_id']}")
    print(f"支付时间：{order['paid_at'].isoformat(sep=' ', timespec='seconds')}")
    print(f"支付金额（分）：{order['paid_amount_fen']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
