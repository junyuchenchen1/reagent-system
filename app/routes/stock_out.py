# -*- coding: utf-8 -*-
"""出库管理：整盒出库、扫码定位、FIFO 批号推荐、确认、导出"""
from datetime import datetime, date
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort, jsonify)
from flask_login import login_required, current_user

from app.extensions import db
from app.models.reagent import Reagent, Category
from app.models.stock import StockOut, ReagentPack, Inventory
from app.services import stock_service
from app.utils.permission import manager_required
from app.utils.common import log_operation, export_excel, send_excel, parse_export_range, range_label
from app.models.user import User
from werkzeug.security import check_password_hash

stock_out_bp = Blueprint("stock_out", __name__)


@stock_out_bp.route("/")
@login_required
def out_list():
    page = int(request.args.get("page", 1))
    keyword = request.args.get("keyword", "").strip()
    out_type = request.args.get("out_type", "")
    status = request.args.get("status", "")

    query = StockOut.query.join(Reagent, StockOut.reagent_id == Reagent.id)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            db.or_(StockOut.order_no.like(like), StockOut.scan_barcode.like(like),
                   Reagent.name.like(like), StockOut.receiver.like(like)))
    if out_type:
        query = query.filter(StockOut.out_type == out_type)
    if status != "":
        query = query.filter(StockOut.status == int(status))

    pagination = query.order_by(StockOut.id.desc()).paginate(
        page=page, per_page=15, error_out=False)
    # 预检查待确认出库单的批号验证状态
    from app.models.stock import StockIn as SI, ReagentPack
    for out in pagination.items:
        if out.status == 0:
            if out.scan_barcode:
                p = ReagentPack.query.filter_by(barcode=out.scan_barcode,
                    status=ReagentPack.STATUS_IN_STOCK).first()
                batches = {p.batch_no} if p else set()
            else:
                packs = ReagentPack.query.filter_by(
                    reagent_id=out.reagent_id,
                    status=ReagentPack.STATUS_IN_STOCK
                ).order_by(ReagentPack.expire_date.asc()).limit(out.quantity).all()
                batches = {p.batch_no for p in packs}
            out._unverified = []
            for bn in batches:
                s = SI.query.filter_by(reagent_id=out.reagent_id, batch_no=bn,
                                        status=SI.STATUS_DONE).first()
                if s and not s.is_verified:
                    out._unverified.append(bn)
        else:
            out._unverified = []
    if current_user.is_manager:
        # 管理员出库：组低耗员=自己，接收人可选其他管理员+检验人员
        receivers = User.query.filter(
            User.id != current_user.id, User.status == 1
        ).order_by(User.real_name).all()
        confirmer_candidates = []
    else:
        # 检验人员出库：接收人=自己，组低耗员选管理员
        receivers = []
        confirmer_candidates = User.query.filter(
            User.role.in_([User.ROLE_ADMIN, User.ROLE_MANAGER]),
            User.status == 1,
            User.username != 'admin'
        ).order_by(User.real_name).all()
    return render_template("stock/out_list.html", pagination=pagination,
                           keyword=keyword, out_type=out_type, status=status,
                           receivers=receivers,
                           confirmer_candidates=confirmer_candidates)


@stock_out_bp.route("/add", methods=["GET", "POST"])
@login_required
def out_add():
    if request.method == "POST":
        data = {
            "reagent_id": request.form.get("reagent_id") or None,
            "quantity": int(request.form.get("quantity", 1) or 1),
            "out_type": request.form.get("out_type", "use"),
            "scan_barcode": request.form.get("scan_barcode", "").strip(),
            "receiver": request.form.get("receiver", "").strip(),
            "reason": request.form.get("reason", "").strip(),
        }
        try:
            out = stock_service.create_stock_out(data, current_user)
            log_operation("出库管理", f"提交出库申请：{out.reagent.name} {out.quantity} {out.reagent.unit}")
            flash("出库申请已提交，请确认出库人后完成出库", "success")
            return redirect(url_for("stock_out.out_list"))
        except ValueError as e:
            flash(str(e), "danger")

    reagents = Reagent.query.filter_by(status=1).order_by(Reagent.name).all()
    categories = Category.query.order_by(Category.sort).all()
    manufacturers = [r[0] for r in db.session.query(Reagent.registrant).filter(
        Reagent.status == 1, Reagent.registrant != None, Reagent.registrant != '').distinct().order_by(Reagent.registrant).all()]
    return render_template("stock/out_add.html", reagents=reagents,
                           categories=categories, manufacturers=manufacturers)


@stock_out_bp.route("/<int:sid>/confirm", methods=["POST"])
@login_required
def out_confirm(sid):
    out = db.session.get(StockOut, sid) or abort(404)
    from app.models.stock import StockIn as SI, ReagentPack
    # 模拟分配，检查涉及批号是否已验证
    if out.scan_barcode:
        pack = ReagentPack.query.filter_by(barcode=out.scan_barcode,
            status=ReagentPack.STATUS_IN_STOCK).first()
        check_batches = {pack.batch_no} if pack else set()
    else:
        packs = ReagentPack.query.filter_by(
            reagent_id=out.reagent_id,
            status=ReagentPack.STATUS_IN_STOCK
        ).order_by(ReagentPack.expire_date.asc()).limit(out.quantity).all()
        check_batches = {p.batch_no for p in packs}
    unverified = []
    for bn in check_batches:
        s = SI.query.filter_by(reagent_id=out.reagent_id, batch_no=bn,
                                status=SI.STATUS_DONE).first()
        if s and not s.is_verified:
            unverified.append(bn)
    if unverified and request.form.get("force") != "yes":
        flash("警告：批号 " + ", ".join(unverified) + " 尚未完成性能验证，确认仍要出库吗？请再次点击确认出库", "warning")
        return redirect(url_for("stock_out.out_list"))

    if current_user.is_manager:
        # 管理员出库：组低耗员=当前登录人
        confirmer = current_user
        # 接收人选择
        receiver_id = request.form.get("receiver_id", type=int)
        receiver = db.session.get(User, receiver_id) if receiver_id else None
        if not receiver or receiver.status != 1:
            flash("请选择接收人", "danger")
            return redirect(url_for("stock_out.out_list"))
    else:
        # 检验人员出库：接收人=当前登录人，组低耗员选管理员
        receiver = current_user
        confirmer_id = request.form.get("confirmer_id", type=int)
        confirmer = db.session.get(User, confirmer_id) if confirmer_id else None
        if not confirmer or not confirmer.is_manager or confirmer.status != 1:
            flash("请选择组低耗员（管理员）", "danger")
            return redirect(url_for("stock_out.out_list"))
        if confirmer.id != current_user.id:
            pwd = request.form.get("confirmer_password") or ""
            if not confirmer.check_password(pwd):
                flash(f"「{confirmer.real_name}」的密码错误，出库未确认", "danger")
                return redirect(url_for("stock_out.out_list"))
    # 接收人先写到出库单上，确认出库时每盒的接收人也随之写入（见 confirm_stock_out）
    out.receiver_id = receiver.id
    out.receiver = receiver.real_name
    db.session.commit()
    try:
        stock_service.confirm_stock_out(out, confirmer)
        log_operation("出库管理", f"确认出库单 {out.order_no}，{out.quantity} {out.reagent.unit}（{confirmer.real_name} 组低耗员，{receiver.real_name} 接收）")
        flash(f"出库成功：{confirmer.real_name}（组低耗员）→ {receiver.real_name}（接收人）", "success")
    except ValueError as e:
        flash(str(e), "danger")
    return redirect(url_for("stock_out.out_list"))


@stock_out_bp.route("/<int:sid>/cancel", methods=["POST"])
@login_required
def out_cancel(sid):
    out = db.session.get(StockOut, sid) or abort(404)
    try:
        stock_service.cancel_stock_out(out)
        log_operation("出库管理", f"取消出库单 {out.order_no}")
        flash("已取消该出库单", "warning")
    except ValueError as e:
        flash(str(e), "danger")
    return redirect(url_for("stock_out.out_list"))


@stock_out_bp.route("/<int:sid>")
@login_required
def out_detail(sid):
    out = db.session.get(StockOut, sid) or abort(404)
    packs = ReagentPack.query.filter_by(stock_out_id=sid).order_by(
        ReagentPack.barcode).all()
    return render_template("stock/out_detail.html", out=out, packs=packs)


# ---------- 接口 ----------
@stock_out_bp.route("/batches")
@login_required
def batches_api():
    """选试剂后返回在库整盒按批次分组的 FIFO 信息"""
    reagent_id = request.args.get("reagent_id", type=int)
    reagent = db.session.get(Reagent, reagent_id) if reagent_id else None
    if not reagent:
        return jsonify({"found": False})

    packs = ReagentPack.query.filter_by(
        reagent_id=reagent_id, status=ReagentPack.STATUS_IN_STOCK
    ).order_by(ReagentPack.expire_date.asc()).all()

    groups = {}
    for p in packs:
        groups.setdefault(p.batch_no, {
            "batch_no": p.batch_no,
            "expire_date": p.expire_date.strftime("%Y-%m-%d"),
            "days_left": p.days_left,
            "boxes": 0,
            "expired": p.days_left < 0,
        })
        groups[p.batch_no]["boxes"] += 1

    valid = [g for g in groups.values() if not g["expired"]]
    expired_groups = [g for g in groups.values() if g["expired"]]
    total = len(packs)
    valid_total = sum(g["boxes"] for g in valid)
    expired_total = sum(g["boxes"] for g in expired_groups)
    out_type = request.args.get("out_type", "use")
    if out_type == StockOut.TYPE_SCRAP:
        # 报废：过期盒也能报，按效期最早优先
        recommend = min(groups.values(), key=lambda g: g["expire_date"]) if groups else None
    else:
        recommend = valid[0] if valid else None

    return jsonify({
        "found": True,
        "reagent": reagent.name,
        "pack_size": reagent.pack_size or 1,
        "small_unit": reagent.small_unit,
        "total_boxes": total,
        "valid_boxes": valid_total,
        "expired_boxes": expired_total,
        "enough": total > 0,
        "recommend": recommend,
        "groups": sorted(groups.values(), key=lambda g: g["expire_date"]),
    })


@stock_out_bp.route("/scan")
@login_required
def scan_api():
    """扫码枪按条码定位盒"""
    barcode = request.args.get("barcode", "").strip()
    pack = stock_service.get_pack_by_barcode(barcode)
    if not pack:
        return jsonify({"found": False, "message": "未找到该条码对应的试剂盒"})
    r = pack.reagent
    in_stock = pack.status == ReagentPack.STATUS_IN_STOCK
    expired = pack.days_left < 0

    # 同试剂里效期更早的“可用”在库盒（先进先出提醒：扫到的这盒不是最早到期时，
    # 页面会提示先出效期更近的那盒，避免新批号先出、老批号放过期）。
    # 已过期的盒不能用于领用/质控，只单独计数提示，不作为“改用这盒”的建议。
    today = date.today()
    earlier_q = ReagentPack.query.filter(
        ReagentPack.reagent_id == pack.reagent_id,
        ReagentPack.status == ReagentPack.STATUS_IN_STOCK,
        ReagentPack.expire_date >= today,
        ReagentPack.expire_date < pack.expire_date,
        ReagentPack.id != pack.id,
    ).order_by(ReagentPack.expire_date.asc(), ReagentPack.id.asc())
    earlier_total = earlier_q.count() if in_stock else 0
    earlier = earlier_q.limit(3).all() if in_stock else []
    expired_earlier = 0
    if in_stock:
        expired_earlier = ReagentPack.query.filter(
            ReagentPack.reagent_id == pack.reagent_id,
            ReagentPack.status == ReagentPack.STATUS_IN_STOCK,
            ReagentPack.expire_date < today,
            ReagentPack.id != pack.id,
        ).count()

    return jsonify({
        "found": True,
        "barcode": pack.barcode,
        "reagent_id": r.id,
        "reagent_name": r.name,
        "batch_no": pack.batch_no,
        "expire_date": pack.expire_date.strftime("%Y-%m-%d"),
        "days_left": pack.days_left,
        "status": pack.status,
        "status_text": pack.status_text,
        "expired": expired,
        "total_units": pack.total_units,
        "used_units": pack.used_units,
        "remaining_units": pack.remaining_units,
        # can_out 只看“盒是否在库”：在库的过期盒仍然可以走「试剂报废」，
        # 是否允许领用由 can_use 表示（前端按出库类型判断）。
        "can_out": in_stock,
        "can_use": in_stock and not expired,
        "earlier_count": earlier_total,
        "earlier_expired_count": expired_earlier,
        "earlier": [{
            "barcode": p.barcode,
            "batch_no": p.batch_no,
            "expire_date": p.expire_date.strftime("%Y-%m-%d"),
            "days_left": p.days_left,
            "expired": p.days_left < 0,
        } for p in earlier],
        "small_unit": r.small_unit,
    })


@stock_out_bp.route("/export")
@login_required
@manager_required
def out_export():
    query = StockOut.query.join(Reagent, StockOut.reagent_id == Reagent.id)
    out_type = request.args.get("out_type", "")
    if out_type:
        query = query.filter(StockOut.out_type == out_type)
    # 导出时间范围：最近一周/一月/半年/一年/自定义起止
    start, end = parse_export_range()
    if start:
        query = query.filter(db.func.date(StockOut.create_time) >= start)
    if end:
        query = query.filter(db.func.date(StockOut.create_time) <= end)
    rows = []
    for o in query.order_by(StockOut.create_time.asc(), StockOut.id.asc()).all():
        barcodes = "、".join(p.barcode for p in o.packs)
        batches = "、".join(set(p.batch_no for p in o.packs))
        out_uid = o.confirmer_id if o.confirmer_id else o.applicant_id
        out_name = (o.confirmer.real_name if o.confirmer
                    else (o.applicant.real_name if o.applicant else ""))
        recv_name = o.receiver_user.real_name if o.receiver_user else (o.receiver or "")
        recv_uid = o.receiver_id if o.receiver_id else None
        rows.append([
            o.order_no, o.reagent.name, o.reagent.registrant or "", o.reagent.spec or "",
            batches, barcodes, o.quantity,
            o.reagent.unit or "盒",
            o.type_text,
            (out_uid, out_name) if out_uid else "",
            (recv_uid, recv_name) if recv_uid else recv_name,
            o.status_text,
            o.create_time.strftime("%Y-%m-%d %H:%M"),
            o.reason or "",
        ])
    filename = f"出库记录_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    path = export_excel(
        "出库记录",
        ["出库单号", "试剂名称", "生产企业", "规格", "批号", "条码", "数量", "单位", "类型",
         "组低耗员", "接收人", "状态", "申请时间", "事由"],
        rows, filename)
    log_operation("出库管理", "导出出库记录 Excel")
    return send_excel(path, filename)
