"""仅压缩售后工具发给模型的重复来源，不改变保存的事实或引用原文。"""
from copy import deepcopy
import json


def compact_support_tool_view(result):
    """完整来源仍在citation_index；形状未知或证据冲突时返回原样深拷贝。

    调用方先用原结果建立结论目录，再对模型消息调用本函数；业务判断、
    原始事实、日志和导出不能改用这里的精简视图。
    """
    view = deepcopy(result)
    if not isinstance(view, dict):
        return view
    citations, index = view.get('citations'), view.get('citation_index')
    if not isinstance(citations, dict) or not citations or not isinstance(index, dict) or not index:
        return view

    compact, derived_supports = {}, {}
    for rule, source in citations.items():
        if not isinstance(rule, str) or not rule or not isinstance(source, dict):
            return view
        ref = source.get('chunk_id')
        if not isinstance(ref, str) or not ref or not isinstance(source.get('text'), str):
            return view
        canonical = index.get(ref)
        if not isinstance(canonical, dict):
            return view
        if 'citation_id' in source and source['citation_id'] != ref:
            return view
        supports = canonical.get('supports')
        if (not isinstance(supports, list) or any(not isinstance(item, str) for item in supports)
                or len(set(supports)) != len(supports)):
            return view
        if 'supports' in source and source['supports'] != supports:
            return view
        # JSON比较保留类型差异，例如true与1不能冒充同一份来源元数据。
        try:
            original = json.dumps({key: value for key, value in source.items() if key != 'supports'},
                                  ensure_ascii=False, sort_keys=True, allow_nan=False)
            indexed = json.dumps({key: value for key, value in canonical.items() if key != 'supports'},
                                 ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            return view
        if original != indexed:
            return view
        compact[rule] = {'citation_id': ref}
        derived_supports.setdefault(ref, []).append(rule)

    # supports必须确实由当前规则映射派生，不能掩盖漏规则或错误归属。
    if set(index) != set(derived_supports):
        return view
    for ref, rules in derived_supports.items():
        if set(index[ref]['supports']) != set(rules):
            return view
    view['citations'] = compact
    return view
