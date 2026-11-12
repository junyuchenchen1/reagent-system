# -*- coding: utf-8 -*-
"""双击使用的本机管理员密码工具（Windows）。"""
import os
import secrets
import tkinter as tk
from tkinter import messagebox

from app import create_app
from app.extensions import db
from app.models.user import User

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIAL_FILE = os.path.join(BASE_DIR, "instance", "initial_admin_password.txt")


class PasswordTool(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("检验科试剂管理系统 - 管理员密码工具")
        self.resizable(False, False)
        self.configure(padx=28, pady=24)
        self.password = tk.StringVar(value="点击下方按钮读取或重置密码")
        tk.Label(self, text="管理员密码工具", font=("Microsoft YaHei", 16, "bold")).pack(anchor="w")
        tk.Label(self, text="仅限在服务器电脑本机使用。", fg="#666666").pack(anchor="w", pady=(4, 16))
        tk.Label(self, text="账号：admin", font=("Microsoft YaHei", 11)).pack(anchor="w")
        tk.Entry(self, textvariable=self.password, width=38, font=("Consolas", 12),
                 state="readonly", readonlybackground="white").pack(fill="x", pady=(8, 14))
        buttons = tk.Frame(self)
        buttons.pack(fill="x")
        tk.Button(buttons, text="读取首次密码", command=self.load_initial, width=15).pack(side="left")
        tk.Button(buttons, text="复制密码", command=self.copy_password, width=12).pack(side="left", padx=8)
        tk.Button(buttons, text="重置管理员密码", command=self.reset_password, width=16).pack(side="right")
        tk.Label(self, text="首次成功登录后，首次密码文件会自动删除；忘记密码时可用“重置”。",
                 fg="#8a6d3b", wraplength=410, justify="left").pack(anchor="w", pady=(18, 0))

    def load_initial(self):
        if not os.path.exists(CREDENTIAL_FILE):
            messagebox.showinfo("未找到", "首次密码文件不存在。可能已成功登录过；请使用“重置管理员密码”。")
            return
        try:
            with open(CREDENTIAL_FILE, "r", encoding="utf-8") as f:
                password_line = next((line for line in f if line.startswith("密码：")), "")
            password = password_line.removeprefix("密码：").strip()
            if not password:
                raise ValueError("密码内容为空")
            self.password.set(password)
            messagebox.showinfo("已读取", "已读取首次管理员密码，可点击“复制密码”。")
        except Exception as exc:
            messagebox.showerror("读取失败", f"无法读取首次密码文件：{exc}")

    def copy_password(self):
        password = self.password.get()
        if not password or password.startswith("点击"):
            messagebox.showwarning("请先读取", "请先读取首次密码或重置管理员密码。")
            return
        self.clipboard_clear()
        self.clipboard_append(password)
        self.update()
        messagebox.showinfo("已复制", "密码已复制到剪贴板。")

    def reset_password(self):
        if not messagebox.askyesno("确认重置", "确定要重置 admin 的密码吗？\n\n重置后旧密码将立即失效。", icon="warning"):
            return
        try:
            app = create_app()
            with app.app_context():
                admin = User.query.filter_by(username="admin").first()
                if not admin:
                    raise ValueError("未找到 admin 账号，请先启动系统完成首次初始化。")
                password = secrets.token_urlsafe(12)
                admin.status = 1
                admin.set_password(password)
                db.session.commit()
            self.password.set(password)
            self.clipboard_clear()
            self.clipboard_append(password)
            self.update()
            messagebox.showinfo("重置成功", "管理员密码已重置，并已复制到剪贴板。")
        except Exception as exc:
            messagebox.showerror("重置失败", str(exc))


if __name__ == "__main__":
    PasswordTool().mainloop()
