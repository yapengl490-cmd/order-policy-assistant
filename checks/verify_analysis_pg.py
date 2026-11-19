"""显式管理员验收：随机临时模式中的实际PG两期/渠道/导入来源，不调用模型。"""
import argparse
import json
from pathlib import Path
import re
import sys
import uuid
from datetime import datetime
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import event,text
from import_data import local_admin_engine,import_payload,ImportConflictError
from sales_analysis import compare_periods


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file",type=Path,required=True);parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args();base=Path(__file__).resolve().parents[1]
    engine=local_admin_engine(args.env_file);schema="analysis_verify_"+uuid.uuid4().hex
    isolated=engine.execution_options(schema_translate_map={"public":schema})
    report={"executor":"assistant","database":"PostgreSQL independent temporary schema","schema":schema,"checks":{}}
    created=False;stage="create_schema"
    try:
        with engine.begin() as c:c.execute(text(f'CREATE SCHEMA "{schema}"'))
        created=True;stage="isolated_ddl"
        for name in ["001_initial_schema.sql","003_import_batches.sql","004_shop_scope.sql","007_import_row_sources.sql","008_order_channels.sql"]:
            sql=(base/"sql"/name).read_text()
            sql=re.sub(r'^\s*(?:BEGIN|COMMIT);\s*$', '',sql,flags=re.M)
            sql=re.sub(r'^\s*GRANT .*?;\s*$', '',sql,flags=re.M).replace("public.",f'"{schema}".')
            with engine.begin() as c:
                c.exec_driver_sql(f'SET LOCAL search_path TO "{schema}", pg_catalog')
                assert c.scalar(text("SELECT current_schema()"))==schema
                c.exec_driver_sql(sql)
        with engine.connect() as c:
            names=set(c.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname=:schema"),{"schema":schema}))
            assert names=={"buyers","orders","refunds","shops","order_shops","import_batches","import_row_sources","order_channels"}
        stage="frozen_data_import"
        payload=json.loads((base/"tests/fixtures/channel_comparison.json").read_text())
        expected=json.loads((base/"tests/fixtures/channel_expected.json").read_text())
        imported=import_payload(isolated,payload)
        report["source_batch"]=imported["batch_sha256"]
        times=[datetime.fromisoformat(x) for x in ["2026-10-01T00:00:00Z","2026-11-01T00:00:00Z","2026-11-01T00:00:00Z","2026-12-01T00:00:00Z","2026-12-03T00:00:00Z"]]
        stage="same_snapshot_buyer_merchant_comparison"
        seen=[]
        def record(conn,cursor,statement,parameters,context,many):seen.append(statement)
        event.listen(isolated,"before_cursor_execute",record)
        try:
            buyer=compare_periods(isolated,{"role":"buyer","buyer_id":"B_ANALYSIS"},*times)
        finally:event.remove(isolated,"before_cursor_execute",record)
        merchant=compare_periods(isolated,{"role":"merchant","shop_id":"S_ANALYSIS"},*times)
        for result in [buyer,merchant]:
            assert result["status"]=="ok"
            for key in ["first","second","delta"]:assert result[key]==expected[key]
            by_channel={row["channel"]:row for row in result["channels"]}
            for channel,row in expected["channels"].items():
                assert by_channel[channel]["presence"]==row["presence"]
                assert by_channel[channel]["contribution_net_fen"]==row["net_delta"]
            assert result["provenance"]["status"]=="recorded"
            assert sum(row["contribution_net_fen"]for row in result["channels"])==4000
            ids={row["record_id"]for row in result["provenance"]["records"]}
            assert not ids & set(expected["excluded_refunds"]+expected["excluded_orders"])
        assert sum("LEFT OUTER JOIN"in statement.upper()for statement in seen)==1
        report["checks"]["frozen_totals_channels_boundary_source_and_single_business_query"]=True
        report["example_result"]=merchant
        stage="scope_and_validation"
        denied=compare_periods(isolated,{"role":"merchant","shop_id":"S_OTHER"},*times)
        assert {row["order_id"]for row in denied["order_evidence"]}=={"X"}
        bad=list(times);bad[2]=datetime.fromisoformat("2026-10-20T00:00:00Z")
        try:compare_periods(isolated,{"role":"buyer","buyer_id":"B_ANALYSIS"},*bad);raise AssertionError("overlap accepted")
        except ValueError:pass
        repeated=import_payload(isolated,payload);assert repeated["status"]=="already_imported"
        payload["order_channels"][0]["channel"]="silently-changed"
        try:import_payload(isolated,payload);raise AssertionError("channel overwritten")
        except ImportConflictError:pass
        report["checks"]["cross_shop_overlap_repeat_and_channel_conflict"]=True
        report["status"]="passed"
    except Exception as exc:
        report.update(status="failed",error_type=type(exc).__name__,error_stage=stage)
    finally:
        if created:
            with engine.begin() as c:c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            report["cleanup"]="temporary_schema_removed"
        engine.dispose();args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({k:report[k]for k in ["status","checks","error_type","error_stage","cleanup"]if k in report},ensure_ascii=False))
    return 0 if report.get("status")=="passed"else 1

if __name__=="__main__":raise SystemExit(main())
