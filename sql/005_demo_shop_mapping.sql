-- 合成多角色演示，先通过import_data.py导入samples/dual_role_demo.json。
-- 不修改原订单，重复执行冲突时停止，不能把已有订单静默分配给其他店铺。
BEGIN;
INSERT INTO public.shops(shop_id, name) VALUES ('S002', '隔离对照店铺');
INSERT INTO public.order_shops(order_id, shop_id)
VALUES ('O_DEMO_002', 'S001'), ('O_DEMO_003', 'S002');
COMMIT;
