# -*- coding: utf-8 -*-
"""认证路由：登录、登出、修改密码"""
import os
import time
from collections import defaultdict
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_user, logout_user, login_required, current_user

from app.extensions import db
from app.models.user import User
from app.utils.common import log_operation
from app.utils.security import is_safe_next_url

auth_bp = Blueprint("auth", __name__)

# 登录失败锁定：内存字典 {username: [失败次数, 锁定截止时间戳]}
_MAX_FAIL = 10
_LOCK_SECONDS = 180  # 3 分钟
_login_attempts = defaultdict(lambda: [0, 0.0])


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        # 检查是否被锁定
        rec = _login_attempts[username]
        now = time.time()
        if rec[1] > now:
            left = int(rec[1] - now)
            flash(f"账号已被锁定，请约 {left // 60 + 1} 分钟后再试（连续失败 {_MAX_FAIL} 次）", "danger")
            return render_template("auth/login.html")

        user = User.query.filter_by(username=username).first()

        if user is None:
            # 账号不存在不累计锁定次数
            flash("账号不存在，请检查用户名", "danger")
        elif not user.check_password(password):
            rec[0] += 1
            if rec[0] >= _MAX_FAIL:
                rec[1] = now + _LOCK_SECONDS
                rec[0] = 0
                flash("密码错误次数过多，账号已锁定 3 分钟", "danger")
            else:
                flash(f"密码错误（已失败 {rec[0]}/{_MAX_FAIL} 次）", "danger")
        elif not user.is_active:
            flash("该账号已被禁用，请联系管理员", "danger")
        else:
            # 登录成功，清零失败计数
            _login_attempts.pop(username, None)
            login_user(user, remember=bool(request.form.get("remember")))
            # 首次凭据只用于找回初始登录信息；成功登录后立即移除。
            credential_file = os.path.join(current_app.config["BASE_DIR"], "instance", "initial_admin_password.txt")
            if user.username == "admin" and os.path.exists(credential_file):
                try:
                    os.remove(credential_file)
                except OSError:
                    pass
            log_operation("登录认证", f"用户 {user.real_name} 登录系统")
            next_url = request.args.get("next")
            if not is_safe_next_url(next_url):
                next_url = url_for("dashboard.index")
            return redirect(next_url)

    return render_template("auth/login.html")


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    log_operation("登录认证", f"用户 {current_user.real_name} 退出系统")
    logout_user()
    flash("您已安全退出系统", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        old_pwd = request.form.get("old_password", "")
        new_pwd = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        if not current_user.check_password(old_pwd):
            flash("原密码不正确", "danger")
        elif len(new_pwd) < 6:
            flash("新密码长度不能少于 6 位", "danger")
        elif new_pwd != confirm:
            flash("两次输入的新密码不一致", "danger")
        else:
            current_user.set_password(new_pwd)
            db.session.commit()
            log_operation("登录认证", "修改了登录密码")
            flash("密码修改成功，下次登录请使用新密码", "success")
            return redirect(url_for("dashboard.index"))

    return render_template("auth/change_password.html")
