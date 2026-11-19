"""仅HTTP替身；1024维向量人为构造，不冒充真实模型质量。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from embedding_client import (EmbeddingClient, EmbeddingSettings, EmbeddingError, MODEL, DIMENSION, validate_vector)


def response(count=1):
    return {"model": MODEL, "data": [{"index": i, "embedding": [1.0] + [0.0]*(DIMENSION-1)} for i in range(count)],
            "usage": {"prompt_tokens": 9, "total_tokens": 9}}


class PolicyEmbeddingTests(unittest.TestCase):
    def client(self, body=None, status=200):
        self.calls = []
        def send(request):
            self.calls.append(request)
            return httpx.Response(status, json=body if body is not None else response())
        return EmbeddingClient(EmbeddingSettings(api_key="fake-test-key"), transport=httpx.MockTransport(send))

    def test_fixed_api_payload_order_and_no_dimension_parameter(self):
        body = response(2); body["data"].reverse()
        client = self.client(body)
        result = client.embed_texts(["受控原文", "用户查询"])
        self.assertEqual(result["source"], "offline_test_transport")
        self.assertEqual(result["dimension"], 1024)
        payload = json.loads(self.calls[0].content)
        self.assertEqual(str(self.calls[0].url), "https://api.siliconflow.cn/v1/embeddings")
        self.assertEqual(payload, {"model": MODEL, "input": ["受控原文", "用户查询"], "encoding_format": "float"})
        self.assertIn("elapsed_seconds", result["stats"])
        self.assertNotIn("fake-test-key", repr(client.settings) + json.dumps(client.last_stats))

    def test_failed_response_is_not_retried_or_logged_with_secret(self):
        client = self.client({"error": "fake-test-key server echoed"}, 401)
        with self.assertRaises(EmbeddingError) as got:
            client.embed_texts(["text"])
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("fake-test-key", str(got.exception) + json.dumps(client.last_stats))

    def test_input_size_and_missing_key_stop_before_network(self):
        client = self.client()
        for value in ([], [""], ["a"*3001], ["t"]*33, "string"):
            with self.assertRaises(EmbeddingError):
                client.embed_texts(value)
        self.assertFalse(self.calls)
        with self.assertRaises(EmbeddingError):
            EmbeddingClient(EmbeddingSettings()).embed_texts(["query"])

    def test_invalid_vector_numbers_dimensions_indices_and_model(self):
        for vector in ([1.0]*1023, [0.0]*1024, [float("nan")]+[1.0]*1023, [True]+[0.0]*1023):
            with self.assertRaises(EmbeddingError):
                validate_vector(vector)
        bodies = [response(), response(), response()]
        bodies[0]["model"] = "other"
        bodies[1]["data"][0]["index"] = True
        bodies[2]["data"][0]["embedding"] = [1]
        for body in bodies:
            with self.assertRaises(EmbeddingError):
                self.client(body).embed_texts(["query"])

    def test_timeout_is_safe_and_no_auto_retry(self):
        count = 0
        def send(request):
            nonlocal count; count += 1
            raise httpx.ReadTimeout("secret should not surface", request=request)
        client = EmbeddingClient(EmbeddingSettings(api_key="fake-test-key"), transport=httpx.MockTransport(send))
        with self.assertRaises(EmbeddingError) as got:
            client.embed_texts(["query"])
        self.assertEqual(count, 1)
        self.assertNotIn("secret", str(got.exception))

    def test_config_reads_external_local_file_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/"local").mkdir()
            (root/"old.env").write_text("SILICONFLOW_API_KEY=fake-test-key\n")
            (root/"local/knowledge.env").write_text("SILICONFLOW_KEY_FILE=old.env\n")
            self.assertEqual(EmbeddingSettings.load(root=root).api_key, "fake-test-key")


if __name__ == "__main__":
    unittest.main()
