"""Single model request shared by fixed-fact analysis runtimes.

Callers own budget reservation, cancellation, retries, business validation and
fallbacks. This module records one attempt; it never starts another request.
"""
from copy import deepcopy
import hashlib
import json

import httpx


def request_model_once(settings, payload, *, secret, transport, remaining_seconds,
                       usage, request_number, begin, end, max_response_bytes):
    """Return ``(body, event)`` and update the caller's existing usage counters.

    ``begin``/``end`` retain the caller's trace timestamps. Usage is collected
    after ``end``, including valid usage in an HTTP error body. Missing or invalid
    total usage remains unmetered; no estimate is substituted for actual usage.
    """
    usage["model_requests"] += 1
    usage["unmetered_requests"] += 1
    event = {"stage": "model", "request_number": request_number, "request": deepcopy(payload)}
    stamp = begin(event)
    body = None
    try:
        with httpx.Client(transport=transport, timeout=settings.model_timeout_seconds,
                          trust_env=False, follow_redirects=False) as client:
            response = client.post(settings.model_base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + secret}, json=payload,
                timeout=max(.001, min(settings.model_timeout_seconds, remaining_seconds())))
            event["http_status"] = response.status_code
            if len(response.content) > max_response_bytes:
                event.update(error_category="response_size_limit", response_bytes=len(response.content),
                             response_sha256=hashlib.sha256(response.content).hexdigest())
            else:
                try:
                    body = response.json()
                    event["response"] = deepcopy(body)
                except ValueError:
                    event["raw_response"] = response.text
                response.raise_for_status()
    except (httpx.HTTPError, ValueError) as error:
        event.update(error_type=type(error).__name__, error_category="model_request_failed")
    finally:
        end(event, stamp)
    raw_usage = body.get("usage", {}) if isinstance(body, dict) else {}
    if isinstance(raw_usage, dict) and type(raw_usage.get("total_tokens")) is int and raw_usage["total_tokens"] >= 0:
        usage["unmetered_requests"] -= 1
        for local, remote in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"),
                              ("total_tokens", "total_tokens")):
            if type(raw_usage.get(remote)) is int and raw_usage[remote] >= 0:
                usage[local] += raw_usage[remote]
    return body, event


def decode_unique_json(content):
    """Reject duplicate keys at every object level; callers validate the schema."""
    def unique(pairs):
        out = {}
        for key, item in pairs:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = item
        return out
    return json.loads(content, object_pairs_hook=unique)
