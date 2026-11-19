"""管理员显式运行：仅新建/删除随机临时模式，不操作正式public记录。

用法：.venv/bin/python -B checks/verify_import_pg.py --env-file /本机/项目/.env --output /证据/文件.json
不打印连接串。脚本不会调用模型；不修改已有表/金额/店铺归属。
"""
import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import threading
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import event, text
from import_data import import_payload, local_admin_engine, ImportConflictError, ImportExecutionError
from merchant_service import check_shop_order
from refund_service import check_order


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    base=Path(__file__).resolve().parents[1]
    schema="import_verify_"+uuid.uuid4().hex
    engine=local_admin_engine(args.env_file)
    test_engine=engine.execution_options(schema_translate_map={"public":schema})
    report={"executor":"assistant","database":"PostgreSQL independent temporary schema", "schema":schema,"checks":{}}
    created=False
    stage="create_temporary_schema"
    def count_all():
        with engine.connect() as c:
            return {name:c.scalar(text(f'SELECT COUNT(*) FROM "{schema}".{name}')) for name in
                ("buyers","orders","refunds","shops","order_shops","import_batches","import_row_sources")}
    try:
        with engine.begin() as c:c.execute(text(f'CREATE SCHEMA "{schema}"'))
        created=True
        stage="isolated_ddl"
        for name in ["001_initial_schema.sql","003_import_batches.sql","004_shop_scope.sql","007_import_row_sources.sql"]:
            sql=(base/"sql"/name).read_text()
            # 本脚本不授予正式角色临时模式权限，完整正式迁移授权由部署层单独核查。
            sql=re.sub(r'^\s*(?:BEGIN|COMMIT);\s*$', '',sql,flags=re.M)
            sql=re.sub(r'^\s*GRANT .*?;\s*$', '',sql,flags=re.M)
            sql=sql.replace("public.",f'"{schema}".')
            with engine.begin() as c:
                # 初始迁移含未限定表名；显式限定搜索路径，禁止落到public。
                c.exec_driver_sql(f'SET LOCAL search_path TO "{schema}", pg_catalog')
                assert c.scalar(text("SELECT current_schema()")) == schema
                c.exec_driver_sql(sql)
        with engine.connect() as c:
            names=set(c.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname=:schema"), {"schema":schema}))
            assert names == {"buyers","orders","refunds","shops","order_shops","import_batches","import_row_sources"},names
        stage="import_and_scope"
        payload=json.loads((base/"samples/import_demo.json").read_text())
        order_id=payload["orders"][0]["order_id"];buyer_id=payload["buyers"][0]["buyer_id"]
        payload.update(shops=[{"shop_id":"S_IMPORT","name":"独立PG导入验收"}],
                       order_shops=[{"order_id":order_id,"shop_id":"S_IMPORT"}])
        cutoff=datetime.fromisoformat("2026-12-01T00:00:00Z")
        first=import_payload(test_engine,payload)
        merchant=check_shop_order(test_engine,order_id,"S_IMPORT",cutoff)
        buyer=check_order(test_engine,order_id,buyer_id,cutoff)
        assert (merchant["paid_amount_fen"],merchant["refund_amount_fen"],merchant["net_amount_fen"])==(10000,3000,7000)
        assert merchant["provenance"]["status"]==buyer["provenance"]["status"]=="recorded"
        assert check_shop_order(test_engine,order_id,"S_DENIED",cutoff)["status"]=="not_found"
        before=count_all(); repeated=import_payload(test_engine,payload); assert before==count_all()
        conflict=copy.deepcopy(payload);conflict["order_shops"][0]["shop_id"]="S001"
        try:import_payload(test_engine,conflict);raise AssertionError("reassignment accepted")
        except ImportConflictError:pass
        assert before==count_all()
        report["checks"]["import_to_buyer_and_shop_query_source"]={"passed":True,"net_fen":7000,"source_batch":first["batch_sha256"]}
        report["checks"]["repeat_and_cross_shop_and_reassign_rejection"]=True
        stage="late_source_failure"
        # 来源最后一步故障：此前的业务行/归属/批次必须一起回滚。
        with engine.begin() as c:
            c.exec_driver_sql(f'''CREATE FUNCTION "{schema}".reject_source() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'controlled provenance failure'; END; $$;
                CREATE TRIGGER reject_source BEFORE INSERT ON "{schema}".import_row_sources FOR EACH ROW EXECUTE FUNCTION "{schema}".reject_source();''')
        fresh=copy.deepcopy(payload);fresh["metadata"]["version"]="late-failure"
        fresh["buyers"][0]["buyer_id"]="B_LATE"
        fresh["orders"][0].update(order_id="O_LATE",buyer_id="B_LATE")
        for i,r in enumerate(fresh["refunds"]):r.update(order_id="O_LATE",refund_id=f"R_LATE_{i}")
        fresh["order_shops"][0]["order_id"]="O_LATE"
        before=count_all()
        try:import_payload(test_engine,fresh);raise AssertionError("late failure accepted")
        except ImportExecutionError:pass
        assert before==count_all()
        with engine.begin() as c:
            c.exec_driver_sql(f'DROP TRIGGER reject_source ON "{schema}".import_row_sources; DROP FUNCTION "{schema}".reject_source();')
        report["checks"]["late_source_failure_rolls_back_all_seven_tables"]=True
        stage="concurrent_refund_protection"
        # 真实两个并发事务在读取累计值后会合，不能各自成功造成超额。
        barrier=threading.Barrier(2)
        waited=threading.local()
        def meet_after_total(conn,cursor,statement,parameters,context,executemany):
            if "sum(" in statement.lower() and "refund_amount_fen" in statement and not getattr(waited,"done",False):
                waited.done=True;barrier.wait(timeout=15)
        event.listen(test_engine,"after_cursor_execute",meet_after_total)
        def concurrent(index):
            item=copy.deepcopy(payload)
            item["metadata"]["version"]=f"parallel-{index}"
            item["buyers"]=[];item["orders"]=[];item["shops"]=[];item["order_shops"]=[]
            item["refunds"]=[{"refund_id":f"R_PARALLEL_{index}","order_id":order_id,"refunded_at":"2026-11-20T00:00:00Z","refund_amount_fen":4000,"status":"succeeded"}]
            try:return {"result":"imported","batch":import_payload(test_engine,item)["batch_sha256"]}
            except ImportExecutionError:return {"result":"database_conflict"}
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:outcomes=list(pool.map(concurrent,[1,2]))
        finally:event.remove(test_engine,"after_cursor_execute",meet_after_total)
        assert sorted(x["result"] for x in outcomes)==["database_conflict","imported"],outcomes
        after=check_order(test_engine,order_id,buyer_id,cutoff)
        assert after["refund_amount_fen"]==7000 and after["net_amount_fen"]==3000
        report["checks"]["concurrent_refund_serializable_protection"]={"passed":True,"outcomes":outcomes,"final_refund_fen":7000}
        report["status"]="passed"
    except Exception as exc:
        report["status"]="failed";report["error_type"]=type(exc).__name__
        report["error_stage"]=stage
        # 只保存类型/受控阶段名，不打印SQLAlchemy异常及SQL参数。
    finally:
        if created:
            with engine.begin() as c:c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            report["cleanup"]="temporary_schema_removed"
        engine.dispose()
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({"status":report.get("status"),"checks":list(report["checks"]),"cleanup":report.get("cleanup"), "error_type":report.get("error_type"),"error_stage":report.get("error_stage")},ensure_ascii=False))
    return 0 if report.get("status")=="passed" else 1

if __name__=="__main__":raise SystemExit(main())
