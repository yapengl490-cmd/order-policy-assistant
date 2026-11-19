-- 助手编写：PostgreSQL 首版表结构；当前保存已支付订单和成功退款。
-- 仅在本项目的新空库执行。已有同名表时停止，不删除或覆盖旧数据。
BEGIN;

-- 本应用的买家编号独立于将来可能接入的平台账号。
CREATE TABLE buyers (
    buyer_id TEXT PRIMARY KEY NOT NULL,
    display_name TEXT NOT NULL
);

-- 订单引用买家；归属字段不替代应用程序的访问权限检查。
CREATE TABLE orders (
    order_id TEXT PRIMARY KEY NOT NULL,
    buyer_id TEXT NOT NULL REFERENCES buyers(buyer_id),
    paid_at TIMESTAMPTZ NOT NULL,
    paid_amount_fen BIGINT NOT NULL CHECK (paid_amount_fen >= 0)
);

-- 每次成功退款有自己的编号，同一订单可以关联多次退款。
CREATE TABLE refunds (
    refund_id TEXT PRIMARY KEY NOT NULL,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    refunded_at TIMESTAMPTZ NOT NULL,
    refund_amount_fen BIGINT NOT NULL CHECK (refund_amount_fen > 0)
);

COMMIT;
