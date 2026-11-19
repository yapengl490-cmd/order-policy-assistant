"""飞书本人私聊入口；独立使用官方SDK，只调用已授权本机应用。

角色选择不是授权：仅服务器access.json中的B001/S001可用。
收到的事件、业务任务和消息交付分开保存；重放复用稳定请求编号及回复uuid。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import threading
import time
import uuid

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
API_BASE = "http://127.0.0.1:18080"
FINISHED = {"completed", "partial", "clarification", "failed", "cancelled", "limited", "interrupted"}
ACTIVE = {"queued", "running", "cancelling"}
MAX_SENDS = 3
RETRY_WINDOW = 1800
MAX_PENDING = 20
MAX_FINISHED_RECEIPTS = 200


@dataclass(frozen=True)
class BridgeConfig:
    app_id: str
    app_secret: str
    sender_id: str
    conversation_id: str
    access_token: str
    storage: Path
    old_lock: Path
    account_id: str | None = None


def load_config(root: Path = ROOT) -> BridgeConfig:
    config = {**dotenv_values(root / "local/app.env"), **os.environ}
    def configured_path(name):
        value = config.get(name)
        if not value:
            raise ValueError("飞书未配置：请显式设置 " + name + "，不会读取其他工程凭据")
        path = Path(value).expanduser()
        return path if path.is_absolute() else root / path
    secrets_path = configured_path("FEISHU_SECRETS_FILE")
    allowed_path = configured_path("FEISHU_ALLOWED_FILE")
    # 旧接收器的互斥锁仅在本机显式配置时复用；新克隆不依赖旁边的工程。
    legacy_root = config.get("DATUS_LEGACY_ROOT")
    legacy_lock = (Path(legacy_root).expanduser() / "local/feishu.lock" if legacy_root
                   else root / "local/feishu-receiver.lock")
    secrets = dotenv_values(secrets_path)
    allowed = json.loads(allowed_path.read_text(encoding="utf-8"))
    access = json.loads((root / "local/access.json").read_text(encoding="utf-8"))
    account = access["accounts"][0]
    roles = account.get("roles", [])
    if not any(role.get("role") == "buyer" and role.get("buyer_id") == "B001" for role in roles):
        raise ValueError("首个本机账号缺少预授权买家B001")
    if not any(role.get("role") == "merchant" and role.get("shop_id") == "S001" for role in roles):
        raise ValueError("首个本机账号缺少预授权店铺S001")
    values = [secrets.get("FEISHU_APP_ID"), secrets.get("FEISHU_APP_SECRET"), allowed.get("sender_id"),
              allowed.get("conversation_id"), account.get("token")]
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("飞书凭据、本人白名单或本机凭证不完整")
    account_id = account.get("account_id")
    if account_id is not None and (not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", account_id)):
        raise ValueError("本机账号标识格式不正确")
    return BridgeConfig(*values, storage=root / "local/feishu", old_lock=legacy_lock, account_id=account_id)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class JsonStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()

    def read(self, name: str, default=None):
        path = self.root / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def save(self, name: str, value: dict):
        path = self.root / name
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    def records(self):
        return [json.loads(path.read_text(encoding="utf-8")) for path in self.root.glob("message-*.json")]


class LocalApi:
    """只向固定本机地址发送凭证，禁用代理和重定向。"""
    def __init__(self, token: str, *, transport=None):
        self.token = token
        self.client = httpx.Client(base_url=API_BASE, timeout=10, transport=transport,
                                   trust_env=False, follow_redirects=False)

    def request(self, method, path, record, **kwargs):
        response = self.client.request(method, path, headers={"Authorization": "Bearer " + self.token,
                                      "X-Access-Mode": record["mode"]}, **kwargs)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("本机接口返回异常")
        return data

    def submit(self, record):
        return self.request("POST", "/api/tasks", record, json={
            "message": record["text"], "session_id": record["session_id"], "request_id": record["request_id"]})

    def identity(self, record):
        # 以已认证后端的身份为准，不让平台消息或模型自报账号范围。
        return self.request("GET", "/api/me", record)

    def fetch(self, record):
        return self.request("GET", "/api/tasks/" + record["task_id"], record)

    def close(self):
        self.client.close()


class LarkSender:
    def __init__(self, app_id: str, app_secret: str):
        import lark_oapi as lark
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).log_level(lark.LogLevel.ERROR).build()
        self.client.config.timeout = 15

    def reply(self, message_id: str, text: str, delivery_key: str) -> dict:
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
        body = (ReplyMessageRequestBody.builder().msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False)).uuid(delivery_key).build())
        request = ReplyMessageRequest.builder().message_id(message_id).request_body(body).build()
        try:
            response = self.client.im.v1.message.reply(request)
        except Exception:
            return {"status": "unknown", "reason": "transport_outcome_unknown"}
        if not response.success():
            return {"status": "failed", "reason": "api_rejected", "code": response.code}
        outbound = getattr(response.data, "message_id", None)
        return {"status": "sent", "message_id": outbound} if outbound else {
            "status": "unknown", "reason": "success_without_message_id"}


def _brief_money(fen: int, amount_format: str | None) -> str:
    """金额事实用整数分格式化，不引入浮点舍入；仅供已核验金额摘要。"""
    if amount_format == "fen":
        return f"{fen}分"
    yuan, cents = divmod(fen, 100)
    suffix = f".{cents:02d}"
    if amount_format == "yuan":
        suffix = suffix.rstrip("0").rstrip(".")
    return f"{yuan}{suffix}元"


def _brief_fact(fact: dict, values: dict) -> str:
    """已授权工具结果的有限摘要；不根据失败、缺字段或模型文字猜金额。"""
    if not isinstance(fact, dict):
        return "未识别的工具记录；请核对完整依据。"
    tool = fact.get("tool") if isinstance(fact.get("tool"), str) and fact["tool"] else "未标注工具"
    result = fact.get("result")
    if not isinstance(result, dict):
        return f"{tool}：完整依据见网页任务与导出。"
    if tool in {"check_order", "summary_orders"}:
        if result.get("status") == "not_found":
            cutoff = f"退款截止 {result['refund_before']}（不含）。" if isinstance(result.get("refund_before"), str) else ""
            return f"{tool}：当前授权范围未取得可用记录，不能据此断言订单不存在。{cutoff}"
        amounts = [result.get(key) for key in ("paid_amount_fen", "refund_amount_fen", "net_amount_fen")]
        valid = (result.get("status") == "ok"
                 and result.get("money_unit", "fen") == "fen"
                 and result.get("currency", "CNY") == "CNY"
                 and all(type(value) is int and value >= 0 for value in amounts))
        if not valid or amounts[0] - amounts[1] != amounts[2]:
            return f"{tool}：未取得可核对的完整金额；状态及原记录见完整依据。"
        name = f"订单 {result['order_id']}" if tool == "check_order" and isinstance(result.get("order_id"), str) else "区间汇总" if tool == "summary_orders" else "订单核查"
        labels = ("支付", "已退", "净额")
        text = f"{tool} · {name}：" + "；".join(
            f"{label}{_brief_money(value, values.get('amount_format'))}" for label, value in zip(labels, amounts))
        if isinstance(result.get("refund_before"), str):
            text += f"。退款截止 {result['refund_before']}（不含）"
        if tool == "summary_orders" and all(isinstance(result.get(key), str) for key in ("paid_from", "paid_before")):
            text += f"；支付范围 [{result['paid_from']}, {result['paid_before']})"
        provenance = result.get("provenance")
        source_status = provenance.get("status") if isinstance(provenance, dict) else None
        source_label = {"recorded": "已登记", "partial": "部分登记", "unknown": "未确认", "unavailable": "暂不可读", "not_applicable": "不适用"}.get(source_status, "未确认")
        return text + f"。导入来源：{source_label}；批次与原明细见完整依据。"
    if tool == "search_policies":
        policies = result.get("policies")
        citations = [p["citation_id"] for p in policies if isinstance(p, dict) and isinstance(p.get("citation_id"), str)] if isinstance(policies, list) else []
        text = "政策引用：" + "、".join(dict.fromkeys(citations)) if result.get("status") == "ok" and citations else "本次未取得足够政策依据"
        if result.get("scope") == "synthetic_demo":
            text += "（合成演示政策）"
        if isinstance(result.get("as_of"), str):
            text += f"；适用日期 {result['as_of']}"
        return f"{tool}：{text}；版本与原文见完整依据。"
    return f"{tool}：完整依据见网页任务与导出。"


def render_reply(task: dict, mode: str) -> str:
    lines = [f"{'商家·授权店铺' if mode == 'merchant' else '买家·本人订单'}只读模式",
             f"任务：{task['id']}\n处理状态：{task['status']}", task.get("answer") or "本次未获得完整说明。"]
    facts = task.get("facts") or []
    memory = task.get("memory")
    values = memory.get("effective_values") if isinstance(memory, dict) else None
    if isinstance(values, dict) and values.get("answer_style") == "brief":
        if facts:
            lines.append("程序依据摘要（与回答正文分开）：")
            lines.extend(_brief_fact(fact, values) for fact in facts)
        lines.append("完整依据：登录本机网页，在当前角色的历史中按上方任务编号查看或导出；消息摘要不替代原记录。")
        # 长度质量只度量模型answer；不截断正文冒充简洁达标，也不重写已缓存的交付正文。
        return "\n\n".join(lines)
    if facts:
        lines.append("程序实际读取结果（金额以这些事实为准）：")
        for fact in facts:
            lines.append(json.dumps(fact, ensure_ascii=False, indent=2))
    text = "\n\n".join(lines)
    if len(text) > 10000:
        text = text[:9800] + "\n\n消息过长，以上展示已截短；请在本机网页按任务编号查看完整事实。"
    return text


class BridgeIdentityError(ValueError):
    """账号、任务范围或原请求不一致；只给安全通知，不交付业务内容。"""


class FeishuBridge:
    def __init__(self, config: BridgeConfig, api, sender, *, clock=time.time, sleep=time.sleep, max_polls=160):
        self.config, self.api, self.sender = config, api, sender
        self.clock, self.sleep, self.max_polls = clock, sleep, max_polls
        self.store = JsonStore(config.storage)
        self.stopping = threading.Event()
        self.processing = set()
        self.send_slots = threading.BoundedSemaphore(2)
        with self.store.lock:
            if self.store.read("state.json") is None:
                self.store.save("state.json", {"mode": "buyer", "session_id": uuid.uuid4().hex, "events": {}, "reject_before": 0})

    def _prune(self, state):
        """最多200个已送达/手动停止收据+20个忙响应；旧事件按创建时间水位拒绝，防止删后重跑。"""
        records = self.store.records()
        terminal = sorted((r for r in records if r["delivery_status"] == "sent" or r["state"] == "closed_by_user"), key=lambda r: r["created_at"])
        busy = sorted((r for r in records if r.get("task_status") in {"busy", "local_command"}
                       and r["delivery_status"] != "sent"), key=lambda r: r["created_at"])
        removed = set()
        for record in terminal[:-MAX_FINISHED_RECEIPTS] + busy[:-MAX_PENDING]:
            (self.config.storage / ("message-" + record["key"] + ".json")).unlink()
            removed.add(record["key"])
            state["reject_before"] = max(state.get("reject_before", 0), record["message_created_at"])
        state["events"] = {event: key for event, key in list(state["events"].items())[-800:] if key not in removed}

    def _needs_review(self, record):
        """只列业务已结束但补送已耗尽的记录；不根据用户命令推定送达。"""
        first = record.get("first_attempt_at")
        return (record.get("state") != "closed_by_user" and record.get("task_status") in FINISHED
                and record.get("delivery_status") != "sent"
                and (record.get("attempts", 0) >= MAX_SENDS or record.get("delivery_status") == "needs_review"
                     or (first is not None and self.clock() - first >= RETRY_WINDOW)))

    def _review_reply(self):
        records = [record for record in self.store.records()
                   if self._load(record["key"]) is not None and self._needs_review(record)]
        if not records:
            return "目前没有达到补送次数或时窗上限的业务记录。此检查不确认平台是否已经送达。"
        lines = ["以下结果已停止自动补送，需要核对；列表本身不更改任何结果："]
        for record in sorted(records, key=lambda item: item["created_at"])[:MAX_PENDING]:
            lines.append(f"编号：{record['key']}\n任务：{record['task_id']}\n业务：{record['task_status']}；交付：{record['delivery_status']}")
        lines.append("如决定停止某条后续补送，可发送 /停止补送 完整编号。此操作仅释放待处理占位，保留结果与原交付状态，不代表已送达。")
        return "\n\n".join(lines)

    def _stop_delivery(self, key):
        target = self._load(key)
        if target and target.get("state") == "closed_by_user":
            return "该记录此前已由你停止补送；原结果与交付状态保留，不代表已送达。"
        if not target or not self._needs_review(target) or key in self.processing:
            return "未停止补送：需提供 /待核对 中的完整编号，且记录已达到补送上限、当前没有正在处理。"
        target.update(state="closed_by_user", closed_at=self.clock(), closure_reason="user_requested_stop")
        self._save(target)
        return "已按你的明确指令停止该记录的后续补送并释放占位。业务结果、原交付状态和去重记录保留；此操作不代表已送达，也不表示已经核实平台结果。"

    def accept(self, event: dict) -> str | None:
        """仅通过SDK事件身份与本机白名单接纳；聊天正文不参与身份映射。"""
        if self.stopping.is_set() or event.get("chat_type") != "p2p" or event.get("message_type") != "text":
            return None
        if event.get("sender_id") != self.config.sender_id or event.get("conversation_id") != self.config.conversation_id:
            return None
        if any(not isinstance(event.get(key), str) or not event[key] or len(event[key]) > 256
               for key in ("message_id", "event_id")):
            return None
        text = event.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        key, event_key = _digest(event["message_id"]), _digest(event["event_id"])
        with self.store.lock:
            state = self.store.read("state.json")
            previous = state["events"].get(event_key)
            if previous and previous != key:
                return None
            path = "message-" + key + ".json"
            existing = self.store.read(path)
            if existing:
                # 同一消息即使以新事件ID重送，也不能改变模式、正文或请求编号。
                state["events"][event_key] = key
                self._prune(state)
                self.store.save("state.json", state)
                return key
            sent_at = event.get("created_at")
            if (type(sent_at) not in (int, float) or not state.get("reject_before", 0) < sent_at <= self.clock() + 60
                    or self.clock() - sent_at > 86400):
                return None
            text = text.strip()
            record = {"key": key, "event_sha256": event_key, "message_id": event["message_id"],
                "sender_id": event["sender_id"], "conversation_id": event["conversation_id"],
                "text": text[:2000], "mode": state["mode"], "session_id": state["session_id"],
                "request_id": "feishu_" + key[:48], "task_id": None, "state": "received",
                "created_at": self.clock(), "message_created_at": sent_at, "reply_text": None, "delivery_key": uuid.uuid4().hex,
                "delivery_status": "not_ready", "attempts": 0, "first_attempt_at": None,
                "uncertain": False, "task_status": None}
            pending = sum(item["delivery_status"] != "sent" and item["state"] != "closed_by_user" and item.get("task_status") not in {"busy", "local_command"}
                          for item in self.store.records())
            if pending >= MAX_PENDING and text not in {"/重试", "/待核对"} and not text.startswith("/停止补送 "):
                record.update(task_id=None, state="finished", task_status="busy", delivery_status="pending",
                    reply_text="当前有20条尚未完成交付的消息，服务繁忙。本条未创建业务任务、未调用模型；请先用/重试检查已有任务，或用/待核对查看达到补送上限的记录，稍后另发新问题。")
            elif text == "/待核对" or text.startswith("/停止补送 "):
                reply_text = self._review_reply() if text == "/待核对" else self._stop_delivery(text[len("/停止补送 "):].strip())
                record.update(state="finished", task_status="local_command", delivery_status="pending", reply_text=reply_text)
            elif text in ("/买家", "/商家"):
                state["mode"] = "buyer" if text == "/买家" else "merchant"
                state["session_id"] = uuid.uuid4().hex
                record.update(mode=state["mode"], session_id=state["session_id"], state="finished",
                    task_status="local_command", delivery_status="pending",
                    reply_text=f"已切换到{'买家本人订单' if text == '/买家' else '商家授权店铺'}只读模式，并开始新会话。角色与范围来自服务器预授权，聊天自报身份不会扩权。")
            elif text == "/重试":
                pending = [item for item in self.store.records() if item["delivery_status"] != "sent"
                           and item["text"] != "/重试" and item.get("task_status") != "busy" and item["sender_id"] == self.config.sender_id
                           and item["conversation_id"] == self.config.conversation_id and item["attempts"] < MAX_SENDS
                           and item["delivery_status"] != "needs_review" and item["state"] != "closed_by_user"
                           and (item["first_attempt_at"] is None or 0 <= self.clock() - item["first_attempt_at"] < RETRY_WINDOW)]
                target = max(pending, key=lambda item: item["created_at"]) if pending else None
                record["retry_target"] = target["key"] if target else None
                record.update(state="retry_command", task_status="local_command")
            elif text.startswith("/") or len(text) > 2000:
                record.update(state="finished", task_status="local_command", delivery_status="pending",
                    reply_text="可用命令：/买家、/商家、/重试、/待核对、/停止补送 完整编号。业务问题限2000字符；不能用命令指定其他买家或店铺。")
            else:
                # 账号范围须从已认证后端取得；受理平台事件时不猜旧的店铺共用任务编号。
                record["task_id"] = None
            if text in ("/买家", "/商家") and record["task_status"] == "local_command":
                # 先提交角色会话再写命令收据；崩溃重放至多再清空会话，不会回退到旧角色。
                self.store.save("state.json", state)
            self.store.save(path, record)
            state["events"][event_key] = key
            self._prune(state)
            self.store.save("state.json", state)
            return key

    def _load(self, key):
        if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            return None
        record = self.store.read("message-" + key + ".json")
        if record and (record.get("sender_id") != self.config.sender_id or record.get("conversation_id") != self.config.conversation_id):
            return None
        return record

    def _save(self, record):
        self.store.save("message-" + record["key"] + ".json", record)

    def _prepare_task_scope(self, record):
        """后端身份决定任务范围；固定后再重试，避免凭据改绑后接续到另一个人。"""
        identity = self.api.identity(record)
        role = record["mode"]
        scope = "buyer:B001" if role == "buyer" else "merchant:S001"
        field, value = ("buyer_id", "B001") if role == "buyer" else ("shop_id", "S001")
        if (not isinstance(identity, dict) or identity.get("role") != role
                or identity.get(field) != value or identity.get("key") != scope):
            raise BridgeIdentityError("backend role or business scope mismatch")
        account = identity.get("account_id")
        if account is not None and (not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", account)):
            raise BridgeIdentityError("invalid backend account")
        if self.config.account_id is not None and account != self.config.account_id:
            raise BridgeIdentityError("configured account changed")
        if account is not None:
            scope += ":account:" + account
        if record.get("task_scope") is not None and record["task_scope"] != scope:
            raise BridgeIdentityError("receipt account changed")
        current_id = _digest(scope + "\0" + record["request_id"])[:32]
        legacy_scope = "buyer:B001" if role == "buyer" else "merchant:S001"
        legacy_id = _digest(legacy_scope + "\0" + record["request_id"])[:32]
        if record.get("task_id") not in (None, current_id, legacy_id):
            raise BridgeIdentityError("receipt task identity mismatch")
        record["task_scope"] = scope
        if record.get("task_id") is None:
            record["task_id"] = current_id
        with self.store.lock:
            self._save(record)
        return current_id

    @staticmethod
    def _check_task(record, task):
        if (not isinstance(task, dict) or task.get("id") != record["task_id"]
                or task.get("message") != record["text"] or task.get("session_id") != record["session_id"]):
            raise BridgeIdentityError("task id or original request mismatch")

    def _notify_local_error(self, key, error):
        """故障通知有独立送达记录，不能把通知成功当业务结果送达。"""
        if not isinstance(error, (httpx.HTTPStatusError, httpx.TransportError, BridgeIdentityError)):
            return
        code = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
        explanation = ("本机账号权限或任务身份核对失败，已停止继续执行；请核对配置"
                       if isinstance(error, BridgeIdentityError) or code in {401, 403}
                       else f"本机接口暂未完成处理（HTTP {code}）" if code
                       else "本机助手连接未完成，提交或查询结果尚未确认")
        with self.store.lock:
            record = self._load(key)
            if not record:
                return
            notice = record.get("notice")
            if notice is None:
                notice = {"text": explanation + "。已保留原请求编号，未把故障当作无订单或业务已完成。请检查本机服务后发送 /重试；将先查询原任务。",
                          "delivery_key": uuid.uuid4().hex, "status": "pending", "attempts": 0,
                          "first_attempt_at": None, "uncertain": False}
            if notice["status"] == "sent" or notice["attempts"] >= MAX_SENDS:
                return
            if notice["first_attempt_at"] is not None and not 0 <= self.clock() - notice["first_attempt_at"] < RETRY_WINDOW:
                return
            was_uncertain = notice.get("uncertain") or notice["status"] in {"sending", "unknown"}
            notice.update(status="sending", attempts=notice["attempts"] + 1,
                          first_attempt_at=self.clock() if notice["first_attempt_at"] is None else notice["first_attempt_at"])
            record["notice"] = notice
            self._save(record)
        try:
            with self.send_slots:
                outcome = self.sender.reply(record["message_id"], notice["text"], notice["delivery_key"])
        except Exception:
            outcome = {"status": "unknown"}
        with self.store.lock:
            record = self._load(key)
            status = outcome.get("status") if isinstance(outcome, dict) else "unknown"
            if status not in {"sent", "failed", "unknown"} or (status == "sent" and not outcome.get("message_id")):
                status = "unknown"
            if status == "failed" and was_uncertain:
                status = "unknown"
            notice.update(status=status, uncertain=status == "unknown", outbound_id=outcome.get("message_id") if status == "sent" else None)
            record["notice"] = notice
            self._save(record)

    def _send(self, key):
        with self.store.lock:
            record = self._load(key)
            if not record or record["state"] == "closed_by_user" or not record.get("reply_text") or record["delivery_status"] in {"sent", "needs_review"}:
                return
            if record["attempts"] >= MAX_SENDS:
                return
            if record["attempts"] and (record["first_attempt_at"] is None or not 0 <= self.clock() - record["first_attempt_at"] < RETRY_WINDOW):
                record["delivery_status"] = "needs_review"
                self._save(record)
                return
            was_uncertain = record.get("uncertain") or record["delivery_status"] in {"sending", "unknown"}
            record["attempts"] += 1
            record["first_attempt_at"] = self.clock() if record["first_attempt_at"] is None else record["first_attempt_at"]
            record["delivery_status"] = "sending"
            self._save(record)
        try:
            with self.send_slots:
                outcome = self.sender.reply(record["message_id"], record["reply_text"], record["delivery_key"])
        except Exception:
            outcome = {"status": "unknown", "reason": "sender_exception"}
        with self.store.lock:
            result = outcome.get("status") if isinstance(outcome, dict) else "unknown"
            if result not in {"sent", "failed", "unknown"} or (result == "sent" and not outcome.get("message_id")):
                result = "unknown"
            if result == "failed" and was_uncertain:
                result = "unknown"
            record.update(delivery_status=result, uncertain=result == "unknown",
                          outbound_id=outcome.get("message_id") if result == "sent" else None)
            self._save(record)
            state = self.store.read("state.json")
            self._prune(state)
            self.store.save("state.json", state)

    def process(self, key):
        with self.store.lock:
            if key in self.processing:
                return
            self.processing.add(key)
        try:
            self._process(key)
        finally:
            with self.store.lock:
                self.processing.discard(key)

    def _process(self, key):
        with self.store.lock:
            record = self._load(key)
            if not record or record["state"] == "closed_by_user" or record["delivery_status"] == "sent" or self.stopping.is_set():
                return
            if record["state"] == "retry_command":
                target = record.get("retry_target")
            else:
                target = None
        if record["state"] == "retry_command":
            if target:
                self.process(target)
            with self.store.lock:
                target_record = self._load(target) if target else None
                outcome = target_record["delivery_status"] if target_record else "none"
                record.update(state="finished", task_status="local_command", delivery_status="pending",
                    reply_text=f"已检查最近未送达结果，交付状态：{outcome}。补送复用原结果及去重编号，不重新生成业务；超时窗或达到次数上限需人工核对。")
                self._save(record)
            self._send(key)
            return
        # 本地命令没有业务任务；业务结果重送也须重新确认账号仍获授权。
        if record.get("reply_text") and record.get("task_status") in {"local_command", "busy"}:
            self._send(key)
            return
        try:
            # 身份请求有超时；等待网络时不占收据锁，其他事件仍可落盘。
            current_id = self._prepare_task_scope(record)
            task = self.api.fetch(record)
            if task is None:
                # 旧ID可能属于升级前或上一版未确认提交；先保留原ID查询，再在可信新范围恢复。
                if record.get("submitted"):
                    raise BridgeIdentityError("accepted task missing")
                if record["task_id"] != current_id:
                    with self.store.lock:
                        record["legacy_task_id"] = record["task_id"]
                        record["task_id"] = current_id
                        self._save(record)
                    task = self.api.fetch(record)
                if task is None:
                    with self.store.lock:
                        record["state"] = "submitting"
                        self._save(record)
                    task = self.api.submit(record)
            self._check_task(record, task)
            if record.get("reply_text"):
                self._send(key)  # 原交付正文和去重编号不因重试重新生成。
                return
            with self.store.lock:
                record.update(submitted=True, state="running", task_status=task.get("status"))
                self._save(record)
            for _ in range(self.max_polls):
                if self.stopping.is_set() or task["status"] in FINISHED:
                    break
                if task["status"] not in ACTIVE:
                    raise ValueError("unknown task status")
                self.sleep(1)
                task = self.api.fetch(record)
                self._check_task(record, task)
            with self.store.lock:
                record["task_status"] = task["status"]
                if task["status"] not in FINISHED or self.stopping.is_set():
                    record["state"] = "waiting"
                    self._save(record)
                    return
                record.update(state="finished", result=task, reply_text=render_reply(task, record["mode"]), delivery_status="pending")
                self._save(record)
        except Exception as error:
            with self.store.lock:
                record.update(state="waiting", last_error=type(error).__name__)
                self._save(record)
            self._notify_local_error(key, error)
            return
        self._send(key)

    def handle(self, event):
        key = self.accept(event)
        if key:
            self.process(key)
        return key


class BoundedDispatcher:
    """最多两个在途worker；其他事件只落盘，不排入无界线程队列。"""
    def __init__(self, bridge):
        self.bridge = bridge
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="feishu-task")
        self.lock = threading.RLock()
        self.inflight = set()
        self.recovery = set()

    def _start(self, key):
        with self.lock:
            if self.bridge.stopping.is_set() or key in self.inflight or len(self.inflight) >= 2:
                return False
            with self.bridge.store.lock:
                record = self.bridge._load(key)
                if not record:
                    self.recovery.discard(key)
                    return False
                if record.get("task_status") not in {"busy", "local_command"}:
                    for other_key in self.inflight:
                        other = self.bridge._load(other_key)
                        if (other and other.get("task_status") not in {"busy", "local_command"}
                                and (other["mode"], other["session_id"]) == (record["mode"], record["session_id"])):
                            return False  # 同会话顺序处理，避免本机API拒绝第二个在途请求。
            self.inflight.add(key)
            self.recovery.discard(key)
            future = self.pool.submit(self.bridge.process, key)
            future.add_done_callback(lambda done: self._done(key, done))
            return True

    def _done(self, key, future):
        # 不打印异常正文，避免凭据/连接详情进入日志。
        try:
            future.result()
        except Exception:
            print("飞书事件处理未完成，请检查本机状态。", flush=True)
        with self.lock:
            self.inflight.discard(key)
        self.pump()

    def submit(self, event):
        key = self.bridge.accept(event)
        if key:
            record = self.bridge._load(key)
            if record and record.get("task_status") == "busy":
                # SDK回调内有界回复，不创建业务线程/任务；发送并发另有2个槽位约束。
                self.bridge.process(key)
            else:
                self._start(key)
        self.pump()

    def pump(self, recover=False):
        with self.bridge.store.lock:
            records = sorted(self.bridge.store.records(), key=lambda record: record["created_at"])
        if recover:
            with self.lock:
                self.recovery.update(record["key"] for record in records if
                    record["state"] in {"submitting", "running", "waiting"} or record["delivery_status"] == "pending")
        for record in records:
            # 自动排空新接纳事件；waiting/未知交付仅启动恢复或用户重试触发一次。
            eligible = record["state"] in {"received", "retry_command"}
            with self.lock:
                eligible = eligible or record["key"] in self.recovery
            if eligible:
                if self._start(record["key"]):
                    with self.lock:
                        self.recovery.discard(record["key"])
                else:
                    with self.lock:
                        if len(self.inflight) >= 2:
                            break

    def close(self):
        self.bridge.stopping.set()
        self.pool.shutdown(wait=True, cancel_futures=False)


def normalize_event(data) -> dict | None:
    """从官方SDK事件取身份；非文本、机器人发送者或无身份事件拒绝。"""
    try:
        event, header = data.event, data.header
        message, sender = event.message, event.sender
        if sender.sender_type != "user":
            return None
        content = json.loads(message.content or "{}")
        return {"event_id": header.event_id, "message_id": message.message_id,
                "sender_id": sender.sender_id.open_id, "conversation_id": message.chat_id,
                "chat_type": message.chat_type, "message_type": message.message_type, "text": content.get("text"),
                "created_at": int(message.create_time) / 1000}
    except (AttributeError, TypeError, ValueError):
        return None


def acquire_locks(config, stack):
    config.storage.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in (config.old_lock, config.storage / "bridge.lock"):
        # 原应用锁必须使用同一路径，不能只给新工程加锁。
        handle = stack.enter_context(path.open("a"))
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("旧机器人或本项目接收器已经运行，请先正常停止原接收器。") from error


def main():
    import lark_oapi as lark
    config = load_config()
    with ExitStack() as stack:
        acquire_locks(config, stack)
        pid_file = config.storage / "bridge.pid"
        pid_file.write_text(str(os.getpid()), encoding="ascii")
        def clear_pid():
            if pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
                pid_file.unlink()
        stack.callback(clear_pid)
        api = LocalApi(config.access_token)
        stack.callback(api.close)
        bridge = FeishuBridge(config, api, LarkSender(config.app_id, config.app_secret))
        dispatcher = BoundedDispatcher(bridge)
        stack.callback(dispatcher.close)
        def callback(data):
            event = normalize_event(data)
            if event:
                dispatcher.submit(event)
        handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(callback).build()
        client = lark.ws.Client(config.app_id, config.app_secret, event_handler=handler, log_level=lark.LogLevel.ERROR)
        print("仅接收已确认本人私聊。/买家、/商家切换预授角色；/重试检查最近未送达结果；/待核对查看达到补送上限的记录。Control+C停止。", flush=True)
        dispatcher.pump(recover=True)
        client.start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("飞书接收已停止；数据与已提交任务保留。")
    except Exception as error:
        print(f"飞书入口未启动或已停止（{type(error).__name__}）；请检查本机配置和服务状态，未输出凭据。")
        raise SystemExit(1)
