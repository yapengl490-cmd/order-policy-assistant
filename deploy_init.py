"""独立容器验证库首次准备；已有业务数据不重灌、不清库，不使用正式Mac配置。"""
import json
import os
from pathlib import Path
import re
import uuid
import psycopg
from psycopg import sql
from mcp_credentials import prepare_mcp_credentials
ROOT=Path(__file__).resolve().parent

def seed_demo_data():
    """仅独立 bootstrap 管理进程写入随仓库分发的合成样本，不请求模型/嵌入。"""
    from sqlalchemy import URL, create_engine
    from import_data import import_payload
    from import_support_data import import_support_payload
    from policy_kb import import_policy_files
    engine = create_engine(URL.create('postgresql+psycopg', username='postgres',
        password=os.environ['POSTGRES_PASSWORD'], host='db', database='datus_core'),
        connect_args={'connect_timeout': 5}, hide_parameters=True)
    try:
        import_payload(engine, json.loads((ROOT/'samples/support/business.json').read_text()))
        import_support_payload(engine, json.loads((ROOT/'samples/support/support_facts.json').read_text()),
                               allowed_shop_id='S001')
        import_policy_files(engine, [ROOT/'knowledge/2026-10-shoes.md', ROOT/'knowledge/2026-11-shoes.md',
                                     ROOT/'knowledge/2026-11-catalog-tshirts.md'])
    finally:
        engine.dispose()


def prepare():
    if os.environ.get('DATUS_DEPLOYMENT')!='container' or os.environ.get('DB_HOST')!='db':
        raise ValueError('只允许显式独立容器初始化')
    required=['POSTGRES_PASSWORD','DB_PASSWORD','MEMORY_DB_PASSWORD','VERIFY_ACCESS_TOKEN']
    if any(not os.environ.get(k) for k in required):raise ValueError('缺独立验证配置，未初始化')
    with psycopg.connect(host='db',dbname='datus_core',user='postgres',password=os.environ['POSTGRES_PASSWORD'],connect_timeout=5) as con:
        for role,key in [('datus_reader','DB_PASSWORD'),('datus_memory','MEMORY_DB_PASSWORD')]:
            if not con.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,)).fetchone():
                con.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT').format(sql.Identifier(role),sql.Literal(os.environ[key])))
            flags=con.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=%s',(role,)).fetchone()
            membership=con.execute('SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=%s)',(role,)).fetchone()
            if any(flags) or membership:raise ValueError('已有验证角色权限超出范围，未继续初始化')
        con.execute('CREATE TABLE IF NOT EXISTS public.verify_migrations (name TEXT PRIMARY KEY, sha256 TEXT NOT NULL)')
        import hashlib
        migrations=['001_initial_schema.sql','002_demo_data.sql','003_import_batches.sql','004_shop_scope.sql','006_preferences.sql','007_import_row_sources.sql','008_order_channels.sql','009_policy_knowledge.sql','010_support_facts.sql']
        for name in migrations:
            path=ROOT/'sql'/name
            if not path.is_file():raise ValueError('缺必要迁移文件：'+name)
            body=path.read_text();digest=hashlib.sha256(body.encode()).hexdigest()
            done=con.execute('SELECT sha256 FROM public.verify_migrations WHERE name=%s',(name,)).fetchone()
            if done:
                if done[0]!=digest:raise ValueError('已应用迁移内容变化，需人工核对：'+name)
                continue
            # 由外层事务统一提交；保留每份原SQL及其摘要，不重复执行已应用迁移。
            body=re.sub(r'^\s*(BEGIN|COMMIT);\s*$', '',body,flags=re.MULTILINE)
            con.execute(body)
            con.execute('INSERT INTO public.verify_migrations(name,sha256) VALUES(%s,%s)',(name,digest))
        con.execute('GRANT CONNECT ON DATABASE datus_core TO datus_reader,datus_memory')
        con.execute('GRANT USAGE ON SCHEMA public TO datus_reader')
        con.execute('GRANT SELECT ON ALL TABLES IN SCHEMA public TO datus_reader')
        con.execute('GRANT USAGE ON SCHEMA app_memory TO datus_memory')
        con.execute('GRANT SELECT,INSERT,UPDATE ON app_memory.preferences TO datus_memory')
    seed_demo_data()
    local=ROOT/'local';local.mkdir(mode=0o700,exist_ok=True)
    path=local/'access.json'
    if not path.exists():
        with path.open('x') as f:
            os.chmod(path,0o600)
            json.dump({'scope':'isolated_container_verify','accounts':[{'label':'独立容器验证','account_id':'verify_'+uuid.uuid4().hex,'token':os.environ['VERIFY_ACCESS_TOKEN'],'roles':[{'role':'buyer','buyer_id':'B001'},{'role':'merchant','shop_id':'S001'}]}]},f,ensure_ascii=False)
    elif json.loads(path.read_text())['accounts'][0]['token']!=os.environ['VERIFY_ACCESS_TOKEN']:
        raise ValueError('验证账号与已有卷不一致；未覆盖账号或数据')
    prepare_mcp_credentials(ROOT)
    print('独立验证库与范围凭据已准备；已存在数据/密码/账号不重设。')
if __name__=='__main__':
    try:prepare()
    except Exception as e:
        print('独立验证初始化未完成：'+type(e).__name__)
        raise SystemExit(1)
