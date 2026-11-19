"""只加载随代码审查发布的指引；用户内容、工具结果和模型输出不能提供路径。

这是程序选择的工作指引，不是插件执行器；工具白名单和授权仍由运行时控制。
"""
from hashlib import sha256
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent / 'skills'
CATALOG = {'order_check': 'order_check.md', 'buyer_support': 'buyer_support.md'}
MAX_SKILL_BYTES = 6000
REQUIRED_SECTIONS = ('## 职责', '## 适用条件', '## 步骤', '## 输出规范', '## 边界')


def select_skill_ids(condition_state, specialist=None):
    """只消费程序已建立的业务状态，不对用户文本做文件名匹配。"""
    kind = (condition_state or {}).get('kind')
    if kind == 'support':
        return ['buyer_support']
    if kind in {'order', 'summary', 'list'} and specialist != 'policy':
        return ['order_check']
    return []


def load_skill(skill_id):
    if skill_id not in CATALOG:
        raise ValueError('unknown_trusted_skill')
    root = SKILL_ROOT
    path = root / CATALOG[skill_id]
    # 不接受符号链接跳出可信发布目录；也不接受目录、超长或缺少规范的文件。
    if root.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError('missing_or_untrusted_skill')
    if path.resolve().parent != root.resolve():
        raise ValueError('untrusted_skill_path')
    with path.open('rb') as stream:
        raw = stream.read(MAX_SKILL_BYTES + 1)
    if not raw or len(raw) > MAX_SKILL_BYTES:
        raise ValueError('invalid_skill_size')
    text = raw.decode('utf-8')
    if '\x00' in text or any(section not in text for section in REQUIRED_SECTIONS):
        raise ValueError('invalid_skill_document')
    return {'id': skill_id, 'path': f'skills/{CATALOG[skill_id]}',
            'sha256': sha256(raw).hexdigest(), 'content': text}
