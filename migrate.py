# -*- coding: utf-8 -*-
"""安全补齐索引的轻量迁移工具。

在升级到使用条码序列和查询索引的版本后执行一次：python migrate.py。
不会删除或修改现有业务数据。
"""
from sqlalchemy import inspect

from app import create_app
from app.extensions import db


INDEXES = {
    "reagent_pack": [
        ("ix_pack_stock_fifo", ["reagent_id", "status", "expire_date", "id"]),
    ],
    "inventory": [
        ("ix_inventory_reagent_expire", ["reagent_id", "expire_date"]),
    ],
}


def main():
    app = create_app()
    with app.app_context():
        # create_all 只创建缺失的表（包括 barcode_sequence），不会覆盖现有数据。
        db.create_all()
        inspector = inspect(db.engine)
        for table, indexes in INDEXES.items():
            existing = {item["name"] for item in inspector.get_indexes(table)}
            for name, columns in indexes:
                if name not in existing:
                    db.Index(name, *(db.metadata.tables[table].c[col] for col in columns)).create(db.engine)
                    print(f"[迁移] 已创建索引 {name}")
        print("[迁移] 完成，未修改任何业务记录。")


if __name__ == "__main__":
    main()
