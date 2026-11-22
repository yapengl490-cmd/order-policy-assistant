"""Offline, deterministic buyer catalog filtering; no model, network, or writes.

The caller supplies a trusted catalog scope and an explicit evaluation time. This
module does not authenticate buyers or establish their shop permissions. Product
text is data, never instructions. Prices and availability are snapshot evidence,
not checkout guarantees. An optional trusted policy lookup checks product scope,
but never approves a future individual return request.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any


CURRENCY_MINOR_UNITS = {"CNY": 2, "USD": 2, "EUR": 2, "GBP": 2, "JPY": 0, "KWD": 3}
REQUIRED_REQUIREMENTS = {"category", "currency", "budget_max_minor"}
ALLOWED_REQUIREMENTS = REQUIRED_REQUIREMENTS | {"material", "size", "cotton_percentage", "require_returnable"}
SELECTION_RULE = "硬条件全部通过后，按价格最小货币单位升序，再按店铺、商品、变体编号字典序；不是模型推荐或学习排序。"


def _text(value: Any, maximum: int = 128) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and value == value.strip()
            and len(value) <= maximum and not any(ord(c) < 32 for c in value))


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    try:
        return result.astimezone(timezone.utc)
    except OverflowError:
        raise ValueError("timestamp exceeds supported UTC range") from None


def _normalize(value: str) -> str:
    return value.casefold()


@dataclass(frozen=True)
class CatalogScope:
    """Trusted server configuration, never populated from model tool arguments."""

    # 可信范围由程序提供，不能用模型参数或用户自报店铺替代授权。

    shop_ids: frozenset[str]
    market: str
    channel: str

    def __post_init__(self):
        if (not isinstance(self.shop_ids, frozenset) or not self.shop_ids
                or any(not _text(item) for item in self.shop_ids)
                or not _text(self.market) or not _text(self.channel)):
            raise ValueError("explicit trusted shop, market and channel scope required")


@dataclass(frozen=True)
class CatalogSnapshot:
    """An immutable copy of exact source bytes, kept for verifiable references."""

    source_path: str
    source_bytes: bytes

    def __post_init__(self):
        if not isinstance(self.source_path, str) or not self.source_path or type(self.source_bytes) is not bytes:
            raise ValueError("source path and immutable source bytes required")

    @classmethod
    def load(cls, path: Path | str) -> "CatalogSnapshot":
        path = Path(path).resolve(strict=True)
        data = path.read_bytes()
        if len(data) > 2_000_000:
            raise ValueError("catalog exceeds bounded offline sample size")
        return cls(str(path), data)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.source_bytes).hexdigest()

    def data(self) -> dict:
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        value = json.loads(self.source_bytes, object_pairs_hook=unique_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non-finite JSON number")))
        if not isinstance(value, dict):
            raise ValueError("catalog must be an object")
        return value


def recommend(snapshot: CatalogSnapshot, requirements: dict, *, scope: CatalogScope,
              as_of: datetime, max_age: timedelta = timedelta(hours=24), limit: int = 3,
              policy_lookup=None) -> dict:
    """Select variants from this bounded snapshot without mutating any inputs.

    Budget is inclusive and denominated in integer minor currency units. Category,
    currency and budget are mandatory; material and size become hard constraints
    only when supplied. Missing data never satisfies a requested constraint.
    This strict first version excludes untracked inventory and backorders.
    """
    if not isinstance(snapshot, CatalogSnapshot) or not isinstance(scope, CatalogScope):
        raise ValueError("trusted snapshot and scope objects required")
    if (not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None
            or not isinstance(max_age, timedelta) or max_age <= timedelta(0)
            or type(limit) is not int or not 1 <= limit <= 10):
        raise ValueError("explicit aware time, positive freshness window and limit 1..10 required")
    as_of = as_of.astimezone(timezone.utc)
    result = {
        "status": "no_match", "mode": "offline_snapshot_filter", "candidates": [],
        "selection_rule": SELECTION_RULE,
        "evaluated_at": as_of.isoformat(), "max_age_seconds": max_age.total_seconds(),
        "source": {"path": snapshot.source_path, "sha256": snapshot.sha256,
                   "fingerprint_type": "exact_file_bytes_sha256"},
        "requirements": deepcopy(requirements),
        "trusted_scope": {"shop_ids": sorted(scope.shop_ids), "market": scope.market, "channel": scope.channel},
        "notice": "仅依据所示时刻的目录快照筛选；不保证此刻库存、最终含税运费价格或可退货。",
        "excluded_counts": {}, "missing": [], "errors": [],
    }
    if not isinstance(requirements, dict):
        result.update(status="invalid_request", errors=["requirements_must_be_object"])
        return result
    if set(requirements) - ALLOWED_REQUIREMENTS:
        result.update(status="invalid_request", errors=["unsupported_requirement_fields"])
        return result
    missing = sorted(field for field in REQUIRED_REQUIREMENTS
                     if field not in requirements or requirements[field] is None or requirements[field] == "")
    if missing:
        result.update(status="clarification", missing=missing)
        return result
    if (any(not _text(requirements[field]) for field in {"category", "currency", "material", "size"}
            if field in requirements)
            or requirements["currency"] not in CURRENCY_MINOR_UNITS
            or type(requirements["budget_max_minor"]) is not int
            or requirements["budget_max_minor"] < 0):
        result.update(status="invalid_request", errors=["invalid_requirement_value"])
        return result
    if (("require_returnable" in requirements and type(requirements["require_returnable"]) is not bool)
            or ("cotton_percentage" in requirements and (
                type(requirements["cotton_percentage"]) is not int or not 0 <= requirements["cotton_percentage"] <= 100))):
        result.update(status="invalid_request", errors=["invalid_requirement_value"])
        return result
    if requirements.get("require_returnable") and policy_lookup is None:
        result["unverified_requirements"] = ["return_eligibility"]
        result["policy_notice"] = "尚缺此商品适用的退货政策，不能确认满足“要能退”的要求。"
    try:
        catalog = snapshot.data()
        if (type(catalog.get("schema_version")) is not int or catalog["schema_version"] != 1
                or catalog.get("data_kind") != "synthetic_demo"
                or not isinstance(catalog.get("products"), list)):
            raise ValueError("unsupported catalog")
        snapshot_at = _timestamp(catalog.get("snapshot_at"))
    except (ValueError, TypeError, UnicodeError):
        result.update(status="invalid_catalog", errors=["invalid_catalog_schema_or_time"])
        return result
    result["data_kind"] = "synthetic_demo"
    result["source"]["snapshot_at"] = snapshot_at.isoformat()
    result["notice"] = "纯合成演示目录，非Shopify或真实商品。" + result["notice"]
    if snapshot_at > as_of or as_of - snapshot_at > max_age:
        result.update(status="insufficient_evidence", errors=["future_snapshot" if snapshot_at > as_of else "stale_snapshot"])
        return result

    def exclude(reason):
        result["excluded_counts"][reason] = result["excluded_counts"].get(reason, 0) + 1

    eligible, seen = [], set()
    for product in catalog["products"]:
        if (not isinstance(product, dict) or not isinstance(product.get("shop_id"), str)
                or product["shop_id"] not in scope.shop_ids or not isinstance(product.get("variants"), list)):
            continue
        for variant in product["variants"]:
            if not isinstance(variant, dict) or not _text(variant.get("variant_id")):
                continue
            identity = (product["shop_id"], variant["variant_id"])
            if identity in seen:
                result.update(status="invalid_catalog", errors=["duplicate_variant_identity"])
                return result
            seen.add(identity)
    for product_index, product in enumerate(catalog["products"]):
        if not isinstance(product, dict) or not _text(product.get("shop_id")):
            exclude("unknown_product_scope")
            continue
        if product["shop_id"] not in scope.shop_ids:
            # Do not return IDs, attributes or counts belonging to other shops.
            continue
        if (any(not _text(product.get(field)) for field in ("product_id", "category"))
                or not _text(product.get("title"), 500) or not isinstance(product.get("variants"), list)):
            exclude("unknown_product_data")
            continue
        if _normalize(product["category"]) != _normalize(requirements["category"]):
            exclude("category_mismatch")
            continue
        # 每个变体独立通过全部硬条件，不能拼接不同货号的价格、尺码和库存。
        for variant_index, variant in enumerate(product["variants"]):
            if not isinstance(variant, dict) or any(not _text(variant.get(f)) for f in ("variant_id", "sku")):
                exclude("unknown_variant_identity")
                continue
            try:
                observed_at = _timestamp(variant.get("observed_at"))
            except (ValueError, TypeError):
                exclude("unknown_observation_time")
                continue
            if observed_at > snapshot_at or as_of - observed_at > max_age:
                exclude("stale_or_future_variant")
                continue
            publication = variant.get("publication")
            if (not isinstance(publication, dict) or type(publication.get("published")) is not bool
                    or not _text(publication.get("market")) or not _text(publication.get("channel"))):
                exclude("unknown_publication")
                continue
            if (not publication["published"] or publication["market"] != scope.market
                    or publication["channel"] != scope.channel):
                exclude("unavailable_in_context")
                continue
            availability = variant.get("availability")
            if (not isinstance(availability, dict) or type(availability.get("available_for_sale")) is not bool
                    or availability.get("inventory_tracked") is not True
                    or type(availability.get("sellable_quantity")) is not int
                    or availability["sellable_quantity"] < 0):
                exclude("unknown_availability")
                continue
            if not availability["available_for_sale"] or availability["sellable_quantity"] == 0:
                exclude("out_of_stock_or_unavailable")
                continue
            price = variant.get("price")
            if (not isinstance(price, dict) or not isinstance(price.get("currency"), str)
                    or price["currency"] not in CURRENCY_MINOR_UNITS
                    or type(price.get("amount_minor")) is not int or price["amount_minor"] < 0
                    or type(price.get("minor_unit")) is not int
                    or price["minor_unit"] != CURRENCY_MINOR_UNITS[price["currency"]]):
                exclude("unknown_price")
                continue
            if price["currency"] != requirements["currency"]:
                exclude("currency_mismatch")
                continue
            if price["amount_minor"] > requirements["budget_max_minor"]:
                exclude("over_budget")
                continue
            attributes = variant.get("attributes")
            if not isinstance(attributes, dict):
                attributes = {}
            requested_attributes = [f for f in ("material", "size") if f in requirements]
            if any(not _text(attributes.get(field)) for field in requested_attributes):
                exclude("unknown_required_attribute")
                continue
            if any(_normalize(attributes[field]) != _normalize(requirements[field]) for field in requested_attributes):
                exclude("attribute_mismatch")
                continue
            if "cotton_percentage" in requirements:
                if type(attributes.get("cotton_percentage")) is not int or not 0 <= attributes["cotton_percentage"] <= 100:
                    exclude("unknown_required_attribute")
                    continue
                if attributes["cotton_percentage"] != requirements["cotton_percentage"]:
                    exclude("attribute_mismatch")
                    continue
                requested_attributes.append("cotton_percentage")
            policy = None
            if policy_lookup is not None:
                # 仅将已过范围/规格检查的事实交给程序政策查询；不接受模型自报范围。
                policy = policy_lookup(store_id=product['shop_id'], category=product['category'],
                                       is_custom=variant.get('is_custom'), policy_at=as_of)
                if not isinstance(policy, dict) or policy.get('status') not in {'conditional', 'excluded', 'unknown'}:
                    raise ValueError('invalid_policy_lookup_result')
                if policy.get('reason') in {'policy_storage_unavailable', 'invalid_policy',
                                           'stored_content_mismatch', 'overlapping_policy_versions'}:
                    errors = result.setdefault('policy_errors', [])
                    if policy['reason'] not in errors: errors.append(policy['reason'])
                checks = result.setdefault('return_policy_checks', [])
                document_id = policy.get('document_id')
                if len(checks) < 20:
                    checks.append({'variant_id': variant['variant_id'], 'is_custom': variant.get('is_custom'),
                        'status': policy['status'], 'reason': policy.get('reason'), 'policy_ref': document_id,
                        'citation_ids': list(policy.get('citation_index', {}))})
                else:
                    result['return_policy_checks_truncated'] = True
                if document_id:
                    # 同版条款只保存一份；规格状态单列，避免把定制例外覆盖成普通商品结论。
                    result.setdefault('return_policies', {})[document_id] = {
                        k: deepcopy(policy[k]) for k in ('document_id', 'document', 'policy_version', 'store_id',
                            'category', 'policy_at', 'applicability', 'rules', 'citation_index', 'claims') if k in policy}
            if requirements.get("require_returnable") and (policy is None or policy['status'] != 'conditional'):
                exclude('excluded_by_return_policy' if policy and policy['status'] == 'excluded' else 'unknown_return_policy')
                continue
            # 字段指针配合原始文件指纹定位证据；指纹不证明商家事实正确。
            product_pointer = f"/products/{product_index}"
            variant_pointer = f"{product_pointer}/variants/{variant_index}"
            evidence = {}
            for field in ("product_id", "shop_id", "category", "title"):
                evidence[field] = {"pointer": product_pointer + "/" + field, "value": deepcopy(product[field])}
            for field in ("variant_id", "sku", "observed_at", "price", "publication", "availability"):
                evidence[field] = {"pointer": variant_pointer + "/" + field, "value": deepcopy(variant[field])}
            for field in requested_attributes:
                evidence[field] = {"pointer": variant_pointer + "/attributes/" + field, "value": attributes[field]}
            if 'is_custom' in variant:
                evidence['is_custom'] = {'pointer': variant_pointer + '/is_custom', 'value': deepcopy(variant['is_custom'])}
            candidate = {
                "product_id": product["product_id"], "variant_id": variant["variant_id"],
                "sku": variant["sku"], "title": product["title"], "shop_id": product["shop_id"],
                "price": deepcopy(price),
                "matched_attributes": {field: attributes[field] for field in requested_attributes},
                "reason": "此变体在所示快照中满足全部已提供硬条件；价格口径为商品标价，不含未提供的税费和运费。",
                "return_eligibility": "unknown", "return_notice": "未读取此商品适用的退货政策，不能承诺可退。",
                "source": {"path": snapshot.source_path, "sha256": snapshot.sha256,
                           "pointer": variant_pointer, "fields": evidence},
            }
            if policy is not None:
                candidate.update(return_policy_status=policy['status'], return_policy_ref=policy.get('document_id'),
                    return_notice={'conditional': '当前有适用的有条件退货条款，购买后仍须核实收货、商品状态和支付时版本；不是退货批准。',
                                   'excluded': '已知为定制规格，不适用该退货条款。',
                                   'unknown': '商品信息或适用政策依据不足，未确认满足退货要求。'}[policy['status']])
            eligible.append(candidate)
    eligible.sort(key=lambda item: (item["price"]["amount_minor"], item["shop_id"], item["product_id"], item["variant_id"]))
    result["candidates"] = eligible[:limit]
    result["matching_variant_count"] = len(eligible)
    result["requested_limit"] = limit
    result["shortfall"] = max(0, limit - len(result["candidates"]))
    if eligible:
        result["status"] = "matched"
    elif any(reason.startswith("unknown_") or reason == "stale_or_future_variant" for reason in result["excluded_counts"]):
        result["status"] = "insufficient_evidence"
    # No-match means only that this bounded snapshot has no verified matching row.
    return result


def main():
    parser = argparse.ArgumentParser(description="Run the offline synthetic catalog replay; no network or model.")
    parser.add_argument("--demo", action="store_true", required=True)
    args = parser.parse_args()
    if args.demo:
        snapshot = CatalogSnapshot.load(Path(__file__).parent / "samples/catalog_demo.json")
        replay_at = _timestamp(snapshot.data()["snapshot_at"])
        result = recommend(snapshot, {"category": "t_shirt", "currency": "USD", "budget_max_minor": 3000,
                                      "material": "cotton", "size": "M"},
                           scope=CatalogScope(frozenset({"DEMO-SHOP-A"}), "US", "demo_storefront"), as_of=replay_at)
        result["demo_mode"] = "按合成快照时间回放；没有查询当前库存，也没有调用Shopify、模型、网页或MCP。"
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
