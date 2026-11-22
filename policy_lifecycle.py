"""管理员本地政策更正/撤回；不是模型工具，也不接普通用户写入接口。

更正表示修复同一销售承诺区间的资料错误，不是把未来优惠追溯到旧订单。
actor 只记录操作者标签，不授予权限；调用进程必须持有管理员数据库连接。
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import uuid

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from policy_documents import IDENTIFIER, aware_time, parse_policy_file
from policy_kb import (PolicyConflictError, _bundle, _db_time, _insert_document,
                       _lifecycle_states, _lock_scopes, policy_documents, policy_lifecycle_events)


def _identity(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("政策编号格式无效")
    return value


def _admin_input(document_id, expected_checksum, reason, actor):
    _identity(document_id)
    if not isinstance(expected_checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_checksum):
        raise ValueError("必须指定核对过的原政策SHA256指纹")
    if (not isinstance(reason, str) or reason != reason.strip() or not 1 <= len(reason) <= 500
            or any(ord(char) < 32 for char in reason)):
        raise ValueError("原因须为1至500字符的单行说明，不能有首尾空白或控制字符")
    if not isinstance(actor, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", actor):
        raise ValueError("操作者审计标签须为1至80位字母数字、点、下划线或连字符")


def _locked_original(connection, document_id, checksum):
    statement = select(policy_documents).where(policy_documents.c.document_id == document_id)
    row = connection.execute(statement).mappings().first()
    if row is None:
        raise PolicyConflictError("原政策不存在，未变更")
    _lock_scopes(connection, [(row["store_id"], row["category"])])
    row = connection.execute(statement).mappings().one()
    if row["checksum"] != checksum:
        raise PolicyConflictError("原政策指纹已变化，未变更")
    state = _lifecycle_states(connection, [document_id])[document_id]
    if state.get("document_checksum", checksum) != checksum:
        raise PolicyConflictError("生命周期记录与原政策指纹不一致，未变更")
    return row, state


def _append_event(connection, row, state, action, *, reason, actor, replacement=None):
    event = dict(event_id=uuid.uuid4().hex, document_id=row["document_id"],
        event_index=state["event_index"] + 1, action=action, replacement_document_id=replacement,
        document_checksum=row["checksum"], reason=reason, actor=actor, recorded_at=datetime.now(timezone.utc))
    connection.execute(insert(policy_lifecycle_events).values(**event))
    return {**event, "status": action, "recorded_at": event["recorded_at"].isoformat(), "version": row["version"]}


def withdraw_policy(engine, document_id, *, expected_checksum, reason, actor):
    """撤回不删除原文、原向量、任务记录；无更正版时新查询停止政策判断。"""
    _admin_input(document_id, expected_checksum, reason, actor)
    try:
        with engine.begin() as connection:
            row, state = _locked_original(connection, document_id, expected_checksum)
            if state["status"] == "corrected":
                raise PolicyConflictError("原稿已被更正替代；如需撤回，请明确当前更正版编号")
            if state["status"] == "withdrawn":
                if state["reason"] != reason or state["actor"] != actor:
                    raise PolicyConflictError("该政策已撤回，不能覆盖原撤回理由或操作者")
                return {"status": "unchanged", "lifecycle": state, "embedding_requests": 0}
            event = _append_event(connection, row, state, "withdrawn", reason=reason, actor=actor)
    except IntegrityError:
        raise PolicyConflictError("生命周期并发/关联约束冲突，撤回事务未写入") from None
    return {"status": "withdrawn", "lifecycle": event, "embedding_requests": 0}


def correct_policy(engine, document_id, replacement_path, *, expected_checksum, reason, actor):
    """同区间可信更正原子替代；新编号和新分块，不复用旧块向量。"""
    _admin_input(document_id, expected_checksum, reason, actor)
    doc = parse_policy_file(replacement_path)  # 与普通导入相同的完整规则/例外校验。
    if doc["document_id"] == document_id:
        raise PolicyConflictError("更正版必须使用新政策编号，不覆盖原文")
    try:
        with engine.begin() as connection:
            row, state = _locked_original(connection, document_id, expected_checksum)
            if any(doc[key] != row[key] for key in ("store_id", "category", "source_kind")):
                raise PolicyConflictError("更正版必须属于原店铺、品类和资料来源类型")
            if any(aware_time(doc[key]) != _db_time(row[key], connection)
                   for key in ("effective_from", "effective_before")):
                raise PolicyConflictError("更正版必须保留原有效区间；未来版本应使用普通导入")
            if doc["version"] == row["version"]:
                raise PolicyConflictError("更正版必须使用新的版本标签")
            if state["status"] == "corrected":
                existing = connection.execute(select(policy_documents).where(
                    policy_documents.c.document_id == state["replacement_document_id"])).mappings().one()
                if (existing["document_id"] == doc["document_id"] and existing["checksum"] == doc["checksum"]
                        and existing["source_text"] == doc["source_text"] and state["reason"] == reason
                        and state["actor"] == actor and _bundle(connection, existing, aware_time(doc["effective_from"]))["status"] == "ok"):
                    return {"status": "unchanged", "lifecycle": state, "replacement_document_id": existing["document_id"],
                            "embedding_requests": 0}
                raise PolicyConflictError("原稿已被其他更正版替代，不能再次分叉更正")
            if _bundle(connection, row, _db_time(row["effective_from"], connection))["status"] != "ok":
                raise PolicyConflictError("原政策存储损坏，须先处理异常，不能跳过原文核验")
            all_rows = connection.execute(select(policy_documents).where(
                policy_documents.c.store_id == row["store_id"], policy_documents.c.category == row["category"])).mappings().all()
            states = _lifecycle_states(connection, [item["document_id"] for item in all_rows])
            # 允许与原稿及其祖先重合，不允许制造另一条相交的政策链。
            ancestors = {document_id}
            while True:
                added = {key for key, item in states.items() if item.get("replacement_document_id") in ancestors}
                if added <= ancestors:
                    break
                ancestors |= added
            start, end = aware_time(doc["effective_from"]), aware_time(doc["effective_before"])
            for item in all_rows:
                if item["version"] == doc["version"]:
                    raise PolicyConflictError("新版本标签已存在，未变更")
                if (start < _db_time(item["effective_before"], connection)
                        and _db_time(item["effective_from"], connection) < end
                        and item["document_id"] not in ancestors):
                    raise PolicyConflictError("更正会与无关政策区间重叠，未变更")
            _insert_document(connection, doc)
            event = _append_event(connection, row, state, "corrected", reason=reason, actor=actor,
                                  replacement=doc["document_id"])
    except IntegrityError:
        raise PolicyConflictError("政策编号/版本/生命周期并发约束冲突，更正事务未写入") from None
    return {"status": "corrected", "lifecycle": event, "replacement_document_id": doc["document_id"],
            "embedding_requests": 0, "index_status": "not_indexed"}


def get_policy_lifecycle(engine, document_ids):
    """只读外部标注；不修改旧任务文件，不返回任何用户账户信息。

    结果以文档编号为键。原稿 corrected 表示不能继续作为当前依据；即使其
    更正版随后撤回，也不自动恢复原稿。原文字节仍可用旧引用核对。
    """
    if not isinstance(document_ids, (tuple, list, set)) or not 1 <= len(document_ids) <= 100:
        raise ValueError("一次核对须为1至100个政策编号")
    identities = sorted({_identity(value) for value in document_ids})
    with engine.connect() as connection:
        rows = {row["document_id"]: row for row in connection.execute(select(policy_documents).where(
            policy_documents.c.document_id.in_(identities))).mappings()}
        states = _lifecycle_states(connection, identities)
        output = {}
        for identity in identities:
            if identity not in rows:
                output[identity] = {"status": "missing", "document_id": identity}
                continue
            row, state = rows[identity], states[identity]
            if state.get("document_checksum", row["checksum"]) != row["checksum"]:
                output[identity] = {"status": "invalid_policy", "document_id": identity, "reason": "lifecycle_checksum_mismatch"}
                continue
            output[identity] = {"status": state["status"], "document_id": identity, "version": row["version"],
                "document_checksum": row["checksum"], "reason": state.get("reason"),
                "recorded_at": state.get("recorded_at"), "replacement_document_id": state.get("replacement_document_id")}
        return output


def validate_policy_reference(engine, *, document_id, document_checksum):
    """模型前后核冻结引用：失效时明确停用，不静默用新稿替换原模型输入。"""
    _identity(document_id)
    if not isinstance(document_checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", document_checksum):
        raise ValueError("冻结引用缺少有效原文指纹")
    state = get_policy_lifecycle(engine, [document_id])[document_id]
    if state["status"] in {"missing", "invalid_policy"}:
        return {"status": state["status"], "reason": state.get("reason", "policy_document_missing"), "lifecycle": state}
    if state["document_checksum"] != document_checksum:
        return {"status": "invalid_policy", "reason": "frozen_policy_checksum_mismatch", "lifecycle": state}
    reason = {"active": "policy_reference_current", "withdrawn": "applicable_policy_withdrawn",
              "corrected": "policy_reference_superseded"}[state["status"]]
    return {"status": "ok" if state["status"] == "active" else state["status"], "reason": reason, "lifecycle": state}
