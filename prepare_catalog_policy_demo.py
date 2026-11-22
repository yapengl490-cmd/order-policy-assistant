"""显式导入合成商品政策；默认只检查材料，不连接数据库或调用模型。

沿用已有政策表与管理员入口。导入只追加新编号，同内容可重跑，冲突拒绝；
不修改订单、账号、旧政策或本机开关。应用只读账号仍由原初始化流程准备。
"""
import argparse
import json
from pathlib import Path

from import_data import local_admin_engine
from policy_documents import parse_policy_file
from policy_kb import import_policy_files
from catalog_recommendations import CatalogSnapshot

ROOT = Path(__file__).resolve().parent


def validate_materials(root=ROOT):
    root = Path(root)
    policy_path = root / 'knowledge/2026-11-catalog-tshirts.md'
    doc = parse_policy_file(policy_path)
    snapshot = CatalogSnapshot.load(root / 'samples/catalog_policy_demo.json')
    if (doc['store_id'] != 'DEMO-SHOP-A' or doc['category'] != 't_shirt'
            or snapshot.data().get('data_kind') != 'synthetic_demo'):
        raise ValueError('演示材料范围或来源不符，未导入')
    return {'document_id': doc['document_id'], 'policy_sha256': doc['checksum'],
            'catalog_sha256': snapshot.sha256, 'scope': 'DEMO-SHOP-A/t_shirt',
            'source_kind': 'synthetic_demo', 'model_calls': 0}, policy_path


def prepare_demo(engine, root=ROOT):
    report, path = validate_materials(root)
    return {**report, 'import': import_policy_files(engine, [path])}


def main():
    parser = argparse.ArgumentParser(description='合成商品政策；默认检查，--apply才写入已有政策库')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    engine = None
    try:
        report, _ = validate_materials()
        if args.apply:
            engine = local_admin_engine(ROOT / '.env')
            report = prepare_demo(engine)
        else:
            report['status'] = 'checked_only_no_database_connection'
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # 不将管理员连接参数或驱动错误回显到终端。
        print('材料检查或导入未完成：' + type(exc).__name__ + '；旧数据未被覆盖，请核对受控材料和数据库。')
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
