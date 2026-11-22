"""Read current policy notices beside immutable task evidence; never rewrite reports."""
from datetime import datetime, timezone
import re


def task_policy_references(record):
    """Only tool facts' controlled-policy citation metadata supplies references.

    Model prose, requests, and trace instructions cannot add lookup authority.
    Authorization of the owning task must happen before calling this function.
    """
    references = {}
    facts = (record.get('result') or {}).get('facts', [])
    pending = [item.get('result', {}) for item in facts if isinstance(item, dict)]
    visited = 0
    while pending:
        visited += 1
        if visited > 20000:
            raise ValueError('policy_reference_scan_limit')
        value = pending.pop()
        if isinstance(value, dict):
            identity, digest = value.get('document_id'), value.get('document_checksum')
            if (value.get('source_kind') == 'synthetic_demo' and isinstance(identity, str)
                    and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', identity)
                    and isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest)):
                references.setdefault(identity, set()).add(digest)
                if len(references) > 100:
                    raise ValueError('policy_reference_count_limit')
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return references


def review_task_policies(engine, record):
    """Current advisory only. A clear status is not a fresh semantic verification."""
    result = {'task_id': record['id'], 'checked_at': datetime.now(timezone.utc).isoformat(),
              'original_report_preserved': True, 'sources': []}
    try:
        references = task_policy_references(record)
        if not references:
            return {**result, 'status': 'not_applicable',
                    'message': '本任务没有可核对的本地受控政策引用；未据此认定其他来源仍然有效。'}
        from policy_lifecycle import get_policy_lifecycle
        states = get_policy_lifecycle(engine, list(references))
        for identity, hashes in sorted(references.items()):
            state = states[identity]
            status = state['status']
            if status == 'active' and hashes != {state.get('document_checksum')}:
                status = 'invalid_policy'
            # 操作者身份不属于普通读者需要的内容，不下发审计actor。
            result['sources'].append({k: state.get(k) for k in
                ('document_id', 'version', 'reason', 'recorded_at', 'replacement_document_id')}
                | {'status': status})
        changed = any(row['status'] != 'active' for row in result['sources'])
        return {**result, 'status': 'warning' if changed else 'clear',
                'message': ('本报告所用部分政策已撤回、更正或无法核实。原回答和原依据保留，请重新核查相关资格与运费结论；这不表示订单事实全部错误。'
                            if changed else '所引用本地政策的当前登记状态未失效；这是来源状态核对，不是重新验证整份回答。')}
    except Exception as exc:
        return {**result, 'status': 'unavailable', 'error_type': type(exc).__name__,
                'message': '暂时无法核对政策当前状态，不能认定旧依据仍有效。原报告未修改，可稍后重新核对。'}


def has_policy_references(record):
    try:
        return bool(task_policy_references(record))
    except (ValueError, TypeError):
        return True  # 让独立核对入口明确报告无法检查，不伪装成没有引用。
