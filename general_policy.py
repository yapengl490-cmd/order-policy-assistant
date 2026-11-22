"""Read-only general policy lookup, separate from an order eligibility decision.

The application supplies a trusted store context, a confirmed category and an
explicit timezone-aware query time. This module neither establishes shop access
nor accepts an order, buyer claims or a model-selected policy version. It uses
the existing controlled policy library, never the legacy explanatory JSON.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from sqlalchemy import inspect, select

from display_preferences import answer_style
from policy_documents import CATEGORY, IDENTIFIER, aware_time
from policy_kb import policy_documents, select_policy


SUPPORTED_TOPICS = frozenset({"eligibility", "shipping", "scope"})
CATEGORY_LABELS = {CATEGORY: "普通鞋类"}
BOUNDARY = "仅为合成演示条款，非真实店铺政策；未核查任何订单，不能据此承诺本单可退，也未受理、批准或执行售后。"


def _policy_library_available(engine):
    with engine.connect() as connection:
        schema = connection.get_execution_options().get("schema_translate_map", {}).get("public", "public")
        inspector = inspect(connection)
        return all(inspector.has_table(name, schema=schema) for name in ("policy_documents", "policy_chunks"))


def _available_categories(engine, store_id, policy_at):
    """Expose only categories with an intact applicable policy in this scope."""
    with engine.connect() as connection:
        categories = connection.execute(select(policy_documents.c.category).where(
            policy_documents.c.store_id == store_id,
            policy_documents.c.effective_from <= policy_at,
            policy_documents.c.effective_before > policy_at,
        ).distinct()).scalars().all()
    return sorted(category for category in categories
                  if category in CATEGORY_LABELS
                  and select_policy(engine, store_id=store_id, category=category, paid_at=policy_at).get("status") == "ok")


def lookup_general_policy(engine, *, store_id: str, category: str | None,
                          policy_at: datetime, topics=("eligibility", "shipping")) -> dict:
    """Return clauses effective at the query time, without assessing an order.

    ``store_id`` must be injected by the application, not taken from tool args.
    ``category=None`` reports actual coverage and asks for a category; it does
    not assume a customer's product is a shoe. Other categories do not inherit
    shoe policy. Unsupported topics return evidence insufficiency, not a
    substitute answer about returns. Invalid input fails before database access.
    """
    if not isinstance(store_id, str) or not IDENTIFIER.fullmatch(store_id):
        raise ValueError("明确的可信店铺范围必填")
    if category is not None and (not isinstance(category, str) or not IDENTIFIER.fullmatch(category)):
        raise ValueError("品类须为程序确认的标识或None")
    if not isinstance(policy_at, datetime):
        raise ValueError("政策查询时刻须为显式带时区datetime")
    query_time = aware_time(policy_at, "policy_at")
    if (not isinstance(topics, (list, tuple)) or not topics
            or any(not isinstance(topic, str) for topic in topics)
            or len(set(topics)) != len(topics)):
        raise ValueError("主题须为非空且不重复的列表")
    result = {
        "status": "insufficient_evidence", "kind": "general_policy",
        "selection_basis": "policy_query_time", "policy_at": query_time.isoformat(),
        "scope": {"store_id": store_id, "category": category},
        "requested_topics": list(topics), "policy_version": None,
        "applicability": {"store_id": store_id, "category": category,
                          "policy_at": query_time.isoformat(), "selection_basis": "policy_query_time"},
        "supported_categories": [], "missing": [], "rules": {}, "citations": {},
        "citation_index": {}, "claims": [], "order_eligibility": "not_evaluated",
        "refund_execution": False, "notice": BOUNDARY,
        "model_requests": 0, "embedding_requests": 0,
    }
    if set(topics) - SUPPORTED_TOPICS:
        return {**result, "reason": "unsupported_topic",
                "message": "当前仅有退货条件、退货运费与适用范围的受控条款；所问的其他政策尚无对应依据。"}
    if not _policy_library_available(engine):
        return {**result, "reason": "policy_data_unavailable", "message": "当前尚未接入完整政策资料库，不能提供条款结论。"}
    if category is None:
        coverage = _available_categories(engine, store_id, query_time)
        result["supported_categories"] = coverage
        if not coverage:
            return {**result, "reason": "no_supported_policy", "message": "该店铺在所选政策查询时刻没有可用的受控政策依据。"}
        return {**result, "status": "clarification", "reason": "category_required", "missing": ["category"],
                "message": "请明确想查看的商品品类；当前资料覆盖：" + "、".join(CATEGORY_LABELS[item] for item in coverage) + "。无需提供订单号。"}

    # The existing selector implements interval/scope/integrity checks. Its
    # paid_at parameter is an internal compatibility detail here: rebuild the
    # output applicability explicitly so this query never claims an order was
    # read or that the selected version applies to a customer's purchase.
    bundle = select_policy(engine, store_id=store_id, category=category, paid_at=query_time)
    if bundle.get("status") != "ok":
        status = bundle.get("status", "invalid_policy")
        reason = bundle.get("reason", "invalid_policy")
        if status == "insufficient_evidence":
            reason = "no_supported_policy"
        messages = {
            "insufficient_evidence": "该店铺、品类在所选政策查询时刻没有适用条款；不能用其他品类、版本或隐私政策替代。",
            "conflict": "适用政策存在版本冲突，当前不能给出规则结论。",
            "invalid_policy": "政策原文或来源校验失败，当前不能给出规则结论。",
        }
        return {**result, "status": status, "reason": reason,
                "message": messages.get(status, "政策资料未完整取得，当前不能给出规则结论。")}
    document = bundle["document"]
    if document.get("source_kind") != "synthetic_demo" or document.get("category") != CATEGORY:
        return {**result, "reason": "unsupported_policy_source", "message": "此来源或品类尚未接入当前受控政策说明能力。"}
    citations = deepcopy(bundle["rule_citations"])
    index = {}
    for rule, source in citations.items():
        identity = source["citation_id"]
        if identity not in index:
            index[identity] = {**deepcopy(source), "supports": []}
        index[identity]["supports"].append(rule)
    rules = deepcopy(bundle["rules"])
    result.update(status="ok", policy_version=document["version"], source_kind=document["source_kind"],
                  supported_categories=[category], rules=rules, citations=citations, citation_index=index,
                  document=deepcopy(document))
    result["applicability"].update(effective_from=bundle["applicability"]["effective_from"],
                                    effective_before=bundle["applicability"]["effective_before"])

    def add(identity, text, rule):
        result["claims"].append({"id": identity, "text": text,
                                 "citation_ids": [citations[rule]["citation_id"]]})

    add("scope", f"按政策查询时刻{result['policy_at']}，{store_id}店铺的{CATEGORY_LABELS[category]}条款版本为{document['version']}；此处仅说明一般规则。", "allowed_categories")
    if "eligibility" in topics:
        add("eligibility_policy", f"退货申请时限为收货起{rules['return_window_hours']}小时内，含收货起点、不含截止时刻；须未穿着且吊牌完好，定制商品不适用；用户自述仍需核实，不代表批准。", "return_window_hours")
    if "shipping" in topics:
        add("shipping_policy", "因尺码或个人偏好退货，运费由买家承担；商家责任须有经核实的本次售后责任记录，否则责任与运费承担仍待核实，不能仅凭自述或退款金额判断。", "shipping_by_reason")
    return result


def render_general_policy(result: dict, display=None) -> str:
    """Render only program-produced policy statements with their own citations."""
    if result.get("kind") != "general_policy":
        raise ValueError("需要泛政策查询结果")
    if result.get("status") != "ok":
        return result.get("message", "没有足够的政策依据。") + "\n" + result["notice"]
    lines = [claim["text"] + " " + " ".join("[" + ref + "]" for ref in claim["citation_ids"])
             for claim in result["claims"]]
    # 简短版也保留所有受控结论：条件、例外和未知事实不能随显示偏好省略。
    if answer_style(display) == "detailed":
        applicability = result.get("applicability", {})
        if applicability.get("effective_from") and applicability.get("effective_before"):
            lines.append("版本有效区间：" + applicability["effective_from"] + "（含）至"
                         + applicability["effective_before"] + "（不含）；本次按政策查询时刻选取，未核查订单支付时版本。")
        # 只展开本次结论引用的条款，每个片段一次，不拼接整份政策或无关条款。
        seen = set()
        for claim in result["claims"]:
            for ref in claim["citation_ids"]:
                if ref in seen:
                    continue
                seen.add(ref)
                source = result.get("citation_index", {}).get(ref, {})
                if source.get("text"):
                    lines.append("条款原文 [" + ref + "]：\n" + source["text"])
    lines.append(result["notice"])
    return "\n".join(lines)
