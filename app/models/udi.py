# -*- coding: utf-8 -*-
"""UDI（医疗器械唯一标识）产品库

由药监局 UDI 发布文件（ZIP 内的 XML）导入，按产品标识（DI）建索引，
供入库扫码时快速识别“这盒试剂是什么”。

reagent_id 是本系统内的试剂档案绑定关系：第一次扫某个 DI 并手工选定试剂后，
系统会记住这条对应关系，下次扫同一款产品直接选中，不再依赖名称模糊匹配。
"""
from datetime import datetime

from app.extensions import db


class UdiProduct(db.Model):
    __tablename__ = "udi_product"

    di = db.Column(db.String(32), primary_key=True,
                   comment="最小销售单元产品标识（GS1 GTIN-14 / MA 码）")
    coding = db.Column(db.String(20), comment="产品标识编码体系：GS1 / MA / ICCBBA")
    name = db.Column(db.String(200), index=True, comment="产品名称")
    spec = db.Column(db.String(255), comment="规格型号")
    mfr = db.Column(db.String(200), index=True, comment="注册人/备案人名称")
    reg_no = db.Column(db.String(200), comment="注册证编号/备案编号")
    cat = db.Column(db.String(20), comment="产品类别")
    source = db.Column(db.String(80), comment="来源发布文件")
    reagent_id = db.Column(db.Integer, db.ForeignKey("reagent_info.id"),
                           nullable=True, comment="已绑定的本系统试剂档案")
    update_time = db.Column(db.DateTime, default=datetime.now,
                            onupdate=datetime.now)

    reagent = db.relationship("Reagent")

    def to_dict(self) -> dict:
        return {
            "di": self.di, "coding": self.coding, "name": self.name,
            "spec": self.spec, "mfr": self.mfr, "reg_no": self.reg_no,
            "cat": self.cat, "reagent_id": self.reagent_id,
        }
