"""Context extraction contracts; all inputs are synthetic and no model is called."""
from copy import deepcopy
from pathlib import Path
import json
import subprocess
import sys
import unittest

from runtime_context import current_preference_overrides, history_pairs, preference_memory


class RuntimeContextTests(unittest.TestCase):
    def test_context_import_does_not_load_an_agent_runtime(self):
        # A fresh interpreter prevents earlier test imports from masking a cycle.
        code = """
import sys
import runtime_context
assert not {'agent_runtime', 'shopping_runtime', 'period_analysis_runtime',
            'refund_investigation_runtime', 'programmatic_runtime', 'snapshot_sql_runtime',
            'multi_agent_runtime'} & set(sys.modules)
assert runtime_context.history_pairs([]) == []
assert runtime_context.preference_memory(None, 'buyer', '')['status'] == 'not_provided'
"""
        subprocess.run([sys.executable, "-B", "-c", code],
                       cwd=Path(__file__).resolve().parents[1], check=True,
                       capture_output=True, text=True, timeout=15)

    def test_history_keeps_complete_recent_pairs_and_drops_extra_fields(self):
        history = []
        for index in range(4):
            history += [{"role": "user", "content": f"question-{index}", "account_id": "synthetic"},
                        {"role": "assistant", "content": str(index) * 3100, "tool_calls": ["old"]}]
        history += [{"role": "user", "content": "unfinished"}]
        before = deepcopy(history)
        result = history_pairs(history)
        self.assertEqual(history, before)
        self.assertEqual(len(result), 6)
        self.assertEqual([item["content"] for item in result[::2]], ["question-1", "question-2", "question-3"])
        self.assertTrue(all(set(item) == {"role", "content"} for item in result))
        self.assertTrue(all(len(item["content"]) == 3000 for item in result[1::2]))
        result[0]["content"] = "edited output"
        self.assertEqual(history, before)

    def test_bad_history_pair_does_not_join_unrelated_messages(self):
        history = [{"role": "user", "content": "old"}, {"role": "tool", "content": "tool output"},
                   {"role": "assistant", "content": "orphan"}, {"role": "user", "content": "orphan"},
                   {"role": "user", "content": "valid"}, {"role": "assistant", "content": "answer"},
                   {"role": "user", "content": "bad"}, {"role": "assistant", "content": None}]
        self.assertEqual(history_pairs(history), [{"role": "user", "content": "valid"},
                                                 {"role": "assistant", "content": "answer"}])

    def test_preference_result_does_not_retain_source_or_alias_input(self):
        snapshot = {"role": "buyer", "values": {"amount_format": "fen", "answer_style": "brief"},
                    "version": 2, "updated_at": "2026-11-22T01:00:00Z",
                    "source": {"kind": "user_chat", "text": "synthetic audit-only source"}}
        before = deepcopy(snapshot)
        result = preference_memory(snapshot, "buyer", "本次用元；本次详细回答")
        self.assertEqual(result["status"], "loaded")
        self.assertEqual(result["stored_values"], snapshot["values"])
        self.assertEqual(result["effective_values"], {"amount_format": "yuan", "answer_style": "detailed"})
        self.assertNotIn("audit-only", json.dumps(result))
        self.assertNotIn("role", result)
        self.assertNotIn("source", result)
        result["stored_values"]["amount_format"] = "yuan_fixed2"
        self.assertEqual(snapshot, before)
        self.assertEqual(result["effective_values"]["amount_format"], "yuan")

    def test_empty_invalid_and_wrong_role_snapshots_keep_current_turn_independent(self):
        empty = {"role": "buyer", "values": {}, "version": 0, "updated_at": None, "source": None}
        self.assertEqual(preference_memory(empty, "buyer", "")["status"], "loaded")
        valid = {"role": "buyer", "values": {"amount_format": "fen"}, "version": 1,
                 "updated_at": "2026-11-22T01:00:00+00:00", "source": {"kind": "user_settings"}}
        for change in ({"updated_at": "2026-11-22T01:00:00"},
                       {"updated_at": "2026-11-22T09:00:00+08:00"},
                       {"updated_at": "not-a-time"}, {"version": True},
                       {"source": {"kind": "untrusted"}}, {"account_id": "unexpected"}):
            with self.subTest(change=change):
                result = preference_memory(valid | change, "buyer", "本次用元")
                self.assertEqual(result["status"], "invalid_snapshot")
                self.assertEqual(result["stored_values"], {})
                self.assertEqual(result["effective_values"], {"amount_format": "yuan"})
                self.assertIsNone(result["version"])
        result = preference_memory(valid, "merchant", "本次用元")
        self.assertEqual(result["status"], "role_mismatch")
        self.assertEqual(result["stored_values"], {})
        self.assertEqual(result["effective_values"], {"amount_format": "yuan"})

    def test_legacy_imports_still_refer_to_shared_functions(self):
        from agent_runtime import _current_preference_overrides, _history_pairs, _preference_memory
        self.assertIs(_history_pairs, history_pairs)
        self.assertIs(_current_preference_overrides, current_preference_overrides)
        self.assertIs(_preference_memory, preference_memory)


if __name__ == "__main__":
    unittest.main()
