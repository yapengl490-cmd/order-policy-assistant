"""只读售后判断：可信订单事实、支付时政策与本轮用户自述分别列明。

应用注入买家或店铺身份；模型不能指定归属、收货时间、商品类别或政策版本。
这里只判断演示条款下的条件，不受理退货、不退款、不把自述当已核实事实。
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, Column, DateTime, MetaData, Table, Text, inspect, select

from merchant_service import order_shops
from models import Order
from refund_service import _identifier, _time
from policy_documents import MAX_RETURN_WINDOW_HOURS

support_metadata = MetaData(schema="public")
order_support_facts = Table("order_support_facts", support_metadata,
    Column("order_id", Text, primary_key=True), Column("product_category", Text),
    Column("delivered_at", DateTime(timezone=True)), Column("is_custom", Boolean),
    Column("source", Text, nullable=False), Column("source_kind", Text, nullable=False),
    Column("source_version", Text, nullable=False), Column("recorded_at", DateTime(timezone=True), nullable=False))
refund_reason_records = Table("refund_reason_records", support_metadata,
    Column("evidence_id", Text, primary_key=True), Column("refund_id", Text, nullable=False),
    Column("reason_code", Text, nullable=False), Column("verification_status", Text, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False), Column("source", Text, nullable=False),
    Column("source_kind", Text, nullable=False), Column("source_version", Text, nullable=False),
    Column("note", Text, nullable=False))
CLAIM_FIELDS = {"is_unworn", "tags_intact", "reason"}
REASONS = {"size", "preference", "merchant_responsibility", "other", "unknown"}


def _db_time(value, dialect):
    if value is None:
        return None
    # SQLite仅用于离线夹具；正式PostgreSQL缺时区属于错误，不能猜时区。
    if value.tzinfo is None and dialect == "sqlite":
        value = value.replace(tzinfo=timezone.utc)
    return _time(value, "数据库时间")


def get_support_context(engine, order_id: str, *, buyer_id=None, shop_id=None) -> dict:
    """可信入口选一个角色范围；先读有权订单，找不到时不泄露其他用户事实。"""
    order_id = _identifier(order_id, "订单编号")
    if (buyer_id is None) == (shop_id is None):
        raise ValueError("需要程序提供且仅提供买家或店铺范围")
    role = "buyer" if buyer_id is not None else "merchant"
    scope_id = _identifier(buyer_id if role == "buyer" else shop_id, "可信查询范围")
    scope = {"role": role, "buyer_id" if role == "buyer" else "shop_id": scope_id}
    base = {"scope": scope, "order_id": order_id, "refund_execution": False}
    statement = (select(Order.__table__, order_shops.c.shop_id)
        .outerjoin(order_shops, order_shops.c.order_id == Order.order_id)
        .where(Order.order_id == order_id))
    statement = statement.where(Order.buyer_id == scope_id) if role == "buyer" else statement.where(order_shops.c.shop_id == scope_id)
    with engine.connect() as connection:
        row = connection.execute(statement).mappings().first()
        if row is None:
            return {**base, "status": "not_found", "message": "当前授权范围内未找到订单。"}
        order = {key: row[key] for key in ("order_id", "buyer_id", "paid_amount_fen", "shop_id")}
        order["paid_at"] = _db_time(row["paid_at"], engine.dialect.name).isoformat()
        schema = connection.get_execution_options().get("schema_translate_map", {}).get("public", "public")
        if not inspect(connection).has_table("order_support_facts", schema=schema):
            return {**base, "status": "support_data_unavailable", "order": order,
                    "message": "尚未接入售后补充记录，不能判断退货条件。"}
        fact = connection.execute(select(order_support_facts).where(order_support_facts.c.order_id == order_id)).mappings().first()
        trusted = {"product_category": None, "delivered_at": None, "is_custom": None,
                   "source": None, "source_kind": None, "source_version": None, "recorded_at": None}
        if fact:
            trusted.update({key: fact[key] for key in trusted})
            for key in ("delivered_at", "recorded_at"):
                value = _db_time(trusted[key], engine.dialect.name)
                trusted[key] = value.isoformat() if value else None
        return {**base, "status": "ok", "order": order, "trusted_facts": trusted,
                "source_proof": "declared_only", "data_kind": "synthetic_demo",
                "notice": "补充记录及政策为合成演示；数据库来源声明不等于真实平台已核实。"}


def _claims(claims):
    if claims is None:
        return {}
    if not isinstance(claims, dict) or set(claims) - CLAIM_FIELDS:
        raise ValueError("用户自述仅允许is_unworn、tags_intact和reason，不得传可信业务字段")
    for key in ("is_unworn", "tags_intact"):
        if key in claims and claims[key] is not None and type(claims[key]) is not bool:
            raise ValueError("商品状态自述必须为布尔值或未知")
    if "reason" in claims and claims["reason"] is not None and (not isinstance(claims["reason"], str) or claims["reason"] not in REASONS):
        raise ValueError("退货原因不属于当前支持范围")
    return deepcopy(claims)


def _topics(topics):
    if (not isinstance(topics, (tuple, list)) or not topics
            or any(not isinstance(value, str) or value not in {"eligibility", "shipping"} for value in topics)
            or len(set(topics)) != len(topics)):
        raise ValueError("本次主题仅允许eligibility与shipping且不能重复或为空")
    return list(topics)


def evaluate_buyer_support(context: dict, policy_bundle: dict, *, claims=None, now: datetime,
                           topics=("eligibility", "shipping")) -> dict:
    """按程序取得的支付版规则判断；自述满足仍只给条件性结论，不批准售后。"""
    current = _time(now, "本次判断时间")
    stated = _claims(claims)
    requested = _topics(topics)
    common = {"order_id": context.get("order_id"), "scope": deepcopy(context.get("scope")),
              "as_of": current.isoformat(), "user_claims": {"values": stated, "verified": False},
              "trusted_facts": deepcopy(context.get("trusted_facts", {})), "order": deepcopy(context.get("order")),
              "refund_execution": False, "source_proof": "declared_only", "missing": [],
              "policy_selection_basis": "order_paid_at", "requested_topics": requested, "eligibility": "undetermined",
              "shipping": {"status": "undetermined", "payer": None}, "citations": {},
              "notice": "仅合成政策下的核对；用户自述未核实，不是退货批准或真实售后承诺。"}
    if context.get("status") != "ok":
        return {**common, "status": context.get("status", "data_error"), "message": context.get("message", "未取得完整订单事实。")}
    facts, order = context["trusted_facts"], context["order"]
    # 类别和店铺决定查哪份政策；收货/定制未知在排除已确定失败后再澄清。
    fact_missing = [key for key in ("product_category",) if facts.get(key) is None]
    if not order.get("shop_id"):
        fact_missing.append("shop_id")
    if fact_missing:
        return {**common, "status": "clarification", "missing": fact_missing,
                "message": "缺少可信商品、收货或店铺记录；不能用用户自述代替后台事实。"}
    try:
        paid = _time(datetime.fromisoformat(order["paid_at"]), "支付时间")
        delivered = _time(datetime.fromisoformat(facts["delivered_at"]), "收货时间") if facts.get("delivered_at") else None
        recorded = _time(datetime.fromisoformat(facts["recorded_at"]), "记录时间")
        if recorded < paid or recorded > current or (delivered is not None and (delivered < paid or recorded < delivered or delivered > current)):
            raise ValueError("invalid time sequence")
    except (TypeError, KeyError, ValueError):
        return {**common, "status": "data_error", "message": "补充事实时间不一致或晚于查询时刻，不能据此判断。"}
    if policy_bundle.get("status") in {"withdrawn", "superseded_without_valid_replacement"}:
        # 撤回只阻止政策推导，不抹掉已授权读取的订单事实，也不套用其他月份条款。
        return {**common, "status": "insufficient_evidence", "policy_status": policy_bundle["status"],
                "reason": policy_bundle.get("reason", "applicable_policy_withdrawn"),
                "policy_lifecycle": deepcopy(policy_bundle.get("lifecycle", {})),
                "message": "订单事实已保留；适用政策已撤回且当前没有可用更正版，暂不能判断退货资格或运费承担。"}
    if (policy_bundle.get("status") != "ok" or facts.get("source_kind") != "synthetic_demo"):
        return {**common, "status": "insufficient_evidence", "message": "未取得此店铺、商品类别和支付时刻适用的政策原文。"}
    applicability = policy_bundle.get("applicability", {})
    try:
        start = _time(datetime.fromisoformat(applicability["effective_from"]), "政策生效时间")
        end = _time(datetime.fromisoformat(applicability["effective_before"]), "政策失效时间")
        valid = (applicability.get("store_id") == order["shop_id"] and applicability.get("category") == facts["product_category"]
                 and applicability.get("selection_basis") == "order_paid_at" and start <= paid < end)
    except (KeyError, ValueError, TypeError):
        valid = False
    rules = policy_bundle.get("rules", {})
    hours = rules.get("return_window_hours")
    if (not valid or type(hours) is not int or not 1 <= hours <= MAX_RETURN_WINDOW_HOURS
            or rules.get("return_window_basis") != "delivered_at"
            or rules.get("return_window_boundary") != "start_inclusive_end_exclusive"
            or not isinstance(rules.get("allowed_categories"), list)
            or any(type(rules.get(key)) is not bool for key in ("custom_excluded", "requires_unworn", "requires_tags"))
            or not isinstance(rules.get("shipping_by_reason"), dict)):
        return {**common, "status": "insufficient_evidence", "message": "政策规则缺失或不匹配本订单，未套用其他版本。"}
    common.update(policy_version=policy_bundle.get("payment_version"),
                  citations=deepcopy(policy_bundle.get("rule_citations", {})))
    if "eligibility" in requested:
        deadline = delivered + timedelta(hours=hours) if delivered is not None else None
        if deadline is not None:
            common["return_window"] = {"starts_at": delivered.isoformat(), "before": deadline.isoformat(),
                                       "hours": hours, "boundary": "delivered_at <= as_of < delivered_at + hours"}
        rejected = []
        if facts["product_category"] not in rules["allowed_categories"]:
            rejected.append("category_not_covered")
        if rules["custom_excluded"] and facts["is_custom"] is True:
            rejected.append("custom_product_excluded")
        if deadline is not None and current >= deadline:
            rejected.append("return_window_expired")
        if rules["requires_unworn"] and stated.get("is_unworn") is False:
            rejected.append("user_reports_worn")
        if rules["requires_tags"] and stated.get("tags_intact") is False:
            rejected.append("user_reports_tags_missing")
        if rejected:
            return {**common, "status": "ok", "eligibility": "ineligible", "failed_conditions": rejected,
                    "shipping": {"status": "not_applicable", "payer": None},
                    "message": "本次未满足所检索演示条款；不据此否定其他未接入的售后权利。"}
        missing_facts = [key for key in ("delivered_at", "is_custom") if facts.get(key) is None]
        if missing_facts:
            return {**common, "status": "clarification", "missing": missing_facts,
                    "message": "缺少可信收货或定制状态记录；不能用用户自述代替后台事实。"}
        missing = [field for field, required in (("is_unworn", rules["requires_unworn"]), ("tags_intact", rules["requires_tags"]))
                   if required and stated.get(field) is None]
        if missing:
            return {**common, "status": "clarification", "missing": missing,
                    "message": "请补充商品是否未穿、吊牌是否完好；这些属于本轮用户自述，仍需售后核验。"}
        common["eligibility"] = "eligible_with_self_report"
    else:
        common["eligibility"] = "not_requested"
    if "shipping" not in requested:
        return {**common, "status": "ok", "shipping": {"status": "not_requested", "payer": None},
                "message": "按现有记录和未核实自述，暂满足演示退货条件；不代表售后批准。"}
    reason = stated.get("reason")
    if reason in (None, "unknown"):
        return {**common, "status": "clarification", "missing": ["reason"],
                "message": "运费判断还需明确退货原因；是否具备退货资格以本次eligibility字段及其依据为准。"}
    payer = rules["shipping_by_reason"].get(reason)
    if payer in {"buyer", "merchant"} and reason != "merchant_responsibility":
        common["shipping"] = {"status": "conditional_on_self_report", "payer": payer,
                              "reason": reason, "verified": False}
        return {**common, "status": "ok", "message": "按已取政策及本轮自述给出条件性判断；不执行售后。"}
    common["shipping"] = {"status": "needs_verified_responsibility" if reason == "merchant_responsibility" else "insufficient_evidence",
                          "payer": None, "reason": reason, "verified": False}
    return {**common, "status": "insufficient_evidence", "message": "本次责任尚无已核实依据，不能仅凭自述确定商家承担运费。"}


def check_buyer_support(engine, order_id: str, *, buyer_id=None, shop_id=None, claims=None,
                        now: datetime | None = None, policy_selector=None,
                        topics=("eligibility", "shipping")) -> dict:
    """应用入口：读可信订单，再按支付时间选择政策，最后做确定性条件判断。"""
    current = _time(now if now is not None else datetime.now(timezone.utc), "本次判断时间")
    _claims(claims)  # 拒绝越权字段后才读数据库。
    _topics(topics)
    context = get_support_context(engine, order_id, buyer_id=buyer_id, shop_id=shop_id)
    bundle = {"status": "insufficient_evidence"}
    facts, order = context.get("trusted_facts", {}), context.get("order", {})
    if context.get("status") == "ok" and order.get("shop_id") and facts.get("product_category"):
        if policy_selector is None:
            from policy_kb import select_policy
            policy_selector = select_policy
        bundle = policy_selector(engine, store_id=order["shop_id"], category=facts["product_category"],
                                 paid_at=datetime.fromisoformat(order["paid_at"]))
    return evaluate_buyer_support(context, bundle, claims=claims, now=current, topics=topics)
