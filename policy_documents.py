"""受控合成政策：从同一份原文解析规则和完整条款，不维护第二套规则JSON。

这是有限模板解析器，不是通用法律文书理解。管理员修改语义须同时审查模板
解析与业务测试；不认识的条款、丢失的例外和过长条款均拒绝导入。
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

MAX_RETURN_WINDOW_HOURS = 8760
MAX_FILE_BYTES = 65536
MAX_CHUNK_CHARS = 3000
MAX_CHUNK_BYTES = 8000
CATEGORY = "ordinary_shoes"
CATEGORY_LABELS = {CATEGORY: "普通鞋类", "t_shirt": "T恤"}
HEADINGS = {"scope": "适用范围", "return_eligibility": "退货资格", "shipping_cost": "退货运费"}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")


class PolicyDocumentError(ValueError):
    pass


def aware_time(value, field="time"):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise PolicyDocumentError(field + "必须为带时区的有效时间") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PolicyDocumentError(field + "必须包含时区")
    return value.astimezone(timezone.utc)


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_policy_text(source_text: str, *, source_path="controlled-memory.md") -> dict:
    if not isinstance(source_text, str) or not source_text or len(source_text.encode()) > MAX_FILE_BYTES:
        raise PolicyDocumentError("政策文件为空或超过受控大小")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in source_text):
        raise PolicyDocumentError("政策含不支持的控制字符")
    lines = source_text.splitlines()
    if not lines or lines[0] != "---json":
        raise PolicyDocumentError("须使用受控JSON元数据头；正文不作任意格式推断")
    try:
        closing = lines.index("---", 1)
        def unique_pairs(items):
            values = {}
            for key, value in items:
                if key in values:
                    raise PolicyDocumentError("元数据字段重复")
                values[key] = value
            return values
        meta = json.loads("\n".join(lines[1:closing]), object_pairs_hook=unique_pairs)
    except (ValueError, json.JSONDecodeError):
        raise PolicyDocumentError("元数据头缺失或无效") from None
    required = {"document_id", "store_id", "category", "version", "title", "source_kind", "effective_from", "effective_before"}
    if not isinstance(meta, dict) or set(meta) != required:
        raise PolicyDocumentError("元数据字段必须完整且不能另存规则或额外指令")
    if any(not isinstance(meta[key], str) or not IDENTIFIER.fullmatch(meta[key]) for key in ("document_id", "store_id", "version")):
        raise PolicyDocumentError("政策标识/店铺/版本格式无效")
    if (not isinstance(meta["category"], str) or meta["category"] not in CATEGORY_LABELS
            or meta["source_kind"] != "synthetic_demo"):
        raise PolicyDocumentError("本版仅支持明确合成的普通鞋类或T恤受控政策")
    if not isinstance(meta["title"], str) or not 1 <= len(meta["title"]) <= 100 or "\n" in meta["title"]:
        raise PolicyDocumentError("标题无效")
    start, end = aware_time(meta["effective_from"], "effective_from"), aware_time(meta["effective_before"], "effective_before")
    if start >= end:
        raise PolicyDocumentError("适用区间必须为正长度")
    content = [(i + 1, line) for i, line in enumerate(lines) if i > closing and line.strip()]
    if not content or content[0][1] != "# " + meta["title"]:
        raise PolicyDocumentError("正文标题与元数据不一致")
    groups, current = {}, None
    for number, line in content[1:]:
        heading = re.fullmatch(r"## (.+) \{#([a-z_]+)\}", line)
        if heading:
            title, section = heading.groups()
            if section not in HEADINGS or title != HEADINGS[section] or section in groups:
                raise PolicyDocumentError("条款标题未知、重复或不一致")
            current = section
            groups[section] = [(number, line)]
        elif current is None or line.lstrip().startswith("#"):
            raise PolicyDocumentError("条款结构不符合受控模板")
        else:
            groups[current].append((number, line))
    if set(groups) != set(HEADINGS):
        raise PolicyDocumentError("必须同时保留适用范围、退货条件与运费条款")
    scope = [text for _, text in groups["scope"]][1:]
    expected_scope = [f"适用店铺：{meta['store_id']}。", f"适用品类：{CATEGORY_LABELS[meta['category']]}（{meta['category']}）。",
        "版本选择：按订单支付时刻匹配，适用区间左含右不含。",
        "资料性质：助手编写的合成演示，不是法律意见或任何真实商家、平台的售后规则。"]
    if scope != expected_scope:
        raise PolicyDocumentError("适用范围/演示声明与元数据或模板不一致")
    eligibility = [text for _, text in groups["return_eligibility"]][1:]
    if len(eligibility) != 4:
        raise PolicyDocumentError("退货条款必须完整保留时限、条件、例外与核验要求")
    window = re.fullmatch(r"退货申请时限：自订单收货时刻起([1-9]\d{0,3})小时内，包含收货时刻，不包含满\1小时的截止时刻；早于收货时刻的申请无效。", eligibility[0])
    if not window or not 1 <= int(window[1]) <= MAX_RETURN_WINDOW_HOURS:
        raise PolicyDocumentError("退货小时数/边界不明确或超受控范围")
    required_return = ["必要条件：商品未穿着且吊牌完好。", "例外：定制商品不适用本条退货资格。",
        "事实核验：买家对未穿着、吊牌完好或定制状态的自述均未核实，不能视为正式售后批准。"]
    if eligibility[1:] != required_return:
        raise PolicyDocumentError("必要条件、定制例外或自述核验规则缺失/改变，须人工审查后扩展解析器")
    shipping = [text for _, text in groups["shipping_cost"]][1:]
    required_shipping = ["尺码或个人偏好：因尺码不合适或个人偏好退货，退货运费由买家承担。",
        "商家责任：只有存在经核实的本次售后商家责任记录，才能判定退货运费由商家承担；否则责任与运费承担均待核实。",
        "核验边界：买家自述或已有退款金额不证明商家责任，不能据此承诺赔付。"]
    if shipping != required_shipping:
        raise PolicyDocumentError("运费或商家责任核验条款缺失/改变，不能按旧逻辑推断")
    checksum = _hash(source_text)
    chunks = []
    for section, group in groups.items():
        first, last = group[0][0], group[-1][0]
        text = "\n".join(lines[first - 1:last])
        if len(text) > MAX_CHUNK_CHARS or len(text.encode()) > MAX_CHUNK_BYTES:
            raise PolicyDocumentError("完整条款过长，需人工处理；不会截断例外或按字符切块")
        chunks.append({"chunk_id": meta["document_id"] + ":" + section, "section": section,
            "line_start": first, "line_end": last, "text": text, "checksum": _hash(text)})
    rules = {"return_window_hours": int(window[1]), "return_window_basis": "delivered_at",
        "return_window_boundary": "start_inclusive_end_exclusive", "allowed_categories": [meta["category"]], "custom_excluded": True,
        "requires_unworn": True, "requires_tags": True, "self_report_verified": False,
        "shipping_by_reason": {"size": "buyer", "preference": "buyer", "merchant_responsibility": "verified_record_required"}}
    citation_sections = {key: "return_eligibility" for key in rules}
    citation_sections.update(allowed_categories="scope", shipping_by_reason="shipping_cost")
    citations = {}
    for key, section in citation_sections.items():
        chunk = next(c for c in chunks if c["section"] == section)
        citations[key] = {**chunk, "citation_id": chunk["chunk_id"], "document_id": meta["document_id"],
            "document_checksum": checksum, "version": meta["version"], "store_id": meta["store_id"],
            "category": meta["category"], "source_kind": "synthetic_demo", "source_path": str(source_path)}
    return {**meta, "effective_from": start.isoformat(), "effective_before": end.isoformat(),
            "checksum": checksum, "source_path": str(source_path), "source_text": source_text,
            "chunks": chunks, "rules": rules, "rule_citations": citations}


def parse_policy_file(path) -> dict:
    path = Path(path)
    if path.suffix.lower() not in {".md", ".txt"} or path.stat().st_size > MAX_FILE_BYTES:
        raise PolicyDocumentError("仅支持大小受控的UTF-8 Markdown/文本政策")
    try:
        source = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise PolicyDocumentError("政策文件须为UTF-8") from None
    return parse_policy_text(source, source_path=str(path))


def load_manifest_files(manifest_path):
    """manifest只登记文件与内容指纹，不含另一套业务规则。"""
    manifest = Path(manifest_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"schema_version", "source_kind", "files"} or data["schema_version"] != 1 or data["source_kind"] != "synthetic_demo":
        raise PolicyDocumentError("资料清单格式无效")
    files, seen = [], set()
    for entry in data["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise PolicyDocumentError("清单不得另存规则")
        path = (manifest.parent / entry["path"]).resolve()
        if not path.is_relative_to(manifest.parent.resolve()) or path in seen:
            raise PolicyDocumentError("资料路径越界或重复")
        seen.add(path)
        parsed = parse_policy_file(path)
        if parsed["checksum"] != entry["sha256"]:
            raise PolicyDocumentError("资料内容与清单指纹不符")
        files.append(path)
    if not files or len(files) > 20:
        raise PolicyDocumentError("资料数量须为1至20")
    return files
