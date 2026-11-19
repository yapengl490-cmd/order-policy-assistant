-- 管理员显式迁移；只新增行级来源，不改任何订单金额、归属或旧批次。
-- 历史行不自动回填。之后重导完全一致的原规范化文件，可登记其声明来源。
BEGIN;
CREATE TABLE public.import_row_sources (
    record_kind TEXT NOT NULL CHECK (record_kind IN ('buyers','orders','refunds','shops','order_shops','order_channels')),
    record_id TEXT NOT NULL CHECK (length(record_id) BETWEEN 1 AND 128),
    batch_sha256 TEXT NOT NULL REFERENCES public.import_batches(batch_sha256),
    PRIMARY KEY (record_kind, record_id, batch_sha256)
);
GRANT SELECT ON TABLE public.import_row_sources TO datus_reader;
-- 来源查询需要读取批次声明；管理员写入权限不授予业务账号。
GRANT SELECT ON TABLE public.import_batches TO datus_reader;
COMMIT;
-- 若还未提交可 ROLLBACK。提交后需要回退时先导出该新增表，再由管理员
-- DROP TABLE public.import_row_sources；这会删除新增来源关联，不删原业务数据。
