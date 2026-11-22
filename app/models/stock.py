# -*- coding: utf-8 -*-
"""库存业务模型：入库、出库、盒包装(条码)、使用记录、库存、盘点

两级库存模型：
  盒(Pack) = 采购/整盒出库单位，一盒含 pack_size 瓶（最小使用单位）
  - in_stock 中心在库整盒
  - issued   已出库到科室、尚未开盒
  - using    已开盒使用中
  - empty    已用完
  - scrapped 已报废
"""
from datetime import datetime, date
from app.extensions import db
from app.models.reagent import Reagent


class StockIn(db.Model):
    """试剂入库单"""
    __tablename__ = "stock_in"

    STATUS_PENDING = 0
    STATUS_DONE = 1
    STATUS_CANCEL = 2
    STATUS_TEXT = {0: "待审核", 1: "已入库", 2: "已取消"}

    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(30), unique=True, index=True, comment="入库单号")
    reagent_id = db.Column(db.Integer, db.ForeignKey("reagent_info.id"), nullable=False)
    batch_no = db.Column(db.String(50), nullable=False, comment="批号")
    expire_date = db.Column(db.Date, nullable=False, comment="有效期")
    quantity = db.Column(db.Integer, nullable=False, comment="入库盒数")
    unit_price = db.Column(db.Numeric(10, 2), default=0, comment="每盒单价")
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"))
    storage_location = db.Column(db.String(100), comment="存放位置")
    status = db.Column(db.SmallInteger, default=0, comment="0待审核 1已入库 2已取消")
    operator_id = db.Column(db.Integer, db.ForeignKey("sys_user.id"), comment="登记人")
    auditor_id = db.Column(db.Integer, db.ForeignKey("sys_user.id"), comment="审核人")
    verifier_id = db.Column(db.Integer, db.ForeignKey("sys_user.id"), comment="验收人（双人验收）")
    accept_status = db.Column(db.String(10), default="合格", comment="接收状态：合格/破损/拒收")
    is_verified = db.Column(db.Boolean, default=False, comment="新批号是否已性能验证")
    udi_di = db.Column(db.String(32), comment="入库时所扫 UDI 产品标识（可追溯）")
    serial_no = db.Column(db.String(64), comment="序列号（UDI 21，非必填）")
    produce_date = db.Column(db.Date, comment="生产日期（UDI 11，非必填）")
    audit_time = db.Column(db.DateTime, comment="审核时间")
    remark = db.Column(db.String(255))
    create_time = db.Column(db.DateTime, default=datetime.now)

    reagent = db.relationship("Reagent", foreign_keys=[reagent_id])
    supplier = db.relationship("Supplier")
    operator = db.relationship("User", foreign_keys=[operator_id], post_update=True)
    auditor = db.relationship("User", foreign_keys=[auditor_id], post_update=True)
    verifier = db.relationship("User", foreign_keys=[verifier_id], post_update=True)
    packs = db.relationship("ReagentPack", back_populates="stock_in", lazy="dynamic")

    @property
    def status_text(self):
        return self.STATUS_TEXT.get(self.status, "未知")

    @property
    def amount(self):
        return round(float(self.unit_price or 0) * self.quantity, 2)


class StockOut(db.Model):
    """试剂出库单（默认整盒出库）"""
    __tablename__ = "stock_out"

    STATUS_PENDING = 0
    STATUS_DONE = 1
    STATUS_CANCEL = 2
    STATUS_TEXT = {0: "待确认", 1: "已出库", 2: "已取消"}

    TYPE_USE = "use"
    TYPE_QC = "qc"
    TYPE_SCRAP = "scrap"
    TYPE_TEXT = {"use": "检验领用", "qc": "质控消耗", "scrap": "试剂报废"}

    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(30), unique=True, index=True)
    reagent_id = db.Column(db.Integer, db.ForeignKey("reagent_info.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, comment="出库盒数")
    out_type = db.Column(db.String(10), default="use", comment="use领用 qc质控 scrap报废")
    scan_barcode = db.Column(db.String(13), nullable=True, comment="扫码锁定的盒条码")
    receiver = db.Column(db.String(50), comment="领用科室/接收人")
    receiver_id = db.Column(db.Integer, db.ForeignKey("sys_user.id"), comment="接收人用户ID")
    status = db.Column(db.SmallInteger, default=0)
    applicant_id = db.Column(db.Integer, db.ForeignKey("sys_user.id"), comment="申请人")
    confirmer_id = db.Column(db.Integer, db.ForeignKey("sys_user.id"), comment="确认人/出库人")
    confirm_time = db.Column(db.DateTime)
    reason = db.Column(db.String(255), comment="事由/用途")
    create_time = db.Column(db.DateTime, default=datetime.now)

    reagent = db.relationship("Reagent", foreign_keys=[reagent_id])
    applicant = db.relationship("User", foreign_keys=[applicant_id])
    confirmer = db.relationship("User", foreign_keys=[confirmer_id])
    receiver_user = db.relationship("User", foreign_keys=[receiver_id])
    packs = db.relationship("ReagentPack", back_populates="stock_out", lazy="dynamic")

    @property
    def status_text(self):
        return self.STATUS_TEXT.get(self.status, "未知")

    @property
    def type_text(self):
        return self.TYPE_TEXT.get(self.out_type, self.out_type)


class ReagentPack(db.Model):
    """单盒试剂（带唯一条码，最小追溯单位）"""
    __tablename__ = "reagent_pack"

    STATUS_IN_STOCK = "in_stock"
    STATUS_ISSUED = "issued"
    STATUS_USING = "using"
    STATUS_EMPTY = "empty"
    STATUS_SCRAPPED = "scrapped"
    STATUS_TEXT = {
        "in_stock": "在库",
        "issued": "已出库",
        "using": "使用中",
        "empty": "已用完",
        "scrapped": "已报废",
    }

    id = db.Column(db.Integer, primary_key=True)
    barcode = db.Column(db.String(13), unique=True, index=True, nullable=False, comment="条码：yyMMdd(6位)+当日序号(4位,0001起)")
    reagent_id = db.Column(db.Integer, db.ForeignKey("reagent_info.id"), nullable=False, index=True)
    batch_no = db.Column(db.String(50), nullable=False, index=True, comment="批号")
    expire_date = db.Column(db.Date, nullable=False, index=True, comment="有效期")
    stock_in_id = db.Column(db.Integer, db.ForeignKey("stock_in.id"), comment="来源入库单")
    stock_out_id = db.Column(db.Integer, db.ForeignKey("stock_out.id"), comment="出库单")
    unit_price = db.Column(db.Numeric(10, 2), default=0, comment="每盒单价")
    total_units = db.Column(db.Integer, default=1, comment="每盒瓶数(规格快照)")
    used_units = db.Column(db.Integer, default=0, comment="已使用瓶数")
    status = db.Column(db.String(10), default=STATUS_IN_STOCK, index=True)
    storage_location = db.Column(db.String(100), comment="存放位置")
    receiver = db.Column(db.String(50), comment="领用科室/接收人")
    receiver_id = db.Column(db.Integer, db.ForeignKey("sys_user.id"))
    issued_time = db.Column(db.DateTime, comment="出库时间")
    close_time = db.Column(db.DateTime, comment="用完/报废时间")
    create_time = db.Column(db.DateTime, default=datetime.now)

    reagent = db.relationship("Reagent", foreign_keys=[reagent_id], back_populates="packs")
    stock_in = db.relationship("StockIn", foreign_keys=[stock_in_id], back_populates="packs")
    stock_out = db.relationship("StockOut", foreign_keys=[stock_out_id], back_populates="packs")
    receiver_user = db.relationship("User", foreign_keys=[receiver_id])
    uses = db.relationship("PackUse", back_populates="pack", lazy="dynamic",
                           cascade="all, delete-orphan")
    __table_args__ = (
        db.Index("ix_pack_stock_fifo", "reagent_id", "status", "expire_date", "id"),
    )

    @property
    def remaining_units(self) -> int:
        return max(self.total_units - self.used_units, 0)

    @property
    def status_text(self):
        return self.STATUS_TEXT.get(self.status, self.status)

    @property
    def days_left(self):
        return (self.expire_date - date.today()).days

    @property
    def expire_status(self):
        d = self.days_left
        if d < 0:
            return "expired"
        warn = self.reagent.expire_warning_days if self.reagent else 90
        if d <= warn:
            return "warning"
        return "normal"

    @property
    def expire_text(self):
        d = self.days_left
        if d < 0:
            return f"已过期{abs(d)}天"
        if d == 0:
            return "今天到期"
        return f"剩{d}天"

    @property
    def is_open(self):
        """是否已开盒（使用中）"""
        return self.status == self.STATUS_USING


class PackUse(db.Model):
    """试剂使用（开盒后按瓶登记）"""
    __tablename__ = "pack_use"

    TYPE_USE = "use"
    TYPE_QC = "qc"
    TYPE_TEXT = {"use": "日常检验", "qc": "质控消耗"}

    id = db.Column(db.Integer, primary_key=True)
    use_no = db.Column(db.String(30), unique=True, index=True)
    pack_id = db.Column(db.Integer, db.ForeignKey("reagent_pack.id"), nullable=False)
    reagent_id = db.Column(db.Integer, db.ForeignKey("reagent_info.id"), nullable=False)
    units_used = db.Column(db.Integer, nullable=False, comment="本次使用瓶数")
    use_type = db.Column(db.String(10), default="use")
    user_id = db.Column(db.Integer, db.ForeignKey("sys_user.id"), comment="使用人")
    remark = db.Column(db.String(255))
    create_time = db.Column(db.DateTime, default=datetime.now)

    pack = db.relationship("ReagentPack", foreign_keys=[pack_id], back_populates="uses")
    reagent = db.relationship("Reagent", foreign_keys=[reagent_id])
    user = db.relationship("User", foreign_keys=[user_id])

    @property
    def type_text(self):
        return self.TYPE_TEXT.get(self.use_type, self.use_type)


class Inventory(db.Model):
    """中心在库库存（按试剂+批号汇总，只统计在库整盒）"""
    __tablename__ = "inventory"

    id = db.Column(db.Integer, primary_key=True)
    reagent_id = db.Column(db.Integer, db.ForeignKey("reagent_info.id"), nullable=False, index=True)
    batch_no = db.Column(db.String(50), nullable=False)
    expire_date = db.Column(db.Date, nullable=False, index=True)
    quantity = db.Column(db.Integer, default=0, comment="在库盒数")
    storage_location = db.Column(db.String(100))
    update_time = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    reagent = db.relationship("Reagent")
    # 注意：这里刻意不建 stock_in 关系。同一 试剂+批号 可能有多张入库单，
    # 用非唯一键做"多对一"会随机取到其中一张（性能验证状态就会显示错）。
    # 需要验证状态时，由路由按 (reagent_id, batch_no) 批量查询后传给模板。
    __table_args__ = (
        db.UniqueConstraint("reagent_id", "batch_no", name="uk_reagent_batch"),
        db.Index("ix_inventory_reagent_expire", "reagent_id", "expire_date"),
    )

    @property
    def days_left(self):
        return (self.expire_date - date.today()).days

    @property
    def stock_status(self):
        d = self.days_left
        if d < 0:
            return "expired"
        warn = self.reagent.expire_warning_days if self.reagent else 90
        if d <= warn:
            return "warning"
        return "normal"

    @property
    def stock_status_text(self):
        return {"normal": "正常", "warning": "临期", "expired": "过期"}.get(self.stock_status)

    @property
    def expire_text(self):
        d = self.days_left
        if d < 0:
            return f"已过期{abs(d)}天"
        if d == 0:
            return "今天到期"
        return f"剩{d}天"


class BarcodeSequence(db.Model):
    """按日递增的条码序列，避免并发入库生成重复条码。"""
    __tablename__ = "barcode_sequence"
    prefix = db.Column(db.String(6), primary_key=True)
    next_value = db.Column(db.Integer, nullable=False, default=1)


class StockCheck(db.Model):
    """库存盘点单（盘中心在库整盒）"""
    __tablename__ = "stock_check"

    STATUS_DRAFT = 0
    STATUS_DONE = 1
    STATUS_TEXT = {0: "盘点中", 1: "已完成"}

    id = db.Column(db.Integer, primary_key=True)
    check_no = db.Column(db.String(30), unique=True, index=True)
    title = db.Column(db.String(100))
    operator_id = db.Column(db.Integer, db.ForeignKey("sys_user.id"))
    status = db.Column(db.SmallInteger, default=0)
    difference_count = db.Column(db.Integer, default=0, comment="有差异项数")
    finish_time = db.Column(db.DateTime)
    remark = db.Column(db.String(255))
    create_time = db.Column(db.DateTime, default=datetime.now)

    operator = db.relationship("User")
    items = db.relationship("StockCheckItem", back_populates="check",
                            cascade="all, delete-orphan")

    @property
    def status_text(self):
        return self.STATUS_TEXT.get(self.status, "未知")


class StockCheckItem(db.Model):
    """盘点明细"""
    __tablename__ = "stock_check_item"

    id = db.Column(db.Integer, primary_key=True)
    check_id = db.Column(db.Integer, db.ForeignKey("stock_check.id"), nullable=False)
    reagent_id = db.Column(db.Integer, db.ForeignKey("reagent_info.id"), nullable=False)
    batch_no = db.Column(db.String(50), nullable=False)
    expire_date = db.Column(db.Date)
    system_qty = db.Column(db.Integer, default=0, comment="系统在库盒数")
    actual_qty = db.Column(db.Integer, comment="实际盘点盒数")
    diff_qty = db.Column(db.Integer, comment="差异")

    check = db.relationship("StockCheck", back_populates="items")
    reagent = db.relationship("Reagent")


class TempRecord(db.Model):
    """冰箱/储存环境温湿度记录"""
    __tablename__ = "temp_record"
    id = db.Column(db.Integer, primary_key=True)
    location = db.Column(db.String(100), nullable=False, comment="冰箱/位置名称")
    record_date = db.Column(db.Date, nullable=False, comment="记录日期")
    am_temp = db.Column(db.Float, comment="上午温度℃")
    pm_temp = db.Column(db.Float, comment="下午温度℃")
    humidity = db.Column(db.Float, comment="湿度%")
    note = db.Column(db.String(255), comment="备注（异常处理）")
    recorder_id = db.Column(db.Integer, db.ForeignKey("sys_user.id"), comment="记录人")
    create_time = db.Column(db.DateTime, default=datetime.now)

    recorder = db.relationship("User", foreign_keys=[recorder_id])


class TempLocation(db.Model):
    """冰箱/温湿度监测位置"""
    __tablename__ = "temp_location"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    temp_type = db.Column(db.String(20), default="room", comment="冷藏/冷冻/室温")
    sort = db.Column(db.Integer, default=0)
