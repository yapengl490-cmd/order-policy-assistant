"""保存本机任务及会话，不写业务订单。单服务进程使用，不是分布式队列。"""

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

MAX_HISTORICAL_EVIDENCE_BYTES = 12000
_PRIVATE_SNAPSHOT_FIELDS = {"buyer_id", "shop_id", "current_buyer_id", "current_shop_id", "sender_id",
                            "conversation_id", "authorization", "api_key", "model_api_key", "token"}


def _contains_private_fields(value):
    if isinstance(value, dict):
        return any(str(key).lower() in _PRIVATE_SNAPSHOT_FIELDS or _contains_private_fields(item)
                   for key, item in value.items())
    return isinstance(value, list) and any(_contains_private_fields(item) for item in value)


class TaskStore:
    """整条任务原子替换落盘；身份检查在每次读取时执行。"""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        # 服务重启后不假装仍在执行，不自动重做可能已收费的模型请求。
        with self.lock:
            for file in self.root.glob("task-*.json"):
                record = json.loads(file.read_text(encoding="utf-8"))
                if record["status"] in {"queued", "running", "cancelling"}:
                    record.update(status="interrupted", error="service_restarted", updated_at=time.time())
                    self._save(file, record)

    def _save(self, path: Path, value: dict):
        temp = path.with_suffix(".tmp")
        data = json.dumps(value, ensure_ascii=False, indent=2)
        # 私密运行记录不进入源码；flush/fsync 后替换，避免半份 JSON 被读取。
        with temp.open("w", encoding="utf-8") as stream:
            os.chmod(temp, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)

    @staticmethod
    def task_id(buyer_id: str, key: str) -> str:
        return hashlib.sha256((buyer_id + "\0" + key).encode()).hexdigest()[:32]

    def create(self, buyer_id: str, key: str, session_id: str, message: str,
               *, execution_mode: str = "single") -> tuple[dict, bool]:
        if execution_mode not in {"single", "multi", "programmatic", "batch_sequential", "snapshot_sql", "period_analysis", "refund_investigation"}:
            raise ValueError("未知执行方案")
        with self.lock:
            task_id = self.task_id(buyer_id, key)
            path = self.root / f"task-{task_id}.json"
            if path.exists():
                record = json.loads(path.read_text(encoding="utf-8"))
                if (record["message"] != message or record["session_id"] != session_id
                        or record.get("execution_mode", "single") != execution_mode):
                    raise ValueError("同一请求编号已用于不同问题；请新建请求编号")
                return record, False
            record = dict(id=task_id, buyer_id=buyer_id, session_id=session_id, message=message,
                          execution_mode=execution_mode,
                          status="queued", created_at=time.time(), updated_at=time.time(),
                          answer="", result=None, error=None, cancel_requested=False)
            self._save(path, record)
            return record, True

    def get(self, task_id: str, buyer_id: str) -> dict | None:
        if len(task_id) != 32 or any(c not in "0123456789abcdef" for c in task_id):
            return None
        with self.lock:
            path = self.root / f"task-{task_id}.json"
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if data["buyer_id"] == buyer_id else None

    def update(self, task_id: str, buyer_id: str, **changes):
        with self.lock:
            record = self.get(task_id, buyer_id)
            if record is None:
                raise KeyError("任务不存在")
            record.update(changes, updated_at=time.time())
            self._save(self.root / f"task-{task_id}.json", record)
            return record

    def session_history(self, buyer_id: str, session_id: str) -> list:
        with self.lock:
            records = []
            for path in self.root.glob("task-*.json"):
                d = json.loads(path.read_text(encoding="utf-8"))
                if d["buyer_id"] == buyer_id and d["session_id"] == session_id and d.get("result"):
                    records.append(d)
            if not records:
                return []
            latest = max(records, key=lambda d: d["created_at"])
            # 澄清上下文30分钟过期；旧已完成结果仍可按任务编号查看。
            if time.time() - latest["updated_at"] > 1800:
                return []
            return latest["result"].get("history", [])

    def session_conditions(self, buyer_id: str, session_id: str) -> dict | None:
        """取同一可信角色范围/会话最新状态；由reconcile显式处理到期。

        不能从模型文字历史恢复条件，也不能越过最新任务回捞更早已放弃的查询。
        buyer_id在应用层是buyer:编号或merchant:店铺的服务器授权范围键。
        """
        with self.lock:
            records = []
            for path in self.root.glob("task-*.json"):
                record = json.loads(path.read_text(encoding="utf-8"))
                if (record["buyer_id"] == buyer_id and record["session_id"] == session_id
                        and record.get("result")):
                    records.append(record)
            if not records:
                return None
            latest = max(records, key=lambda record: record["created_at"])
            return latest["result"].get("condition_state")

    def session_historical_evidence(self, buyer_id: str, session_id: str) -> list:
        """最近成功金额查询的完整历史快照，不能把旧assistant文字视为证据。

        只返回同一可信范围/会话，最多一个任务的两个成功工具结果；遇到新任务、
        重置、到期或换业务边界不向前回捞。超大结果整份省略，不截明细冒充完整。
        """
        if not buyer_id.startswith(("buyer:", "merchant:")):
            return []
        with self.lock:
            records = []
            for path in self.root.glob("task-*.json"):
                record = json.loads(path.read_text(encoding="utf-8"))
                if record["buyer_id"] == buyer_id and record["session_id"] == session_id and record.get("result"):
                    records.append(record)
            for record in sorted(records, key=lambda item: item["created_at"], reverse=True):
                if not 0 <= time.time() - record["updated_at"] <= 1800:
                    return []
                result = record["result"]
                state = result.get("condition_state") or {}
                boundary = any(state.get(key) for key in ("reset", "expired", "switched", "new_query"))
                if record["status"] == "completed":
                    selected = []
                    for fact in reversed(result.get("facts", [])):
                        if (not isinstance(fact, dict) or fact.get("tool") not in {"check_order", "summary_orders"}
                                or not isinstance(fact.get("arguments"), dict) or not isinstance(fact.get("result"), dict)
                                or fact["result"].get("status") != "ok"):
                            continue
                        try:
                            observed = datetime.fromisoformat(fact["observed_at"])
                            if observed.tzinfo is None or not 0 <= time.time() - observed.timestamp() <= 1800:
                                continue
                            digest = hashlib.sha256(json.dumps(fact["result"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                            if fact.get("result_snapshot_sha256") != digest:
                                continue
                            snapshot = {key: fact[key] for key in ("tool", "arguments", "result", "observed_at", "result_snapshot_sha256")}
                            if _contains_private_fields(snapshot):
                                continue
                        except (KeyError, TypeError, ValueError):
                            continue
                        selected.append(snapshot)
                        if len(selected) == 2:
                            break
                    if selected:
                        evidence = [{"evidence_kind": "historical_tool_snapshot", "is_current_database_fact": False,
                                     "source_task_id": record["id"],
                                     "task_completed_at": datetime.fromtimestamp(record["updated_at"], timezone.utc).isoformat(),
                                     "facts": list(reversed(selected))}]
                        encoded = json.dumps(evidence, ensure_ascii=False)
                        return json.loads(encoded) if len(encoded.encode()) <= MAX_HISTORICAL_EVIDENCE_BYTES else []
                if boundary:
                    return []
            return []

    def active_in_session(self, buyer_id: str, session_id: str) -> bool:
        with self.lock:
            for p in self.root.glob("task-*.json"):
                d = json.loads(p.read_text(encoding="utf-8"))
                if d["buyer_id"] == buyer_id and d["session_id"] == session_id and d["status"] in {"queued", "running", "cancelling"}:
                    return True
            return False

    def session_execution_mode(self, buyer_id: str, session_id: str) -> str | None:
        """方案是整段会话的属性；旧任务没有字段时仍按单智能体解释。"""
        with self.lock:
            rows = [json.loads(p.read_text(encoding="utf-8")) for p in self.root.glob("task-*.json")]
            matching = [r for r in rows if r["buyer_id"] == buyer_id and r["session_id"] == session_id]
            if not matching:
                return None
            return max(matching, key=lambda r: r["created_at"]).get("execution_mode", "single")

    def today_count(self) -> int:
        date = time.strftime("%Y-%m-%d")
        with self.lock:
            return sum(time.strftime("%Y-%m-%d", time.localtime(json.loads(p.read_text())["created_at"])) == date
                       for p in self.root.glob("task-*.json"))

    def recent(self, buyer_id: str) -> list[dict]:
        with self.lock:
            rows = [json.loads(p.read_text(encoding="utf-8")) for p in self.root.glob("task-*.json")]
            return sorted((r for r in rows if r["buyer_id"] == buyer_id), key=lambda r: r["created_at"], reverse=True)[:20]

    def cancel_all_active(self):
        """关闭服务时遍历全部在途任务，不使用界面历史列表的20条上限。"""
        with self.lock:
            for path in self.root.glob("task-*.json"):
                record = json.loads(path.read_text(encoding="utf-8"))
                if record["status"] in {"queued", "running", "cancelling"}:
                    record.update(status="cancelling", cancel_requested=True, updated_at=time.time())
                    self._save(path, record)
