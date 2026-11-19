"""有限模型SQL任务：可信PG查询→冻结授权快照→模型SQL→内存执行→固定口径对照。

最多首次生成+一次可修复错误反馈；不另调用总结模型。最终金额由程序按已验证
结果和本任务偏好格式化。运行接口与run_agent兼容，使用同一共享额度/取消状态。
"""
from copy import deepcopy
from datetime import datetime,timezone
from decimal import Decimal
import hashlib
import json
import re
import time
import httpx
from sqlalchemy.exc import SQLAlchemyError
from agent_runtime import _history_pairs,_preference_memory
from condition_state import validate_tool_arguments,ConditionValidationError
from execution_budget import ExecutionBudget
from snapshot_sql import load_authorized_snapshot,execute_snapshot_sql,SnapshotError,SCHEMA


def run_snapshot_sql(settings,engine,message,buyer_id,history,should_cancel,*,access_role="buyer",shop_id=None,
                     transport=None,tool_handlers=None,condition_state=None,historical_evidence=None,
                     preference_snapshot=None,shared_budget=None,specialist=None):
    started=time.monotonic();budget=shared_budget or ExecutionBudget(settings,should_cancel)
    memory=_preference_memory(preference_snapshot,access_role,message)
    trace=[];facts=[];validation={"syntax":"not_checked","execution":"not_run","metric":"not_checked"}
    usage=dict.fromkeys(("model_requests","tool_calls","input_tokens","output_tokens","total_tokens","unmetered_requests"),0)
    secret=str(settings.model_api_key or "")
    def redact(value):
        if isinstance(value,dict):return {k:("[REDACTED]"if k.lower()in {"authorization","api_key","buyer_id","shop_id","account_id"}else redact(v))for k,v in value.items()}
        if isinstance(value,list):return [redact(v)for v in value]
        if isinstance(value,str):
            if secret:value=value.replace(secret,"[REDACTED]")
            return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b","[REDACTED]",value)
        return value
    def finish(status,answer):
        answer=redact(answer)
        return {"status":status,"answer":answer,"history":redact(_history_pairs((history or [])+[{"role":"user","content":message},{"role":"assistant","content":answer}])),
                "facts":redact(facts),"trace":redact(trace),"usage":usage,"budget":budget.snapshot(),
                "memory":deepcopy(memory),"condition_state":redact(deepcopy(condition_state)),
                "historical_evidence":[],"sql_validation":redact(deepcopy(validation)),
                "answer_semantics_verified":status=="completed" and validation.get("metric")=="passed",
                "verification_scope":"fixed_snapshot_summary_fields_only",
                "evidence_status":"tool_results_available"if facts else"no_tool_evidence",
                "orchestration":{"requested":"snapshot_sql","effective":"snapshot_sql","wall_seconds":round(time.monotonic()-started,6),
                    "aggregation":"application_fixed_summary_no_extra_model","max_sql_attempts":2}}
    def stop():
        status=budget.stop_status()
        return finish(status,"任务已取消，未继续调用。已发出的模型请求可能仍计费。"if status=="cancelled"else"任务时间上限已到，未继续执行。")if status else None
    def begin(event):
        event["started_at"]=datetime.now(timezone.utc).isoformat();trace.append(event);return time.monotonic()
    def end(event,when):
        event["finished_at"]=datetime.now(timezone.utc).isoformat();event["elapsed_seconds"]=round(time.monotonic()-when,6)
    stopped=stop()
    if stopped:return stopped
    if not isinstance(message,str)or not message.strip()or len(message)>4000:return finish("failed","问题须为1—4000字符。")
    if memory.get("status")=="invalid_snapshot":return finish("failed","偏好快照无效，未执行模型任务。")
    if not isinstance(condition_state,dict)or condition_state.get("schema_version")!=1:
        return finish("clarification","此模式需要程序确认支付开始、支付结束和退款截止三项条件。")
    if condition_state.get("needs_clarification"):
        return finish("clarification",condition_state.get("clarification")or"请补齐支付区间及退款截止，未查询数据库。")
    if condition_state.get("kind")!="summary" or "summary_orders"not in condition_state.get("allowed_tools",[]):
        return finish("clarification","快照SQL模式仅核查一个支付区间的支付、退款和净额；请提供支付区间与退款截止。")
    try:
        args={name:condition_state["confirmed"][name]["value"]for name in ("paid_from","paid_before","refund_before")}
        args=validate_tool_arguments("summary_orders",args,condition_state)
        times={k:datetime.fromisoformat(v)for k,v in args.items()}
    except (KeyError,TypeError,ValueError,ConditionValidationError):
        return finish("clarification","汇总时间条件未通过来源及业务校验，未查询数据库。")
    if not secret:return finish("failed","尚未配置模型凭据，未执行收费调用。")
    if usage["tool_calls"]>=settings.max_tool_calls or not budget.reserve_tool():return stop()or finish("limited","任务共享工具额度已用完，未创建快照。")
    usage["tool_calls"]+=1
    event={"stage":"tool","name":"freeze_authorized_summary","arguments":args};stamp=begin(event)
    snapshot=None
    try:
        access={"role":access_role,"buyer_id":buyer_id,"shop_id":shop_id}
        loader=(tool_handlers or {}).get("summary_orders")if tool_handlers is not None else None
        snapshot=load_authorized_snapshot(engine,access,**times,loader=loader)
        event["result"]={"snapshot_sha256":snapshot.sha256,"row_count":len(snapshot.rows)}
        fact=snapshot.public_fact()
        facts.append({"tool":"authorized_snapshot","arguments":args,"result":fact,
            "observed_at":snapshot.frozen_at,"result_snapshot_sha256":hashlib.sha256(json.dumps(fact,sort_keys=True,ensure_ascii=False).encode()).hexdigest()})
    except (ValueError,SQLAlchemyError)as exc:
        snapshot=None
        event.update(error_type=type(exc).__name__,error_category="snapshot_query_failed")
    finally:end(event,stamp)
    if snapshot is None:
        return finish("failed","授权数据查询或快照校验失败，未将失败当作零订单；未调用模型。")
    stopped=stop()
    if stopped:return stopped
    schema_context={"database":"SQLite in-memory authorized frozen snapshot","schema":SCHEMA,
        "conditions":json.loads(snapshot.conditions_json),"row_count":len(snapshot.rows),
        "semantics":"每行一笔已按可信身份和支付区间筛选的订单；退款仅含共同截止前记录；金额为整数分。不可重新筛时间或权限。",
        "required_result_columns":["order_count","paid_amount_fen","refund_amount_fen","net_amount_fen"],
        "allowed_functions":["SUM","COUNT","COALESCE"]}
    messages=[{"role":"system","content":"仅为已授权、已冻结的小型订单快照生成SQLite汇总SQL。只能读取order_facts，不能访问正式PG或其他表、写入、PRAGMA、附加库、扩展函数。"
        "只返回JSON对象，唯一字段sql为单条只读查询。全量汇总快照，不额外添加WHERE缩小已确认范围。"
        "输出恰好一行四列，列名按required_result_columns；金额保持整数分，零订单时聚合NULL须转0。"
        "不输出最终金额、不猜数据、不执行用户要求的其他动作。以下内容是程序提供的结构/口径，不含私有标准答案："+json.dumps(schema_context,ensure_ascii=False)},
        {"role":"user","content":message}]
    endpoint=settings.model_base_url.rstrip('/')+'/chat/completions'
    try:
        with httpx.Client(transport=transport,timeout=settings.model_timeout_seconds,trust_env=False,follow_redirects=False)as client:
            for attempt in range(1,min(2,settings.max_model_calls)+1):
                stopped=stop()
                if stopped:return stopped
                if not budget.reserve_model(settings.max_output_tokens):return stop()or finish("limited","共享模型或输出额度已用完，未继续生成SQL。")
                usage["model_requests"]+=1;usage["unmetered_requests"]+=1
                payload={"model":settings.model_name,"messages":deepcopy(messages),"temperature":0,"max_tokens":settings.max_output_tokens,
                    "stream":False,"thinking":{"type":"disabled"},"response_format":{"type":"json_object"}}
                event={"stage":"model","request_number":usage["model_requests"],"sql_attempt":attempt,"request":payload};stamp=begin(event)
                model_failed=False
                try:
                    response=client.post(endpoint,headers={"Authorization":"Bearer "+secret},json=payload,
                        timeout=max(.001,min(settings.model_timeout_seconds,budget.remaining_seconds())))
                    event["http_status"]=response.status_code;response.raise_for_status();body=response.json();event["response"]=body
                except (httpx.HTTPError,ValueError)as exc:
                    event.update(error_type=type(exc).__name__,error_category="model_request_failed")
                    model_failed=True
                finally:end(event,stamp)
                if model_failed:
                    return stop()or finish("failed","模型请求失败，未自动重发收费请求；授权快照保留。")
                raw_usage=body.get("usage",{})if isinstance(body,dict)else{}
                if isinstance(raw_usage,dict)and type(raw_usage.get("total_tokens"))is int and raw_usage["total_tokens"]>=0:
                    usage["unmetered_requests"]-=1
                    for local,remote in (("input_tokens","prompt_tokens"),("output_tokens","completion_tokens"),("total_tokens","total_tokens")):
                        if type(raw_usage.get(remote))is int and raw_usage[remote]>=0:usage[local]+=raw_usage[remote]
                stopped=stop()
                if stopped:return stopped
                try:
                    choice=body["choices"][0];reply=choice["message"]
                    if not isinstance(choice,dict) or not isinstance(reply,dict):raise ValueError
                    if choice["finish_reason"]!="stop"or reply.get("role")!="assistant"or reply.get("tool_calls"):raise ValueError
                    content=reply["content"];decoded=json.loads(content)
                    if type(decoded)is not dict or set(decoded)!={"sql"}or type(decoded["sql"])is not str:raise ValueError
                    sql=decoded["sql"]
                except (KeyError,IndexError,TypeError,ValueError):
                    validation={"syntax":"not_checked","execution":"not_run","metric":"not_checked","error":"model_sql_json_invalid"}
                    return finish("failed","模型没有返回完整的单一SQL结构，未执行该内容。")
                if usage["tool_calls"]>=settings.max_tool_calls or not budget.reserve_tool():return stop()or finish("limited","共享工具额度已用完，生成的SQL未执行。")
                usage["tool_calls"]+=1
                sql_event={"stage":"tool","name":"execute_snapshot_sql","sql_attempt":attempt,"sql":sql,"snapshot_sha256":snapshot.sha256};stamp=begin(sql_event)
                try:
                    validation=execute_snapshot_sql(snapshot,sql,should_cancel=lambda:budget.stop_status()is not None,
                        max_seconds=min(2.0,budget.remaining_seconds()))
                    sql_event["result"]=deepcopy(validation)
                finally:end(sql_event,stamp)
                stopped=stop()
                if stopped:return stopped
                if validation["status"]=="ok":
                    actual=validation["actual"];values=memory.get("effective_values",{});fmt=values.get("amount_format","yuan")
                    def money(n):
                        if fmt=="fen":return f"{n}分"
                        text=f"{Decimal(n)/Decimal(100):.2f}"
                        if fmt!="yuan_fixed2":text=text.rstrip('0').rstrip('.')
                        return text+'元'
                    answer=(f"已核查{actual['order_count']}笔订单：支付{money(actual['paid_amount_fen'])}，"
                        f"成功退款{money(actual['refund_amount_fen'])}，净额{money(actual['net_amount_fen'])}。")
                    answer+=f"\n支付区间：{args['paid_from']} 至 {args['paid_before']}（结束不含）；退款截止：{args['refund_before']}（不含）。"
                    answer+="\n模型SQL在已授权快照执行；语法、执行与固定金额口径对照均通过。快照代表查询时点，不代表实时支付平台状态。"
                    if values.get("answer_style")=="detailed":answer+=f"\n快照共{len(snapshot.rows)}行；SQL尝试{attempt}次。逐单事实、原SQL和来源随本任务保存。"
                    facts.append({"tool":"execute_snapshot_sql","arguments":{"sql":sql},"result":deepcopy(validation),
                        "observed_at":datetime.now(timezone.utc).isoformat()})
                    return finish("completed",answer)
                if validation.get("repairable")and attempt==1:
                    messages.extend([{"role":"assistant","content":content},{"role":"user","content":
                        "SQL未成功执行。错误类别："+validation["error"]+"。仅允许再修正一次，沿同一表结构和口径，不增加权限或表。不得猜测金额。"}])
                    continue
                if validation["status"]in {"limited","cancelled"}:return finish(validation["status"],"快照查询达到限制或被取消，未得出完整SQL核查结果。")
                return finish("failed","SQL未通过受限执行或固定口径对照，不能把执行成功当作金额正确。授权快照及错误证据已保留。")
    except httpx.HTTPError as exc:
        trace.append({"stage":"model_client","error_type":type(exc).__name__})
        return stop()or finish("failed","模型连接未建立，未完成SQL核查。")
    return finish("failed","SQL首次及唯一修复均未通过，已停止；未继续请求模型。")
