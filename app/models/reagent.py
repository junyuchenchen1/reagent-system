# -*- coding: utf-8 -*-
"""基础数据模型：试剂分类、供应商、试剂档案"""
from datetime import datetime
from app.extensions import db


class Category(db.Model):
    """试剂分类：生化 / 免疫 / 临检 / 分子 / 微生物等"""
    __tablename__ = "reagent_category"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, comment="分类名称")
    sort = db.Column(db.Integer, default=0, comment="排序")
    create_time = db.Column(db.DateTime, default=datetime.now)

    reagents = db.relationship("Reagent", back_populates="category")

    @property
    def reagent_count(self):
        return len(self.reagents)

    def __repr__(self):
        return f"<Category {self.name}>"


class Supplier(db.Model):
    """供应商档案"""
    __tablename__ = "supplier"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, comment="供应商名称")
    contact = db.Column(db.String(50), comment="联系人")
    phone = db.Column(db.String(20), comment="联系电话")
    address = db.Column(db.String(200), comment="地址")
    license_no = db.Column(db.String(100), comment="经营许可证号")
    license_expire_date = db.Column(db.Date, comment="经营许可证有效期")
    supplier_type = db.Column(db.String(20), default="经销商", comment="类型：生产厂家/一级代理/经销商")
    cert_image = db.Column(db.String(255), comment="经营许可证图片文件名")
    status = db.Column(db.SmallInteger, default=1, comment="1合格 0停用")
    remark = db.Column(db.String(255), comment="备注")
    create_time = db.Column(db.DateTime, default=datetime.now)

    def __repr__(self):
        return f"<Supplier {self.name}>"


class StorageLocation(db.Model):
    """存放位置字典：入库/在库可选择的存储位置"""
    __tablename__ = "storage_location"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, comment="位置名称")
    sort = db.Column(db.Integer, default=0, comment="排序")
    status = db.Column(db.SmallInteger, default=1, comment="1启用 0停用")
    remark = db.Column(db.String(255), comment="备注")
    create_time = db.Column(db.DateTime, default=datetime.now)

    def __repr__(self):
        return f"<StorageLocation {self.name}>"


class Reagent(db.Model):
    """试剂字典 / 档案"""
    __tablename__ = "reagent_info"

    # 存储条件
    STORAGE_NORMAL = "常温"
    STORAGE_COOL = "冷藏"      # 2~8℃
    STORAGE_FREEZE = "冷冻"    # -20℃ 以下
    STORAGE_CHOICES = [STORAGE_NORMAL, STORAGE_COOL, STORAGE_FREEZE]

    # 最低库存预警单位
    MIN_UNIT_PACK = "pack"   # 按大包装（盒/箱/袋）计数
    MIN_UNIT_UNIT = "unit"   # 按最小使用单位（瓶/支/人份）计数
    MIN_UNIT_CHOICES = [
        (MIN_UNIT_PACK, "按大包装"),
        (MIN_UNIT_UNIT, "按最小单位"),
    ]

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, index=True, comment="试剂名称")
    category_id = db.Column(db.Integer, db.ForeignKey("reagent_category.id"), comment="所属分类")
    spec = db.Column(db.String(100), comment="规格型号")
    unit = db.Column(db.String(20), default="盒", comment="采购/整盒单位")
    pack_size = db.Column(db.Integer, default=1, comment="每盒包含的最小使用单位数（如每盒12瓶）")
    small_unit = db.Column(db.String(20), default="瓶", comment="最小使用单位（瓶/支/人份）")
    min_stock = db.Column(db.Integer, default=10, comment="最低库存预警值（单位见 min_stock_unit）")
    min_stock_unit = db.Column(db.String(10), default=MIN_UNIT_PACK,
                               comment="最低库存预警单位：pack=大包装 unit=最小单位")
    expire_warning_days = db.Column(db.Integer, default=90, comment="效期预警天数")
    storage_condition = db.Column(db.String(20), default=STORAGE_COOL, comment="存储条件")
    is_hazard = db.Column(db.SmallInteger, default=0, comment="是否危险品：1是 0否")
    status = db.Column(db.SmallInteger, default=1, comment="1启用 0停用")
    # 医疗器械注册证信息
    reg_no = db.Column(db.String(100), comment="医疗器械注册证编号")
    reg_expire_date = db.Column(db.Date, comment="注册证有效期")
    registrant = db.Column(db.String(100), comment="注册人/生产企业")
    agent = db.Column(db.String(100), comment="代理人（进口试剂）")
    cert_image = db.Column(db.String(255), comment="注册证图片文件名")
    remark = db.Column(db.String(255), comment="备注")
    create_time = db.Column(db.DateTime, default=datetime.now)

    category = db.relationship("Category", back_populates="reagents")
    packs = db.relationship("ReagentPack", back_populates="reagent")

    # ---------- 库存统计（基于盒） ----------
    @property
    def in_stock_boxes(self) -> int:
        """中心在库（整盒）数量"""
        from app.models.stock import ReagentPack
        return sum(1 for p in self.packs if p.status == ReagentPack.STATUS_IN_STOCK)

    @property
    def issued_boxes(self) -> int:
        """已出库到科室、尚未用完的盒数（含未开盒/使用中）"""
        from app.models.stock import ReagentPack
        return sum(1 for p in self.packs if p.status in
                   (ReagentPack.STATUS_ISSUED, ReagentPack.STATUS_USING))

    @property
    def total_stock(self) -> int:
        """兼容旧调用：在库盒数"""
        return self.in_stock_boxes

    @property
    def in_stock_units(self) -> int:
        """中心在库的最小单位数（整盒未开封，按每盒登记数量求和）"""
        from app.models.stock import ReagentPack
        return sum(p.total_units for p in self.packs
                   if p.status == ReagentPack.STATUS_IN_STOCK)

    @property
    def available_units(self) -> int:
        """全科可用瓶数：中心在库未开封 + 已出库未用完（科室在用）"""
        return self.in_stock_units + self.in_use_units

    @property
    def min_stock_unit_text(self) -> str:
        """最低库存预警值所用的单位名称"""
        if self.min_stock_unit == self.MIN_UNIT_UNIT:
            return self.small_unit or "瓶"
        return self.unit or "盒"

    @property
    def current_stock_for_min(self) -> int:
        """按预警口径计算当前库存量。

        按大包装：只看中心在库盒数（科室已领出的盒不再计入）；
        按最小单位：中心在库瓶数 + 科室已出库未用完瓶数（全科可用量）。
        """
        if self.min_stock_unit == self.MIN_UNIT_UNIT:
            return self.available_units
        return self.in_stock_boxes

    @property
    def is_low_stock(self) -> bool:
        """当前在库量是否低于预警值（预警值为 0 表示不预警）"""
        minv = self.min_stock or 0
        return minv > 0 and self.current_stock_for_min < minv

    @property
    def in_use_units(self) -> int:
        """已出库未使用（科室可用）的最小单位数量"""
        from app.models.stock import ReagentPack
        return sum(p.remaining_units for p in self.packs
                   if p.status in (ReagentPack.STATUS_ISSUED, ReagentPack.STATUS_USING))

    @property
    def hazard_text(self):
        return "危险品" if self.is_hazard else "普通"

    def __repr__(self):
        return f"<Reagent {self.name}>"
