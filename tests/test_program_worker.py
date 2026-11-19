"""受限语法与资源拒绝，不启动容器或外部模型。"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from program_worker import Interpreter, ProgramError, bounded_json, validate_source
from program_executor import execute_program, container_command, IMAGE


class ProgramWorkerTests(unittest.TestCase):
    def test_list_then_readonly_checks_preserve_status_and_values(self):
        calls = []
        def handle(name, args):
            calls.append((name, args))
            if name == "list_orders":
                return {"status": "ok", "orders": [{"order_id": "O001"}, {"order_id": "O002"}]}
            return {"status": "ok", "net_amount_fen": 85} if args["order_id"] == "O001" else {"status": "not_found"}
        result = Interpreter(handle).run('''page = list_orders(limit=2)
rows = []
for order in page["orders"]:
    checked = check_order(order_id=order["order_id"], refund_before="2026-11-03T00:00:00+08:00")
    rows.append(checked)
result = rows
''')
        self.assertEqual(result, [{"status": "ok", "net_amount_fen": 85}, {"status": "not_found"}])
        self.assertEqual(len(calls), 3)

    def test_condition_and_integer_computation(self):
        result = Interpreter(lambda *_: None).run('''values = [10, 20, 30]
total = 0
for value in values:
    if value >= 20:
        total = total + value
result = {"sum": total, "remaining": 100 - total}
''')
        self.assertEqual(result, {"sum": 50, "remaining": 50})

    def test_forbidden_syntax_is_rejected_even_in_unexecuted_branch(self):
        programs = ["import os", "result = open('/etc/passwd')", "result = __import__('os')",
                    "result = (1).__class__", "while True:\n    result = 1", "def f():\n    return 1",
                    "result = [x for x in [1]]", "result = 2 ** 100", "result = eval('1')",
                    "result = list_orders(**{})", "result = list_orders(3)", "list_orders = 3",
                    "result = (lambda: 1)()", "if False:\n    import os"]
        for source in programs:
            with self.subTest(source=source), self.assertRaises(ProgramError):
                Interpreter(lambda *_: self.fail("No tool call allowed")).run(source)

    def test_model_cannot_supply_identity_to_tool(self):
        with self.assertRaisesRegex(ProgramError, "仅接受"):
            Interpreter(lambda *_: self.fail("Must reject before broker")).run(
                'result = check_order(order_id="O001", buyer_id="B002")')

    def test_tool_count_is_bounded_independently_of_parent_budget(self):
        calls = []
        with self.assertRaises(ProgramError) as caught:
            Interpreter(lambda *args: calls.append(args) or {"status": "ok"}, max_tool_calls=2).run(
                'for id in ["O1", "O2", "O3"]:\n    result = check_order(order_id=id)')
        self.assertEqual(caught.exception.code, "tool_limit")
        self.assertEqual(len(calls), 2)

    def test_invalid_tool_result_is_not_assumed_success(self):
        with self.assertRaises(ProgramError) as caught:
            Interpreter(lambda *_: {"amount": 0}).run('result = check_order(order_id="O1")')
        self.assertEqual(caught.exception.code, "invalid_tool_result")

    def test_missing_value_is_not_zero(self):
        with self.assertRaises(ProgramError) as caught:
            Interpreter(lambda *_: {"status": "not_found"}).run(
                'order = check_order(order_id="O1")\nresult = order["net_amount_fen"]')
        self.assertEqual(caught.exception.code, "invalid_subscript")

    def test_cyclic_data_and_large_data_fail_closed(self):
        for source in ('rows = []\nrows.append(rows)\nresult = rows', 'result = ' + repr('x' * 8192)):
            with self.subTest(source=source[:20]), self.assertRaises(ProgramError):
                Interpreter(lambda *_: None).run(source)
        with self.assertRaises(ProgramError): bounded_json({"x": "y" * 17000})
        with self.assertRaises(ProgramError): bounded_json({"x": float("nan")})

    def test_steps_iterations_and_result_are_bounded(self):
        with self.assertRaises(ProgramError) as caught:
            Interpreter(lambda *_: None, max_steps=2).run("result = [1, 2, 3]")
        self.assertEqual(caught.exception.code, "step_limit")
        with self.assertRaises(ProgramError) as caught:
            Interpreter(lambda *_: None).run("for i in " + str(list(range(9))) + ":\n    for j in " + str(list(range(9))) + ":\n        result = j")
        self.assertEqual(caught.exception.code, "loop_limit")
        with self.assertRaises(ProgramError) as caught: Interpreter(lambda *_: None).run("x = 1")
        self.assertEqual(caught.exception.code, "missing_result")

    def test_container_flags_and_single_readonly_mount(self):
        args = container_command("/allowed/docker", "test-fixed-name")
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertEqual(args[args.index("--user") + 1], "65534:65534")
        self.assertIn("--read-only", args)
        self.assertIn("no-new-privileges", args)
        self.assertEqual(args.count("--mount"), 1)
        mount = args[args.index("--mount") + 1]
        self.assertTrue(mount.endswith(",readonly"))
        self.assertNotIn("docker.sock", " ".join(args))
        self.assertIn(IMAGE, args)
        self.assertNotIn("--privileged", args)

    def test_invalid_code_and_pre_cancel_do_not_spawn_container(self):
        with patch("program_executor.subprocess.Popen", side_effect=AssertionError("must not spawn")):
            invalid = execute_program("import os", lambda *_: None)
            cancelled = execute_program("result = 1", lambda *_: None, cancelled=lambda: True)
        self.assertEqual(invalid["status"], "rejected")
        self.assertEqual(cancelled["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
