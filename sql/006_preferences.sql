-- 由管理员显式执行；应用运行时不建表，不授予订单/退款表写权限。
-- 账号来自服务端认证，不能用用户消息或模型返回的账号作为表键。
-- 不存在时 API 返回 version=0；删除保留空值和递增版本，不物理删除。
-- 事务由管理员准备入口统一管理。IF NOT EXISTS 方便重跑，不替代既有表结构核验。

CREATE SCHEMA IF NOT EXISTS app_memory;

CREATE TABLE IF NOT EXISTS app_memory.preferences (
    account_id VARCHAR(128) NOT NULL,
    role VARCHAR(16) NOT NULL,
    "values" JSONB NOT NULL,
    version BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    source JSONB NOT NULL,
    CONSTRAINT preferences_pkey PRIMARY KEY (account_id, role),
    CONSTRAINT preferences_account_id_valid CHECK (
        account_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$'
    ),
    CONSTRAINT preferences_role_valid CHECK (role IN ('buyer', 'merchant')),
    CONSTRAINT preferences_version_nonnegative CHECK (version >= 0),
    CONSTRAINT preferences_values_valid CHECK (
        jsonb_typeof("values") = 'object'
        AND ("values" - 'amount_format' - 'answer_style') = '{}'::jsonb
        AND (NOT "values" ? 'amount_format' OR (
            jsonb_typeof("values" -> 'amount_format') = 'string'
            AND "values" ->> 'amount_format' IN ('yuan', 'yuan_fixed2', 'fen')
        ))
        AND (NOT "values" ? 'answer_style' OR (
            jsonb_typeof("values" -> 'answer_style') = 'string'
            AND "values" ->> 'answer_style' IN ('brief', 'standard', 'detailed')
        ))
    ),
    CONSTRAINT preferences_source_valid CHECK (
        jsonb_typeof(source) = 'object'
        AND (source - 'kind' - 'text') = '{}'::jsonb
        AND source ? 'kind'
        AND jsonb_typeof(source -> 'kind') = 'string'
        AND source ->> 'kind' IN ('user_settings', 'user_chat', 'deletion')
        AND (NOT source ? 'text' OR (
            jsonb_typeof(source -> 'text') = 'string'
            AND length(source ->> 'text') <= 500
        ))
        AND (source ->> 'kind' <> 'deletion' OR source = '{"kind":"deletion"}'::jsonb)
    )
);

COMMENT ON COLUMN app_memory.preferences.source IS
    'Audit only. Never interpolate source text into model instructions. Deletion clears old text.';
COMMENT ON COLUMN app_memory.preferences.updated_at IS
    'UTC instant; TIMESTAMPTZ display follows the connection timezone.';

