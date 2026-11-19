"""有限退款原因调查：核算原因记录对应的退款金额，不将关联金额说成经营因果。

沿两期支付订单批次、共同退款截止；原因记录只读取本次观察时间以前的版本。
同一退款多条一致证据仅计款一次；互相冲突不替用户裁决，单列冲突金额。
"""
from datetime import datetime, timezone

from sqlalchemy import and_, inspect, or_, select

from buyer_support_service import _db_time, refund_reason_records
from merchant_service import order_shops
from models import Order, Refund
from refund_service import _time
from sales_analysis import compare_periods

MAX_EVIDENCE_ROWS = 10000
BUCKETS = {
    "verified_size": "源记录标为核实的尺码原因",
    "buyer_reported_size": "买家自述尺码原因",
    "unverified_size": "尚未核实的尺码原因",
    "verified_other": "源记录标为核实的其他原因",
    "buyer_reported_other": "买家自述其他原因",
    "unverified_other": "尚未核实的其他原因",
    "reason_unknown": "记录未提供明确原因",
    "conflicting_reasons": "同退款存在冲突原因记录",
    "missing_reason": "没有本次可用原因记录",
}


def _bucket(records):
    if not records:
        return "missing_reason"
    reasons = {row["reason_code"] for row in records}
    if len(reasons) > 1:
        return "conflicting_reasons"
    reason = next(iter(reasons))
    if reason == "unknown":
        return "reason_unknown"
    levels = {row["verification_status"] for row in records}
    level = "verified" if "verified" in levels else "buyer_reported" if "buyer_reported" in levels else "unverified"
    return level + ("_size" if reason == "size" else "_other")


def investigate_refund_increase(engine, access: dict, first_from: datetime, first_before: datetime,
                                second_from: datetime, second_before: datetime, refund_before: datetime,
                                *, observed_at: datetime | None = None) -> dict:
    """可信身份由应用传入；查询失败不改写为零退款或“无尺码原因”。"""
    observed = _time(observed_at if observed_at is not None else datetime.now(timezone.utc), "原因记录观察时间")
    cutoff = _time(refund_before, "共同退款截止")
    if cutoff > observed:
        raise ValueError("退款调查截止不能晚于本次观察时间，不能把未来缺记录当成事实")
    with engine.connect() as connection:
        # 两期金额与原因记录在同一快照读取，避免查询之间的新增记录改变分母。
        if engine.dialect.name == "postgresql":
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
        with connection.begin():
            comparison = compare_periods(connection, access, first_from, first_before, second_from, second_before, cutoff)
            common = {"scope": comparison["scope"], "conditions": comparison["conditions"],
                "observed_at": observed.isoformat(), "reason_boundary": "recorded_at <= observed_at",
                "currency": "CNY", "money_unit": "fen", "comparison": comparison,
                "source_proof": "declared_only", "causality_verified": False, "reason_status": "unknown",
                "notice": "仅核对原因记录与退款金额的关联；verified为来源中的核实状态声明，合成演示不证明真实经营原因。"}
            if comparison["status"] != "ok":
                return {**common, "status": comparison["status"], "message": "两期金额尚未可靠核对，未作原因调查。"}
            schema = connection.get_execution_options().get("schema_translate_map", {}).get("public", "public")
            if not inspect(connection).has_table("refund_reason_records", schema=schema):
                return {**common, "status": "reason_data_unavailable", "message": "尚未接入退款原因记录，金额变化本身不能证明尺码原因。"}
            start1, end1, start2, end2 = [_time(x, "支付区间") for x in (first_from, first_before, second_from, second_before)]
            evidence = refund_reason_records
            statement = (select(Refund.refund_id, Refund.order_id, Refund.refunded_at,
                Refund.refund_amount_fen, Order.paid_at, *evidence.c)
                .select_from(Refund.__table__.join(Order.__table__, Order.order_id == Refund.order_id))
                .outerjoin(evidence, and_(evidence.c.refund_id == Refund.refund_id, evidence.c.recorded_at <= observed))
                .where(Refund.refunded_at < cutoff, or_(and_(Order.paid_at >= start1, Order.paid_at < end1),
                                                       and_(Order.paid_at >= start2, Order.paid_at < end2))))
            if comparison["scope"]["role"] == "merchant":
                statement = statement.join(order_shops, order_shops.c.order_id == Order.order_id).where(
                    order_shops.c.shop_id == comparison["scope"]["shop_id"])
            else:
                statement = statement.where(Order.buyer_id == comparison["scope"]["buyer_id"])
            rows = connection.execute(statement.order_by(Refund.refund_id, evidence.c.evidence_id).limit(MAX_EVIDENCE_ROWS+1)).mappings().all()
            if len(rows) > MAX_EVIDENCE_ROWS:
                return {**common, "status": "too_many_evidence_rows", "message": "原因关联记录超过本次核查上限，未用部分证据作结论。"}
            grouped = {}
            for row in rows:
                identity = row["refund_id"]
                if identity not in grouped:
                    paid = _db_time(row["paid_at"], engine.dialect.name)
                    grouped[identity] = {"refund_id": identity, "order_id": row["order_id"],
                        "period": "first" if start1 <= paid < end1 else "second",
                        "refund_amount_fen": row["refund_amount_fen"],
                        "refunded_at": _db_time(row["refunded_at"], engine.dialect.name).isoformat(), "evidence": []}
                if row["evidence_id"] is not None:
                    item = {field: row[field] for field in ("evidence_id", "reason_code", "verification_status", "source", "source_kind", "source_version", "note")}
                    if (item["reason_code"] not in {"size", "preference", "merchant_responsibility", "other", "unknown"}
                            or item["verification_status"] not in {"verified", "buyer_reported", "unverified"}
                            or item["source_kind"] != "synthetic_demo"):
                        return {**common, "status": "data_error", "message": "原因记录字段不符合已接入约定，未据异常记录作结论。"}
                    recorded = _db_time(row["recorded_at"], engine.dialect.name)
                    if recorded < _db_time(row["refunded_at"], engine.dialect.name):
                        return {**common, "status": "data_error", "message": "退款后核对记录早于退款事件，需核对来源。"}
                    item["recorded_at"] = recorded.isoformat(); grouped[identity]["evidence"].append(item)
            totals = {period: {name: {"refund_count": 0, "refund_amount_fen": 0} for name in BUCKETS} for period in ("first", "second")}
            for item in grouped.values():
                bucket = _bucket(item["evidence"]); item["evidence_bucket"] = bucket
                totals[item["period"]][bucket]["refund_count"] += 1
                totals[item["period"]][bucket]["refund_amount_fen"] += item["refund_amount_fen"]
            for period in ("first", "second"):
                if sum(row["refund_amount_fen"] for row in totals[period].values()) != comparison[period]["refund_amount_fen"]:
                    return {**common, "status": "data_error", "message": "原因调查与金额核查范围不一致，未输出不完整的贡献。"}
            breakdown = [{"bucket": name, "label": label, "first": totals["first"][name], "second": totals["second"][name],
                          "delta_refund_amount_fen": totals["second"][name]["refund_amount_fen"]-totals["first"][name]["refund_amount_fen"]}
                         for name, label in BUCKETS.items()]
            size = next(row for row in breakdown if row["bucket"] == "verified_size")
            increase = comparison["delta"]["refund_amount_fen"]
            support = ("recorded_size_amount_increased" if increase > 0 and size["delta_refund_amount_fen"] > 0 else
                       "no_total_refund_increase" if increase <= 0 else "no_verified_size_increase")
            incomplete = {period: sum(totals[period][name]["refund_amount_fen"] for name in
                ("missing_reason", "reason_unknown", "conflicting_reasons", "buyer_reported_size", "buyer_reported_other", "unverified_size", "unverified_other"))
                for period in ("first", "second")}
            return {**common, "status": "ok", "question": "两期退款增加是否有尺码原因记录支持",
                "finding": support, "total_refund_delta_fen": increase,
                "verified_size_delta_fen": size["delta_refund_amount_fen"], "reason_breakdown": breakdown,
                "refund_evidence": list(grouped.values()), "unverified_or_missing_amount_fen": incomplete,
                "evidence_status": "partial_reason_coverage" if any(incomplete.values()) else "declared_records_available",
                "message": "来源中的尺码记录可解释对应退款金额属于哪类；不能据此断定尺码是整体经营变化的原因。"}
