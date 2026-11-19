"""真实PG验证仓库合成样例，只在随机临时schema中导入；不改正式数据/账号绑定。"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import text
from import_data import import_payload,local_admin_engine
from merchant_service import check_shop_order,summary_shop_orders


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('证据文件已存在，未覆盖；请使用新文件名')
    base=Path(__file__).resolve().parents[1]
    schema='uci_verify_'+uuid.uuid4().hex
    engine=local_admin_engine(args.env_file)
    isolated=engine.execution_options(schema_translate_map={'public':schema})
    report={'executor':'assistant','database':'PostgreSQL independent temporary schema','schema':schema,
            'classification':'仓库独立合成订单；不含真实发票行，不是UCI源资料或支付平台验证','checks':{}}
    created=False;stage='initial_read'
    def formal_fingerprints():
        with engine.connect() as connection:
            result={}
            for table,key in [('buyers','buyer_id'),('orders','order_id'),('refunds','refund_id'),('order_shops','order_id')]:
                rows=[dict(r) for r in connection.execute(text(f'SELECT * FROM public.{table} ORDER BY {key}')).mappings()]
                result[table]={'rows':len(rows),'sha256':hashlib.sha256(json.dumps(rows,sort_keys=True,default=str).encode()).hexdigest()}
            return result
    def counts():
        with engine.connect() as connection:
            return {name:connection.scalar(text(f'SELECT count(*) FROM "{schema}".{name}')) for name in
                    ['buyers','orders','refunds','shops','order_shops','import_batches','import_row_sources']}
    try:
        before=formal_fingerprints();report['formal_before']=before
        stage='create_temporary_schema'
        with engine.begin() as connection:connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        created=True;stage='isolated_ddl'
        for filename in ['001_initial_schema.sql','003_import_batches.sql','004_shop_scope.sql','007_import_row_sources.sql']:
            sql=(base/'sql'/filename).read_text()
            sql=re.sub(r'^\s*(?:BEGIN|COMMIT);\s*$','',sql,flags=re.M)
            sql=re.sub(r'^\s*GRANT .*?;\s*$','',sql,flags=re.M)
            sql=sql.replace('public.',f'"{schema}".')
            with engine.begin() as connection:
                connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}", pg_catalog')
                assert connection.scalar(text('SELECT current_schema()'))==schema
                connection.exec_driver_sql(sql)
        stage='public_projection_import'
        payload=json.loads((base/'samples/uci/uci_projection.json').read_text())
        expected=json.loads((base/'samples/uci/uci_expected.json').read_text())
        imported=import_payload(isolated,payload)
        report['import_result']=imported
        cutoff=datetime.fromisoformat(expected['cutoff'])
        checked=[]
        stage='money_provenance_and_shop_scope'
        for row in expected['orders']:
            actual=check_shop_order(isolated,row['order_id'],'S_UCI_DEMO',cutoff)
            assert actual['status']=='ok'
            assert (actual['paid_amount_fen'],actual['refund_amount_fen'],actual['net_amount_fen'])==(row['paid_fen'],row['refund_fen'],row['net_fen'])
            assert actual['provenance']['status']=='recorded'
            assert actual['provenance']['batches']
            assert all(batch['batch_sha256']==imported['batch_sha256'] and batch['source']==payload['metadata']['source'] for batch in actual['provenance']['batches'])
            denied=check_shop_order(isolated,row['order_id'],'S001',cutoff)
            assert denied['status']=='not_found' and 'paid_amount_fen' not in denied
            assert row['order_id'].startswith('O_PUBLIC_SYNTH_')
            checked.append({'order_id':row['order_id'],'expected':row,'actual':actual,'original_S001_denied':True})
        summary=summary_shop_orders(isolated,'S_UCI_DEMO',datetime.fromisoformat('2026-10-01T00:00:00+08:00'),datetime.fromisoformat('2026-11-01T00:00:00+08:00'),cutoff)
        assert summary['status']=='ok'
        assert (summary['paid_amount_fen'],summary['refund_amount_fen'],summary['net_amount_fen'])==(expected['total_paid_fen'],expected['total_refund_fen'],expected['total_net_fen'])
        report['checks']['orders']=checked;report['checks']['summary']=summary
        stage='repeat_import'
        original_counts=counts();repeated=import_payload(isolated,payload)
        assert counts()==original_counts
        assert all(value==0 for value in repeated['inserted_counts'].values())
        report['checks']['repeat_skip']={'passed':True,'counts_unchanged':True,'result':repeated}
        report['status']='passed'
    except Exception as exc:
        report.update(status='failed',error_type=type(exc).__name__,error_stage=stage)
    finally:
        if created:
            try:
                with engine.begin() as connection:connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                with engine.connect() as connection:
                    assert not connection.scalar(text('SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:schema)'),{'schema':schema})
                report['cleanup']='temporary_schema_removed'
            except Exception as exc:
                report.update(status='failed',cleanup='unconfirmed',cleanup_error_type=type(exc).__name__)
        try:
            after=formal_fingerprints();report['formal_after']=after
            report['checks']['formal_data_unchanged']=report.get('formal_before')==after
            if not report['checks']['formal_data_unchanged']:report['status']='failed'
        except Exception as exc:
            report.update(status='failed',formal_comparison_error_type=type(exc).__name__)
        engine.dispose()
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({key:report.get(key) for key in ('status','error_stage','error_type','cleanup')},ensure_ascii=False))
    return 0 if report.get('status')=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
