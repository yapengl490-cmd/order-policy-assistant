"""可选：独立演示库的政策嵌入索引。明确确认参数才会发送政策文本并可能计费。"""
import argparse
import json
import os
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-external-embedding', action='store_true')
    args = parser.parse_args(argv)
    if not args.allow_external_embedding:
        parser.error('本操作会外发合成政策文本，需显式 --allow-external-embedding')
    if os.environ.get('DATUS_DEPLOYMENT') != 'container' or os.environ.get('DB_HOST') != 'db':
        parser.error('此入口仅用于独立演示 bootstrap 容器')
    from sqlalchemy import URL, create_engine
    from embedding_client import EmbeddingClient, EmbeddingSettings
    from policy_kb import index_policy_embeddings
    root = Path(__file__).resolve().parent
    client = EmbeddingClient(EmbeddingSettings.load(root=root))
    if not client.settings.api_key:
        parser.error('未配置嵌入密钥，不发送请求')
    engine = create_engine(URL.create('postgresql+psycopg', username='postgres',
        password=os.environ['POSTGRES_PASSWORD'], host='db', database='datus_core'),
        connect_args={'connect_timeout':5}, hide_parameters=True)
    try:
        result = index_policy_embeddings(engine, client)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'status':'failed','error_type':type(error).__name__}))
        raise SystemExit(1)
