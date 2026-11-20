# -*- coding: utf-8 -*-
"""入库管理：登记、审核、详情、条码标签打印、导出"""
from datetime import datetime, date
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort, jsonify)
from flask_login import login_required, current_user

from app.extensions import db
from app.models.reagent import Reagent, Supplier, Category, StorageLocation
from app.models.stock import StockIn, ReagentPack
from app.services import stock_service, udi_service
from app.utils.permission import manager_required
from app.utils.common import log_operation, export_excel, send_excel, parse_export_range, range_label
from app.utils.paging import page_arg, paginate_query

stock_in_bp = Blueprint("stock_in", __name__)


def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


@stock_in_bp.route("/")
@login_required
def in_list():
    page = page_arg()
    keyword = request.args.get("keyword", "").strip()
    status = request.args.get("status", "")
    start = _parse_date(request.args.get("start", ""))
    end = _parse_date(request.args.get("end", ""))

    query = StockIn.query.join(Reagent, StockIn.reagent_id == Reagent.id)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            db.or_(StockIn.order_no.like(like), StockIn.batch_no.like(like),
                   Reagent.name.like(like)))
    if status != "":
        query = query.filter(StockIn.status == int(status))
    if start:
        query = query.filter(db.func.date(StockIn.create_time) >= start)
    if end:
        query = query.filter(db.func.date(StockIn.create_time) <= end)

    pagination = paginate_query(query.order_by(StockIn.id.desc()))
    return render_template("stock/in_list.html",
                           pagination=pagination,
                           keyword=keyword, status=status,
                           start=request.args.get("start", ""),
                           end=request.args.get("end", ""))


@stock_in_bp.route("/add", methods=["GET", "POST"])
@login_required
def in_add():
    if request.method == "POST":
        reagent_id = request.form.get("reagent_id")
        batch_no = request.form.get("batch_no", "").strip()
        expire_date = _parse_date(request.form.get("expire_date", ""))
        quantity = int(request.form.get("quantity", 0) or 0)
        unit_price = request.form.get("unit_price", 0) or 0
        supplier_id = request.form.get("supplier_id") or None
        storage_location = request.form.get("storage_location", "").strip()
        remark = request.form.get("remark", "").strip()
        verifier_id = request.form.get("verifier_id") or None
        accept_status = request.form.get("accept_status", "合格")
        udi_di = (request.form.get("udi_di", "") or "").strip()[:32]

        reagent = db.session.get(Reagent, int(reagent_id)) if reagent_id else None
        if reagent is None:
            flash("请选择试剂", "danger")
        elif not batch_no:
            flash("请填写批号", "danger")
        elif expire_date is None:
            flash("请选择正确的有效期", "danger")
        elif expire_date < date.today():
            flash("该批次已过期，禁止入库", "danger")
        elif quantity <= 0:
            flash("入库盒数必须大于 0", "danger")
        elif not verifier_id:
            flash("请选择验收人（双人验收）", "danger")
        else:
            data = {
                "reagent_id": reagent.id, "batch_no": batch_no,
                "expire_date": expire_date, "quantity": quantity,
                "unit_price": float(unit_price), "supplier_id": supplier_id,
                "storage_location": storage_location, "remark": remark,
                "verifier_id": int(verifier_id), "accept_status": accept_status,
                "udi_di": udi_di,
            }
            si = stock_service.create_stock_in(data, current_user)
            if udi_di:
                # 记住“这个 DI 对应哪条试剂档案”，下次扫同款直接选中
                udi_service.bind_reagent(udi_di, reagent.id)
            if current_user.is_manager:
                stock_service.confirm_stock_in(si, current_user)
                log_operation("入库管理",
                              f"试剂 {reagent.name}（批号 {batch_no}）入库 {quantity} 盒，"
                              f"已生成 {quantity} 枚条码")
                flash(f"入库成功，已为 {quantity} 盒生成条码，请打印并粘贴", "success")
                return redirect(url_for("stock_in.labels", sid=si.id))
            log_operation("入库管理", f"提交入库申请：{reagent.name} {quantity} 盒")
            flash("入库申请已提交，等待管理员审核", "success")
            return redirect(url_for("stock_in.in_list"))

    from app.models.user import User
    reagents = Reagent.query.filter_by(status=1).order_by(Reagent.name).all()
    suppliers = Supplier.query.filter_by(status=1).order_by(Supplier.name).all()
    categories = Category.query.order_by(Category.sort).all()
    locations = StorageLocation.query.filter_by(status=1).order_by(
        StorageLocation.sort.asc(), StorageLocation.id.asc()).all()
    verifiers = User.query.filter(
        User.id != current_user.id, User.status == 1
    ).order_by(User.real_name).all()
    manufacturers = [r[0] for r in db.session.query(Reagent.registrant).filter(
        Reagent.status == 1, Reagent.registrant != None, Reagent.registrant != '').distinct().order_by(Reagent.registrant).all()]
    return render_template("stock/in_add.html", reagents=reagents,
                           suppliers=suppliers, categories=categories,
                           locations=locations, manufacturers=manufacturers,
                           verifiers=verifiers)


@stock_in_bp.route("/udi", methods=["GET"])
@login_required
def udi_scan():
    """扫试剂盒 DM 码：解析 GS1 内容 → 查 UDI 产品库 → 匹配本系统试剂。"""
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"error": "缺少 code 参数"}), 400
    if len(code) > 512:
        code = code[:512]
    return jsonify(udi_service.resolve(code))


@stock_in_bp.route("/<int:sid>/audit", methods=["POST"])
@login_required
@manager_required
def in_audit(sid):
    si = db.session.get(StockIn, sid) or abort(404)
    if si.status != StockIn.STATUS_PENDING:
        flash("该单据已处理，无需重复操作", "warning")
        return redirect(url_for("stock_in.in_list"))

    action = request.form.get("action")
    if action == "approve":
        stock_service.confirm_stock_in(si, current_user)
        log_operation("入库管理", f"审核通过入库单 {si.order_no}，生成 {si.quantity} 枚条码")
        flash("已审核通过并生成条码，请打印粘贴", "success")
        return redirect(url_for("stock_in.labels", sid=si.id))
    stock_service.cancel_stock_in(si)
    log_operation("入库管理", f"驳回入库单 {si.order_no}")
    flash("已驳回该入库申请", "warning")
    return redirect(url_for("stock_in.in_list"))


@stock_in_bp.route("/<int:sid>/edit", methods=["GET", "POST"])
@login_required
def in_edit(sid):
    from app.models.reagent import Reagent, Supplier
    si = db.session.get(StockIn, sid) or abort(404)
    if not current_user.is_admin:
        abort(403)
    if si.status != StockIn.STATUS_PENDING:
        flash("已审核入库单不可直接修改；请作废后重新登记，确保条码与追溯记录一致", "danger")
        return redirect(url_for("stock_in.in_list"))
    if request.method == "POST":
        si.batch_no = request.form.get("batch_no", "").strip()
        si.expire_date = datetime.strptime(request.form.get("expire_date"), "%Y-%m-%d").date()
        si.quantity = int(request.form.get("quantity"))
        si.unit_price = float(request.form.get("unit_price"))
        si.supplier_id = request.form.get("supplier_id", type=int)
        si.storage_location = request.form.get("storage_location", "").strip()
        si.remark = request.form.get("remark", "").strip()
        db.session.commit()
        flash("入库记录已更新", "success")
        return redirect(url_for("stock_in.in_list"))
    reagents = Reagent.query.filter_by(status=1).order_by(Reagent.name).all()
    suppliers = Supplier.query.order_by(Supplier.name).all()
    return render_template("stock/in_edit.html", si=si, reagents=reagents, suppliers=suppliers)


@stock_in_bp.route("/<int:sid>/delete", methods=["POST"])
@login_required
def in_delete(sid):
    si = db.session.get(StockIn, sid) or abort(404)
    if not current_user.is_admin:
        abort(403)
    if si.status != StockIn.STATUS_PENDING:
        flash("已审核入库单不得删除；请保留原始记录以满足追溯要求", "danger")
        return redirect(url_for("stock_in.in_list"))
    # 待审核单尚未生成盒与库存；若出现异常数据（已生成盒）则拒绝删除，避免库存失去来源
    from app.models.stock import ReagentPack
    if ReagentPack.query.filter_by(stock_in_id=sid).first():
        flash("该入库单已生成试剂盒，不能删除；请走报废或盘点流程", "danger")
        return redirect(url_for("stock_in.in_list"))
    db.session.delete(si)
    db.session.commit()
    flash("入库记录已删除", "warning")
    return redirect(url_for("stock_in.in_list"))


@stock_in_bp.route("/<int:sid>/verify", methods=["POST"])
@login_required
@manager_required
def in_verify(sid):
    """标记该入库批号已完成性能验证"""
    si = db.session.get(StockIn, sid) or abort(404)
    if si.status != StockIn.STATUS_DONE:
        flash("仅已入库的批号可标记性能验证", "warning")
        return redirect(url_for("stock_in.in_list"))
    # 同一批号可能分多次入库，验证状态按批号统一
    StockIn.query.filter_by(reagent_id=si.reagent_id, batch_no=si.batch_no,
                            status=StockIn.STATUS_DONE).update(
        {StockIn.is_verified: True}, synchronize_session=False)
    db.session.commit()
    log_operation("入库管理", f"批号 {si.batch_no} 性能验证完成")
    flash(f"批号 {si.batch_no} 已标记为验证合格", "success")
    return redirect(url_for("stock_in.in_list"))


@stock_in_bp.route("/<int:sid>")
@login_required
def in_detail(sid):
    si = db.session.get(StockIn, sid) or abort(404)
    packs = ReagentPack.query.filter_by(stock_in_id=sid).order_by(
        ReagentPack.barcode).all()
    return render_template("stock/in_detail.html", si=si, packs=packs)


@stock_in_bp.route("/<int:sid>/labels")
@login_required
def labels(sid):
    """条码标签打印页（50mm × 30mm，TSC TDP-245）"""
    si = db.session.get(StockIn, sid) or abort(404)
    packs = ReagentPack.query.filter_by(stock_in_id=sid).order_by(
        ReagentPack.barcode).all()
    return render_template("stock/labels.html", si=si, packs=packs)


@stock_in_bp.route("/export")
@login_required
@manager_required
def in_export():
    query = StockIn.query.join(Reagent, StockIn.reagent_id == Reagent.id)
    status = request.args.get("status", "")
    if status != "":
        query = query.filter(StockIn.status == int(status))
    # 导出时间范围：最近一周/一月/半年/一年/自定义起止
    start, end = parse_export_range()
    if start:
        query = query.filter(db.func.date(StockIn.create_time) >= start)
    if end:
        query = query.filter(db.func.date(StockIn.create_time) <= end)
    rows = []
    for si in query.order_by(StockIn.create_time.asc(), StockIn.id.asc()).all():
        rows.append([
            si.order_no, si.reagent.name, si.reagent.registrant or "", si.reagent.spec or "",
            si.batch_no,
            si.expire_date.strftime("%Y-%m-%d"), si.quantity,
            si.reagent.unit or "盒",
            float(si.unit_price or 0), si.amount,
            si.supplier.name if si.supplier else "",
            si.accept_status or "合格",
            si.status_text,
            (si.operator_id, si.operator.real_name) if si.operator else "",
            (si.verifier_id, si.verifier.real_name) if si.verifier else "",
            si.create_time.strftime("%Y-%m-%d %H:%M"),
        ])
    filename = f"入库记录_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    path = export_excel(
        "入库记录",
        ["入库单号", "试剂名称", "生产企业", "规格", "批号", "有效期", "数量", "单位", "单价(元)", "金额",
         "供应商", "接收状态", "状态", "入库人", "验收人", "登记时间"],
        rows, filename)
    log_operation("入库管理", f"导出入库记录 Excel（{range_label(start, end)}）")
    return send_excel(path, filename)
