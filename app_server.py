"""本机演示入口：认证→受限任务→真实工具→结果和证据。只启动一个进程。"""

import fcntl
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Header
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from agent_runtime import run_agent
from chat_memory_service import ChatMemoryService, MemoryDecisionConflict, public_memory_update
from multi_agent_runtime import run_multi_agent
from programmatic_runtime import run_programmatic_agent, run_batch_sequential_agent
from sales_analysis import compare_periods
from app_config import AppSettings, ROOT, authorize
from condition_state import public_condition_state, reconcile_conditions
from database import get_engine, get_preference_engine
from preference_store import PreferenceStore, PreferenceConflict, validate_values
from refund_service import check_order, list_orders
from task_store import TaskStore


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,64}$")
    execution_mode: Literal["single", "multi", "programmatic", "batch_sequential", "snapshot_sql"] = "single"


class PeriodComparisonRequest(BaseModel):
    """只接受业务时间条件；身份及店铺从认证结果取得。"""
    model_config = ConfigDict(extra="forbid")
    first_from: datetime
    first_before: datetime
    second_from: datetime
    second_before: datetime
    refund_before: datetime


class PeriodAnalysisRequest(PeriodComparisonRequest):
    """固定比较表单触发有限模型解释；不接受客户端自报事实或身份。"""
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,64}$")


class PreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, str]
    expected_version: Annotated[StrictInt, Field(ge=0)]


class PreferenceDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: Annotated[StrictInt, Field(ge=0)]


class MemoryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["accept", "dismiss"]


def public_record(record: dict) -> dict:
    """普通接口不返回内部对话提示；事实和本次调用摘要供本人核查。"""
    result = record.get("result") or {}
    return {k: record.get(k) for k in ("id", "session_id", "message", "status", "created_at", "updated_at", "answer", "error")} | {
        "facts": result.get("facts", []), "usage": result.get("usage", {}),
        "evidence_status": result.get("evidence_status", "no_tool_evidence"),
        "answer_semantics_verified": result.get("answer_semantics_verified", False),
        "conditions": public_condition_state(result["condition_state"]) if result.get("condition_state") else None,
        "memory": result.get("memory"),
        "presentation": result.get("presentation"),
        "memory_update": public_memory_update(result),
        "execution_mode": record.get("execution_mode", "single"),
        "support_validation": result.get("support_validation"),
        "shopping_validation": result.get("shopping_validation"),
        "orchestration": result.get("orchestration"),
        "budget": result.get("budget"),
        "program_executions": result.get("program_executions"),
        "sql_validation": result.get("sql_validation"),
        "analysis_conditions": record.get("analysis_conditions"),
        "analysis_validation": result.get("analysis_validation"),
        "analysis_evidence": result.get("analysis_evidence"),
        "fixed_answer": result.get("fixed_answer"),
        "answer_quality": result.get("answer_quality"),
    } | {k: result[k] for k in ("support_contract_version", "support_claims") if k in result}


def create_app(settings=None, engine=None, agent=run_agent, preference_engine=None,
               multi_agent=run_multi_agent, programmatic_agent=run_programmatic_agent,
               batch_sequential_agent=run_batch_sequential_agent, snapshot_sql_agent=None, period_analysis_agent=None):
    settings = settings or AppSettings.load()
    own_engine = engine is None
    engine = engine or get_engine()
    own_preference_engine = preference_engine is None and settings.memory_enabled
    if own_preference_engine:
        preference_engine = get_preference_engine()
    preferences = PreferenceStore(preference_engine) if preference_engine is not None else None
    guard = threading.Lock()
    slots = threading.BoundedSemaphore(settings.max_concurrent_tasks)
    bearer = HTTPBearer(auto_error=False)

    @asynccontextmanager
    async def lifespan(app):
        settings.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_file = (settings.runtime_dir / "server.lock").open("a")
        locked = False
        pid_file = settings.runtime_dir / "server.pid"
        try:
            # 防止两个服务同时将彼此任务误判为中断，单进程约束由程序执行。
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            pid_file.write_text(str(os.getpid()), encoding="ascii")
            app.state.store = TaskStore(settings.runtime_dir)
            app.state.pool = ThreadPoolExecutor(max_workers=settings.max_concurrent_tasks)
            yield
        finally:
            if hasattr(app.state, "pool"):
                # 取消标记阻止后续外部调用；已经发出的HTTP请求要等超时或响应。
                app.state.store.cancel_all_active()
                app.state.pool.shutdown(wait=True, cancel_futures=False)
            if own_engine:
                engine.dispose()
            if own_preference_engine:
                preference_engine.dispose()
            if locked and pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
                pid_file.unlink()
            lock_file.close()

    app = FastAPI(title="订单与退款核查助手", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def current_buyer(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
                      x_access_mode: str = Header("buyer")):
        scope = authorize(credentials.credentials if credentials else "", x_access_mode, settings)
        if scope is None:
            raise HTTPException(401, "请输入本机测试凭证；聊天自报买家编号不能登录")
        return scope

    def preference_account(access):
        if preferences is None or not access.get("account_id"):
            raise HTTPException(503, "偏好存储或账号绑定尚未就绪；没有把未读取当作没有偏好")
        return access["account_id"]

    def read_preferences(access):
        account = preference_account(access)
        try:
            return preferences.read(account, access["role"])
        except SQLAlchemyError:
            raise HTTPException(503, "偏好读取失败，请稍后重试；订单直接核查仍可使用") from None

    def task_scope(access):
        return access.get("task_key", access["key"])

    def owned_task(task_id, access):
        store = app.state.store
        record = store.get(task_id, task_scope(access))
        if record is None and access.get("legacy_task_scope"):
            record = store.get(task_id, access["legacy_task_scope"])
        return record

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return JSONResponse(status_code=503, content={"detail": "数据库读取失败，未得出查询结论", "error_type": type(exc).__name__})

    @app.exception_handler(ValueError)
    async def invalid_input(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.get("/")
    def index():
        return FileResponse(ROOT / "web" / "index.html")

    @app.get("/app.js")
    def script():
        return FileResponse(ROOT / "web" / "app.js", media_type="text/javascript")

    @app.get("/style.css")
    def style():
        return FileResponse(ROOT / "web" / "style.css", media_type="text/css")

    @app.get("/health")
    def health():
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "model_enabled": settings.model_enabled}

    @app.get("/api/me")
    def me(buyer=Depends(current_buyer)):
        return {key: value for key, value in buyer.items() if key not in {"task_key", "legacy_task_scope"}} | {
            "scope": "local_test", "model_enabled": settings.model_enabled,
            "memory_enabled": preferences is not None and bool(buyer.get("account_id"))}

    @app.get("/api/preferences")
    def get_preferences(buyer=Depends(current_buyer)):
        return read_preferences(buyer)

    @app.put("/api/preferences")
    def put_preferences(body: PreferenceUpdate, buyer=Depends(current_buyer)):
        account = preference_account(buyer)
        values = validate_values(body.values)
        try:
            return preferences.replace(account, buyer["role"], values, body.expected_version,
                                       {"kind": "user_settings", "text": "用户在当前角色的偏好设置中明确保存"})
        except PreferenceConflict:
            raise HTTPException(409, "偏好已被更新，请刷新后再决定；未覆盖新值") from None
        except SQLAlchemyError:
            raise HTTPException(503, "偏好保存未确认，请重新读取后核对；未宣称已保存") from None

    @app.delete("/api/preferences")
    def delete_preferences(body: PreferenceDelete, buyer=Depends(current_buyer)):
        account = preference_account(buyer)
        try:
            return preferences.delete(account, buyer["role"], body.expected_version)
        except PreferenceConflict:
            raise HTTPException(409, "偏好已被更新，请刷新后再决定；未删除新值") from None
        except SQLAlchemyError:
            raise HTTPException(503, "偏好删除未确认，请重新读取后核对") from None

    @app.get("/api/orders")
    def orders(limit: int = Query(20, ge=1, le=20), buyer=Depends(current_buyer)):
        if buyer["role"] == "merchant":
            from merchant_service import list_shop_orders
            return list_shop_orders(engine, buyer["shop_id"], limit=limit)
        return list_orders(engine, buyer["buyer_id"], limit=limit)

    @app.get("/api/orders/{order_id}")
    def order(order_id: str, refund_before: datetime | None = None, buyer=Depends(current_buyer)):
        if buyer["role"] == "merchant":
            from merchant_service import check_shop_order
            return check_shop_order(engine, order_id, buyer["shop_id"], refund_before)
        return check_order(engine, order_id, buyer["buyer_id"], refund_before)

    @app.post("/api/analysis/compare")
    def comparison(body: PeriodComparisonRequest, buyer=Depends(current_buyer)):
        # 计算不需要模型；同一退款截止比较两批支付订单，不猜变化原因。
        return compare_periods(engine, buyer, **body.model_dump())

    def execute(record, access):
        store = app.state.store
        buyer, task_id = record["buyer_id"], record["id"]
        def cancelled():
            return store.get(task_id, buyer)["cancel_requested"]
        try:
            if cancelled():
                store.update(task_id, buyer, status="cancelled")
                return
            store.update(task_id, buyer, status="running")
            kwargs = {"access_role": "merchant", "shop_id": access["shop_id"]} if access["role"] == "merchant" else {}
            mode = record.get("execution_mode", "single")
            previous = store.session_conditions(buyer, record["session_id"])
            history = store.session_history(buyer, record["session_id"])
            historical_evidence = store.session_historical_evidence(buyer, record["session_id"])
            state = (None if mode in {"period_analysis", "refund_investigation"} else
                     reconcile_conditions(record["message"], previous, datetime.now(timezone.utc), task_id))
            if state is None or any(state[key] for key in ("reset", "expired", "switched", "new_query")):
                history = []
                historical_evidence = []
            # 在外部请求前落盘；失败/重启也保留真实条件，不从模型解释重建。
            store.update(task_id, buyer, result={"condition_state": state})
            # 平台店铺绑定只来自已认证账号文件，客户端和模型不能提交这个字段。
            if state and state.get("kind") in {"shopify_order", "shopify_policy"}:
                kwargs["platform_context"] = access.get("platform_context")
            kwargs["condition_state"] = state
            kwargs["historical_evidence"] = historical_evidence
            if record.get("preference_snapshot") is not None:
                kwargs["preference_snapshot"] = record["preference_snapshot"]
            # 售后多角色先查事实再交政策；其余新业务仍使用各自单路，旧金额/政策并行保留。
            support_fallback = bool(state and state.get("kind") in {"support", "catalog", "general_policy", "shopify_order", "shopify_policy"}
                                    and mode != "single" and not (state.get("kind") == "support" and mode == "multi"))
            if support_fallback:
                runner = agent
            elif mode == "period_analysis":
                from period_analysis_runtime import run_period_analysis
                runner = period_analysis_agent or run_period_analysis
                kwargs["period_conditions"] = record.get("analysis_conditions")
            elif mode == "refund_investigation":
                from refund_investigation_runtime import run_refund_investigation
                runner = run_refund_investigation
                kwargs["period_conditions"] = record.get("analysis_conditions")
            elif mode == "snapshot_sql":
                from snapshot_sql_runtime import run_snapshot_sql
                runner = snapshot_sql_agent or run_snapshot_sql
            else:
                runner = {"single": agent, "multi": multi_agent,
                          "programmatic": programmatic_agent,
                          "batch_sequential": batch_sequential_agent}[mode]
            started = time.monotonic()
            result = runner(settings, engine, record["message"], access.get("buyer_id", buyer),
                           history, cancelled, **kwargs)
            if mode == "single" or support_fallback:
                result["orchestration"] = {"requested": mode, "effective": "single",
                    "reason": ("本次导购/店铺规则查询使用各自受限工具，不拆成旧金额/政策分支" if state.get("kind") in {"catalog", "general_policy", "shopify_order", "shopify_policy"} else "售后核对沿同一授权订单和政策执行，不拆成旧金额/政策分支") if support_fallback else None,
                    "wall_seconds": round(time.monotonic() - started, 6), "branches": []}
            result.setdefault("condition_state", state)
            with store.lock:
                status = "cancelled" if cancelled() else result["status"]
                if settings.memory_enabled and preferences is not None:
                    # 在同一任务锁内决定取消与保存先后；身份和版本都来自受理时的可信记录。
                    result = ChatMemoryService(preferences, store).after_run(
                        record, access, result, cancelled=status == "cancelled")
                store.update(task_id, buyer, status=status, answer=result.get("answer", ""), result=result)
        except Exception as error:
            # 保存类别与任务编号，不写可能包含凭据的原始异常全文。
            store.update(task_id, buyer, status="cancelled" if cancelled() else "failed", error=type(error).__name__,
                         answer="处理未完成，请查看任务错误类型；未将失败当作没有订单。")
        finally:
            slots.release()

    def submit_task(body, buyer, *, analysis_conditions=None):
        access, buyer = buyer, task_scope(buyer)
        if not body.message.strip():
            raise HTTPException(422, "问题不能为空")
        if not settings.model_enabled or not settings.model_api_key:
            raise HTTPException(503, "模型调用尚未启用；订单查询仍可使用")
        store = app.state.store
        with guard:
            existing = store.get(store.task_id(buyer, body.request_id), buyer)
            if existing is None and access.get("legacy_task_scope"):
                legacy = access["legacy_task_scope"]
                existing = store.get(store.task_id(legacy, body.request_id), legacy)
            if existing is not None:
                if (existing["message"] != body.message or existing["session_id"] != body.session_id
                        or existing.get("execution_mode", "single") != body.execution_mode
                        or existing.get("analysis_conditions") != analysis_conditions):
                    raise HTTPException(409, "同一请求编号对应了不同内容")
                return public_record(existing)
            previous_mode = store.session_execution_mode(buyer, body.session_id)
            if previous_mode is not None and previous_mode != body.execution_mode:
                raise HTTPException(409, "切换执行方案须新建会话，避免混用两种方案的历史")
            if store.active_in_session(buyer, body.session_id):
                raise HTTPException(409, "本会话仍有任务执行，请等待完成或取消后再提问")
            if store.today_count() >= settings.max_daily_tasks:
                raise HTTPException(429, "本机每日任务次数达到上限；这不是金额预算锁")
            if not slots.acquire(blocking=False):
                raise HTTPException(429, "当前服务忙，请稍后重试；未创建排队任务")
            try:
                # 在受理时冻结本次偏好，执行期间更新只作用于后续任务。
                snapshot = read_preferences(access) if settings.memory_enabled else None
                record, fresh = store.create(buyer, body.request_id, body.session_id, body.message,
                                             execution_mode=body.execution_mode)
                if analysis_conditions is not None:
                    record = store.update(record["id"], buyer, analysis_conditions=analysis_conditions)
                if snapshot is not None:
                    record = store.update(record["id"], buyer, preference_snapshot=snapshot)
                app.state.pool.submit(execute, record, access)
            except Exception:
                slots.release()
                raise
        return public_record(record)

    @app.post("/api/tasks", status_code=202)
    def ask(body: AskRequest, buyer=Depends(current_buyer)):
        return submit_task(body, buyer)

    @app.post("/api/analysis/tasks", status_code=202)
    def analyze_periods(body: PeriodAnalysisRequest, buyer=Depends(current_buyer)):
        from period_analysis_runtime import validate_period_conditions, analysis_message
        conditions = validate_period_conditions(body.model_dump(exclude={"session_id", "request_id"}))
        request = SimpleNamespace(message=analysis_message(conditions), session_id=body.session_id,
                                  request_id=body.request_id, execution_mode="period_analysis")
        return submit_task(request, buyer, analysis_conditions=conditions)

    @app.post("/api/analysis/investigate", status_code=202)
    def investigate_refunds(body: PeriodAnalysisRequest, buyer=Depends(current_buyer)):
        from period_analysis_runtime import validate_period_conditions
        conditions = validate_period_conditions(body.model_dump(exclude={"session_id", "request_id"}))
        message = "核查两期退款增加是否有已核实的尺码原因记录支持；只说明证据贡献和缺口，不推断因果。"
        request = SimpleNamespace(message=message, session_id=body.session_id,
                                  request_id=body.request_id, execution_mode="refund_investigation")
        return submit_task(request, buyer, analysis_conditions=conditions)

    @app.get("/api/tasks")
    def recent(buyer=Depends(current_buyer)):
        rows = app.state.store.recent(task_scope(buyer))
        if buyer.get("legacy_task_scope"):
            rows += app.state.store.recent(buyer["legacy_task_scope"])
        return [public_record(record) for record in sorted(rows, key=lambda r: r["created_at"], reverse=True)[:20]]

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str, buyer=Depends(current_buyer)):
        record = owned_task(task_id, buyer)
        if record is None:
            raise HTTPException(404, "任务不存在")
        return public_record(record)

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel(task_id: str, buyer=Depends(current_buyer)):
        with guard, app.state.store.lock:
            record = owned_task(task_id, buyer)
            if record is None:
                raise HTTPException(404, "任务不存在")
            if record["status"] in {"queued", "running", "cancelling"}:
                record = app.state.store.update(task_id, record["buyer_id"], status="cancelling", cancel_requested=True)
            return public_record(record)

    @app.post("/api/tasks/{task_id}/memory-decision")
    def memory_decision(task_id: str, body: MemoryDecision, buyer=Depends(current_buyer)):
        preference_account(buyer)
        if not settings.memory_enabled:
            raise HTTPException(503, "聊天记忆尚未启用")
        with app.state.store.lock:
            record = owned_task(task_id, buyer)
            if record is None:
                raise HTTPException(404, "任务不存在")
            try:
                updated = ChatMemoryService(preferences, app.state.store).decide(record, buyer, body.action)
            except MemoryDecisionConflict as error:
                raise HTTPException(409, str(error)) from None
            return public_record(updated)

    @app.get("/api/tasks/{task_id}/export")
    def export(task_id: str, buyer=Depends(current_buyer)):
        record = owned_task(task_id, buyer)
        if record is None:
            raise HTTPException(404, "任务不存在")
        # 导出包含事实/真实模型请求响应，供本机核查；凭据未进入记录。
        return JSONResponse(record, headers={"Content-Disposition": f'attachment; filename="task-{task_id}.json"'})

    return app
