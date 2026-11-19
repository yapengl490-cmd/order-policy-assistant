"""已授权的小型业务快照 + SQLite只读SQL执行器；模型SQL永不连接正式数据库。

安全边界由独立内存数据库、SQLite authorizer白名单、query_only及执行/输出
限制共同提供；没有靠SELECT前缀或正则表达式授权SQL。此处不调用模型。
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import time

from refund_service import _identifier, _time, summary_orders, MAX_SUMMARY_ORDERS
from merchant_service import summary_shop_orders

COLUMNS=("order_id","paid_at","paid_amount_fen","refund_amount_fen","net_amount_fen","refund_count")
TOTAL_COLUMNS=("order_count","paid_amount_fen","refund_amount_fen","net_amount_fen")
SCHEMA="order_facts(order_id TEXT, paid_at TEXT, paid_amount_fen INTEGER, refund_amount_fen INTEGER, net_amount_fen INTEGER, refund_count INTEGER)"
MAX_SQL_BYTES=8192
MAX_RESULT_BYTES=8192
MAX_RESULT_ROWS=20
MAX_VM_STEPS=200_000
MAX_SECONDS=2.0


class SnapshotError(ValueError):
    pass


@dataclass(frozen=True)
class FrozenSnapshot:
    rows: tuple
    totals: tuple
    conditions_json: str
    provenance_json: str
    frozen_at: str
    sha256: str

    def baseline(self):
        return dict(zip(TOTAL_COLUMNS,self.totals))

    def public_fact(self):
        return {"status":"ok","snapshot_sha256":self.sha256,"frozen_at":self.frozen_at,
                "snapshot_database":"SQLite :memory:; no formal DB connection for model SQL",
                "schema":SCHEMA,"conditions":json.loads(self.conditions_json),
                "row_count":len(self.rows),"rows":[dict(zip(COLUMNS,row))for row in self.rows],
                "baseline":self.baseline(),"provenance":json.loads(self.provenance_json)}


def snapshot_from_summary(summary,conditions):
    if not isinstance(summary,dict) or summary.get("status")!="ok" or not isinstance(summary.get("orders"),list):
        raise SnapshotError("固定查询未取得可冻结的完整结果")
    if len(summary["orders"])>MAX_SUMMARY_ORDERS:
        raise SnapshotError("快照订单超过上限")
    rows=[];seen=set()
    for item in summary["orders"]:
        oid=_identifier(item.get("order_id"),"订单编号")
        if oid in seen:raise SnapshotError("快照订单重复")
        seen.add(oid)
        paid_at=item.get("paid_at")
        if not isinstance(paid_at,str):raise SnapshotError("快照缺少支付时间")
        try:_time(datetime.fromisoformat(paid_at),"支付时间")
        except ValueError:raise SnapshotError("快照支付时间无效")from None
        amounts=[item.get(key)for key in COLUMNS[2:]]
        if any(type(value)is not int or not 0<=value<=2**63-1 for value in amounts):
            raise SnapshotError("快照金额或计数不合法")
        paid,refund,net,count=amounts
        if paid-refund!=net:raise SnapshotError("快照净额不一致")
        rows.append((oid,paid_at,*amounts))
    rows.sort(key=lambda row:row[0])
    totals=(len(rows),*(sum(row[index]for row in rows)for index in (2,3,4)))
    if any(type(summary.get(k))is not int or summary[k]!=v for k,v in zip(TOTAL_COLUMNS,totals)):
        raise SnapshotError("固定汇总与快照明细不一致")
    condition_text=json.dumps(conditions,sort_keys=True,ensure_ascii=False)
    serialized=json.dumps({"rows":rows,"conditions":conditions},sort_keys=True,ensure_ascii=False)
    return FrozenSnapshot(tuple(rows),totals,condition_text,
        json.dumps(summary.get("provenance",{"status":"unknown"}),ensure_ascii=False),
        datetime.now(timezone.utc).isoformat(),hashlib.sha256(serialized.encode()).hexdigest())


def load_authorized_snapshot(engine,access,paid_from,paid_before,refund_before,*,loader=None):
    if not isinstance(access,dict)or access.get("role")not in {"buyer","merchant"}:
        raise SnapshotError("缺少可信访问范围")
    start,end,cutoff=[_time(value,name)for value,name in ((paid_from,"支付开始"),(paid_before,"支付结束"),(refund_before,"退款截止"))]
    if not start<end<=cutoff:raise SnapshotError("支付区间或退款截止无效")
    role=access["role"]
    identity=_identifier(access.get("buyer_id")if role=="buyer"else access.get("shop_id"),"可信访问编号")
    if loader is not None:
        result=loader(paid_from=start.isoformat(),paid_before=end.isoformat(),refund_before=cutoff.isoformat())
    elif role=="buyer":result=summary_orders(engine,identity,start,end,cutoff)
    else:result=summary_shop_orders(engine,identity,start,end,cutoff)
    return snapshot_from_summary(result,{"paid_from":start.isoformat(),"paid_before":end.isoformat(),
        "refund_before":cutoff.isoformat(),"payment_boundary":"paid_from <= paid_at < paid_before",
        "refund_boundary":"refunded_at < refund_before","money_unit":"fen","currency":"CNY"})


def execute_snapshot_sql(snapshot:FrozenSnapshot,sql:str,*,should_cancel=lambda:False,
                         max_seconds=MAX_SECONDS,max_vm_steps=MAX_VM_STEPS,
                         max_rows=MAX_RESULT_ROWS,max_bytes=MAX_RESULT_BYTES):
    """仅SQL结构/执行错误可有限修复；权限/资源拒绝和口径错误不自动改写重试。"""
    result={"syntax":"not_checked","execution":"not_run","metric":"not_checked",
            "snapshot_sha256":snapshot.sha256,"repairable":False}
    try:
        valid_sql=type(sql)is str and bool(sql.strip())and len(sql.encode())<=MAX_SQL_BYTES and "\x00"not in sql
    except UnicodeError:valid_sql=False
    if not valid_sql:return {**result,"status":"rejected","error":"sql_input_invalid"}
    def cancelled():
        try:return bool(should_cancel())
        except Exception:return True
    if cancelled():return {**result,"status":"cancelled","error":"cancelled"}
    if max_seconds<=0 or max_vm_steps<1:return {**result,"status":"limited","error":"resource_limit"}
    started=time.monotonic();state={"steps":0,"stop":None,"denied":False,"read":False}
    conn=sqlite3.connect(":memory:")
    try:
        conn.enable_load_extension(False)
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("CREATE TABLE order_facts (order_id TEXT,paid_at TEXT,paid_amount_fen INTEGER,refund_amount_fen INTEGER,net_amount_fen INTEGER,refund_count INTEGER)")
        conn.executemany("INSERT INTO order_facts VALUES (?,?,?,?,?,?)",snapshot.rows)
        conn.commit()
        conn.execute("PRAGMA query_only=ON")
        for category,limit in ((sqlite3.SQLITE_LIMIT_SQL_LENGTH,MAX_SQL_BYTES),(sqlite3.SQLITE_LIMIT_LENGTH,131072),
            (sqlite3.SQLITE_LIMIT_COLUMN,32),(sqlite3.SQLITE_LIMIT_EXPR_DEPTH,30),
            (sqlite3.SQLITE_LIMIT_COMPOUND_SELECT,1),(sqlite3.SQLITE_LIMIT_ATTACHED,0),
            (sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,0),(sqlite3.SQLITE_LIMIT_VDBE_OP,10000)):
            conn.setlimit(category,limit)
        def authorize(action,arg1,arg2,database,source):
            if action==sqlite3.SQLITE_SELECT:return sqlite3.SQLITE_OK
            # SQLite COUNT(*)的空列读取回调可能不给database名；本连接只含这个内存表。
            if (action==sqlite3.SQLITE_READ and (database=="main"or database is None and arg2=="")
                    and arg1=="order_facts" and arg2 in (*COLUMNS,"")):
                state["read"]=True;return sqlite3.SQLITE_OK
            if action==sqlite3.SQLITE_FUNCTION and (arg2 or "").lower()in {"sum","count","coalesce"}:
                return sqlite3.SQLITE_OK
            state["denied"]=True;return sqlite3.SQLITE_DENY
        def progress():
            state["steps"]+=100
            if cancelled():state["stop"]="cancelled"
            elif state["steps"]>max_vm_steps or time.monotonic()-started>max_seconds:state["stop"]="resource_limit"
            return 1 if state["stop"]else 0
        conn.set_authorizer(authorize);conn.set_progress_handler(progress,100)
        try:
            cursor=conn.execute(sql)
            result["syntax"]="passed"
            rows=cursor.fetchmany(max_rows+1)
            if cancelled():return {**result,"status":"cancelled","execution":"interrupted","error":"cancelled"}
            if time.monotonic()-started>max_seconds:
                return {**result,"status":"limited","execution":"limited","error":"resource_limit"}
            if not state["read"]:
                return {**result,"status":"rejected","execution":"blocked","error":"authorized_snapshot_read_required"}
            if len(rows)>max_rows:
                return {**result,"status":"limited","execution":"limited","error":"result_row_limit"}
            names=[item[0]for item in cursor.description or []]
            raw={"columns":names,"rows":rows}
            if len(json.dumps(raw,ensure_ascii=False).encode())>max_bytes:
                return {**result,"status":"limited","execution":"limited","error":"result_byte_limit"}
            result.update(execution="passed",columns=names,rows=rows)
            if len(rows)!=1 or len(names)!=len(TOTAL_COLUMNS)or set(names)!=set(TOTAL_COLUMNS):
                return {**result,"status":"mismatch","metric":"failed","error":"summary_shape_mismatch"}
            actual=dict(zip(names,rows[0]))
            if any(type(actual[k])is not int or actual[k]!=v for k,v in snapshot.baseline().items()):
                return {**result,"status":"mismatch","metric":"failed","error":"fixed_metric_mismatch","actual":actual}
            return {**result,"status":"ok","metric":"passed","actual":actual}
        except (sqlite3.Error,MemoryError)as exc:
            if state["stop"]:
                return {**result,"status":"cancelled"if state["stop"]=="cancelled"else"limited",
                        "syntax":"passed","execution":"interrupted","error":state["stop"]}
            if state["denied"]:
                return {**result,"status":"rejected","execution":"blocked","error":"sql_policy_denied"}
            message=str(exc).lower()
            repairable=any(x in message for x in ("syntax error","incomplete input","no such column","no such table","no such function","unrecognized token"))
            if isinstance(exc,MemoryError) or "too big"in message or "too many"in message:
                code="sql_resource_limit";repairable=False
            elif "one statement"in message:code="multiple_statements_denied";repairable=False
            elif "no such"in message:code="sql_reference_error"
            elif repairable:code="sql_syntax_error"
            else:code="sql_execution_error"
            syntax=("failed"if code=="sql_syntax_error"else"passed"if code=="sql_reference_error"or"integer overflow"in message else"not_verified")
            return {**result,"status":"error","syntax":syntax,
                    "execution":"failed","error":code,"repairable":repairable}
    finally:
        conn.close()
