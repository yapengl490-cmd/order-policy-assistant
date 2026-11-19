"""按可信账号及角色保存显式偏好；不读取任务历史，不创建表或修改业务库。

source 只供审计查看，绝不能拼入模型指令。表由管理员执行 sql/006_preferences.sql
建立。删除保留递增版本及空值墓碑，防止旧窗口把已删除的偏好重新写回。
"""

import re
from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, CheckConstraint, Column, DateTime,
                        Engine, MetaData, String, Table, insert, select, update)
from sqlalchemy.exc import IntegrityError


class PreferenceValidationError(ValueError):
    """偏好输入不在允许的有限字段内。错误消息不包含用户原文。"""


class PreferenceConflict(Exception):
    """版本已经变化；调用者必须重新读取并让用户确认新修改。"""


_ACCOUNT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z", re.ASCII)
_ROLES = frozenset({"buyer", "merchant"})
_ALLOWED_VALUES = {
    "amount_format": frozenset({"yuan", "yuan_fixed2", "fen"}),
    "answer_style": frozenset({"brief", "standard", "detailed"}),
}
_SOURCE_KINDS = frozenset({"user_settings", "user_chat", "deletion"})
_MAX_VERSION = 2**63 - 1

preference_metadata = MetaData(schema="app_memory")
preferences = Table(
    "preferences", preference_metadata,
    Column("account_id", String(128), primary_key=True),
    Column("role", String(16), primary_key=True),
    Column("values", JSON, nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("source", JSON, nullable=False),
    CheckConstraint("role IN ('buyer', 'merchant')", name="preferences_role_valid"),
    CheckConstraint("version >= 0", name="preferences_version_nonnegative"),
)


def validate_values(values: dict) -> dict:
    """严格枚举校验并复制；空字典表示未保存任何偏好，不补默认值。"""
    if type(values) is not dict or len(values) > len(_ALLOWED_VALUES):
        raise PreferenceValidationError("偏好必须是最多两个允许字段组成的对象")
    validated = {}
    for key, value in values.items():
        if type(key) is not str or key not in _ALLOWED_VALUES:
            raise PreferenceValidationError("偏好包含未支持的字段")
        if type(value) is not str or value not in _ALLOWED_VALUES[key]:
            raise PreferenceValidationError("偏好字段值不在允许范围内")
        validated[key] = value
    return validated


def _validate_identity(account_id: str, role: str) -> None:
    if type(account_id) is not str or not _ACCOUNT_ID.fullmatch(account_id):
        raise PreferenceValidationError("可信账号标识格式不合法")
    if type(role) is not str or role not in _ROLES:
        raise PreferenceValidationError("角色必须是 buyer 或 merchant")


def _validate_version(expected_version: int) -> None:
    if type(expected_version) is not int or not 0 <= expected_version < _MAX_VERSION:
        raise PreferenceValidationError("预期版本必须是可递增的非负整数")


def _validate_source(source: dict) -> dict:
    if type(source) is not dict or set(source) - {"kind", "text"}:
        raise PreferenceValidationError("偏好来源只允许 kind 与 text")
    kind = source.get("kind")
    if type(kind) is not str or kind not in _SOURCE_KINDS:
        raise PreferenceValidationError("偏好来源类型不合法")
    if "text" in source and (type(source["text"]) is not str or len(source["text"]) > 500):
        raise PreferenceValidationError("偏好来源原文必须是最多500个字符的文本")
    # 即使调用方误传了删除原文，也不保留已删除来源的内容。
    return {"kind": "deletion"} if kind == "deletion" else dict(source)


def _snapshot(row) -> dict:
    timestamp = row["updated_at"]
    # SQLite 测试驱动不保留 tzinfo；本模块写入时间始终为 UTC。
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return {
        "role": row["role"],
        "values": validate_values(row["values"]),
        "version": row["version"],
        "updated_at": timestamp.astimezone(timezone.utc).isoformat(),
        "source": _validate_source(row["source"]),
    }


def _is_unique_violation(exc: IntegrityError) -> bool:
    """只将主键/唯一约束冲突转为版本冲突，不吞掉其他数据库故障。"""
    original = exc.orig
    return (getattr(original, "sqlstate", None) == "23505"
            or getattr(original, "sqlite_errorcode", None) in {1555, 2067})


class PreferenceStore:
    def __init__(self, engine: Engine):
        self.engine = engine

    def read(self, account_id: str, role: str) -> dict:
        _validate_identity(account_id, role)
        with self.engine.connect() as conn:
            row = conn.execute(select(preferences).where(
                preferences.c.account_id == account_id,
                preferences.c.role == role,
            )).mappings().first()
        if row is None:
            return {"role": role, "values": {}, "version": 0, "updated_at": None, "source": None}
        return _snapshot(row)

    def replace(self, account_id: str, role: str, values: dict,
                expected_version: int, source: dict) -> dict:
        """整份替换偏好，版本检查和写入在同一条数据库语句完成。"""
        _validate_identity(account_id, role)
        _validate_version(expected_version)
        values = validate_values(values)
        source = _validate_source(source)
        fields = {"values": values, "version": expected_version + 1,
                  "updated_at": datetime.now(timezone.utc), "source": source}
        try:
            with self.engine.begin() as conn:
                if expected_version == 0:
                    # 直接插入并由复合主键裁决竞争，不做存在检查后再插入。
                    statement = insert(preferences).values(
                        account_id=account_id, role=role, **fields)
                else:
                    statement = update(preferences).where(
                        preferences.c.account_id == account_id,
                        preferences.c.role == role,
                        preferences.c.version == expected_version,
                    ).values(**fields)
                row = conn.execute(statement.returning(*preferences.c)).mappings().first()
                if row is None:
                    raise PreferenceConflict("偏好版本已变化，请重新读取后再保存")
                snapshot = _snapshot(row)
            return snapshot
        except IntegrityError as exc:
            # 离开 begin 上下文已回滚；隐藏数据库异常中可能带有的来源原文。
            if expected_version == 0 and _is_unique_violation(exc):
                raise PreferenceConflict("偏好版本已变化，请重新读取后再保存") from None
            raise

    def delete(self, account_id: str, role: str, expected_version: int) -> dict:
        """清空偏好和旧来源，保留递增版本；无记录时也写入删除墓碑。"""
        return self.replace(account_id, role, {}, expected_version, {"kind": "deletion"})
