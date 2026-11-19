-- 独立教学检查，不是初始化或导入脚本；O_FK_TEST 应为未使用的测试编号。
-- 先用 B404 观察外键拒绝，再仅改成 B001 对照；两次均不提交测试订单。
BEGIN;

INSERT INTO orders (order_id, buyer_id, paid_at, paid_amount_fen)
VALUES ('O_FK_TEST', 'B001', '2026-11-13 10:00:00+08:00', 100);

ROLLBACK;
