# -*- coding: utf-8 -*-
"""用户模型与角色定义"""
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from app.extensions import db


class User(UserMixin, db.Model):
    __tablename__ = "sys_user"

    # 角色常量
    ROLE_ADMIN = "admin"      # 系统管理员
    ROLE_MANAGER = "manager"  # 试剂管理员
    ROLE_STAFF = "staff"      # 检验人员

    ROLE_MAP = {
        ROLE_ADMIN: "系统管理员",
        ROLE_MANAGER: "试剂管理员",
        ROLE_STAFF: "检验人员",
    }

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False, index=True, comment="登录账号")
    password_hash = db.Column(db.String(255), nullable=False, comment="加密密码")
    real_name = db.Column(db.String(50), nullable=False, comment="真实姓名")
    department = db.Column(db.String(50), default="检验科", comment="所属科室")
    phone = db.Column(db.String(20), comment="联系电话")
    role = db.Column(db.String(20), nullable=False, default=ROLE_STAFF, comment="角色")
    status = db.Column(db.SmallInteger, default=1, comment="状态：1正常 0禁用")
    signature_image = db.Column(db.String(255), comment="手写签名图片文件名")
    create_time = db.Column(db.DateTime, default=datetime.now)

    # ---------- 密码 ----------
    def set_password(self, raw: str):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)

    # ---------- 角色辅助 ----------
    @property
    def role_name(self) -> str:
        return self.ROLE_MAP.get(self.role, self.role)

    @property
    def is_admin(self) -> bool:
        return self.role == self.ROLE_ADMIN

    @property
    def is_manager(self) -> bool:
        return self.role in (self.ROLE_ADMIN, self.ROLE_MANAGER)

    def has_role(self, *roles) -> bool:
        return self.role in roles

    # ---------- 登录状态 ----------
    @property
    def is_active(self) -> bool:
        return self.status == 1

    def __repr__(self):
        return f"<User {self.username}>"
