# -*- coding: utf-8 -*-
"""试剂使用模块：整盒出库后，开盒按瓶登记实际消耗；统计已出库未使用余量"""
from datetime import datetime
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify)
from flask_login import login_required, current_user

from app.extensions import db
from app.models.reagent import Reagent, Category
from app.models.stock import ReagentPack, PackUse
from app.services import stock_service
from app.utils.common import log_operation, export_excel, send_excel, parse_export_range, range_label
from app.utils.paging import page_arg, paginate_query

usage_bp = Blueprint("usage", __name__, url_prefix="/usage")


@usage_bp.route("/")
@login_required
def index():
    """已出库未使用（科室在用）盒列表"""
    keyword = request.args.get("keyword", "").strip()
    category_id = request.args.get("category_id", "")
    mfr = request.args.get("mfr", "")
    page = page_arg()

    query = ReagentPack.query.join(Reagent, ReagentPack.reagent_id == Reagent.id).filter(
        ReagentPack.status.in_([ReagentPack.STATUS_ISSUED, ReagentPack.STATUS_USING]))
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(db.or_(
            Reagent.name.like(like), ReagentPack.barcode.like(like),
            ReagentPack.batch_no.like(like)))
    if category_id:
        query = query.filter(Reagent.category_id == int(category_id))
    if mfr:
        query = query.filter(Reagent.registrant == mfr)

    pagination = paginate_query(query.order_by(
        ReagentPack.status.desc(), ReagentPack.expire_date.asc(),
        ReagentPack.id.asc()))

    total_boxes = ReagentPack.query.filter(ReagentPack.status.in_(
        [ReagentPack.STATUS_ISSUED, ReagentPack.STATUS_USING])).count()
    total_units = db.session.query(db.func.coalesce(
        db.func.sum(ReagentPack.total_units - ReagentPack.used_units), 0)).filter(
        ReagentPack.status.in_(
            [ReagentPack.STATUS_ISSUED, ReagentPack.STATUS_USING])).scalar()
    categories = Category.query.order_by(Category.sort).all()
    mfr_query = db.session.query(Reagent.registrant).join(
        ReagentPack, ReagentPack.reagent_id == Reagent.id).filter(
        ReagentPack.status.in_([ReagentPack.STATUS_ISSUED, ReagentPack.STATUS_USING]),
        Reagent.registrant != None, Reagent.registrant != '')
    if category_id:
        mfr_query = mfr_query.filter(Reagent.category_id == int(category_id))
    manufacturers = [r[0] for r in mfr_query.distinct().order_by(Reagent.registrant).all()]
    cat_query = db.session.query(Reagent.category_id).join(
        ReagentPack, ReagentPack.reagent_id == Reagent.id).filter(
        ReagentPack.status.in_([ReagentPack.STATUS_ISSUED, ReagentPack.STATUS_USING]),
        Reagent.category_id != None)
    if mfr:
        cat_query = cat_query.filter(Reagent.registrant == mfr)
        valid_cat_ids = set(r[0] for r in cat_query.distinct().all())
        filtered_categories = [c for c in categories if c.id in valid_cat_ids]
    else:
        filtered_categories = categories
    return render_template(
        "usage/index.html", pagination=pagination, keyword=keyword,
        category_id=category_id, mfr=mfr, categories=filtered_categories,
        manufacturers=manufacturers,
        total_boxes=total_boxes, total_units=int(total_units or 0))


@usage_bp.route("/add", methods=["GET", "POST"])
@login_required
def add():
    """登记使用（按瓶）"""
    if request.method == "POST":
        barcode = request.form.get("barcode", "").strip()
        units = int(request.form.get("units_used", 0) or 0)
        use_type = request.form.get("use_type", "use")
        remark = request.form.get("remark", "").strip()
        pack = stock_service.get_pack_by_barcode(barcode)
        if not pack:
            flash("未找到该条码对应的试剂盒", "danger")
        else:
            try:
                record = stock_service.register_use(
                    pack, units, use_type, current_user, remark)
                if pack.status == ReagentPack.STATUS_EMPTY:
                    tail = "该盒已全部用完"
                else:
                    tail = f"该盒剩余 {pack.remaining_units} {pack.reagent.small_unit}"
                log_operation("试剂使用",
                              f"{pack.reagent.name}（{pack.barcode}）使用 {units} "
                              f"{pack.reagent.small_unit}，{tail}")
                flash(f"登记成功：本次使用 {units} {pack.reagent.small_unit}，{tail}",
                      "success")
                return redirect(url_for("usage.index"))
            except ValueError as e:
                flash(str(e), "danger")

    reagents = Reagent.query.filter_by(status=1).order_by(Reagent.name).all()
    categories = Category.query.order_by(Category.sort).all()
    manufacturers = [r[0] for r in db.session.query(Reagent.registrant).filter(
        Reagent.status == 1, Reagent.registrant != None, Reagent.registrant != '').distinct().order_by(Reagent.registrant).all()]
    return render_template("usage/add.html", reagents=reagents,
                           categories=categories, manufacturers=manufacturers)


@usage_bp.route("/records")
@login_required
def records():
    """使用记录"""
    keyword = request.args.get("keyword", "").strip()
    page = page_arg()
    query = PackUse.query.join(Reagent, PackUse.reagent_id == Reagent.id)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(db.or_(
            Reagent.name.like(like), PackUse.use_no.like(like),
            PackUse.pack.has(ReagentPack.barcode.like(like))))
    pagination = paginate_query(query.order_by(PackUse.id.desc()))
    return render_template("usage/records.html", pagination=pagination,
                           keyword=keyword)


# ---------------- 接口 ----------------
@usage_bp.route("/scan")
@login_required
def scan_api():
    """扫码定位可使用的盒（必须已出库/使用中）"""
    barcode = request.args.get("barcode", "").strip()
    pack = stock_service.get_pack_by_barcode(barcode)
    if not pack:
        return jsonify({"found": False, "message": "未找到该条码对应的试剂盒"})
    r = pack.reagent
    can_use = pack.status in (ReagentPack.STATUS_ISSUED, ReagentPack.STATUS_USING)
    message = ""
    if pack.status == ReagentPack.STATUS_IN_STOCK:
        message = "该盒仍在中心库，请先在【试剂出库】整盒出库后再使用"
    elif pack.status in (ReagentPack.STATUS_EMPTY, ReagentPack.STATUS_SCRAPPED):
        message = f"该盒已{pack.status_text}，无法登记使用"
    return jsonify({
        "found": True,
        "can_use": can_use,
        "message": message,
        "barcode": pack.barcode,
        "reagent_id": r.id,
        "reagent_name": r.name,
        "batch_no": pack.batch_no,
        "expire_date": pack.expire_date.strftime("%Y-%m-%d"),
        "days_left": pack.days_left,
        "status": pack.status,
        "status_text": pack.status_text,
        "total_units": pack.total_units,
        "used_units": pack.used_units,
        "remaining_units": pack.remaining_units,
        "small_unit": r.small_unit,
    })


@usage_bp.route("/packs")
@login_required
def packs_api():
    """按试剂列出已出库、可使用的盒（优先使用中）"""
    reagent_id = request.args.get("reagent_id", type=int)
    if not reagent_id:
        return jsonify({"found": False, "packs": []})
    packs = stock_service.get_issued_packs_for_use(reagent_id)
    return jsonify({
        "found": True,
        "small_unit": packs[0].reagent.small_unit if packs else "瓶",
        "packs": [{
            "barcode": p.barcode,
            "batch_no": p.batch_no,
            "expire_date": p.expire_date.strftime("%Y-%m-%d"),
            "days_left": p.days_left,
            "status": p.status,
            "status_text": p.status_text,
            "used_units": p.used_units,
            "remaining_units": p.remaining_units,
            "total_units": p.total_units,
        } for p in packs],
    })


@usage_bp.route("/export")
@login_required
def records_export():
    keyword = request.args.get("keyword", "").strip()
    query = PackUse.query.join(Reagent, PackUse.reagent_id == Reagent.id)
    if keyword:
        query = query.filter(Reagent.name.like(f"%{keyword}%"))
    # 导出时间范围：最近一周/一月/半年/一年/自定义起止
    start, end = parse_export_range()
    if start:
        query = query.filter(db.func.date(PackUse.create_time) >= start)
    if end:
        query = query.filter(db.func.date(PackUse.create_time) <= end)
    rows = []
    for u in query.order_by(PackUse.create_time.asc(), PackUse.id.asc()).all():
        rows.append([
            u.use_no, u.reagent.name, u.reagent.registrant or "", u.reagent.spec or "",
            u.pack.barcode, u.pack.batch_no,
            u.pack.expire_date.strftime('%Y-%m-%d') if u.pack.expire_date else "",
            u.units_used, u.reagent.small_unit or "瓶",
            u.type_text,
            (u.user_id, u.user.real_name) if u.user else "",
            u.create_time.strftime("%Y-%m-%d %H:%M"),
            u.remark or "",
        ])
    filename = f"试剂使用记录_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    path = export_excel(
        "试剂使用记录",
        ["使用单号", "试剂名称", "生产企业", "规格", "条码", "批号", "有效期",
         "使用数量", "单位", "类型", "使用人", "使用时间", "备注"],
        rows, filename)
    log_operation("试剂使用", f"导出试剂使用记录 Excel（{range_label(start, end)}）")
    return send_excel(path, filename)
