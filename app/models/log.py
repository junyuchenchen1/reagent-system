# -*- coding: utf-8 -*-
"""操作日志模型"""
from datetime import datetime
from app.extensions import db


class OperLog(db.Model):
    __tablename__ = "sys_oper_log"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, comment="操作人ID")
    username = db.Column(db.String(50), comment="操作账号")
    module = db.Column(db.String(50), comment="操作模块")
    content = db.Column(db.String(500), comment="操作内容")
    ip = db.Column(db.String(50), comment="操作IP")
    create_time = db.Column(db.DateTime, default=datetime.now, index=True)

    def __repr__(self):
        return f"<OperLog {self.module} {self.content}>"
