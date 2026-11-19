-- 助手编写：用户确认第一版加入商家只读模式后新增店铺归属。
-- 由管理员显式执行一次；若表已存在则事务停止，不覆盖旧表或重新分配已有订单。
-- 原买家、订单、退款表和金额记录均保留；这不是线上商家账号认证。
BEGIN;

CREATE TABLE public.shops (
    shop_id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL
);

CREATE TABLE public.order_shops (
    order_id TEXT PRIMARY KEY NOT NULL REFERENCES public.orders(order_id),
    shop_id TEXT NOT NULL REFERENCES public.shops(shop_id)
);

INSERT INTO public.shops (shop_id, name) VALUES ('S001', '本地演示店铺');

-- 仅映射现有合成样本；没有该订单时不凭空创建交易，也不扩大到所有买家订单。
INSERT INTO public.order_shops (order_id, shop_id)
SELECT order_id, 'S001' FROM public.orders WHERE order_id = 'O001' AND buyer_id = 'B001';

-- 查询角色仍只有读取权限；应用另外核对账号获授权的店铺，不靠数据库角色代替授权。
GRANT SELECT ON TABLE public.shops, public.order_shops TO datus_reader;

COMMIT;
