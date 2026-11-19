"""真实PG临时模式→授权快照→SQLite实际SQL；不调用模型、不接触正式业务行。"""
import argparse
from datetime import datetime
import hashlib,json,re,sys,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import text
from import_data import local_admin_engine,import_payload
from snapshot_sql import load_authorized_snapshot,execute_snapshot_sql

SQL='SELECT COUNT(*) AS order_count,COALESCE(SUM(paid_amount_fen),0) AS paid_amount_fen,COALESCE(SUM(refund_amount_fen),0) AS refund_amount_fen,COALESCE(SUM(net_amount_fen),0) AS net_amount_fen FROM order_facts'


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--env-file',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();base=Path(__file__).resolve().parents[1];engine=local_admin_engine(args.env_file)
    schema='snapshot_verify_'+uuid.uuid4().hex;isolated=engine.execution_options(schema_translate_map={'public':schema})
    report={'executor':'assistant','database':'PostgreSQL isolated schema → SQLite in-memory snapshot','schema':schema,'checks':{}}
    created=False;stage='isolated_ddl'
    try:
        with engine.begin()as c:c.execute(text(f'CREATE SCHEMA "{schema}"'))
        created=True
        for name in ['001_initial_schema.sql','003_import_batches.sql','004_shop_scope.sql','007_import_row_sources.sql','008_order_channels.sql']:
            sql=(base/'sql'/name).read_text();sql=re.sub(r'^\s*(?:BEGIN|COMMIT);\s*$','',sql,flags=re.M)
            sql=re.sub(r'^\s*GRANT .*?;\s*$','',sql,flags=re.M).replace('public.',f'"{schema}".')
            with engine.begin()as c:
                c.exec_driver_sql(f'SET LOCAL search_path TO "{schema}",pg_catalog')
                assert c.scalar(text('SELECT current_schema()'))==schema;c.exec_driver_sql(sql)
        payload=json.loads((base/'tests/fixtures/channel_comparison.json').read_text());import_payload(isolated,payload)
        times=[datetime.fromisoformat(x)for x in ['2026-10-01T00:00:00Z','2026-11-01T00:00:00Z','2026-12-03T00:00:00Z']]
        stage='authorized_freeze_and_sql'
        snapshots=[]
        for access in [{'role':'buyer','buyer_id':'B_ANALYSIS'},{'role':'merchant','shop_id':'S_ANALYSIS'}]:
            snap=load_authorized_snapshot(isolated,access,*times);snapshots.append(snap)
            assert {row[0]for row in snap.rows}=={'A','B'}
            result=execute_snapshot_sql(snap,SQL)
            assert result['status']=='ok'and result['actual']=={'order_count':2,'paid_amount_fen':15000,'refund_amount_fen':2000,'net_amount_fen':13000}
        report['checks']['buyer_and_merchant_authorized_pg_snapshot_sql']=True
        empty=load_authorized_snapshot(isolated,{'role':'merchant','shop_id':'S_OTHER'},*times)
        assert not empty.rows and execute_snapshot_sql(empty,SQL)['actual']['net_amount_fen']==0
        report['checks']['other_shop_does_not_access_current_shop']=True
        original_hash=snapshots[0].sha256
        stage='freeze_survives_later_db_change'
        with engine.begin()as c:c.execute(text(f'UPDATE "{schema}".orders SET paid_amount_fen=11000 WHERE order_id=:oid'),{'oid':'A'})
        newer=load_authorized_snapshot(isolated,{'role':'buyer','buyer_id':'B_ANALYSIS'},*times)
        assert newer.sha256!=original_hash and newer.baseline()['paid_amount_fen']==16000
        assert execute_snapshot_sql(snapshots[0],SQL)['actual']['paid_amount_fen']==15000
        report['checks']['frozen_rows_unchanged_after_new_pg_data']=True
        for sql in ["ATTACH DATABASE '/tmp/snapshot-disallowed.sqlite' AS other",'DELETE FROM order_facts','SELECT * FROM sqlite_master']:
            assert execute_snapshot_sql(snapshots[0],sql)['status']=='rejected'
        report['checks']['sql_denies_write_attach_and_catalog']=True
        report['snapshot_sha256']=original_hash;report['status']='passed'
    except Exception as exc:report.update(status='failed',error_type=type(exc).__name__,error_stage=stage)
    finally:
        if created:
            with engine.begin()as c:c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            report['cleanup']='temporary_schema_removed'
        engine.dispose();args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({k:report[k]for k in ['status','checks','error_type','error_stage','cleanup']if k in report},ensure_ascii=False))
    return 0 if report.get('status')=='passed'else 1

if __name__=='__main__':raise SystemExit(main())
