"""助手辅助：将 README 中的合成样本存入本地数据库，不包含核查计算。"""

import sqlite3
from pathlib import Path


def init_db(db_path: Path) -> bool:
    """由本文件入口调用；首次建库返回 True，已有文件则保留并返回 False。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 独占创建：重复运行时不覆盖文件，也不再次插入样本。
        db_path.touch(exist_ok=False)
    except FileExistsError:
        print(f"已有数据库，未改动：{db_path}")
        return False

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        # 建表和录入作为一次事务提交；异常时回滚，保留文件供排查。
        with connection:
            connection.execute("BEGIN")
            connection.execute("""
                CREATE TABLE orders (
                    order_id TEXT PRIMARY KEY NOT NULL,
                    paid_at TEXT NOT NULL,
                    paid_amount_fen INTEGER NOT NULL CHECK (paid_amount_fen >= 0)
                )
            """)
            connection.execute("""
                CREATE TABLE refunds (
                    refund_id TEXT PRIMARY KEY NOT NULL,
                    order_id TEXT NOT NULL REFERENCES orders(order_id),
                    refunded_at TEXT NOT NULL,
                    refund_amount_fen INTEGER NOT NULL CHECK (refund_amount_fen > 0)
                )
            """)
            connection.execute(
                "INSERT INTO orders VALUES (?, ?, ?)",
                ("O001", "2026-10-15 10:00:00", 12500),
            )
            connection.executemany(
                "INSERT INTO refunds VALUES (?, ?, ?, ?)",
                [
                    ("R001", "O001", "2026-10-20 09:00:00", 2500),
                    ("R002", "O001", "2026-11-02 15:00:00", 1500),
                ],
            )
    finally:
        connection.close()

    print(f"已保存合成样本：1 笔订单、2 笔退款。数据库：{db_path}")
    return True


if __name__ == "__main__":
    init_db(Path(__file__).resolve().parent / "data" / "sales_demo.sqlite3")
