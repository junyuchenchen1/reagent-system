# -*- coding: utf-8 -*-
"""基础数据：试剂分类、供应商、试剂档案"""
import os
import uuid
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

from app.extensions import db
from app.models.reagent import Category, Supplier, Reagent, StorageLocation
from app.models.stock import (StockIn, Inventory, StockOut, ReagentPack, PackUse)
from app.utils.permission import manager_required
from app.utils.common import log_operation

basic_bp = Blueprint("basic", __name__)

CERT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "instance", "certs")
os.makedirs(CERT_DIR, exist_ok=True)
ALLOWED_IMG = {".png", ".jpg", ".jpeg", ".bmp"}


def save_cert_file(file):
    """保存上传的证件图片，返回文件名；无文件返回 None"""
    if not file or not file.filename:
        return None
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_IMG:
        flash("证件图片仅支持 PNG/JPG 格式", "danger")
        return None
    fname = f"cert_{uuid.uuid4().hex[:12]}{ext}"
    file.save(os.path.join(CERT_DIR, fname))
    return fname


# ===================== 试剂分类 =====================
@basic_bp.route("/category")
@login_required
def category_list():
    keyword = request.args.get("keyword", "").strip()
    query = Category.query
    if keyword:
        query = query.filter(Category.name.like(f"%{keyword}%"))
    items = query.order_by(Category.sort.asc(), Category.id.asc()).all()
    return render_template("basic/category.html", items=items, keyword=keyword)


@basic_bp.route("/category/save", methods=["POST"])
@login_required
@manager_required
def category_save():
    cid = request.form.get("id")
    name = request.form.get("name", "").strip()
    sort = request.form.get("sort", 0) or 0
    if not name:
        flash("分类名称不能为空", "danger")
        return redirect(url_for("basic.category_list"))

    if cid:
        obj = db.session.get(Category, int(cid))
        obj.name, obj.sort = name, int(sort)
        log_operation("基础数据", f"修改试剂分类：{name}")
    else:
        obj = Category(name=name, sort=int(sort))
        db.session.add(obj)
        log_operation("基础数据", f"新增试剂分类：{name}")
    db.session.commit()
    flash("保存成功", "success")
    return redirect(url_for("basic.category_list"))


@basic_bp.route("/category/delete", methods=["POST"])
@login_required
@manager_required
def category_delete():
    cid = int(request.form.get("id"))
    obj = db.session.get(Category, cid)
    if not obj:
        flash("该分类不存在", "danger")
    elif obj.reagents:
        flash(f"分类「{obj.name}」下仍有试剂，无法删除", "danger")
    else:
        name = obj.name
        try:
            db.session.delete(obj)
            db.session.commit()
            log_operation("基础数据", f"删除试剂分类：{name}")
            flash("删除成功", "success")
        except IntegrityError:
            db.session.rollback()
            flash("该分类仍被其他数据引用，无法删除", "danger")
    return redirect(url_for("basic.category_list"))


# ===================== 供应商 =====================
@basic_bp.route("/supplier")
@login_required
def supplier_list():
    keyword = request.args.get("keyword", "").strip()
    query = Supplier.query
    if keyword:
        query = query.filter(Supplier.name.like(f"%{keyword}%"))
    items = query.order_by(Supplier.id.desc()).all()
    return render_template("basic/supplier.html", items=items, keyword=keyword)


@basic_bp.route("/supplier/save", methods=["POST"])
@login_required
@manager_required
def supplier_save():
    sid = request.form.get("id")
    data = {
        "name": request.form.get("name", "").strip(),
        "contact": request.form.get("contact", "").strip(),
        "phone": request.form.get("phone", "").strip(),
        "address": request.form.get("address", "").strip(),
        "license_no": request.form.get("license_no", "").strip(),
        "supplier_type": request.form.get("supplier_type", "经销商").strip() or "经销商",
        "status": int(request.form.get("status", 1)),
        "remark": request.form.get("remark", "").strip(),
    }
    # 许可证有效期
    exp = request.form.get("license_expire_date", "").strip()
    data["license_expire_date"] = datetime.strptime(exp, "%Y-%m-%d").date() if exp else None
    if not data["name"]:
        flash("供应商名称不能为空", "danger")
        return redirect(url_for("basic.supplier_list"))

    # 证件图片
    cert = save_cert_file(request.files.get("cert_image"))

    if sid:
        obj = db.session.get(Supplier, int(sid))
        for k, v in data.items():
            setattr(obj, k, v)
        if cert:
            obj.cert_image = cert
        log_operation("基础数据", f"修改供应商：{data['name']}")
    else:
        if cert:
            data["cert_image"] = cert
        obj = Supplier(**data)
        db.session.add(obj)
        log_operation("基础数据", f"新增供应商：{data['name']}")
    db.session.commit()
    flash("保存成功", "success")
    return redirect(url_for("basic.supplier_list"))


@basic_bp.route("/supplier/delete", methods=["POST"])
@login_required
@manager_required
def supplier_delete():
    sid = int(request.form.get("id"))
    obj = db.session.get(Supplier, sid)
    if not obj:
        flash("该供应商不存在", "danger")
    elif StockIn.query.filter_by(supplier_id=sid).first():
        flash(f"供应商「{obj.name}」存在入库记录，无法删除，建议改为停用", "danger")
    else:
        name = obj.name
        try:
            db.session.delete(obj)
            db.session.commit()
            log_operation("基础数据", f"删除供应商：{name}")
            flash("删除成功", "success")
        except IntegrityError:
            db.session.rollback()
            flash("该供应商仍被其他数据引用，无法删除", "danger")
    return redirect(url_for("basic.supplier_list"))


# ===================== 存放位置 =====================
@basic_bp.route("/location")
@login_required
def location_list():
    keyword = request.args.get("keyword", "").strip()
    query = StorageLocation.query
    if keyword:
        query = query.filter(StorageLocation.name.like(f"%{keyword}%"))
    items = query.order_by(StorageLocation.sort.asc(), StorageLocation.id.asc()).all()
    return render_template("basic/location.html", items=items, keyword=keyword)


@basic_bp.route("/location/save", methods=["POST"])
@login_required
@manager_required
def location_save():
    lid = request.form.get("id")
    name = request.form.get("name", "").strip()
    sort = request.form.get("sort", 0) or 0
    status = int(request.form.get("status", 1) or 1)
    remark = request.form.get("remark", "").strip()
    if not name:
        flash("位置名称不能为空", "danger")
        return redirect(url_for("basic.location_list"))

    if lid:
        obj = db.session.get(StorageLocation, int(lid))
        obj.name, obj.sort, obj.status, obj.remark = name, int(sort), status, remark
        log_operation("基础数据", f"修改存放位置：{name}")
    else:
        obj = StorageLocation(name=name, sort=int(sort), status=status, remark=remark)
        db.session.add(obj)
        log_operation("基础数据", f"新增存放位置：{name}")
    db.session.commit()
    flash("保存成功", "success")
    return redirect(url_for("basic.location_list"))


@basic_bp.route("/location/delete", methods=["POST"])
@login_required
@manager_required
def location_delete():
    lid = int(request.form.get("id"))
    obj = db.session.get(StorageLocation, lid)
    if not obj:
        flash("该存放位置不存在", "danger")
        return redirect(url_for("basic.location_list"))
    name = obj.name
    if StockIn.query.filter_by(storage_location=name).first():
        flash(f"位置「{name}」已被入库记录使用，无法删除，建议改为停用", "danger")
    else:
        try:
            db.session.delete(obj)
            db.session.commit()
            log_operation("基础数据", f"删除存放位置：{name}")
            flash("删除成功", "success")
        except IntegrityError:
            db.session.rollback()
            flash("该位置仍被其他数据引用，无法删除", "danger")
    return redirect(url_for("basic.location_list"))


# ===================== 试剂档案 =====================
@basic_bp.route("/reagent")
@login_required
def reagent_list():
    keyword = request.args.get("keyword", "").strip()
    category_id = request.args.get("category_id", "").strip()
    if category_id and not category_id.isdigit():
        category_id = ""
    mfr = request.args.get("mfr", "")
    query = Reagent.query
    if keyword:
        query = query.filter(Reagent.name.like(f"%{keyword}%"))
    if category_id:
        query = query.filter(Reagent.category_id == int(category_id))
    if mfr:
        query = query.filter(Reagent.registrant == mfr)
    items = query.options(selectinload(Reagent.packs)).order_by(Reagent.id.desc()).all()
    categories = Category.query.order_by(Category.sort).all()
    # 厂家与分类的对应关系（供前端“选分类后厂家下拉自动筛选”使用）
    mfr_cat_rows = db.session.query(
        Reagent.registrant, Reagent.category_id).filter(
        Reagent.registrant != None, Reagent.registrant != '').distinct().all()
    mfr_cat_map = {}
    for name, cat_id in mfr_cat_rows:
        mfr_cat_map.setdefault(name, set()).add(str(cat_id) if cat_id else "")
    all_manufacturers = sorted(mfr_cat_map.keys())
    # 厂家下拉给出全集，由 data-cats + 前端联动决定可见范围（服务端仍按 category_id 过滤列表）
    manufacturers = all_manufacturers
    mfr_cat_map = {k: sorted(v) for k, v in mfr_cat_map.items()}
    cat_query = db.session.query(Reagent.category_id).filter(Reagent.category_id != None)
    if mfr:
        cat_query = cat_query.filter(Reagent.registrant == mfr)
        valid_cat_ids = set(r[0] for r in cat_query.distinct().all())
        filtered_categories = [c for c in categories if c.id in valid_cat_ids]
    else:
        filtered_categories = categories
    return render_template("basic/reagent.html", items=items,
                           categories=filtered_categories, manufacturers=manufacturers,
                           mfr_cat_map=mfr_cat_map,
                           keyword=keyword, category_id=category_id, mfr=mfr)


@basic_bp.route("/reagent/save", methods=["POST"])
@login_required
@manager_required
def reagent_save():
    rid = request.form.get("id")
    data = {
        "name": request.form.get("name", "").strip(),
        "category_id": request.form.get("category_id") or None,
        "spec": request.form.get("spec", "").strip(),
        "unit": request.form.get("unit", "盒").strip() or "盒",
        "pack_size": int(request.form.get("pack_size", 1) or 1),
        "small_unit": request.form.get("small_unit", "瓶").strip() or "瓶",
        "min_stock": int(request.form.get("min_stock", 10) or 0),
        "min_stock_unit": Reagent.MIN_UNIT_UNIT
        if request.form.get("min_stock_unit") == Reagent.MIN_UNIT_UNIT
        else Reagent.MIN_UNIT_PACK,
        "expire_warning_days": int(request.form.get("expire_warning_days", 90) or 0),
        "storage_condition": request.form.get("storage_condition", Reagent.STORAGE_COOL),
        "is_hazard": 1 if request.form.get("is_hazard") else 0,
        "status": 1 if request.form.get("status") else 0,
        "reg_no": request.form.get("reg_no", "").strip(),
        "registrant": request.form.get("registrant", "").strip(),
        "agent": request.form.get("agent", "").strip(),
        "remark": request.form.get("remark", "").strip(),
    }
    exp = request.form.get("reg_expire_date", "").strip()
    data["reg_expire_date"] = datetime.strptime(exp, "%Y-%m-%d").date() if exp else None
    if not data["name"]:
        flash("试剂名称不能为空", "danger")
        return redirect(url_for("basic.reagent_list"))

    cert = save_cert_file(request.files.get("cert_image"))

    if rid:
        obj = db.session.get(Reagent, int(rid))
        for k, v in data.items():
            setattr(obj, k, v)
        if cert:
            obj.cert_image = cert
        log_operation("基础数据", f"修改试剂档案：{data['name']}")
    else:
        if cert:
            data["cert_image"] = cert
        obj = Reagent(**data)
        db.session.add(obj)
        log_operation("基础数据", f"新增试剂档案：{data['name']}")
    db.session.commit()
    flash("保存成功", "success")
    return redirect(url_for("basic.reagent_list"))


@basic_bp.route("/reagent/delete", methods=["POST"])
@login_required
@manager_required
def reagent_delete():
    rid = int(request.form.get("id"))
    obj = db.session.get(Reagent, rid)
    history = (Inventory.query.filter_by(reagent_id=rid).first()
               or ReagentPack.query.filter_by(reagent_id=rid).first()
               or StockIn.query.filter_by(reagent_id=rid).first()
               or StockOut.query.filter_by(reagent_id=rid).first()
               or PackUse.query.filter_by(reagent_id=rid).first())
    if not obj:
        flash("该试剂不存在", "danger")
    elif history:
        flash(f"试剂「{obj.name}」存在库存或历史记录，无法删除，建议改为停用", "danger")
    else:
        name = obj.name
        try:
            db.session.delete(obj)
            db.session.commit()
            log_operation("基础数据", f"删除试剂档案：{name}")
            flash("删除成功", "success")
        except IntegrityError:
            db.session.rollback()
            flash("该试剂仍被其他数据引用，无法删除，建议改为停用", "danger")
    return redirect(url_for("basic.reagent_list"))


@basic_bp.route("/cert/<path:fname>")
@login_required
def view_cert(fname):
    """查看证件图片"""
    from flask import send_from_directory
    return send_from_directory(CERT_DIR, fname)
