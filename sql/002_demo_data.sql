-- 助手准备的合成教学数据：沿用 README 的一单两退，仅增加买家归属。
-- 首次录入使用；重复编号时报错并回滚本次插入，不覆盖已有记录。
BEGIN;

INSERT INTO buyers (buyer_id, display_name)
VALUES ('B001', '演示买家');

INSERT INTO orders (order_id, buyer_id, paid_at, paid_amount_fen)
VALUES ('O001', 'B001', '2026-10-15 10:00:00+08:00', 12500);

INSERT INTO refunds (refund_id, order_id, refunded_at, refund_amount_fen)
VALUES
    ('R001', 'O001', '2026-10-20 09:00:00+08:00', 2500),
    ('R002', 'O001', '2026-11-02 15:00:00+08:00', 1500);

COMMIT;

-- 以下只读核对段可重复执行；时区设置只作用于当前连接的显示方式。
SET TIME ZONE 'Asia/Shanghai';
SELECT buyer_id, display_name FROM buyers WHERE buyer_id = 'B001';
SELECT order_id, buyer_id, paid_at, paid_amount_fen
FROM orders WHERE order_id = 'O001';
SELECT refund_id, order_id, refunded_at, refund_amount_fen
FROM refunds WHERE order_id = 'O001' ORDER BY refund_id;
