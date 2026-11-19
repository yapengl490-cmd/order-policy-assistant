"""确定性业务工具：读取有权限的订单、筛退款并用整数分核对金额。

调用方负责提供可信买家身份；模型只能提供业务条件，不能选择身份。
当前表约定只保存已支付订单、成功退款。服务不替模型解释政策，也不发起退款。
"""

from datetime import datetime, timezone

from sqlalchemy import Engine, and_, select
from sqlalchemy.orm import Session

from models import Order, Refund
from provenance import read_provenance

MAX_REFUNDS = 200
MAX_SUMMARY_ORDERS = 500
MAX_SUMMARY_ROWS = 5000


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(f"{name}必须是1—128字符的非空字符串")
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError(f"{name}不能含首尾空白或控制字符")
    return value


def _time(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name}必须是带时区的时间")
    return value.astimezone(timezone.utc)


def _context(refund_before: datetime) -> dict:
    return {
        "refund_before": refund_before.isoformat(),
        "refund_boundary": "refunded_at < refund_before",
        "currency": "CNY",
        "money_unit": "fen",
        "data_assumption": "仅含规范化的已支付订单与成功退款；不代表支付平台实时状态",
    }


def _order_fields(order: Order) -> dict:
    return {
        "order_id": order.order_id,
        "buyer_id": order.buyer_id,
        "paid_at": order.paid_at.isoformat(),
        "paid_amount_fen": order.paid_amount_fen,
    }


def _calculate(order: Order, refunds: list[Refund], cutoff: datetime) -> dict:
    """已完成授权和范围筛选后才计算；异常数据不伪装成正常零值。"""
    fields = _order_fields(order)
    if order.paid_at >= cutoff:
        return {"status": "not_yet_paid", **fields, **_context(cutoff),
                "message": "该订单支付时间不早于本次截止时刻，未计算净额。"}
    if len(refunds) > MAX_REFUNDS:
        return {"status": "too_many_refunds", **fields, **_context(cutoff),
                "message": f"符合条件的退款超过{MAX_REFUNDS}条，未返回部分金额作为总额。"}
    paid = order.paid_amount_fen
    valid_paid = type(paid) is int and 0 <= paid <= 9223372036854775807
    invalid = [refund for refund in refunds if (
        type(refund.refund_amount_fen) is not int or refund.refund_amount_fen <= 0
        or refund.refunded_at < order.paid_at
    )]
    total = sum(refund.refund_amount_fen for refund in refunds) if not invalid else None
    if not valid_paid or invalid or total > paid:
        return {"status": "data_error", **fields, **_context(cutoff),
                "message": "金额或退款时间存在异常，不能给出可信净额；需核对源记录。"}
    return {
        "status": "ok", **fields, **_context(cutoff),
        "refund_amount_fen": total,
        "net_amount_fen": paid - total,
        "refund_count": len(refunds),
        "refunds": [{
            "refund_id": refund.refund_id,
            "refunded_at": refund.refunded_at.isoformat(),
            "refund_amount_fen": refund.refund_amount_fen,
        } for refund in refunds],
    }


def check_order(engine: Engine, order_id: str, current_buyer_id: str,
                refund_before: datetime | None = None) -> dict:
    """入口传入编号、可信身份、可选截止；返回金额/依据或明确业务状态。

    未指定截止时只捕获一次当前时间。支付月份不限制单笔查询。
    一个查询同时读取订单与退款，避免两个查询间新增退款导致快照不一致。
    数据库故障向上抛出，调用方不得将故障转换成“未找到”或零退款。
    """
    order_id = _identifier(order_id, "订单编号")
    buyer_id = _identifier(current_buyer_id, "买家编号")
    cutoff = _time(refund_before if refund_before is not None else datetime.now(timezone.utc), "退款截止")
    # 截止条件放在连接条件里，保留没有退款的订单；放在WHERE会误删这类订单。
    statement = (
        select(Order, Refund).outerjoin(Refund, and_(
            Refund.order_id == Order.order_id, Refund.refunded_at < cutoff,
        ))
        .where(Order.order_id == order_id, Order.buyer_id == buyer_id)
        .order_by(Refund.refunded_at, Refund.refund_id).limit(MAX_REFUNDS + 1)
    )
    with Session(engine) as session:
        rows = session.execute(statement).all()
        if not rows:
            return {"status": "not_found", "order_id": order_id, **_context(cutoff),
                    "message": "当前买家范围内未找到订单。"}
        order = rows[0][0]
        refunds = [refund for _, refund in rows if refund is not None]
        result = _calculate(order, refunds, cutoff)
        result["provenance"] = read_provenance(session,
            [("orders", order.order_id)] + [("refunds", r.refund_id) for r in refunds])
        return result


def list_orders(engine: Engine, current_buyer_id: str, limit: int = 20) -> dict:
    """列出当前买家最近订单供选单；不是全店列表，也不隐式查询其他买家。"""
    buyer_id = _identifier(current_buyer_id, "买家编号")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("订单列表条数必须为1—50之间的整数")
    statement = (select(Order).where(Order.buyer_id == buyer_id)
                 .order_by(Order.paid_at.desc(), Order.order_id).limit(limit + 1))
    with Session(engine) as session:
        orders = session.scalars(statement).all()
        return {"status": "ok", "orders": [_order_fields(order) for order in orders[:limit]],
                "has_more": len(orders) > limit, "limit": limit,
                "provenance": read_provenance(session, [("orders", o.order_id) for o in orders[:limit]]),
                "currency": "CNY", "money_unit": "fen"}


def summary_orders(engine: Engine, current_buyer_id: str, paid_from: datetime,
                   paid_before: datetime, refund_before: datetime) -> dict:
    """原A口径在本人范围内复用：支付区间选订单，截止前成功退款求净额。

    按订单编号分组后每笔支付只计一次；不是按相同金额去重，也不是现金流。
    商家跨买家查询要另有可信店铺授权，当前接口不提供这种权限。
    """
    buyer_id = _identifier(current_buyer_id, "买家编号")
    start, end, cutoff = (_time(paid_from, "支付开始"), _time(paid_before, "支付结束"),
                           _time(refund_before, "退款截止"))
    if start >= end:
        raise ValueError("支付开始必须早于支付结束")
    if cutoff < end:
        raise ValueError("退款截止不得早于支付区间结束")
    scope = {"paid_from": start.isoformat(), "paid_before": end.isoformat(),
             "payment_boundary": "paid_from <= paid_at < paid_before", **_context(cutoff)}
    statement = (select(Order, Refund).outerjoin(Refund, and_(
        Refund.order_id == Order.order_id, Refund.refunded_at < cutoff,
    )).where(Order.buyer_id == buyer_id, Order.paid_at >= start, Order.paid_at < end)
        .order_by(Order.order_id, Refund.refunded_at, Refund.refund_id).limit(MAX_SUMMARY_ROWS + 1))
    with Session(engine) as session:
        rows = session.execute(statement).all()
        if len(rows) > MAX_SUMMARY_ROWS:
            return {"status": "too_many_refunds", **scope,
                    "message": "关联明细超过核查上限，未返回部分汇总；请缩小支付范围。"}
        grouped: dict[str, tuple[Order, list[Refund]]] = {}
        for order, refund in rows:
            if order.order_id not in grouped:
                grouped[order.order_id] = (order, [])
            if refund is not None:
                grouped[order.order_id][1].append(refund)
        if len(grouped) > MAX_SUMMARY_ORDERS:
            return {"status": "too_many_orders", **scope,
                    "message": f"订单超过{MAX_SUMMARY_ORDERS}笔，未返回部分汇总；请缩小支付范围。"}
        results = [_calculate(order, refunds, cutoff) for order, refunds in grouped.values()]
        errors = [result for result in results if result["status"] != "ok"]
        if errors:
            return {"status": "data_error", **scope,
                    "message": "所选订单中存在不能可靠汇总的记录；未输出部分总额。",
                    "errors": [{"order_id": error["order_id"], "status": error["status"]} for error in errors]}
        return {
            "status": "ok", **scope, "order_count": len(results),
            "provenance": read_provenance(session, [("orders", o.order_id) for o, _ in grouped.values()]
                + [("refunds", r.refund_id) for _, refunds in grouped.values() for r in refunds]),
            "paid_amount_fen": sum(result["paid_amount_fen"] for result in results),
            "refund_amount_fen": sum(result["refund_amount_fen"] for result in results),
            "net_amount_fen": sum(result["net_amount_fen"] for result in results),
            "orders": [{key: result[key] for key in (
                "order_id", "paid_at", "paid_amount_fen", "refund_amount_fen",
                "net_amount_fen", "refund_count",
            )} for result in results],
        }
