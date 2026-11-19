"""一个任务共用一份额度；分支失败不重置计数，也不另领一份预算。

调用次数与输出 token（模型输出计量单位）的预留上限由程序执行。
这不是人民币预算锁；输入长度、供应商计费及已发请求的费用需另行核对。
截止时间阻止后续调用，并收紧等待时间；不能撤销已经发往外部的请求。
"""
from __future__ import annotations

from threading import Lock
import time


class ExecutionBudget:
    def __init__(self, settings, should_cancel, clock=time.monotonic):
        self._clock, self._should_cancel = clock, should_cancel
        self._started = clock()
        self._duration = float(getattr(settings, "task_timeout_seconds", 90))
        if self._duration <= 0:
            raise ValueError("task timeout must be positive")
        self._limits = {
            "model_requests": int(settings.max_model_calls),
            "tool_calls": int(settings.max_tool_calls),
            "reserved_output_tokens": int(settings.max_model_calls) * int(settings.max_output_tokens),
        }
        if any(value < 0 for value in self._limits.values()):
            raise ValueError("budget limits must be nonnegative")
        self._used = {key: 0 for key in self._limits}
        self._lock = Lock()
        self._last_denial = None
        self._cancelled = False

    def remaining_seconds(self):
        return max(0.0, self._duration - (self._clock() - self._started))

    def stop_status(self):
        # 取消是单向状态；调用方检查失败时也按取消处理，不能失控继续外部调用。
        try:
            requested = bool(self._should_cancel())
        except Exception:
            requested = True
        with self._lock:
            self._cancelled = self._cancelled or requested
            if self._cancelled:
                return "cancelled"
        return "limited" if self.remaining_seconds() <= 0 else None

    def reserve_model(self, max_output_tokens):
        if type(max_output_tokens) is not int or max_output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer")
        return self._reserve({"model_requests": 1, "reserved_output_tokens": max_output_tokens})

    def reserve_tool(self):
        return self._reserve({"tool_calls": 1})

    def _reserve(self, amounts):
        if self.stop_status():
            return False
        with self._lock:
            # 锁内再核时限；并发分支不能同时领取最后一个名额。
            if self._cancelled or self.remaining_seconds() <= 0:
                return False
            for key, amount in amounts.items():
                if self._used[key] + amount > self._limits[key]:
                    self._last_denial = key + "_limit"
                    return False
            for key, amount in amounts.items():
                self._used[key] += amount
            return True

    def snapshot(self):
        stopped = self.stop_status()
        with self._lock:
            return {
                "limits": dict(self._limits), "used": dict(self._used),
                "timeout_seconds": self._duration,
                "elapsed_seconds": round(max(0.0, self._clock() - self._started), 6),
                "remaining_seconds": round(self.remaining_seconds(), 6),
                "stop_reason": "cancelled" if stopped == "cancelled" else "deadline" if stopped else None,
                "last_denial": self._last_denial,
                "monetary_hard_limit": False,
            }
