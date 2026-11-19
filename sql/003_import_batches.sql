-- 管理员显式执行的新增表迁移；不更改原 buyers/orders/refunds，也不清空数据。
-- 普通查询、模型工具和 import_payload 都不会自动执行此文件。
-- 仅记录规范化样本的来源声明和本次导入，不证明来源平台状态真实。
BEGIN;
CREATE TABLE public.import_batches (
    batch_sha256 TEXT PRIMARY KEY CHECK (batch_sha256 ~ '^[0-9a-f]{64}$'),
    source TEXT NOT NULL CHECK (length(source) BETWEEN 1 AND 200),
    version TEXT NOT NULL CHECK (length(version) BETWEEN 1 AND 80),
    currency TEXT NOT NULL CHECK (currency = 'CNY'),
    row_count INTEGER NOT NULL CHECK (row_count BETWEEN 0 AND 1000),
    inserted_counts JSONB NOT NULL,
    skipped_counts JSONB NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
COMMIT;
