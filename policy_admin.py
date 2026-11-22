"""受控政策维护：默认预览材料，--apply 才使用已有本机管理员连接。

此入口不注册为模型工具，也不向普通网页用户提供写权限。
不能仅靠 actor 字符串取得权限；真正执行仍受数据库账号权限约束。
"""
import argparse
import json
from pathlib import Path

from import_data import local_admin_engine
from policy_documents import parse_policy_file
from policy_lifecycle import _admin_input, correct_policy, withdraw_policy


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('withdraw', 'correct'))
    parser.add_argument('document_id')
    parser.add_argument('--expected-checksum', required=True)
    parser.add_argument('--reason', required=True, help='会显示给原报告持有人的原因；勿填写凭据、个人信息或私密审计内容')
    parser.add_argument('--actor', required=True, help='操作者审计标签，不是授权凭据')
    parser.add_argument('--replacement', type=Path, help='受控更正版文件；必须新编号/新版本、同店品类和原有效期')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    engine = None
    try:
        _admin_input(args.document_id, args.expected_checksum, args.reason, args.actor)
        if (args.action == 'correct') != (args.replacement is not None):
            raise ValueError('更正必须提供replacement，撤回不能夹带替代文件')
        replacement = parse_policy_file(args.replacement) if args.replacement else None
        if not args.apply:
            report = {'status': 'preview_only_no_database_connection', 'action': args.action,
                      'document_id': args.document_id, 'expected_checksum': args.expected_checksum,
                      'reason': args.reason, 'replacement_document_id': replacement['document_id'] if replacement else None,
                      'database_scope_and_version_validation': 'performed_in_apply_transaction',
                      'embedding_requests': 0}
        else:
            engine = local_admin_engine(Path(__file__).resolve().parent / '.env')
            kwargs = dict(expected_checksum=args.expected_checksum, reason=args.reason, actor=args.actor)
            report = (correct_policy(engine, args.document_id, args.replacement, **kwargs)
                      if args.action == 'correct' else withdraw_policy(engine, args.document_id, **kwargs))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # 驱动异常可能带连接内容，不回显；原始数据由事务保护。
        print('政策维护未完成：' + type(exc).__name__ + '；请核对原指纹、受控材料和管理员配置。')
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
