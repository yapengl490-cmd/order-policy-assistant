"""实际PostgreSQL独立随机schema验收；不调用模型/嵌入，不改public业务数据。"""
import argparse
import copy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text
from buyer_support_service import check_buyer_support, get_support_context, order_support_facts, refund_reason_records
from import_data import local_admin_engine, import_payload, ImportConflictError, ImportValidationError
from import_support_data import import_support_payload
from policy_kb import import_policy_files, select_policy
from refund_investigation import investigate_refund_increase


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file",type=Path,required=True);parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(argv);base=Path(__file__).resolve().parents[1]
    engine=local_admin_engine(args.env_file);schema="support_verify_"+uuid.uuid4().hex
    isolated=engine.execution_options(schema_translate_map={"public":schema})
    report={"executor":"assistant","database":"PostgreSQL independent temporary schema","schema":schema,
            "model_requests":0,"embedding_requests":0,"checks":{},"source_proof":"declared_only"}
    created=False;stage="create_schema"
    try:
        with engine.begin() as c:c.execute(text(f'CREATE SCHEMA "{schema}"'))
        created=True;stage="isolated_migrations"
        names=["001_initial_schema.sql","003_import_batches.sql","004_shop_scope.sql","007_import_row_sources.sql","008_order_channels.sql","009_policy_knowledge.sql","010_support_facts.sql"]
        report["migrations"]={name:hashlib.sha256((base/"sql"/name).read_bytes()).hexdigest()for name in names}
        for name in names:
            sql=(base/"sql"/name).read_text()
            sql=re.sub(r'^\s*(?:BEGIN|COMMIT);\s*$', '',sql,flags=re.M)
            sql=re.sub(r'^\s*GRANT .*?;\s*$', '',sql,flags=re.M).replace("public.",f'"{schema}".')
            with engine.begin() as c:
                c.exec_driver_sql(f'SET LOCAL search_path TO "{schema}", pg_catalog')
                assert c.scalar(text("SELECT current_schema()"))==schema
                c.exec_driver_sql(sql)
        stage="seed_and_new_business"
        initial={"metadata":{"source":"独立PG旧O001夹具，不是正式记录","version":"support-check-v1","currency":"CNY","normalized":True,"data_kind":"normalized_sample"},
            "buyers":[{"buyer_id":"B001","display_name":"独立核验买家"}],"orders":[{"order_id":"O001","buyer_id":"B001","paid_at":"2026-10-15T10:00:00+08:00","paid_amount_fen":12500,"status":"paid"}],
            "refunds":[],"shops":[],"order_shops":[{"order_id":"O001","shop_id":"S001"}]}
        import_payload(isolated,initial)
        business=json.loads((base/"samples/support/business.json").read_text())
        support=json.loads((base/"samples/support/support_facts.json").read_text())
        expected=json.loads((base/"samples/support/expected.json").read_text())
        report["business_import"]=import_payload(isolated,business)
        report["support_import"]=import_support_payload(isolated,support,allowed_shop_id="S001")
        stage="actual_policy_import"
        policy_paths=[base/"knowledge/2026-10-shoes.md",base/"knowledge/2026-11-shoes.md"]
        report["policy_import"]=import_policy_files(isolated,policy_paths)
        report["policy_documents"]={path.name:hashlib.sha256(path.read_bytes()).hexdigest()for path in policy_paths}
        assert import_policy_files(isolated,policy_paths)["status"]=="unchanged"
        now=datetime.fromisoformat(expected["now"]);claims=expected["r06"]["claims"]
        stage="frozen_support_cases"
        results={}
        for identity in ("O_SUPPORT_OK","O001","O_SUPPORT_UNKNOWN","O_SUPPORT_CUSTOM"):
            result=check_buyer_support(isolated,identity,buyer_id="B001",claims=claims,now=now)
            results[identity]=result
            for key,value in expected["r06"][identity].items():
                assert (result["shipping"]["payer"]if key=="payer"else result[key])==value,(identity,key)
        assert results["O001"]["policy_version"]=="2026-10-v1"
        assert results["O_SUPPORT_OK"]["policy_version"]=="2026-11-v1"
        assert results["O_SUPPORT_OK"]["citations"]
        report["checks"]["normal_expired_missing_custom_payment_version_and_original_citations"]=True
        stage="boundaries_and_claims"
        deadline=datetime.fromisoformat(results["O_SUPPORT_OK"]["return_window"]["before"])
        for moment,eligible in ((deadline-timedelta(microseconds=1),True),(deadline,False)):
            got=check_buyer_support(isolated,"O_SUPPORT_OK",buyer_id="B001",claims=claims,now=moment)
            assert (got["eligibility"]=="eligible_with_self_report")==eligible
        missing=check_buyer_support(isolated,"O_SUPPORT_OK",buyer_id="B001",claims={},now=now)
        assert missing["status"]=="clarification"and set(missing["missing"])=={"is_unworn","tags_intact"}
        shipping=check_buyer_support(isolated,"O_SUPPORT_UNKNOWN",buyer_id="B001",claims={"reason":"size"},now=now,topics=["shipping"])
        assert shipping["eligibility"]=="not_requested"and shipping["shipping"]["payer"]=="buyer"
        responsibility=check_buyer_support(isolated,"O_SUPPORT_OK",buyer_id="B001",claims={**claims,"reason":"merchant_responsibility"},now=now)
        assert responsibility["status"]=="insufficient_evidence"and responsibility["shipping"]["payer"]is None
        report["checks"]["exclusive_end_self_report_distinction_and_unproven_responsibility"]=True
        stage="scope_and_import_replay"
        for access in ({"buyer_id":"B404"},{"shop_id":"S404"}):
            denied=get_support_context(isolated,"O_SUPPORT_OK",**access)
            assert denied["status"]=="not_found"and "trusted_facts"not in denied and "order"not in denied
        assert import_payload(isolated,business)["status"]=="already_imported"
        assert import_support_payload(isolated,support,allowed_shop_id="S001")["status"]=="already_imported"
        changed=copy.deepcopy(support);changed["order_support_facts"][0]["is_custom"]=True
        try:import_support_payload(isolated,changed,allowed_shop_id="S001");raise AssertionError("conflict accepted")
        except ImportConflictError:pass
        try:import_support_payload(isolated,support,allowed_shop_id="S404");raise AssertionError("cross-shop import accepted")
        except ImportValidationError:pass
        assert check_buyer_support(isolated,"O001",buyer_id="B001",claims=claims,now=now)["trusted_facts"]["is_custom"]is False
        report["checks"]["buyer_shop_isolation_idempotency_conflict_atomicity"]=True
        stage="isolated_incomplete_and_future_fact_variations"
        # 仅改本脚本临时schema，验证已确定的排除条件优先于不再需要的未知项。
        with isolated.begin() as c:
            c.execute(order_support_facts.update().where(order_support_facts.c.order_id=="O_SUPPORT_CUSTOM").values(delivered_at=None))
            c.execute(order_support_facts.update().where(order_support_facts.c.order_id=="O_SUPPORT_OK").values(recorded_at=now+timedelta(seconds=1)))
        known=check_buyer_support(isolated,"O_SUPPORT_CUSTOM",buyer_id="B001",claims={},now=now)
        assert known["eligibility"]=="ineligible" and known["failed_conditions"]==["custom_product_excluded"]
        future=check_buyer_support(isolated,"O_SUPPORT_OK",buyer_id="B001",claims=claims,now=now)
        assert future["status"]=="data_error" and future["eligibility"]=="undetermined"
        report["checks"]["known_exclusion_with_missing_receipt_and_future_record_not_accepted"]=True
        stage="same_snapshot_refund_investigation"
        times=[datetime.fromisoformat(expected["r05"]["conditions"][key])for key in ("first_from","first_before","second_from","second_before","refund_before")]
        investigate=investigate_refund_increase(isolated,{"role":"merchant","shop_id":"S001"},*times,observed_at=now)
        assert investigate["status"]=="ok"
        for period in ("first","second"):assert investigate["comparison"][period]==expected["r05"][period]
        assert investigate["total_refund_delta_fen"]==3000 and investigate["verified_size_delta_fen"]==2000
        assert sum(item["delta_refund_amount_fen"]for item in investigate["reason_breakdown"])==3000
        assert investigate["causality_verified"]is False
        denied=investigate_refund_increase(isolated,{"role":"merchant","shop_id":"S404"},*times,observed_at=now)
        assert denied["refund_evidence"]==[]
        report["checks"]["postgres_repeatable_read_frozen_amounts_reason_coverage_cross_scope"]=True
        report["example_support"]=results;report["example_investigation"]=investigate
        report["status"]="passed"
    except Exception as exc:
        report.update(status="failed",error_type=type(exc).__name__,error_stage=stage)
    finally:
        if created:
            try:
                with engine.begin() as c:c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                report["cleanup"]="temporary_schema_removed"
            except Exception as exc:
                report.update(status="failed",cleanup="failed",cleanup_error_type=type(exc).__name__)
        engine.dispose();args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({k:report[k]for k in ("status","checks","error_type","error_stage","cleanup")if k in report},ensure_ascii=False))
    return 0 if report.get("status")=="passed"else 1


if __name__=="__main__":raise SystemExit(main())
