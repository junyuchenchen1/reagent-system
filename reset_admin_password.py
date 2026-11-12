# -*- coding: utf-8 -*-
"""本机管理员密码重置工具：python reset_admin_password.py。"""
import secrets

from app import create_app
from app.extensions import db
from app.models.user import User


def main():
    app = create_app()
    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        if not admin:
            print("未找到 admin 账号；请先启动系统完成首次初始化。")
            return
        password = secrets.token_urlsafe(12)
        admin.status = 1
        admin.set_password(password)
        db.session.commit()
        print("管理员密码已重置。")
        print(f"账号：admin / admin\n密码：{password}")
        print("请立即登录并修改密码。")


if __name__ == "__main__":
    main()
