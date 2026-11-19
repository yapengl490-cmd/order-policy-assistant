"""把已有订单、退款表映射为 Python 对象；本文件不创建或修改数据库表。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy 收集表与类之间对应关系的基类。"""


class Order(Base):
    """一个 Order 对象对应 orders 表的一行；由 get_order 查询。"""

    __tablename__ = "orders"
    __table_args__ = {"schema": "public"}

    order_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # 当前仅读取编号，买家外键仍由已有数据库约束负责检查。
    buyer_id: Mapped[str] = mapped_column(Text)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paid_amount_fen: Mapped[int] = mapped_column(BigInteger)


class Refund(Base):
    """一行对应一次成功退款；由退款核查服务按订单及截止时刻读取。"""

    __tablename__ = "refunds"
    __table_args__ = {"schema": "public"}

    refund_id: Mapped[str] = mapped_column(Text, primary_key=True)
    order_id: Mapped[str] = mapped_column(Text)
    refunded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refund_amount_fen: Mapped[int] = mapped_column(BigInteger)
