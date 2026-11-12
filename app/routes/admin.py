# -*- coding: utf-8 -*-
"""系统管理：用户管理、操作日志"""
import os
import uuid
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_from_directory
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.user import User
from app.models.log import OperLog
from app.models.stock import StockIn, StockOut, ReagentPack, PackUse
from app.utils.permission import admin_required
from app.utils.common import log_operation, export_excel, send_excel

admin_bp = Blueprint("admin", __name__)

SIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "instance", "signatures")
os.makedirs(SIG_DIR, exist_ok=True)


# ===================== 用户管理 =====================
@admin_bp.route("/user")
@login_required
@admin_required
def user_list():
    keyword = request.args.get("keyword", "").strip()
    query = User.query
    if keyword:
        query = query.filter(
            db.or_(User.username.like(f"%{keyword}%"),
                   User.real_name.like(f"%{keyword}%")))
    items = query.order_by(User.id.asc()).all()
    return render_template("admin/user.html", items=items, keyword=keyword,
                           role_map=User.ROLE_MAP)


@admin_bp.route("/user/save", methods=["POST"])
@login_required
@admin_required
def user_save():
    uid = request.form.get("id")
    username = request.form.get("username", "").strip()
    real_name = request.form.get("real_name", "").strip()
    password = request.form.get("password", "")
    data = {
        "real_name": real_name,
        "department": request.form.get("department", "检验科").strip(),
        "phone": request.form.get("phone", "").strip(),
        "role": request.form.get("role", User.ROLE_STAFF),
        "status": int(request.form.get("status", 1)),
    }

    if uid:
        user = db.session.get(User, int(uid))
        for k, v in data.items():
            setattr(user, k, v)
        if password:
            user.set_password(password)
        # 签名图片
        sig = request.files.get("signature_image")
        if sig and sig.filename:
            ext = os.path.splitext(sig.filename)[1].lower()
            if ext in {".png", ".jpg", ".jpeg", ".bmp"}:
                fname = f"sig_{uuid.uuid4().hex[:12]}{ext}"
                sig.save(os.path.join(SIG_DIR, fname))
                if user.signature_image:
                    old = os.path.join(SIG_DIR, user.signature_image)
                    if os.path.exists(old):
                        try: os.remove(old)
                        except Exception: pass
                user.signature_image = fname
        elif request.form.get("remove_signature") == "1":
            if user.signature_image:
                old = os.path.join(SIG_DIR, user.signature_image)
                if os.path.exists(old):
                    try: os.remove(old)
                    except Exception: pass
                user.signature_image = None
        log_operation("用户管理", f"修改用户：{user.username}")
        flash("用户信息已更新", "success")
    else:
        if not username or not real_name:
            flash("账号和姓名不能为空", "danger")
            return redirect(url_for("admin.user_list"))
        if User.query.filter_by(username=username).first():
            flash(f"账号 {username} 已存在", "danger")
            return redirect(url_for("admin.user_list"))
        if len(password) < 6:
            flash("新建用户密码不能少于 6 位", "danger")
            return redirect(url_for("admin.user_list"))
        user = User(username=username, **data)
        user.set_password(password)
        # 签名图片
        sig = request.files.get("signature_image")
        if sig and sig.filename:
            ext = os.path.splitext(sig.filename)[1].lower()
            if ext in {".png", ".jpg", ".jpeg", ".bmp"}:
                fname = f"sig_{uuid.uuid4().hex[:12]}{ext}"
                sig.save(os.path.join(SIG_DIR, fname))
                user.signature_image = fname
        db.session.add(user)
        log_operation("用户管理", f"新建用户：{username}")
        flash("用户创建成功", "success")
    db.session.commit()
    return redirect(url_for("admin.user_list"))


@admin_bp.route("/user/remove_sig", methods=["POST"])
@login_required
@admin_required
def user_remove_sig():
    uid = int(request.form.get("id"))
    user = db.session.get(User, uid)
    if user and user.signature_image:
        old = os.path.join(SIG_DIR, user.signature_image)
        if os.path.exists(old):
            try:
                os.remove(old)
            except Exception:
                pass
        user.signature_image = None
        db.session.commit()
        flash("签名已移除", "success")
    return redirect(url_for("admin.user_list"))


@admin_bp.route("/sig/<path:fname>")
@login_required
def view_sig(fname):
    return send_from_directory(SIG_DIR, fname)


@admin_bp.route("/user/delete", methods=["POST"])
@login_required
@admin_required
def user_delete():
    uid = int(request.form.get("id"))
    if uid == current_user.id:
        flash("不能删除当前登录账号", "danger")
        return redirect(url_for("admin.user_list"))
    user = db.session.get(User, uid)
    if not user:
        flash("该账号不存在", "danger")
        return redirect(url_for("admin.user_list"))
    if user.is_admin and User.query.filter_by(
            role=User.ROLE_ADMIN, status=1).count() <= 1:
        flash("系统至少需要保留一个可用的管理员账号", "danger")
        return redirect(url_for("admin.user_list"))
    # 有业务记录的用户不能物理删除，否则历史单据上的签字人会变成空号
    used = (StockIn.query.filter(
                db.or_(StockIn.operator_id == uid, StockIn.verifier_id == uid)).first()
            or StockOut.query.filter(db.or_(
                StockOut.applicant_id == uid, StockOut.confirmer_id == uid,
                StockOut.receiver_id == uid)).first()
            or ReagentPack.query.filter_by(receiver_id=uid).first()
            or PackUse.query.filter_by(user_id=uid).first())
    if used:
        flash(f"用户「{user.real_name}」已有业务记录，无法删除；请改为「停用」以保留追溯", "danger")
        return redirect(url_for("admin.user_list"))
    username = user.username
    sig = user.signature_image
    try:
        db.session.delete(user)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("该用户仍被数据引用，无法删除，请改为停用", "danger")
        return redirect(url_for("admin.user_list"))
    if sig:
        old = os.path.join(SIG_DIR, sig)
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                pass
    log_operation("用户管理", f"删除用户：{username}")
    flash("用户已删除", "success")
    return redirect(url_for("admin.user_list"))


# ===================== 操作日志 =====================
@admin_bp.route("/log")
@login_required
@admin_required
def log_list():
    page = int(request.args.get("page", 1))
    keyword = request.args.get("keyword", "").strip()
    module = request.args.get("module", "").strip()
    query = OperLog.query
    if keyword:
        query = query.filter(
            db.or_(OperLog.username.like(f"%{keyword}%"),
                   OperLog.content.like(f"%{keyword}%")))
    if module:
        query = query.filter(OperLog.module == module)
    pagination = query.order_by(OperLog.id.desc()).paginate(
        page=page, per_page=20, error_out=False)
    modules = [m[0] for m in db.session.query(OperLog.module).distinct().all()
               if m[0]]
    return render_template("admin/log.html", pagination=pagination,
                           keyword=keyword, module=module, modules=modules)


@admin_bp.route("/log/export")
@login_required
@admin_required
def log_export():
    logs = OperLog.query.order_by(OperLog.id.desc()).limit(5000).all()
    # 按 user_id 批量取真实姓名，避免逐行查询
    uid_set = {l.user_id for l in logs if l.user_id}
    name_map = ({u.id: u.real_name for u in User.query.filter(User.id.in_(uid_set)).all()}
                if uid_set else {})
    rows = [[name_map.get(l.user_id) or "系统", l.username, l.module, l.content, l.ip,
             l.create_time.strftime("%Y-%m-%d %H:%M:%S")] for l in logs]
    filename = f"操作日志_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    path = export_excel("操作日志", ["操作人", "操作账号", "模块", "内容", "IP", "时间"], rows, filename)
    return send_excel(path, filename)
