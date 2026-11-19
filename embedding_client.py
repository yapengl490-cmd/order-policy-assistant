"""硅基流动嵌入边界：只有显式embed_texts调用才发请求，不安装或启动模型。

固定模型及地址，不发送dimensions参数；任何测试transport都明确标为离线替身。
统计不含密钥或原始输入，服务错误只保留类别，不记录请求头/错误响应正文。
"""
from dataclasses import dataclass, field
import math
from pathlib import Path
import time

from dotenv import dotenv_values
import httpx

ENDPOINT = "https://api.siliconflow.cn/v1/embeddings"
MODEL = "BAAI/bge-m3"
DIMENSION = 1024
MAX_INPUTS = 32
MAX_INPUT_CHARS = 3000
MAX_INPUT_BYTES = 8000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class EmbeddingError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__("嵌入请求未完成：" + code)


@dataclass(frozen=True)
class EmbeddingSettings:
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = 15

    @classmethod
    def load(cls, *, root=None):
        root = Path(root or Path(__file__).resolve().parent)
        values = dotenv_values(root / "local/knowledge.env")
        key = values.get("SILICONFLOW_API_KEY") or ""
        key_file = values.get("SILICONFLOW_KEY_FILE")
        if not key and key_file:
            path = Path(key_file).expanduser()
            if not path.is_absolute():
                path = root / path
            key = dotenv_values(path).get("SILICONFLOW_API_KEY") or ""
        return cls(api_key=key)


def validate_vector(vector, dimension=DIMENSION):
    if not isinstance(vector, list) or len(vector) != dimension:
        raise EmbeddingError("invalid_vector_dimension")
    if any(type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e6 for value in vector):
        raise EmbeddingError("invalid_vector_number")
    if not any(value != 0 for value in vector):
        raise EmbeddingError("zero_vector")
    return [float(value) for value in vector]


class EmbeddingClient:
    model = MODEL
    dimension = DIMENSION

    def __init__(self, settings=None, *, root=None, transport=None):
        self.settings = settings or EmbeddingSettings.load(root=root)
        self.transport = transport
        self.source = "offline_test_transport" if transport is not None else "siliconflow_api"
        self.last_stats = {"requests": 0, "source": self.source}

    def embed_texts(self, texts):
        if (not isinstance(texts, list) or not 1 <= len(texts) <= MAX_INPUTS
                or any(not isinstance(text, str) or not text.strip() or len(text) > MAX_INPUT_CHARS
                       or len(text.encode()) > MAX_INPUT_BYTES for text in texts)):
            raise EmbeddingError("invalid_input_size")
        key = self.settings.api_key
        if not isinstance(key, str) or not key or not key.isascii() or key != key.strip():
            raise EmbeddingError("missing_or_invalid_local_key")
        timeout = self.settings.timeout_seconds
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise EmbeddingError("invalid_timeout")
        started = time.monotonic()
        self.last_stats = {"requests": 1, "inputs": len(texts), "source": self.source,
            "model": MODEL, "dimension": DIMENSION, "usage": {}, "unmetered_requests": 1}
        try:
            with httpx.Client(transport=self.transport, timeout=timeout, trust_env=False, follow_redirects=False) as client:
                # 用stream限制响应大小，且不在失败后自动重试。
                with client.stream("POST", ENDPOINT, headers={"Authorization": "Bearer " + key},
                    json={"model": MODEL, "input": texts, "encoding_format": "float"}) as response:
                    self.last_stats["http_status"] = response.status_code
                    if response.status_code != 200:
                        raise EmbeddingError("http_" + str(response.status_code))
                    body = bytearray()
                    for part in response.iter_bytes():
                        body.extend(part)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise EmbeddingError("response_too_large")
            import json
            decoded = json.loads(body)
            if (not isinstance(decoded, dict) or decoded.get("model") != MODEL
                    or not isinstance(decoded.get("data"), list) or len(decoded["data"]) != len(texts)):
                raise EmbeddingError("invalid_response_structure_or_model")
            indexed = {}
            for item in decoded["data"]:
                if not isinstance(item, dict) or type(item.get("index")) is not int or not 0 <= item["index"] < len(texts) or item["index"] in indexed:
                    raise EmbeddingError("invalid_response_index")
                indexed[item["index"]] = validate_vector(item.get("embedding"))
            usage = decoded.get("usage")
            if isinstance(usage, dict):
                self.last_stats["usage"] = {k: usage[k] for k in ("prompt_tokens", "total_tokens") if type(usage.get(k)) is int and usage[k] >= 0}
                if "total_tokens" in self.last_stats["usage"]:
                    self.last_stats["unmetered_requests"] = 0
            self.last_stats["status"] = "passed"
            self.last_stats["elapsed_seconds"] = round(time.monotonic() - started, 6)
            return {"vectors": [indexed[i] for i in range(len(texts))], "model": MODEL,
                "dimension": DIMENSION, "source": self.source, "stats": dict(self.last_stats)}
        except EmbeddingError as error:
            self.last_stats.update(status="failed", error_code=error.code)
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            self.last_stats.update(status="failed", error_code="transport_or_json_error")
            raise EmbeddingError("transport_or_json_error") from None
        finally:
            self.last_stats["elapsed_seconds"] = round(time.monotonic() - started, 6)
