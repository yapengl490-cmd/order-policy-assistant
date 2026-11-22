-- 管理员显式迁移；不改既有政策原文、分块、向量或任务记录。
-- actor 是审计标签而不是授权凭据；普通应用仍只有 SELECT。
BEGIN;
CREATE TABLE public.policy_lifecycle_events (
    event_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES public.policy_documents(document_id),
    event_index INTEGER NOT NULL CHECK (event_index > 0),
    action TEXT NOT NULL,
    replacement_document_id TEXT REFERENCES public.policy_documents(document_id),
    document_checksum TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    UNIQUE (document_id, event_index),
    UNIQUE (replacement_document_id),
    CHECK ((action = 'withdrawn' AND replacement_document_id IS NULL)
        OR (action = 'corrected' AND replacement_document_id IS NOT NULL))
);
GRANT SELECT ON public.policy_lifecycle_events TO datus_reader;
COMMIT;
-- 不设自动回滚/删除；撤回也是需要保留的审计证据。
