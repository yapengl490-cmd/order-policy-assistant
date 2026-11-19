"""容器内的受限程序解释器：只解释允许的语法树，不运行Python源码。

可用工具由父进程代理；没有网络、数据库或Docker权限。普通Python的导入、
属性访问、函数定义和无限循环均不属于这个小语言。此文件也可离线测试。
"""
from __future__ import annotations

import ast
import json
import sys

MAX_SOURCE_BYTES = 8192
MAX_JSON_BYTES = 65536
MAX_NODES = 512
MAX_STEPS = 600
MAX_ITERATIONS = 64
TOOLS = frozenset({"list_orders", "check_order"})


class ProgramError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def bounded_json(value, max_bytes=MAX_JSON_BYTES):
    """先限制结构/整数，再序列化，防循环引用和指数展开占满内存。"""
    count, active = 0, set()

    def visit(item, depth):
        nonlocal count
        count += 1
        if count > 4096 or depth > 16:
            raise ProgramError("data_limit", "数据项数或嵌套深度超限")
        if type(item) in (list, dict):
            identity = id(item)
            if identity in active:
                raise ProgramError("data_limit", "不允许循环引用")
            active.add(identity)
            if type(item) is dict:
                for key, child in item.items():
                    if type(key) is not str or len(key) > 256:
                        raise ProgramError("data_type", "字典键须为有限长度字符串")
                    visit(child, depth + 1)
            else:
                for child in item:
                    visit(child, depth + 1)
            active.remove(identity)
        elif item is None or type(item) is bool:
            pass
        elif type(item) is int:
            if abs(item) > 10 ** 15:
                raise ProgramError("data_limit", "整数超出本实验范围")
        elif type(item) is str:
            if len(item.encode("utf-8")) > 16384:
                raise ProgramError("data_limit", "字符串长度超限")
        else:
            raise ProgramError("data_type", "仅允许JSON整数、文字、布尔、空值、列表和字典")

    visit(value, 0)
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(data.encode("utf-8")) > max_bytes:
        raise ProgramError("data_limit", "序列化数据长度超限")
    return data


def validate_tool_request(name, arguments):
    if name not in TOOLS or type(arguments) is not dict:
        raise ProgramError("tool_not_allowed", "工具不在只读白名单")
    if name == "list_orders":
        if set(arguments) - {"limit"} or type(arguments.get("limit", 20)) is not int or not 1 <= arguments.get("limit", 20) <= 20:
            raise ProgramError("invalid_tool_arguments", "list_orders仅接受1至20的limit")
    else:
        if (set(arguments) - {"order_id", "refund_before"} or type(arguments.get("order_id")) is not str
                or not 1 <= len(arguments["order_id"]) <= 128
                or (arguments.get("refund_before") is not None and
                    (type(arguments["refund_before"]) is not str or len(arguments["refund_before"]) > 64))):
            raise ProgramError("invalid_tool_arguments", "check_order仅接受订单号和可选截止时间")
    bounded_json(arguments, 4096)


def validate_source(source):
    if type(source) is not str or not source.strip() or len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ProgramError("source_limit", "程序须为1至8192字节")
    try:
        tree = ast.parse(source, mode="exec")
    except (SyntaxError, ValueError, RecursionError):
        raise ProgramError("invalid_syntax", "无法解析受限程序") from None
    nodes = list(ast.walk(tree))
    allowed = (ast.Module, ast.Assign, ast.Name, ast.Load, ast.Store, ast.Constant, ast.List, ast.Dict,
               ast.For, ast.If, ast.Expr, ast.Call, ast.keyword, ast.Attribute, ast.Subscript,
               ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
               ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.BinOp, ast.Add, ast.Sub)
    if len(nodes) > MAX_NODES or any(not isinstance(node, allowed) for node in nodes):
        raise ProgramError("unsupported_syntax", "仅允许赋值、有限for、if、JSON数据、索引和白名单工具")
    parents = {child: node for node in nodes for child in ast.iter_child_nodes(node)}
    for node in nodes:
        if isinstance(node, ast.Name):
            if node.id.startswith("_") or len(node.id) > 64:
                raise ProgramError("invalid_name", "不允许内部名称")
            if isinstance(node.ctx, ast.Store) and node.id in TOOLS:
                raise ProgramError("invalid_name", "不能覆盖工具名称")
        if isinstance(node, ast.Assign) and (len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name)):
            raise ProgramError("unsupported_assignment", "仅允许单个变量赋值")
        if isinstance(node, ast.For) and (not isinstance(node.target, ast.Name) or node.orelse):
            raise ProgramError("unsupported_loop", "for须遍历列表并赋给一个变量，不支持else")
        if isinstance(node, ast.Dict) and any(key is None for key in node.keys):
            raise ProgramError("unsupported_syntax", "不支持字典解包")
        if isinstance(node, ast.Attribute):
            parent = parents.get(node)
            if not (node.attr == "append" and isinstance(node.value, ast.Name)
                    and isinstance(parent, ast.Call) and parent.func is node):
                raise ProgramError("attribute_forbidden", "仅允许列表变量的append调用")
        if isinstance(node, ast.Call):
            tool = isinstance(node.func, ast.Name) and node.func.id in TOOLS
            append = isinstance(node.func, ast.Attribute) and node.func.attr == "append"
            if not (tool or append) or (tool and node.args) or (append and (len(node.args) != 1 or node.keywords)):
                raise ProgramError("call_forbidden", "工具只能使用命名参数，列表仅支持append一个值")
            names = [kw.arg for kw in node.keywords]
            if None in names or len(names) != len(set(names)):
                raise ProgramError("invalid_tool_arguments", "不支持参数解包或重复参数")
        if isinstance(node, ast.Constant):
            bounded_json(node.value)
    # 限制语法深度，避免解释时递归耗尽；不是仅依赖容器内存保护。
    def depth(node, level=0):
        if level > 24:
            raise ProgramError("syntax_depth", "语法嵌套过深")
        for child in ast.iter_child_nodes(node):
            depth(child, level + 1)
    depth(tree)
    return tree


class Interpreter:
    def __init__(self, tool_handler, max_steps=MAX_STEPS, max_tool_calls=6):
        self.tool_handler = tool_handler
        self.max_steps, self.max_tool_calls = min(max_steps, MAX_STEPS), min(max_tool_calls, 6)
        self.steps = self.tool_calls = self.iterations = 0
        self.variables = {}

    def tick(self):
        self.steps += 1
        if self.steps > self.max_steps:
            raise ProgramError("step_limit", "程序解释步数超限")

    def value(self, node):
        self.tick()
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in self.variables:
                raise ProgramError("unknown_variable", "变量尚未赋值：" + node.id)
            return self.variables[node.id]
        if isinstance(node, ast.List):
            return [self.value(item) for item in node.elts]
        if isinstance(node, ast.Dict):
            value = {self.value(key): self.value(item) for key, item in zip(node.keys, node.values)}
            bounded_json(value)
            return value
        if isinstance(node, ast.Subscript):
            container, key = self.value(node.value), self.value(node.slice)
            if type(container) is dict and type(key) is str and key in container:
                return container[key]
            if type(container) is list and type(key) is int and 0 <= key < len(container):
                return container[key]
            raise ProgramError("invalid_subscript", "列表索引或字典键无效；不能将缺失事实视为零")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                target = self.value(node.func.value)
                if type(target) is not list or len(target) >= 128:
                    raise ProgramError("data_limit", "append只支持至多128项列表")
                target.append(self.value(node.args[0]))
                bounded_json(self.variables)
                return None
            arguments = {kw.arg: self.value(kw.value) for kw in node.keywords}
            validate_tool_request(node.func.id, arguments)
            if self.tool_calls >= self.max_tool_calls:
                raise ProgramError("tool_limit", "程序工具次数达到上限")
            self.tool_calls += 1
            result = self.tool_handler(node.func.id, arguments)
            bounded_json(result)
            if type(result) is not dict or type(result.get("status")) is not str:
                raise ProgramError("invalid_tool_result", "工具结果缺少明确状态")
            return result  # 包含not_found等状态，不把未找到/失败转成零金额。
        if isinstance(node, ast.UnaryOp):
            value = self.value(node.operand)
            if isinstance(node.op, ast.Not):
                return not value
            if type(value) is not int:
                raise ProgramError("type_error", "负号只支持整数")
            return -value
        if isinstance(node, ast.BinOp):
            left, right = self.value(node.left), self.value(node.right)
            if type(left) is not int or type(right) is not int:
                raise ProgramError("type_error", "加减只支持整数金额或计数")
            value = left + right if isinstance(node.op, ast.Add) else left - right
            bounded_json(value)
            return value
        if isinstance(node, ast.BoolOp):
            for child in node.values:
                value = self.value(child)
                if isinstance(node.op, ast.And) and not value or isinstance(node.op, ast.Or) and value:
                    return value
            return value
        if isinstance(node, ast.Compare):
            left = self.value(node.left)
            for op, child in zip(node.ops, node.comparators):
                right = self.value(child)
                if isinstance(op, ast.Eq): answer = left == right
                elif isinstance(op, ast.NotEq): answer = left != right
                elif isinstance(op, (ast.In, ast.NotIn)):
                    if type(right) not in (list, dict):
                        raise ProgramError("type_error", "成员比较只支持列表或字典")
                    answer = left in right
                    if isinstance(op, ast.NotIn): answer = not answer
                else:
                    if type(left) is not int or type(right) is not int:
                        raise ProgramError("type_error", "大小比较仅支持整数")
                    if isinstance(op, ast.Lt): answer = left < right
                    elif isinstance(op, ast.LtE): answer = left <= right
                    elif isinstance(op, ast.Gt): answer = left > right
                    else: answer = left >= right
                if not answer:
                    return False
                left = right
            return True
        raise ProgramError("unsupported_syntax", "不支持该表达式")

    def statements(self, nodes, loop_depth=0):
        for node in nodes:
            self.tick()
            if isinstance(node, ast.Assign):
                self.variables[node.targets[0].id] = self.value(node.value)
            elif isinstance(node, ast.Expr):
                self.value(node.value)
            elif isinstance(node, ast.If):
                self.statements(node.body if self.value(node.test) else node.orelse, loop_depth)
            elif isinstance(node, ast.For):
                values = self.value(node.iter)
                if type(values) is not list or len(values) > 32 or loop_depth >= 2:
                    raise ProgramError("loop_limit", "for仅支持至多32项列表和两层循环")
                for value in list(values):
                    self.iterations += 1
                    if self.iterations > MAX_ITERATIONS:
                        raise ProgramError("loop_limit", "累计循环次数超限")
                    self.variables[node.target.id] = value
                    self.statements(node.body, loop_depth + 1)
            else:
                raise ProgramError("unsupported_syntax", "不支持该语句")
            if len(self.variables) > 32:
                raise ProgramError("variable_limit", "变量数量超限")
            bounded_json(self.variables)

    def run(self, source):
        self.statements(validate_source(source).body)
        if "result" not in self.variables:
            raise ProgramError("missing_result", "程序须给result赋值")
        result = self.variables["result"]
        bounded_json(result, 32768)
        return result


def main():
    interpreter = None
    def read():
        raw = sys.stdin.buffer.readline(MAX_JSON_BYTES + 2)
        if not raw or len(raw) > MAX_JSON_BYTES or not raw.endswith(b"\n"):
            raise ProgramError("protocol_input", "父进程输入超限或已关闭")
        return json.loads(raw)

    def emit(value):
        sys.stdout.write(bounded_json(value) + "\n")
        sys.stdout.flush()

    sequence = 0
    def request_tool(name, arguments):
        nonlocal sequence
        sequence += 1
        emit({"type": "tool", "id": sequence, "name": name, "arguments": arguments})
        reply = read()
        if type(reply) is not dict or reply.get("id") != sequence or reply.get("type") != "tool_result":
            raise ProgramError("protocol_input", "工具回执不匹配")
        if reply.get("error"):
            raise ProgramError("tool_failed", "工具未完成，已有事实由父进程保留")
        return reply.get("result")

    try:
        initial = read()
        if type(initial) is not dict or set(initial) != {"source"}:
            raise ProgramError("protocol_input", "初始化消息无效")
        interpreter = Interpreter(request_tool)
        result = interpreter.run(initial["source"])
        emit({"type": "result", "status": "ok", "result": result,
              "steps": interpreter.steps, "tool_calls": interpreter.tool_calls})
    except Exception as exc:
        code = exc.code if isinstance(exc, ProgramError) else "invalid_program_data"
        emit({"type": "result", "status": "failed", "error": code,
              "steps": interpreter.steps if interpreter else 0,
              "tool_calls": interpreter.tool_calls if interpreter else 0})


if __name__ == "__main__":
    main()
