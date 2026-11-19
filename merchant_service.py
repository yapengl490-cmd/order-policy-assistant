"""商家只读工具：应用传入已授权店铺，查询始终保留店铺归属条件。

同店不同买家可核查，其他店铺和未登记店铺归属的订单均不可见。
该模块不认证商家；调用入口必须先从可信账号映射中取得 shop_id。
"""

from datetime import datetime, timezone

from sqlalchemy import Column, Engine, MetaData, Table, Text, and_, select
from sqlalchemy.orm import Session

from models import Order, Refund
from provenance import read_provenance
from refund_service import (
    MAX_REFUNDS, MAX_SUMMARY_ORDERS, MAX_SUMMARY_ROWS,
    _calculate, _context, _identifier, _order_fields, _time,
)

# 单独映射关联表，不改变原Order类；实际外键由004迁移创建并约束。
shop_metadata = MetaData()
shops = Table("shops", shop_metadata,
              Column("shop_id", Text, primary_key=True), Column("name", Text, nullable=False), schema="public")
order_shops = Table("order_shops", shop_metadata,
                    Column("order_id", Text, primary_key=True), Column("shop_id", Text, nullable=False), schema="public")


def _joined_refunds(shop_id: str, cutoff: datetime):
    # 先限定订单所属店铺，再保留该订单的零条或多条退款；不取消访问范围查全库。
    return (select(Order, Refund)
            .join(order_shops, order_shops.c.order_id == Order.order_id)
            .outerjoin(Refund, and_(Refund.order_id == Order.order_id, Refund.refunded_at < cutoff))
            .where(order_shops.c.shop_id == shop_id))


def check_shop_order(engine: Engine, order_id: str, shop_id: str,
                     refund_before: datetime | None = None) -> dict:
    """商家入口调用；同一语句取得授权店铺订单及退款，再复用买家金额口径。"""
    order_id = _identifier(order_id, "订单编号")
    shop_id = _identifier(shop_id, "店铺编号")
    cutoff = _time(refund_before if refund_before is not None else datetime.now(timezone.utc), "退款截止")
    statement = (_joined_refunds(shop_id, cutoff).where(Order.order_id == order_id)
                 .order_by(Refund.refunded_at, Refund.refund_id).limit(MAX_REFUNDS + 1))
    with Session(engine) as session:
        rows = session.execute(statement).all()
        if not rows:
            return {"status": "not_found", "shop_id": shop_id, "order_id": order_id, **_context(cutoff),
                    "message": "当前店铺范围内未找到订单。"}
        result = _calculate(rows[0][0], [refund for _, refund in rows if refund is not None], cutoff)
        return {**result, "shop_id": shop_id,
                "provenance": read_provenance(session, [("orders", order_id), ("order_shops", order_id)]
                    + [("refunds", r.refund_id) for _, r in rows if r is not None])}


def list_shop_orders(engine: Engine, shop_id: str, limit: int = 20) -> dict:
    """列出授权店铺的最近订单；归属映射缺失时不将订单推定属于本店。"""
    shop_id = _identifier(shop_id, "店铺编号")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("订单列表条数必须为1—50之间的整数")
    statement = (select(Order).join(order_shops, order_shops.c.order_id == Order.order_id)
                 .where(order_shops.c.shop_id == shop_id)
                 .order_by(Order.paid_at.desc(), Order.order_id).limit(limit + 1))
    with Session(engine) as session:
        orders = session.scalars(statement).all()
        return {"status": "ok", "shop_id": shop_id,
                "orders": [{**_order_fields(order), "shop_id": shop_id} for order in orders[:limit]],
                "has_more": len(orders) > limit, "limit": limit,
                "provenance": read_provenance(session, [(kind, o.order_id) for o in orders[:limit]
                    for kind in ("orders", "order_shops")]),
                "currency": "CNY", "money_unit": "fen"}


def summary_shop_orders(engine: Engine, shop_id: str, paid_from: datetime,
                        paid_before: datetime, refund_before: datetime) -> dict:
    """汇总一个授权店铺的支付订单批次；每个订单支付金额仅计一次。"""
    shop_id = _identifier(shop_id, "店铺编号")
    start, end, cutoff = (_time(paid_from, "支付开始"), _time(paid_before, "支付结束"),
                           _time(refund_before, "退款截止"))
    if start >= end:
        raise ValueError("支付开始必须早于支付结束")
    if cutoff < end:
        raise ValueError("退款截止不得早于支付区间结束")
    scope = {"shop_id": shop_id, "paid_from": start.isoformat(), "paid_before": end.isoformat(),
             "payment_boundary": "paid_from <= paid_at < paid_before", **_context(cutoff)}
    statement = (_joined_refunds(shop_id, cutoff).where(Order.paid_at >= start, Order.paid_at < end)
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
            "provenance": read_provenance(session, [(kind, o.order_id) for o, _ in grouped.values()
                for kind in ("orders", "order_shops")]
                + [("refunds", r.refund_id) for _, refunds in grouped.values() for r in refunds]),
            "paid_amount_fen": sum(result["paid_amount_fen"] for result in results),
            "refund_amount_fen": sum(result["refund_amount_fen"] for result in results),
            "net_amount_fen": sum(result["net_amount_fen"] for result in results),
            "orders": [{"shop_id": shop_id, **{key: result[key] for key in (
                "order_id", "buyer_id", "paid_at", "paid_amount_fen", "refund_amount_fen",
                "net_amount_fen", "refund_count",
            )}} for result in results],
        }
