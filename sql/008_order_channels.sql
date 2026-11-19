-- 管理员显式执行：新增可选渠道元数据，保持原Order字段/金额/店铺归属不变。
-- 依赖本轮007（来源record_kind含order_channels）；不推断或回填旧订单渠道。
BEGIN;
CREATE TABLE public.order_channels (
    order_id TEXT PRIMARY KEY NOT NULL REFERENCES public.orders(order_id),
    channel TEXT NOT NULL CHECK (length(channel) BETWEEN 1 AND 100 AND channel = btrim(channel))
);
GRANT SELECT ON TABLE public.order_channels TO datus_reader;
COMMIT;
-- 未提交时ROLLBACK；提交后回退先备份此新表，再删order_channels，原业务表不受影响。
