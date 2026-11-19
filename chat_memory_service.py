"""聊天候选的写入边界：模型只提议，应用按可信身份及冻结版本保存。

本模块不调用模型，也不增加业务工具权限。先把写入意图落入原任务记录，
再尝试数据库版本更新；结果不明只核对，不自动重新写。数据库与文件不是一个事务。
"""
from copy import deepcopy
import time

from sqlalchemy.exc import SQLAlchemyError

from memory_capture import validate_memory_proposal, fallback_memory_proposal, source_text
from preference_store import PreferenceConflict, validate_values

TTL_SECONDS = 1800
_LABELS = {
    "amount_format": {"yuan": "金额用元", "yuan_fixed2": "金额用元并保留两位小数", "fen": "金额用整数分"},
    "answer_style": {"brief": "简短回答", "standard": "标准详略", "detailed": "详细回答"},
}


class MemoryDecisionConflict(ValueError):
    pass


def public_memory_update(result):
    update = result.get("memory_update")
    if not isinstance(update, dict):
        return None
    return {key: deepcopy(update[key]) for key in ("status", "values", "source_quote", "reason", "role", "candidate_origin",
            "created_at", "expires_at", "version", "notice") if key in update}


def _notice(update):
    status = update["status"]
    title = "买家" if update.get("role") == "buyer" else "商家"
    values = "、".join(_LABELS[key][value] for key, value in update.get("values", {}).items())
    if status == "saved":
        return f"已保存{title}偏好：{values}（版本{update['version']}）。后续任务读取最新偏好；本任务原快照保留。"
    if status == "unchanged":
        return f"{title}已保存的偏好与本次要求一致，未重复写入。"
    if status == "proposed":
        return f"偏好建议：在{title}模式下{values}。尚未保存；可在本机网页查看本任务后确认或忽略，30分钟内有效。"
    return {"rejected": "本轮偏好候选缺少可靠依据或超出允许范围，未保存；业务结果单独保留。",
            "conflict": "偏好已被其他操作更新，未覆盖新设置；请查看最新偏好后重新表达要求。",
            "unknown": "偏好保存结果暂时无法确认，未宣称保存成功；可查看当前设置核对，不要重复提交同一任务。",
            "not_saved": "本次偏好未确认保存，未自动重写；如仍需要，请重新明确表达。",
            "dismissed": "已忽略本条偏好建议，没有修改长期偏好。",
            "expired": "本条偏好建议已过期，没有修改长期偏好。",
            "discarded": "本轮任务未完成或已取消，未保存聊天偏好。",
            "preparing": "正在确认偏好写入结果。"}.get(status, "")


class ChatMemoryService:
    def __init__(self, preferences, task_store, clock=time.time):
        self.preferences, self.tasks, self.clock = preferences, task_store, clock

    def _persist(self, record, result):
        # notice由应用生成；模型不能用一句“已记住”替代数据库写入确认。
        update = result["memory_update"]
        update["notice"] = _notice(update)
        original = result.setdefault("answer_before_memory_notice", result.get("answer", ""))
        result["answer"] = original + ("\n\n【偏好状态】" + update["notice"] if update["notice"] else "")
        # 同一会话历史只保留真实返回的程序通知，避免重放旧的模型保存承诺。
        history = result.get("history", [])
        if history and history[-1].get("role") == "assistant":
            history[-1]["content"] = result["answer"]
        return self.tasks.update(record["id"], record["buyer_id"], result=result, answer=result["answer"])

    def _inspect_or_commit(self, update, access, *, commit):
        expected = update["expected_version"]
        try:
            current = self.preferences.read(access["account_id"], access["role"])
            # 上次数据库可能已提交、任务文件尚未来得及记录；只凭来源/版本/值完整匹配认领。
            if (current["version"] == expected + 1 and current["values"] == update["target_values"]
                    and current["source"] == update["source"]):
                update.update(status="saved", version=current["version"], reason="confirmed_existing_write")
            elif current["version"] != expected:
                update.update(status="conflict", reason="version_changed")
            elif not commit:
                update.update(status="not_saved", reason="write_not_confirmed_no_retry")
            elif current["values"] == update["target_values"]:
                update.update(status="unchanged", version=current["version"], reason="same_values")
            else:
                saved = self.preferences.replace(access["account_id"], access["role"], update["target_values"],
                    expected, update["source"])
                update.update(status="saved", version=saved["version"], reason="version_checked_write")
        except PreferenceConflict:
            update.update(status="conflict", reason="version_changed")
        except SQLAlchemyError:
            update.update(status="unknown", reason="database_outcome_unconfirmed")

    def after_run(self, record, access, result, *, cancelled=False):
        raw = result.get("memory_proposal")
        origin = "model"
        if raw is None:
            # 模型漏掉候选时，程序只补有限、可核的展示建议，仍须用户确认；不伪称模型已提取。
            raw = fallback_memory_proposal(record["message"])
            origin = "program_signal"
            if raw is None:
                return result
        result = deepcopy(result)
        candidate = validate_memory_proposal(raw, record["message"])
        if candidate["status"] == "none":
            return result
        snapshot = record.get("preference_snapshot")
        if (not isinstance(snapshot, dict) or snapshot.get("role") != access["role"]
                or not access.get("account_id")):
            candidate = {"status": "rejected", "reason": "trusted_snapshot_missing"}
        update = {key: candidate[key] for key in ("values", "source_quote", "reason") if key in candidate}
        created_at = self.clock()
        update.update(role=access["role"], candidate_origin=origin,
                      created_at=created_at, expires_at=created_at + TTL_SECONDS)
        if cancelled or result.get("status") not in {"completed", "partial", "clarification"}:
            update.update(status="discarded", reason="task_not_eligible")
        elif candidate["status"] == "rejected":
            update["status"] = "rejected"
        else:
            update.update(status="preparing" if candidate["status"] == "explicit" else "proposed",
                expected_version=snapshot["version"],
                target_values={**validate_values(snapshot["values"]), **candidate["values"]},
                source={"kind": "user_chat", "text": source_text(record["id"], candidate["source_quote"])})
        result["memory_update"] = update
        # 先落写入意图，故障后不依赖模型答复猜测是否保存。
        self._persist(record, result)
        if update["status"] == "preparing":
            self._inspect_or_commit(update, access, commit=True)
            self._persist(record, result)
        return result

    def decide(self, record, access, action):
        """值和版本从本人原任务取，确认请求不能重传值或伪造别人的身份。"""
        if action not in {"accept", "dismiss"}:
            raise MemoryDecisionConflict("未知偏好处理动作")
        result = deepcopy(record.get("result") or {})
        update = result.get("memory_update")
        if not isinstance(update, dict) or update.get("role") != access["role"]:
            raise MemoryDecisionConflict("本任务没有当前角色可处理的偏好建议")
        status = update["status"]
        if status in {"saved", "unchanged", "dismissed", "expired", "conflict", "not_saved"}:
            return record  # 同一确认重放只读原结果，不再次写入或续期。
        if status in {"unknown", "preparing"}:
            self._inspect_or_commit(update, access, commit=False)
            return self._persist(record, result)
        if status != "proposed" or record["status"] not in {"completed", "partial", "clarification"}:
            raise MemoryDecisionConflict("该偏好建议当前不可确认")
        if self.clock() >= update["expires_at"]:
            update.update(status="expired", reason="proposal_expired")
        elif action == "dismiss":
            update.update(status="dismissed", reason="user_dismissed")
        else:
            update["source"]["text"] = source_text(record["id"], update["source_quote"], confirmed=True)
            update["status"] = "preparing"
            self._persist(record, result)
            self._inspect_or_commit(update, access, commit=True)
        return self._persist(record, result)
