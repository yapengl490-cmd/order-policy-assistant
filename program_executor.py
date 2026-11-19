"""可信父进程管理独立临时容器，只将白名单工具请求交给调用方的鉴权代理。

模型程序不接触宿主凭据、Docker socket或真实业务库。容器只挂本文件同目录的
固定解释器；源文本通过标准输入传入，绝不拼接到shell命令。共享业务预算由
tool_handler执行，容器层另加资源、消息和调用次数上限作为纵深限制。
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import select
import selectors
import subprocess
import threading
import time
import uuid

from program_worker import MAX_JSON_BYTES, ProgramError, bounded_json, validate_source, validate_tool_request

IMAGE = "python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
DOCKER_PATHS = (Path.home() / ".docker/bin/docker", Path("/usr/local/bin/docker"),
                Path("/Applications/Docker.app/Contents/Resources/bin/docker"))
MAX_WALL_SECONDS = 25
MAX_OUTPUT_BYTES = 262144


def _docker_binary(value=None):
    path = Path(value) if value else next((p for p in DOCKER_PATHS if p.is_file()), DOCKER_PATHS[0])
    if path not in DOCKER_PATHS or not path.is_file():
        raise ProgramError("docker_unavailable", "找不到已允许的Docker程序")
    return str(path)



def program_execution_capability(docker_binary=None, *, timeout_seconds=3):
    """只读预检宿主执行能力；不启动Docker、不拉镜像，不授予普通应用容器管理权限。

    这只是发模型请求前的能力快照；通过后引擎仍可能退出，实际执行继续处理失败。
    """
    def outcome(available, reason, message):
        return {"available": available, "reason": reason, "message": message, "image": IMAGE}
    if os.environ.get("DATUS_DEPLOYMENT", "local") != "local":
        return outcome(False, "program_host_required", "当前普通应用容器没有程序执行权限；请使用已配置的宿主执行入口，本次未调用模型。")
    if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 5:
        return outcome(False, "invalid_probe_limit", "执行能力检查配置不合法，本次未调用模型。")
    try:
        binary = _docker_binary(docker_binary)
        engine = subprocess.run([binary, "version", "--format", "{{.Server.Version}}"],
                                capture_output=True, text=True, timeout=timeout_seconds)
        if engine.returncode != 0 or not engine.stdout.strip():
            return outcome(False, "docker_engine_unavailable", "Docker引擎尚不可用；请先启动已有Docker，本次未调用模型。")
        image = subprocess.run([binary, "image", "inspect", IMAGE, "--format", "{{.Os}}/{{.Architecture}}"],
                               capture_output=True, text=True, timeout=timeout_seconds)
        if image.returncode != 0 or image.stdout.strip() not in {"linux/arm64", "linux/amd64"}:
            return outcome(False, "program_image_unavailable", "固定程序执行镜像尚未准备好；请按部署说明准备，本次未调用模型，也未自动下载。")
    except ProgramError:
        return outcome(False, "docker_binary_unavailable", "找不到已允许的Docker程序；本次未调用模型。")
    except (OSError, subprocess.SubprocessError):
        return outcome(False, "docker_probe_failed", "Docker执行能力检查超时或失败；本次未调用模型。")
    return outcome(True, "available", "宿主Docker与固定执行镜像可用；实际执行仍需通过权限和预算检查。")


def container_command(binary, name):
    worker = Path(__file__).resolve().with_name("program_worker.py")
    if not worker.is_file() or "," in str(worker):
        raise ProgramError("worker_unavailable", "可信解释器文件不可挂载")
    return [binary, "run", "--rm", "--init", "--name", name,
            "--label", "datus-core.program-worker=true", "--pull", "never", "--network", "none",
            "--read-only", "--user", "65534:65534", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--cpus", "0.5",
            "--memory", "96m", "--memory-swap", "96m", "--pids-limit", "32",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=8m,mode=1777", "--workdir", "/tmp",
            "--log-driver", "none", "--mount", f"type=bind,src={worker},dst=/opt/program_worker.py,readonly",
            "-i", IMAGE, "python", "-I", "-B", "-S", "/opt/program_worker.py"]


def execute_program(source, tool_handler, *, cancelled=lambda: False, remaining_seconds=lambda: MAX_WALL_SECONDS,
                    docker_binary=None, max_seconds=MAX_WALL_SECONDS, max_tool_calls=6):
    """同步返回有界执行结果；调用方须让tool_handler执行原身份、条件和共享预算校验。

    父进程拒绝不在白名单的工具/参数。取消可停止容器与后续请求，不能撤销已经
    发往工具服务的只读请求；迟到结果不再交给程序，execution中会明确标识。
    """
    started = time.monotonic()
    metadata = {"isolation": "docker_restricted_ast", "image": IMAGE, "tool_calls": 0,
                "steps": 0, "container_started": False, "container_cleanup": "not_started",
                "in_flight_tool_may_continue": False}
    outcome = {"status": "failed", "error": "program_not_started"}
    process = selector = None
    name = "datus-core-program-" + uuid.uuid4().hex
    binary = None
    wall_limit = min(float(max_seconds), MAX_WALL_SECONDS)
    if not math.isfinite(wall_limit) or wall_limit <= 0 or type(max_tool_calls) is not int or not 0 <= max_tool_calls <= 6:
        return {"status": "rejected", "error": "invalid_limits", "execution": metadata}

    def stop_reason():
        try:
            if cancelled():
                return "cancelled"
            if time.monotonic() - started >= wall_limit or remaining_seconds() <= 0:
                return "limited"
        except Exception:
            return "cancelled"  # 取消/预算查询失败时不继续执行。
        return None

    def send(value):
        raw = (bounded_json(value) + "\n").encode()
        offset = 0
        while offset < len(raw):
            if stop_reason():
                raise ProgramError("program_stopped", "写入回执前任务已停止")
            if not select.select([], [process.stdin], [], .05)[1]:
                continue
            try:
                written = os.write(process.stdin.fileno(), raw[offset:])
            except BlockingIOError:
                continue
            if written <= 0:
                raise OSError("worker input closed")
            offset += written

    try:
        validate_source(source)
        reason = stop_reason()
        if reason:
            return {"status": reason, "error": "stopped_before_start", "execution": metadata}
        binary = _docker_binary(docker_binary)
        process = subprocess.Popen(container_command(binary, name), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        metadata["container_started"] = True
        os.set_blocking(process.stdin.fileno(), False)
        selector = selectors.DefaultSelector()
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        send({"source": source})
        buffer = b""
        total_output = lines = 0
        expected_id = 1
        finished = False
        while not finished:
            reason = stop_reason()
            if reason:
                outcome = {"status": reason, "error": "program_stopped"}
                break
            events = selector.select(0.05)
            if not events and process.poll() is not None:
                outcome = {"status": "failed", "error": "container_exit_without_result"}
                break
            for key, _ in events:
                raw = os.read(key.fileobj.fileno(), 8192)
                if not raw:
                    selector.unregister(key.fileobj)
                    continue
                total_output += len(raw)
                if total_output > MAX_OUTPUT_BYTES:
                    raise ProgramError("output_limit", "容器输出总量超限")
                if key.fileobj is process.stderr:
                    continue  # 可能含本机路径，不直接展示Docker原始错误。
                buffer += raw
                if len(buffer.split(b"\n", 1)[0]) > MAX_JSON_BYTES:
                    raise ProgramError("output_limit", "容器单条消息超限")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    lines += 1
                    if lines > max_tool_calls + 1:
                        raise ProgramError("protocol_output", "容器消息次数超限")
                    try:
                        message = json.loads(line)
                    except (ValueError, UnicodeError):
                        raise ProgramError("protocol_output", "容器返回非JSON消息") from None
                    bounded_json(message)
                    if type(message) is not dict:
                        raise ProgramError("protocol_output", "容器消息类型错误")
                    if message.get("type") == "result":
                        if message.get("status") not in {"ok", "failed"}:
                            raise ProgramError("protocol_output", "容器结果状态无效")
                        metadata["steps"] = message.get("steps", 0)
                        outcome = {key: message[key] for key in ("status", "result", "error") if key in message}
                        finished = True
                        break
                    if (set(message) != {"type", "id", "name", "arguments"}
                            or message["type"] != "tool" or type(message["id"]) is not int
                            or message["id"] != expected_id):
                        raise ProgramError("protocol_output", "工具消息次序或结构错误")
                    validate_tool_request(message["name"], message["arguments"])
                    if metadata["tool_calls"] >= max_tool_calls:
                        raise ProgramError("tool_limit", "程序工具次数达到上限")
                    reason = stop_reason()
                    if reason:
                        outcome = {"status": reason, "error": "stopped_before_tool"}
                        finished = True
                        break
                    metadata["tool_calls"] += 1
                    expected_id += 1
                    completed = queue.Queue(maxsize=1)

                    def call_tool(name=message["name"], arguments=message["arguments"], response_queue=completed):
                        try:
                            result = tool_handler(name, arguments)
                            bounded_json(result, 60000)
                            response_queue.put((True, result))
                        except Exception:
                            response_queue.put((False, None))

                    thread = threading.Thread(target=call_tool, daemon=True, name="bounded-program-tool")
                    thread.start()
                    while True:
                        reason = stop_reason()
                        if reason:
                            metadata["in_flight_tool_may_continue"] = thread.is_alive()
                            outcome = {"status": reason, "error": "stopped_waiting_for_tool"}
                            finished = True
                            break
                        try:
                            success, result = completed.get(timeout=0.05)
                            break
                        except queue.Empty:
                            continue
                    if finished:
                        break
                    send({"type": "tool_result", "id": message["id"], **({"result": result} if success else {"error": "tool_failed"})})
                if finished:
                    break
    except ProgramError as exc:
        outcome = {"status": stop_reason() or ("rejected" if process is None else "failed"), "error": exc.code}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        outcome = {"status": "failed", "error": "container_unavailable_or_protocol_closed"}
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            # 名字由本次调用生成，只回收自己的容器，绝不清理其他项目或数据卷。
            try:
                removed = subprocess.run([binary, "rm", "-f", name], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, timeout=5)
                if removed.returncode == 0:
                    metadata["container_cleanup"] = "removed"
                else:
                    inspect = subprocess.run([binary, "ps", "-aq", "--filter", "name=^/" + name + "$"],
                                             capture_output=True, timeout=5)
                    metadata["container_cleanup"] = "absent" if inspect.returncode == 0 and not inspect.stdout.strip() else "unconfirmed"
            except (OSError, subprocess.SubprocessError):
                metadata["container_cleanup"] = "unconfirmed"
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            if metadata["container_cleanup"] == "unconfirmed":
                outcome = {"status": "failed", "error": "container_cleanup_unconfirmed"}
        metadata["elapsed_seconds"] = round(time.monotonic() - started, 6)
    return {**outcome, "execution": metadata}
