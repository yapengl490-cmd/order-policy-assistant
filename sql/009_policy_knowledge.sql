-- 新增受控合成政策库，不改订单/退款/原旧政策JSON。管理员显式执行，失败整批回滚。
-- 应用读账号只有SELECT；导入与索引由管理员离线命令执行。
BEGIN;
CREATE TABLE public.policy_documents (
    document_id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL,
    category TEXT NOT NULL,
    version TEXT NOT NULL,
    title TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind = 'synthetic_demo'),
    effective_from TIMESTAMPTZ NOT NULL,
    effective_before TIMESTAMPTZ NOT NULL,
    checksum TEXT NOT NULL CHECK (length(checksum) = 64),
    source_path TEXT NOT NULL,
    source_text TEXT NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT policy_period_nonempty CHECK (effective_from < effective_before),
    UNIQUE (store_id, category, version)
);
CREATE TABLE public.policy_chunks (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES public.policy_documents(document_id),
    section TEXT NOT NULL,
    line_start INTEGER NOT NULL CHECK (line_start > 0),
    line_end INTEGER NOT NULL CHECK (line_end >= line_start),
    text TEXT NOT NULL,
    checksum TEXT NOT NULL CHECK (length(checksum) = 64),
    embedding JSON,
    embedding_model TEXT,
    embedding_dimension INTEGER,
    embedding_source TEXT,
    embedding_text_checksum TEXT,
    embedded_at TIMESTAMPTZ,
    UNIQUE (document_id, section),
    CHECK ((embedding IS NULL AND embedding_model IS NULL AND embedding_dimension IS NULL
        AND embedding_source IS NULL AND embedding_text_checksum IS NULL AND embedded_at IS NULL)
        OR (embedding IS NOT NULL AND embedding_model IS NOT NULL AND embedding_dimension IS NOT NULL
        AND embedding_model = 'BAAI/bge-m3' AND embedding_dimension = 1024
        AND embedding_source IS NOT NULL AND embedding_text_checksum IS NOT NULL AND embedded_at IS NOT NULL))
);
CREATE INDEX policy_scope_period ON public.policy_documents (store_id, category, effective_from, effective_before);
GRANT SELECT ON public.policy_documents, public.policy_chunks TO datus_reader;
COMMIT;
-- 回退仅在管理员确认本次新表数据不再需要后执行DROP TABLE policy_chunks、policy_documents。
-- 不自动删表，不使用CASCADE，不影响原订单与退款持久数据。
