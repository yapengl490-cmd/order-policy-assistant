"""两期支付订单批次的固定变化核查，不让模型计算金额或推断变化原因。

调用方传入可信角色及范围；一条查询取得两个支付区间的订单、截止前退款和
可选渠道。对同一截止时刻作比较，因此不是分别按各月发生退款统计的现金流。
"""
from datetime import datetime
from sqlalchemy import Column, MetaData, Table, Text, and_, inspect, literal, or_, select
from sqlalchemy.orm import Session
from merchant_service import order_shops
from models import Order, Refund
from provenance import read_provenance
from refund_service import MAX_SUMMARY_ORDERS, MAX_SUMMARY_ROWS, _calculate, _identifier, _time

metadata = MetaData(schema="public")
order_channels = Table("order_channels", metadata,
    Column("order_id", Text, primary_key=True), Column("channel", Text, nullable=False))
MONEY_FIELDS = ("paid_amount_fen", "refund_amount_fen", "net_amount_fen")
TOTAL_FIELDS = ("order_count", *MONEY_FIELDS)


def _totals(rows):
    return {"order_count": len(rows), **{field: sum(row[field] for row in rows) for field in MONEY_FIELDS}}


def _delta(first, second):
    return {field: second[field] - first[field] for field in TOTAL_FIELDS}


def compare_periods(engine, access: dict, first_from: datetime, first_before: datetime,
                    second_from: datetime, second_before: datetime, refund_before: datetime) -> dict:
    """返回两期金额/差额/渠道贡献/来源；参数失败抛ValueError，数据库失败不报零。

    access由服务器认证后传入，不能来自模型参数。旧库未迁移渠道表时保留总体
    比较、全部标unknown并明确not_migrated；不从店铺名或订单号猜渠道。
    """
    if not isinstance(access, dict) or access.get("role") not in {"buyer", "merchant"}:
        raise ValueError("需要程序提供可信买家或商家范围")
    role = access["role"]
    scope_id = _identifier(access.get("buyer_id") if role == "buyer" else access.get("shop_id"),
                           "可信买家编号" if role == "buyer" else "可信店铺编号")
    start1,end1,start2,end2,cutoff = [_time(value,name) for value,name in (
        (first_from,"第一期开始"),(first_before,"第一期结束"),(second_from,"第二期开始"),
        (second_before,"第二期结束"),(refund_before,"共同退款截止"))]
    if not start1 < end1 <= start2 < end2:
        raise ValueError("两期必须依时间先后排列、各有正长度且不重叠；区间上界不含")
    if cutoff < end2:
        raise ValueError("共同退款截止不得早于第二期支付区间结束")
    scope = {"role":role, "buyer_id" if role=="buyer" else "shop_id":scope_id}
    conditions = {"first":{"paid_from":start1.isoformat(),"paid_before":end1.isoformat()},
                  "second":{"paid_from":start2.isoformat(),"paid_before":end2.isoformat()},
                  "refund_before":cutoff.isoformat(),"payment_boundary":"start <= paid_at < end",
                  "refund_boundary":"refunded_at < refund_before"}
    common = {"scope":scope,"conditions":conditions,"currency":"CNY","money_unit":"fen",
              "comparison_kind":"payment_cohort_same_refund_cutoff",
              "reason_status":"unknown", "reason_notice":"仅核算金额变化及渠道贡献，现有数据不足以判断变化原因。"}
    with Session(engine) as session:
        conn=session.connection()
        schema=conn.get_execution_options().get("schema_translate_map",{}).get("public","public")
        channel_table_present=inspect(conn).has_table("order_channels",schema=schema)
        channel_column=order_channels.c.channel if channel_table_present else literal(None)
        statement=(select(Order,Refund,channel_column)
            .outerjoin(Refund,and_(Refund.order_id==Order.order_id,Refund.refunded_at<cutoff)))
        if channel_table_present:
            statement=statement.outerjoin(order_channels,order_channels.c.order_id==Order.order_id)
        if role=="merchant":
            statement=statement.join(order_shops,order_shops.c.order_id==Order.order_id).where(order_shops.c.shop_id==scope_id)
        else:
            statement=statement.where(Order.buyer_id==scope_id)
        statement=(statement.where(or_(and_(Order.paid_at>=start1,Order.paid_at<end1),
                                         and_(Order.paid_at>=start2,Order.paid_at<end2)))
                   .order_by(Order.order_id,Refund.refunded_at,Refund.refund_id).limit(MAX_SUMMARY_ROWS+1))
        rows=session.execute(statement).all()
        if len(rows)>MAX_SUMMARY_ROWS:
            return {"status":"too_many_rows",**common,"message":"两期关联明细超过上限，未输出部分比较；请缩小区间。"}
        grouped={}
        for order,refund,channel in rows:
            if order.order_id not in grouped:
                grouped[order.order_id]={"order":order,"refunds":[],"channel":channel or "unknown","has_channel":channel is not None}
            if refund is not None:grouped[order.order_id]["refunds"].append(refund)
        if len(grouped)>MAX_SUMMARY_ORDERS:
            return {"status":"too_many_orders",**common,"message":"两期订单数超过上限，未输出部分比较；请缩小区间。"}
        periods={"first":[],"second":[]}; channels={}; refs=[]; order_evidence=[]
        for item in grouped.values():
            order,refunds,channel=item["order"],item["refunds"],item["channel"]
            result=_calculate(order,refunds,cutoff)
            if result["status"]!="ok":
                return {"status":"data_error",**common,"message":"所选订单有异常，未输出部分比较。",
                        "errors":[{"order_id":order.order_id,"status":result["status"]}]}
            period="first" if start1<=order.paid_at<end1 else "second"
            periods[period].append(result)
            channels.setdefault(channel,{"first":[],"second":[]})[period].append(result)
            refs.append(("orders",order.order_id))
            refs.extend(("refunds",r.refund_id) for r in refunds)
            if role=="merchant":refs.append(("order_shops",order.order_id))
            if channel_table_present and item["has_channel"]:refs.append(("order_channels",order.order_id))
            order_evidence.append({"order_id":order.order_id,"period":period,"channel":channel,
                **{field:result[field] for field in MONEY_FIELDS},"refund_count":result["refund_count"]})
        first,second=_totals(periods["first"]),_totals(periods["second"])
        contributions=[]
        for channel,by_period in sorted(channels.items()):
            a,b=_totals(by_period["first"]),_totals(by_period["second"])
            presence="new" if not a["order_count"] else "disappeared" if not b["order_count"] else "continued"
            contributions.append({"channel":channel,"label":"未提供渠道" if channel=="unknown" else channel,
                "presence":presence,"first":a,"second":b,"delta":_delta(a,b),
                "contribution_net_fen":b["net_amount_fen"]-a["net_amount_fen"],"reason_status":"unknown"})
        return {"status":"ok",**common,"first":first,"second":second,"delta":_delta(first,second),
                "channels":contributions,"channel_data_status":"available" if channel_table_present else "not_migrated",
                "order_evidence":order_evidence,"provenance":read_provenance(session,refs)}
