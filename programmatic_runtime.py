"""模型生成有限程序→隔离执行→按真实工具事实解释；同任务共享次数和时间预算。

首版只做授权范围最近1至5笔订单的逐笔核查；模型不能自行选择身份或扩展范围。
程序输出只是模型编排产物，业务结论仍以父进程保存的工具事实为准。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
import time
from zoneinfo import ZoneInfo

import httpx

from agent_runtime import SCHEMAS, _default_handlers, _owned_result, _tools_for_role
from runtime_context import history_pairs, preference_memory
from execution_budget import ExecutionBudget
from program_executor import execute_program, program_execution_capability
from program_worker import ProgramError, bounded_json, validate_source


def programmatic_contract(message, state):
    """复用原条件来源；批量编号只能来自本次授权列表，不由模型自由填写。"""
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        return None
    counts = re.findall(r"(?:最近|前)\s*([1-5])\s*(?:笔|个|条)", message)
    all_counts = re.findall(r"(?:最近|前)\s*(\d+)\s*(?:笔|个|条)", message)
    cutoff = state.get("confirmed", {}).get("refund_before")
    if (state.get("kind") != "list" or not state.get("ready") or state.get("issues")
            or len(counts) != 1 or counts != all_counts
            or not re.search(r"逐笔|逐单|逐一|每笔|分别核查", message)
            or "search_policies" in state.get("allowed_tools", [])
            or not isinstance(cutoff, dict) or not cutoff.get("source", {}).get("quote")):
        return None
    try:
        value = datetime.fromisoformat(cutoff["value"])
        if value.tzinfo is None:
            return None
    except (KeyError, ValueError, TypeError):
        return None
    return {"limit": int(counts[0]), "refund_before": cutoff["value"],
            "source": deepcopy(cutoff["source"]), "scope": "authorized_recent_orders_only"}


def run_programmatic_agent(settings, engine, message, buyer_id, history, should_cancel, *,
                          access_role="buyer", shop_id=None, transport=None, tool_handlers=None,
                          condition_state=None, historical_evidence=None, preference_snapshot=None,
                          program_executor=None, _sequential=False):
    """入口身份/快照由后端提供；替身transport/handlers/executor只用于离线验收。"""
    started = time.monotonic()
    mode = "batch_sequential" if _sequential else "programmatic"
    budget = ExecutionBudget(settings, should_cancel)
    usage = {"model_requests": 0, "tool_calls": 0, "input_tokens": 0,
             "output_tokens": 0, "total_tokens": 0, "unmetered_requests": 0}
    trace, facts, executions = [], [], []
    capability = None
    memory = preference_memory(preference_snapshot, access_role, message)
    contract = programmatic_contract(message, condition_state)
    secret = str(settings.model_api_key or "")

    def redact(value):
        if isinstance(value, dict):
            return {key: "[REDACTED]" if key.lower() in {"authorization", "api_key", "model_api_key", "buyer_id", "shop_id", "account_id"}
                    else redact(item) for key, item in value.items()}
        if isinstance(value, list): return [redact(item) for item in value]
        if isinstance(value, str): return value.replace(secret, "[REDACTED]") if secret else value
        return value

    def finish(status, answer):
        answer = redact(answer)
        return {"status": status, "answer": answer, "facts": redact(facts), "trace": redact(trace),
                "usage": deepcopy(usage), "budget": budget.snapshot(), "memory": memory,
                "condition_state": deepcopy(condition_state), "programmatic_contract": contract,
                "program_executions": redact(executions), "program_capability": capability,
                "answer_semantics_verified": False,
                "evidence_status": "tool_results_available" if facts else "no_tool_evidence",
                "historical_evidence": [],
                "history": history_pairs((history or []) + [{"role": "user", "content": message}, {"role": "assistant", "content": answer}]),
                "orchestration": {"requested": mode, "effective": mode,
                                  "wall_seconds": round(time.monotonic() - started, 6), "branches": []}}

    if (access_role not in {"buyer", "merchant"} or
            not (shop_id if access_role == "merchant" else buyer_id)):
        return finish("failed", "缺少可信授权范围，未执行程序。")
    if preference_snapshot is not None and memory["status"] != "loaded":
        return finish("failed", "偏好快照不属于当前角色，未执行程序。")
    if contract is None:
        return finish("clarification", "程序化核查目前需要：列出本人或本店最近1至5笔订单并逐笔核查，以及明确退款截止时刻。请补齐数量和截止；不自动猜测或混入政策查询。")
    if not settings.model_api_key:
        return finish("failed", "模型配置不可用，未执行程序。")
    if budget.stop_status():
        return finish(budget.stop_status(), "任务已停止，未调用模型或执行程序。")
    if not _sequential and program_executor is None:
        capability = program_execution_capability()
        if not capability["available"]:
            return finish("failed", capability["message"])
    handlers = tool_handlers if tool_handlers is not None else _default_handlers(
        engine, buyer_id, access_role=access_role, shop_id=shop_id)
    client = None
    if getattr(settings, "mcp_enabled", False) and tool_handlers is None:
        from mcp_order_client import OrderProtocolClient
        client = OrderProtocolClient("merchant:" + shop_id if access_role == "merchant" else "buyer:" + buyer_id)
        client.deadline_seconds = budget.remaining_seconds
        handlers["check_order"] = client.check_order
    listed = None
    checked = set()
    seen_call_ids = set()
    executor = program_executor or execute_program

    def begin(stage, **fields):
        event = {"stage": stage, "started_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), **fields}
        trace.append(event)
        return event, time.monotonic()

    def end(event, start):
        event.update(finished_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), elapsed_seconds=round(time.monotonic() - start, 6))

    def broker(name, arguments):
        nonlocal listed
        if budget.stop_status() or not budget.reserve_tool():
            raise ProgramError("shared_budget", "整任务工具或时间额度已到")
        usage["tool_calls"] += 1
        event, step_start = begin("tool", name=name, raw_arguments=deepcopy(arguments))
        try:
            if name not in {"list_orders", "check_order"}:
                raise ProgramError("tool_not_allowed", "工具不在本次范围")
            args = SCHEMAS[name][0].model_validate(arguments).model_dump()
            if name == "list_orders":
                if listed is not None or args.get("limit") != contract["limit"]:
                    raise ProgramError("scope_mismatch", "仅允许按用户指定数量列出一次")
            else:
                if listed is None or args["order_id"] not in listed or args["order_id"] in checked:
                    raise ProgramError("scope_mismatch", "仅核查本次授权列表中的订单且不重复")
                if args.get("refund_before") is not None and datetime.fromisoformat(args["refund_before"]) != datetime.fromisoformat(contract["refund_before"]):
                    raise ProgramError("condition_mismatch", "退款截止与用户来源不符")
                args["refund_before"] = contract["refund_before"]
            event["arguments"] = deepcopy(args)
            result = handlers[name](**args)
            result = _owned_result(result, buyer_id, access_role=access_role, shop_id=shop_id)
            bounded_json(result, 60000)
            if not isinstance(result, dict) or not isinstance(result.get("status"), str):
                raise ProgramError("invalid_tool_result", "工具结果缺少状态")
            if name == "list_orders":
                orders = result.get("orders")
                if (result["status"] != "ok" or not isinstance(orders, list) or len(orders) > contract["limit"]
                        or any(not isinstance(row, dict) or not isinstance(row.get("order_id"), str) for row in orders)):
                    raise ProgramError("invalid_tool_result", "订单列表未取得或形状无效")
                ids = [row["order_id"] for row in orders]
                if len(set(ids)) != len(ids):
                    raise ProgramError("invalid_tool_result", "订单列表重复")
                listed = set(ids)
            else:
                checked.add(args["order_id"])
            event["result"] = redact(result)
            if name == "check_order" and client is not None:
                event["protocol"] = deepcopy(client.last_trace)
            facts.append({"tool": name, "arguments": deepcopy(args), "result": redact(result)})
            return redact(result)
        except Exception as exc:
            event.update(error_type=type(exc).__name__, error_category="program_tool", error_code=getattr(exc, "code", "tool_execution_failed"))
            raise
        finally:
            end(event, step_start)

    def model(messages, http, *, tools=None):
        if budget.stop_status() or not budget.reserve_model(settings.max_output_tokens):
            raise ProgramError("shared_budget", "整任务模型或时间额度已到")
        payload = {"model": settings.model_name, "messages": messages, "temperature": 0,
                   "max_tokens": settings.max_output_tokens, "stream": False,
                   "thinking": {"type": "disabled"}, "response_format": {"type": "json_object"}}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 60000:
            raise ProgramError("context_limit", "上下文超限")
        event, step_start = begin("model", request_number=usage["model_requests"] + 1, request=deepcopy(payload))
        usage["model_requests"] += 1
        usage["unmetered_requests"] += 1
        try:
            response = http.post(settings.model_base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + secret}, json=payload,
                timeout=max(.001, min(settings.model_timeout_seconds, budget.remaining_seconds())))
            event["http_status"] = response.status_code
            response.raise_for_status()
            body = response.json()
            event["response"] = body
            measured = body.get("usage", {})
            if isinstance(measured, dict) and all(type(measured.get(key)) is int and measured[key] >= 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens")):
                usage["unmetered_requests"] -= 1
                for a, b in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"), ("total_tokens", "total_tokens")):
                    usage[a] += measured[b]
            choice = body["choices"][0]
            if tools is not None and choice.get("finish_reason") == "tool_calls":
                calls = choice["message"].get("tool_calls")
                if not isinstance(calls, list) or not 1 <= len(calls) <= 6:
                    raise ProgramError("model_format", "工具请求列表无效")
                return {"_tool_calls": calls, "_assistant": choice["message"]}
            if choice.get("finish_reason") != "stop" or choice["message"].get("tool_calls"):
                raise ProgramError("model_format", "模型返回未完成或走错调用形式")
            return json.loads(choice["message"]["content"])
        except Exception as exc:
            event.update(error_type=type(exc).__name__, error_category="model_or_json")
            raise
        finally:
            end(event, step_start)

    try:
        specification = (
            '输出JSON对象且只有source字段，值为受限Python程序。不能输出Markdown。'
            '允许变量赋值、整数/字符串/布尔/list/dict、for遍历列表、if比较、字典索引、列表变量.append、整数加减。'
            '禁止import、函数定义、while、推导式、任何其他属性或函数。最后给result赋值。'
            '仅有list_orders(limit=整数)和check_order(order_id=字符串,refund_before=可选ISO时间)两个工具，必须命名参数。'
            # 生成程序之前给出实际返回层级；工具名不代表其返回值就是列表。
            'list_orders正常返回的是dict对象，不是可直接for遍历的列表。返回结构为：'
            '{"status":"ok","orders":[{"order_id":字符串,"paid_at":ISO时间字符串,"paid_amount_fen":整数分}],'
            '"has_more":布尔值,"limit":整数,"provenance":来源对象,"currency":"CNY","money_unit":"fen"}。'
            '若把该返回对象保存为page，列表访问路径是page["orders"]；列表内每项是订单dict，编号访问路径是order["order_id"]。'
            'orders可能为空；has_more只表示授权范围还有更多订单，不允许因此额外翻页或扩大本次limit。'
            'check_order返回一笔核查结果dict；status为ok时包括order_id、paid_at、paid_amount_fen、refund_amount_fen、'
            'net_amount_fen、refund_count、refunds列表、refund_before、currency、money_unit及provenance等字段。'
            'status不是ok时不保证包含金额或退款列表，须原样保留状态和信息，不能补零。'
            '身份字段由应用检查并移除，不是程序可读取或传入的参数；工具执行异常会停止程序，不能自行重试。'
            '先按合同指定limit列出一次，仅遍历返回对象中的orders列表逐笔check_order，并把每笔完整返回dict放到result列表；不能猜编号或跳过订单。'
            '所有工具结果都有status，不得把not_found或失败当零金额。禁止写入或自报身份。'
            '本次程序合同：' + json.dumps(contract, ensure_ascii=False))
        generation = [{"role": "system", "content": specification}, {"role": "user", "content": message}]
        with httpx.Client(transport=transport, trust_env=False, follow_redirects=False) as http:
            if _sequential:
                # 与程序路径共用合同和broker；工具可在同一响应中出现多个，不人为加轮次。
                messages = [{"role": "system", "content":
                    '按可信合同核查当前授权范围最近订单。先list_orders一次，再核查列表里的每个订单；不能猜编号或身份。'
                    '退款截止由程序注入合同值，不能改成当前时间。工具失败/无匹配不视为零金额。'
                    '需要时调用工具；最后仅输出JSON对象{"answer":"中文回答"}，逐笔说明真实金额和截止，缺项说明未完成。'
                    '合同：' + json.dumps(contract, ensure_ascii=False) + '；展示偏好：' + json.dumps(memory["effective_values"], ensure_ascii=False)},
                    {"role": "user", "content": message}]
                while usage["model_requests"] < settings.max_model_calls:
                    allowed = {"list_orders"} if listed is None else ({"check_order"} if checked != listed else set())
                    tools = [item for item in _tools_for_role(access_role) if item["function"]["name"] in allowed]
                    for item in tools:
                        item["function"]["description"] += "本批量任务的退款截止使用可信合同，由程序注入；不能使用当前时间或改写数量。"
                    output = model(messages, http, tools=tools)
                    if budget.stop_status():
                        return finish(budget.stop_status(), "任务已停止，未继续执行工具或宣布完整完成。")
                    if "_tool_calls" not in output:
                        if set(output) != {"answer"} or not isinstance(output["answer"], str) or not output["answer"].strip():
                            raise ProgramError("model_format", "最终解释格式无效")
                        complete = (listed is not None and checked == listed and all(f["result"]["status"] == "ok" for f in facts))
                        answer = output["answer"]
                        if not complete:
                            answer = "【程序状态】本次仅部分完成，未取得完整核查结果。\n" + answer
                        return finish("completed" if complete else "partial" if facts else "failed", answer)
                    # 先核整组编号，再执行任一工具；同组/跨轮重复都不能靠回执混过去。
                    call_ids = []
                    for call in output["_tool_calls"]:
                        if (not isinstance(call, dict) or call.get("type") != "function"
                                or not isinstance(call.get("id"), str) or not call["id"].strip()
                                or len(call["id"]) > 256 or call["id"] in seen_call_ids or call["id"] in call_ids):
                            raise ProgramError("model_format", "工具调用编号为空、重复或无效")
                        call_ids.append(call["id"])
                    seen_call_ids.update(call_ids)
                    messages.append(output["_assistant"])
                    for call in output["_tool_calls"]:
                        try:
                            if not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("id"), str):
                                raise ProgramError("model_format", "工具请求结构无效")
                            name = call["function"]["name"]
                            arguments = json.loads(call["function"]["arguments"])
                            returned = broker(name, arguments)
                        except Exception as exc:
                            returned = {"status": "error", "error": getattr(exc, "code", "invalid_or_failed_tool"),
                                        "message": "工具未完成，不能当零金额或无订单；参数须满足原合同。"}
                        if not isinstance(call, dict) or not isinstance(call.get("id"), str):
                            raise ProgramError("model_format", "无法关联工具结果")
                        messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(redact(returned), ensure_ascii=False)})
                        if budget.stop_status():
                            return finish(budget.stop_status(), "任务已停止，已取得工具事实保留。")
                return finish("limited", "达到整任务模型次数限制，未形成完整答复；已取得事实保留。")
            source = None
            # 仅修一次生成格式/语法；已经执行的工具不重跑，避免重复费用和混用新旧数据。
            for attempt in range(2):
                try:
                    generated = model(generation, http)
                    if type(generated) is not dict or set(generated) != {"source"}:
                        raise ProgramError("model_format", "只允许source字段")
                    validate_source(generated["source"])
                    source = generated["source"]
                    break
                except (ProgramError, ValueError, TypeError, KeyError) as exc:
                    if attempt or budget.stop_status():
                        raise
                    generation.append({"role": "user", "content": "上次程序格式/语法被程序拒绝。只修正允许的语法；错误类别：" + getattr(exc, "code", "model_format")})
            if budget.stop_status():
                return finish(budget.stop_status(), "任务已停止，未执行生成的程序。")
            execution = executor(source, broker, cancelled=should_cancel, remaining_seconds=budget.remaining_seconds,
                                 max_tool_calls=settings.max_tool_calls)
            executions.append(execution)
            trace.append({"stage": "program", "source": source, "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                          "execution": deepcopy(execution)})
            if execution["status"] in {"cancelled", "limited"} or budget.stop_status():
                return finish(budget.stop_status() or execution["status"], "程序已停止；已取得的工具事实保留，未完成部分不能推断。")
            complete = (execution["status"] == "ok" and listed is not None and checked == listed
                        and all(f["result"]["status"] == "ok" for f in facts))
            if not facts:
                return finish("failed", "程序未取得可核对的业务事实；请查看执行错误，未自动重跑。")
            summary_input = {"requested": contract, "facts": redact(facts), "all_requested_orders_checked": complete,
                             "program_status": execution["status"], "preferences": memory["effective_values"]}
            summary = model([
                {"role": "system", "content": '仅输出JSON对象{"answer":"中文回答"}。仅依据下面真实工具facts说明每笔支付、退款、净额和截止；按偏好展示金额。'
                 '完整性由all_requested_orders_checked决定；false必须明确部分完成，不以缺失为零。不推测退款到账，不输出未查到的订单信息。'},
                {"role": "user", "content": message + "\n程序核验事实：" + json.dumps(summary_input, ensure_ascii=False)}], http)
            if budget.stop_status():
                return finish(budget.stop_status(), "任务已停止；已取得的工具事实保留。")
            if type(summary) is not dict or set(summary) != {"answer"} or not isinstance(summary["answer"], str) or not summary["answer"].strip():
                raise ProgramError("model_format", "解释输出格式无效")
            answer = summary["answer"]
            if not complete:
                answer = "【程序状态】本次仅部分完成，未取得完整核查结果。\n" + answer
            return finish("completed" if complete else "partial", answer)
    except Exception as exc:
        trace.append({"stage": "program_runtime_error", "error_type": type(exc).__name__,
                      "error_code": getattr(exc, "code", "execution_failed")})
        return finish(budget.stop_status() or ("partial" if facts else "failed"),
                      "本轮程序或模型执行未完成；已取得的事实保留，未自动重新查询或补造结果。")


def run_batch_sequential_agent(settings, engine, message, buyer_id, history, should_cancel, **kwargs):
    """相同业务合同/身份/预算的普通工具调用基线，不强制单次只调用一个工具。"""
    return run_programmatic_agent(settings, engine, message, buyer_id, history, should_cancel,
                                  _sequential=True, **kwargs)
