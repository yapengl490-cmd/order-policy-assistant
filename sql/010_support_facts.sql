-- 管理员显式迁移：补充售后事实与退款原因证据；不改原订单金额或归属。
-- 本轮来源为明确合成记录，verified只是源记录的核实状态声明，不代表真实平台核验。
BEGIN;
CREATE TABLE public.order_support_facts (
    order_id TEXT PRIMARY KEY REFERENCES public.orders(order_id),
    product_category TEXT CHECK (product_category IS NULL OR length(product_category) BETWEEN 1 AND 80),
    delivered_at TIMESTAMPTZ,
    is_custom BOOLEAN,
    source TEXT NOT NULL CHECK (length(source) BETWEEN 1 AND 500),
    source_kind TEXT NOT NULL CHECK (source_kind = 'synthetic_demo'),
    source_version TEXT NOT NULL CHECK (length(source_version) BETWEEN 1 AND 80),
    recorded_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE public.refund_reason_records (
    evidence_id TEXT PRIMARY KEY CHECK (length(evidence_id) BETWEEN 1 AND 128),
    refund_id TEXT NOT NULL REFERENCES public.refunds(refund_id),
    reason_code TEXT NOT NULL CHECK (reason_code IN ('size','preference','merchant_responsibility','other','unknown')),
    verification_status TEXT NOT NULL CHECK (verification_status IN ('verified','buyer_reported','unverified')),
    recorded_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL CHECK (length(source) BETWEEN 1 AND 500),
    source_kind TEXT NOT NULL CHECK (source_kind = 'synthetic_demo'),
    source_version TEXT NOT NULL CHECK (length(source_version) BETWEEN 1 AND 80),
    note TEXT NOT NULL CHECK (length(note) <= 1000)
);
CREATE INDEX refund_reason_records_refund_id_idx ON public.refund_reason_records(refund_id);
GRANT SELECT ON TABLE public.order_support_facts, public.refund_reason_records TO datus_reader;
COMMIT;
-- 失败时ROLLBACK。提交后回退应先备份新表，由管理员单独处理；不级联删除订单/退款。
