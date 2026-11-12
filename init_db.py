# -*- coding: utf-8 -*-
"""数据库初始化工具（双模式）

用法：
  python init_db.py                 # 演示模式：建表 + 演示账号 + 演示数据（默认）
  python init_db.py --empty         # 正式模式：仅建表 + 管理员（随机一次性密码），不造演示数据
  python init_db.py --force         # 先清空所有表，再按上面的模式执行（可组合 --force --empty）

说明：
  - 演示模式用于体验/演示，账号为固定演示口令（admin/admin123 等），切勿用于正式环境。
  - 正式模式（--empty）创建随机一次性管理员密码，首次登录后请立即修改；
    凭据同时写入 instance/initial_admin_password.txt（首次登录成功后自动删除）。
  - 两种模式都不会覆盖“库中已有业务数据”的现有库，除非加 --force。
"""
import os
import sys
import secrets
import random
from datetime import date, datetime, time, timedelta

from app import create_app
from app.config import Config
from app.extensions import db
from app.models.user import User
from app.models.reagent import Category, Supplier, Reagent, StorageLocation
from app.models.stock import StockIn, StockOut, ReagentPack, PackUse
from app.services import stock_service
from werkzeug.security import generate_password_hash

random.seed(20261019)

# （名称, 分类, 规格, 每盒瓶数, 小单位, 存储, 最低预警盒, 效期预警天数）
REAGENTS = [
    ("葡萄糖(GLU)测定试剂盒", "生化试剂", "R1 40ml×4", 12, "瓶", "冷藏", 5, 90),
    ("丙氨酸氨基转移酶(ALT)试剂盒", "生化试剂", "R1 60ml×2", 12, "瓶", "冷藏", 5, 90),
    ("门冬氨酸氨基转移酶(AST)试剂盒", "生化试剂", "R1 60ml×2", 12, "瓶", "冷藏", 5, 90),
    ("肌酐(CREA)测定试剂盒", "生化试剂", "R1 50ml×2", 24, "瓶", "冷藏", 4, 90),
    ("尿素氮(BUN)测定试剂盒", "生化试剂", "R1 50ml×2", 24, "瓶", "冷藏", 4, 90),
    ("总胆固醇(TC)测定试剂盒", "生化试剂", "R1 40ml×2", 12, "瓶", "冷藏", 5, 120),
    ("乙肝表面抗原(HBsAg)试剂盒", "免疫试剂", "96人份/盒", 96, "人份", "冷藏", 4, 120),
    ("丙肝抗体(HCV-Ab)试剂盒", "免疫试剂", "96人份/盒", 96, "人份", "冷藏", 4, 120),
    ("人类免疫缺陷病毒抗体(HIV)试剂盒", "免疫试剂", "96人份/盒", 96, "人份", "冷藏", 4, 180),
    ("梅毒螺旋体抗体(TP)试剂盒", "免疫试剂", "96人份/盒", 96, "人份", "冷藏", 4, 120),
    ("甲胎蛋白(AFP)化学发光试剂盒", "免疫试剂", "100T/盒", 24, "测试", "冷藏", 3, 150),
    ("凝血酶原时间(PT)测定试剂盒", "血凝试剂", "10瓶/盒", 10, "瓶", "冷藏", 3, 90),
    ("活化部分凝血活酶(APTT)试剂盒", "血凝试剂", "10瓶/盒", 10, "瓶", "冷藏", 3, 90),
    ("血细胞分析用溶血剂", "临检试剂", "4L×2瓶", 2, "瓶", "常温", 2, 365),
    ("需氧菌血培养瓶", "微生物试剂", "40瓶/盒", 40, "瓶", "常温", 6, 180),
]

GROUP_MAP = {
    "生化试剂": "生化组", "免疫试剂": "免疫组", "血凝试剂": "凝血组",
    "临检试剂": "临检组", "分子试剂": "分子组", "微生物试剂": "微生物组",
}

STORAGE_DEFAULTS = [
    ("常温试剂柜", 1, 1, "室温 15~30℃"),
    ("2-8℃ 冷藏冰箱", 2, 1, "冷藏保存"),
    ("-20℃ 冷冻冰柜", 3, 1, "冷冻保存"),
    ("阴凉柜", 4, 1, "阴凉避光"),
]


def _has_business_data() -> bool:
    """库中是否已有业务数据（分类 / 试剂 / 入库单 / 盒 任一存在记录）"""
    return any(
        db.session.query(model.id).first() is not None
        for model in (Category, Reagent, StockIn, ReagentPack)
    )


def _ensure_storage_locations():
    """确保默认存放位置字典存在（两种模式都调用，避免入库页面无位置可选）"""
    if StorageLocation.query.count() == 0:
        for name, sort, status, remark in STORAGE_DEFAULTS:
            db.session.add(StorageLocation(name=name, sort=sort,
                                           status=status, remark=remark))
        db.session.commit()
        print("已写入默认存放位置（常温试剂柜 / 冷藏冰箱 / 冷冻冰柜 / 阴凉柜）。")


def _write_credential_file(password: str):
    """把首次部署管理员凭据写入 instance/initial_admin_password.txt"""
    cred = os.path.join(Config.BASE_DIR, "instance", "initial_admin_password.txt")
    with open(cred, "w", encoding="utf-8") as f:
        f.write("检验科试剂管理系统 - 首次部署管理员凭据\n")
        f.write("账号：admin\n")
        f.write(f"密码：{password}\n\n")
        f.write("请立即登录并修改密码。首次登录成功后，本文件会自动删除。\n")


# ============================================================
# 正式模式（--empty）：仅建表 + 管理员，不造演示数据
# ============================================================
def _init_empty():
    admin = User.query.filter_by(username="admin").first()
    if admin is None:
        password = secrets.token_urlsafe(12)
        admin = User(username="admin", real_name="系统管理员",
                     role=User.ROLE_ADMIN, status=1)
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
        _write_credential_file(password)
        print(f"[初始化] 已创建管理员账号 admin / {password}")
    else:
        print("[初始化] 管理员账号已存在，保持原密码，未做修改。")

    print("-" * 50)
    print("数据库初始化完成（正式模式，无演示数据）")
    print(f"  存放位置 {StorageLocation.query.count()} 个，用户 {User.query.count()} 个")
    print("-" * 50)
    print("登录账号：admin（密码见 instance/initial_admin_password.txt，若为新库）")
    print("首次登录后请立即修改密码。")


# ============================================================
# 演示模式（默认）：建表 + 演示账号 + 演示数据
# ============================================================
def _init_demo():
    # 演示模式把 admin 口令固定为 admin123，首启生成的随机密码凭据随即失效，
    # 删除该文件，避免用户照着文件里的旧密码登录失败。
    cred = os.path.join(Config.BASE_DIR, "instance", "initial_admin_password.txt")
    if os.path.exists(cred):
        try:
            os.remove(cred)
            print("已删除首启生成的 initial_admin_password.txt（演示模式口令固定为 admin123）。")
        except OSError:
            pass

    # ---------------- 用户（admin 若已存在则重置为演示口令） ----------------
    admin = User.query.filter_by(username="admin").first()
    if admin is None:
        admin = User(username="admin", real_name="管理员",
                     role="admin", status=1)
        db.session.add(admin)
    admin.set_password("admin123")

    manager = User.query.filter_by(username="manager").first()
    if manager is None:
        manager = User(username="manager", real_name="李试剂",
                       role="manager", status=1)
        db.session.add(manager)
    manager.set_password("manager123")

    staff = User.query.filter_by(username="staff").first()
    if staff is None:
        staff = User(username="staff", real_name="王检验",
                     role="staff", status=1)
        db.session.add(staff)
    staff.set_password("staff123")
    db.session.flush()

    # ---------------- 分类 ----------------
    cats = {}
    for idx, name in enumerate(
            ["生化试剂", "免疫试剂", "临检试剂", "血凝试剂",
             "分子试剂", "微生物试剂"]):
        c = Category(name=name, sort=idx)
        db.session.add(c)
        cats[name] = c
    db.session.flush()

    # ---------------- 供应商 ----------------
    suppliers = []
    for name, contact, phone in [
        ("迈瑞生物股份有限公司", "周经理", "13800001111"),
        ("罗氏诊断产品(上海)有限公司", "吴经理", "13900002222"),
        ("深圳迈瑞生物医疗电子", "郑经理", "13700003333"),
    ]:
        s = Supplier(name=name, contact=contact, phone=phone,
                     status=1, license_no="粤械经营证2026-0000")
        db.session.add(s)
        suppliers.append(s)
    db.session.flush()

    today = date.today()

    def d_in(days):
        return today + timedelta(days=days)

    all_in, all_out = [], []
    reagent_index = 0
    for (name, cat_name, spec, pack_size, small_unit,
         storage, min_stock, warn_days) in REAGENTS:
        reagent_index += 1
        reagent = Reagent(
            name=name, category_id=cats[cat_name].id, spec=spec,
            unit="盒", pack_size=pack_size, small_unit=small_unit,
            min_stock=min_stock, expire_warning_days=warn_days,
            storage_condition=storage,
            is_hazard=1 if cat_name in ("分子试剂",) else 0,
            status=1)
        db.session.add(reagent)
        db.session.flush()

        price = round(random.uniform(200, 4000), 2)

        def inbound(qty, expire, supplier, loc):
            si = stock_service.create_stock_in({
                "reagent_id": reagent.id,
                "batch_no": f"{expire.strftime('%y%m%d')}{chr(65 + random.randint(0, 2))}",
                "expire_date": expire,
                "quantity": qty,
                "unit_price": price,
                "supplier_id": supplier.id,
                "storage_location": loc,
                "remark": "",
            }, manager)
            stock_service.confirm_stock_in(si, manager)
            all_in.append(si)
            return si

        # 正常批次（6~11 盒）
        inbound(random.randint(6, 11), d_in(random.randint(180, 480)),
                suppliers[reagent_index % 3],
                f"{storage}区{random.randint(1, 6)}架")

        # 约 55% 试剂有临期批次（2~4 盒）
        if random.random() < 0.55:
            inbound(random.randint(2, 4), d_in(random.randint(6, 45)),
                    suppliers[reagent_index % 3], "临期待处理区")

        # 约 30% 试剂有过期批次（1~3 盒），随后报废
        expired_qty = 0
        if random.random() < 0.30:
            expired_qty = random.randint(1, 3)
            inbound(expired_qty, d_in(-random.randint(2, 30)),
                    suppliers[reagent_index % 3], "过期隔离区")
            out = stock_service.create_stock_out({
                "reagent_id": reagent.id, "quantity": expired_qty,
                "out_type": "scrap", "receiver": "库管",
                "reason": "超过有效期，按报废流程处理",
            }, manager)
            stock_service.confirm_stock_out(out, manager)
            all_out.append(out)

        # 约 85% 试剂发生整盒领用（2~5 盒，FIFO 先用临期）
        group = GROUP_MAP.get(cat_name, "检验科")
        if random.random() < 0.85:
            qty = random.randint(2, 5)
            avail = stock_service.available_in_stock_boxes(reagent.id)
            qty = min(qty, avail)
            if qty > 0:
                out = stock_service.create_stock_out({
                    "reagent_id": reagent.id, "quantity": qty,
                    "out_type": "use", "receiver": group,
                    "reason": "日常检验领用",
                }, staff)
                stock_service.confirm_stock_out(out, manager)
                all_out.append(out)

        # 约 25% 试剂发生质控出库 1 盒
        if random.random() < 0.25 and \
                stock_service.available_in_stock_boxes(reagent.id) >= 1:
            out = stock_service.create_stock_out({
                "reagent_id": reagent.id, "quantity": 1,
                "out_type": "qc", "receiver": group,
                "reason": "室内质控",
            }, staff)
            stock_service.confirm_stock_out(out, manager)
            all_out.append(out)

        # 对已出库的盒登记瓶级使用
        issued = stock_service.get_issued_packs_for_use(reagent.id)
        for p in issued:
            # 每盒约 70% 已开盒使用，其中约 15% 一次性用完
            if random.random() < 0.70:
                if random.random() < 0.15:
                    use_n = p.remaining_units
                else:
                    use_n = random.randint(1, max(1, pack_size // 2))
                use_n = min(use_n, p.remaining_units)
                if use_n > 0:
                    is_qc = random.random() < 0.2
                    stock_service.register_use(
                        p, use_n,
                        "qc" if is_qc else "use",
                        staff, "室内质控" if is_qc else "日常检验")

    # ---------------- 时间散布（近 30 天） ----------------
    def random_dt(keep_today=False):
        if keep_today or random.random() < 0.22:
            return datetime.now()
        back = random.randint(1, 29)
        return datetime.combine(today - timedelta(days=back), time()) + \
            timedelta(hours=random.randint(8, 16), minutes=random.randint(0, 59))

    # 第一笔入库/出库强制保留在今天，保证今日统计有数据
    for idx, si in enumerate(all_in):
        t = random_dt(keep_today=(idx == 0))
        si.create_time = t
        si.auditor_id = manager.id
        si.audit_time = t
    out_time_map = {}
    for idx, out in enumerate(all_out):
        t = random_dt(keep_today=(idx == 0))
        out.create_time = t
        out.confirmer_id = manager.id
        out.confirm_time = t
        out_time_map[out.id] = t
    db.session.commit()

    # 盒出库时间、使用记录时间与出库对齐
    for p in ReagentPack.query.all():
        if p.stock_out_id and p.stock_out_id in out_time_map:
            p.issued_time = out_time_map[p.stock_out_id]
    for u in PackUse.query.all():
        base = out_time_map.get(u.pack.stock_out_id, datetime.now())
        t = base + timedelta(hours=random.randint(2, 60))
        if t > datetime.now():
            t = datetime.now()
        u.create_time = t
    db.session.commit()

    # ---------------- 完成统计 ----------------
    sku = ReagentPack.query.filter_by(
        status=ReagentPack.STATUS_IN_STOCK).count()
    print("-" * 50)
    print("数据库初始化完成（演示模式）！")
    print(f"  用户 3 个，分类 {len(cats)} 个，供应商 {len(suppliers)} 个")
    print(f"  试剂 {len(REAGENTS)} 种，在库盒 {sku} 个，"
          f"盒条码 {ReagentPack.query.count()} 枚")
    print(f"  入库单 {len(all_in)} 张，出库单 {len(all_out)} 张，"
          f"使用记录 {PackUse.query.count()} 条")
    print("-" * 50)
    print("演示账号：")
    print("  admin / admin123      （系统管理员）")
    print("  manager / manager123  （试剂管理员）")
    print("  staff / staff123      （检验人员）")
    print("注意：演示口令仅用于体验，正式使用请改密并删除演示数据。")


def init(force: bool = False, empty: bool = False):
    app = create_app()
    with app.app_context():
        has_table = db.inspect(db.engine).has_table("sys_user")
        if force:
            db.drop_all()
            db.create_all()
            print("已清空并重建数据表。")
        elif has_table and _has_business_data():
            print("数据库已存在业务数据，跳过初始化。如需清空重建请加 --force")
            return
        else:
            db.create_all()

        # 两模式都确保默认存放位置存在（入库页面直接可选）
        _ensure_storage_locations()

        if empty:
            _init_empty()
        else:
            _init_demo()


if __name__ == "__main__":
    args = set(sys.argv[1:])
    init(force="--force" in args, empty="--empty" in args)
