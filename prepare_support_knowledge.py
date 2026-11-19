"""显式恢复受控售后资料；默认只验文件，--apply才导入，--index才请求嵌入服务。"""
import argparse
import json
from pathlib import Path
from import_data import import_payload, local_admin_engine, load_payload, prepare_payload
from import_support_data import import_support_payload, prepare_support_payload
from policy_documents import parse_policy_file
from policy_kb import import_policy_files, index_policy_embeddings
from embedding_client import EmbeddingClient, EmbeddingSettings

ROOT=Path(__file__).resolve().parent

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--index',action='store_true')
    parser.add_argument('--env-file',type=Path,default=ROOT/'.env')
    args=parser.parse_args(argv)
    if args.index and not args.apply:
        parser.error('--index必须同时明确--apply')
    try:
        business=load_payload(ROOT/'samples/support/business.json')
        facts=load_payload(ROOT/'samples/support/support_facts.json')
        prepare_payload(business);prepare_support_payload(facts)
        files=[ROOT/'knowledge/2026-10-shoes.md',ROOT/'knowledge/2026-11-shoes.md']
        documents=[parse_policy_file(path) for path in files]
        report={'executor':'assistant_or_explicit_local_operator','source_kind':'synthetic_demo',
                'documents':len(documents),'chunks':sum(len(d['chunks']) for d in documents)}
        if args.apply:
            engine=local_admin_engine(args.env_file)
            try:
                report['business']=import_payload(engine,business)
                report['support']=import_support_payload(engine,facts,allowed_shop_id='S001')
                report['policies']=import_policy_files(engine,files)
                if args.index:
                    report['embedding']=index_policy_embeddings(engine,EmbeddingClient(EmbeddingSettings.load(root=ROOT)))
            finally:engine.dispose()
            report['status']='imported_and_checked'
        else:report['status']='files_validated_only_no_database_or_network'
        print(json.dumps(report,ensure_ascii=False,indent=2));return 0
    except Exception as exc:
        # 数据库/API错误消息可能含配置，控制台仅显示类别，不伪装成功。
        print(json.dumps({'status':'failed','error_type':type(exc).__name__,
                          'message':'未完成；保留已提交步骤，可检查迁移、文件与本机配置后幂等重试。'},ensure_ascii=False))
        return 1

if __name__=='__main__':raise SystemExit(main())
