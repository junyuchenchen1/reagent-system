# -*- coding: utf-8 -*-
"""入库管理：登记、审核、详情、条码标签打印、导出"""
from datetime import datetime, date
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort, jsonify, current_app)
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


def _serial_used(serial_no):
    """序列号是否已登记过入库（已取消的单子不算，允许重录）。返回已有入库单或 None。"""
    if not serial_no:
        return None
    return (StockIn.query
            .filter(StockIn.serial_no == serial_no, StockIn.status != 2)
            .order_by(StockIn.id.desc())
            .first())


def _serial_dup_info(serial_no):
    """给扫码接口用：返回该序列号已有的入库单摘要（没有则 None）。"""
    si = _serial_used(serial_no)
    if not si:
        return None
    return {
        "order_no": si.order_no,
        "reagent": si.reagent.name if si.reagent else "",
        "batch_no": si.batch_no,
        "status_text": si.status_text,
        "create_time": si.create_time.strftime("%Y-%m-%d %H:%M") if si.create_time else "",
        "sid": si.id,
    }


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
        serial_no = (request.form.get("serial_no", "") or "").strip()[:64]
        produce_date = _parse_date(request.form.get("produce_date", ""))
        # 序列号（UDI 21）是单盒唯一号：入库前先查有没有已经登记过，避免同一盒重复入库
        dup_serial = _serial_used(serial_no) if serial_no else None

        reagent = db.session.get(Reagent, int(reagent_id)) if reagent_id else None
        if reagent is None:
            flash("请选择试剂", "danger")
        elif not batch_no:
            flash("请填写批号", "danger")
        elif expire_date is None:
            flash("请选择正确的有效期", "danger")
        elif produce_date and produce_date > expire_date:
            flash("生产日期不能晚于有效期", "danger")
        elif expire_date < date.today():
            flash("该批次已过期，禁止入库", "danger")
        elif quantity <= 0:
            flash("入库盒数必须大于 0", "danger")
        elif serial_no and quantity > 1:
            flash("填了序列号就是单盒入库：盒数只能为 1（一个序列号对应一盒，"
                  "写 2 会生成两条同序列号的盒）。要按批号一次入多盒，请清空「序列号」框或取消勾选「按序列号管理」再提交。",
                  "danger")
        elif not verifier_id:
            flash("请选择验收人（双人验收）", "danger")
        elif serial_no and dup_serial:
            # 勾了「识别序列号」才会带 serial_no：同一盒（同序列号）不能重复入库
            flash(f"序列号 {serial_no} 已在入库单 {dup_serial.order_no} 中登记过"
                  f"（{dup_serial.reagent.name if dup_serial.reagent else '未知试剂'}，"
                  f"批号 {dup_serial.batch_no}），同一盒不能重复入库。"
                  f"若这次是按批号整批入库，请取消勾选「识别序列号」再提交。",
                  "danger")
        else:
            data = {
                "reagent_id": reagent.id, "batch_no": batch_no,
                "expire_date": expire_date, "quantity": quantity,
                "unit_price": float(unit_price), "supplier_id": supplier_id,
                "storage_location": storage_location, "remark": remark,
                "verifier_id": int(verifier_id), "accept_status": accept_status,
                "udi_di": udi_di, "serial_no": serial_no,
                "produce_date": produce_date,
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
                           verifiers=verifiers,
                           udi_code=(request.args.get("udi") or "").strip()[:512],
                           can_create_reagent=current_user.is_manager)


@stock_in_bp.route("/add-batch", methods=["POST"])
@login_required
def in_add_batch():
    """批量入库：一次提交前端「待入库清单」里的多条试剂。

    共享字段（验收人/供应商/存放位置/接收状态）由右侧表单提供，每条试剂自带
    reagent_id / batch_no / expire_date / quantity / 单价（可空）/ serial_no（勾选序列号时）。
    校验全部通过才一起落库；有任何一条不合格就整批不提交，返回逐条错误。
    """
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    verifier_id = payload.get("verifier_id") or None
    supplier_id = payload.get("supplier_id") or None
    storage_location = (payload.get("storage_location") or "").strip()
    accept_status = payload.get("accept_status") or "合格"
    remark = (payload.get("remark") or "").strip()

    errors = []
    if not items:
        return jsonify({"ok": False, "errors": ["待入库清单是空的"]}), 400
    if not verifier_id:
        errors.append("请先选择验收人（双人验收）")
    if len(items) > 200:
        return jsonify({"ok": False, "errors": ["单次最多提交 200 条"]}), 400

    prepared = []
    seen_serial = {}
    for idx, it in enumerate(items, start=1):
        reagent_id = it.get("reagent_id")
        batch_no = (it.get("batch_no") or "").strip()
        serial_no = (it.get("serial_no") or "").strip()[:64]
        expire_date = _parse_date(it.get("expire_date") or "")
        produce_date = _parse_date(it.get("produce_date") or "")
        try:
            quantity = int(it.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        try:
            unit_price = float(it.get("unit_price") or 0)
        except (TypeError, ValueError):
            unit_price = 0.0
        reagent = db.session.get(Reagent, int(reagent_id)) if reagent_id else None
        label = f"第 {idx} 条"
        if reagent is None:
            errors.append(f"{label}：没选到试剂")
            continue
        if not batch_no:
            errors.append(f"{label}（{reagent.name}）：缺批号")
            continue
        if expire_date is None:
            errors.append(f"{label}（{reagent.name}）：有效期没填或格式不对")
            continue
        if produce_date and produce_date > expire_date:
            errors.append(f"{label}（{reagent.name}）：生产日期晚于有效期")
            continue
        if expire_date < date.today():
            errors.append(f"{label}（{reagent.name}）：批号 {batch_no} 已过期，禁止入库")
            continue
        if quantity <= 0:
            errors.append(f"{label}（{reagent.name}）：入库数量必须大于 0")
            continue
        if serial_no and quantity != 1:
            # 序列号唯一对应一盒：数量写成 2 会生成两个同序列号的盒
            errors.append(f"{label}（{reagent.name}）：填了序列号就是单盒入库，数量只能为 1")
            continue
        if serial_no:
            if serial_no in seen_serial:
                errors.append(f"{label}（{reagent.name}）：序列号 {serial_no} "
                              f"在上面的第 {seen_serial[serial_no]} 条里重复了")
                continue
            dup = _serial_used(serial_no)
            if dup:
                errors.append(f"{label}（{reagent.name}）：序列号 {serial_no} 已在入库单 "
                              f"{dup.order_no} 登记过，不能重复入库")
                continue
            seen_serial[serial_no] = idx
        prepared.append({
            "reagent": reagent, "batch_no": batch_no, "serial_no": serial_no,
            "expire_date": expire_date, "produce_date": produce_date,
            "quantity": quantity, "unit_price": unit_price,
            "udi_di": (it.get("udi_di") or "").strip()[:32],
        })

    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    sids = []
    try:
        for p in prepared:
            data = {
                "reagent_id": p["reagent"].id, "batch_no": p["batch_no"],
                "expire_date": p["expire_date"], "quantity": p["quantity"],
                "unit_price": p["unit_price"], "supplier_id": supplier_id,
                "storage_location": storage_location, "remark": remark,
                "verifier_id": int(verifier_id), "accept_status": accept_status,
                "udi_di": p["udi_di"], "serial_no": p["serial_no"],
                "produce_date": p["produce_date"],
            }
            si = stock_service.create_stock_in(data, current_user)
            if p["udi_di"]:
                udi_service.bind_reagent(p["udi_di"], p["reagent"].id)
            if current_user.is_manager:
                stock_service.confirm_stock_in(si, current_user)
            sids.append(si.id)
    except Exception:
        current_app.logger.exception("[批量入库] 落库失败")
        return jsonify({"ok": False,
                        "errors": ["保存过程中出错，已写入 logs/error.log。"
                                   "可到入库记录页核对哪些已经生成。"]}), 500

    total_boxes = sum(p["quantity"] for p in prepared)
    log_operation("入库管理",
                  f"批量入库 {len(sids)} 条、共 {total_boxes} 盒（扫码清单一次提交）")
    if current_user.is_manager:
        return jsonify({"ok": True, "sids": sids,
                        "labels_url": url_for("stock_in.labels_batch",
                                              sids=",".join(str(s) for s in sids))})
    return jsonify({"ok": True, "sids": sids, "labels_url": None,
                    "message": "批量入库申请已提交，等待管理员审核"})


@stock_in_bp.route("/labels-batch")
@login_required
def labels_batch():
    """批量条码标签：一次打印多张入库单的全部条码（批量入库后直接跳到这）。"""
    raw = request.args.get("sids", "")
    sids = [int(x) for x in raw.split(",") if x.strip().isdigit()][:50]
    batches = []
    for sid in sids:
        si = db.session.get(StockIn, sid)
        if not si:
            continue
        packs = ReagentPack.query.filter_by(stock_in_id=sid).order_by(
            ReagentPack.barcode).all()
        if packs:
            batches.append((si, packs))
    if not batches:
        flash("没有可打印的条码（入库单不存在，或还在等审核）", "warning")
        return redirect(url_for("stock_in.in_list"))
    total = sum(len(p) for _, p in batches)
    return render_template("stock/labels_batch.html", batches=batches, total=total)


@stock_in_bp.route("/udi", methods=["GET"])
@login_required
def udi_scan():
    """扫试剂盒 DM 码：解析 GS1 内容 → 查 UDI 产品库 → 匹配本系统试剂。"""
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"error": "缺少 code 参数"}), 400
    if len(code) > 512:
        code = code[:512]
    data = udi_service.resolve(code)
    # 若码里有 (21) 序列号，顺手告诉前端这一盒有没有入过库（前端勾了「按序列号管理」才用）
    serial = ((data.get("parsed") or {}).get("serial") or "").strip()
    if serial:
        data["serial_dup"] = _serial_dup_info(serial)
    # 带出该试剂最近一次入库的单价作为默认，省得每次手填、也避免漏填成 0 元
    reagent_id = data.get("reagent_id")
    last_price = 0.0
    if reagent_id:
        last = (StockIn.query
                .filter(StockIn.reagent_id == int(reagent_id), StockIn.status != 2,
                        StockIn.unit_price.isnot(None))
                .order_by(StockIn.id.desc()).first())
        if last and last.unit_price:
            last_price = float(last.unit_price)
    data["last_price"] = last_price
    return jsonify(data)


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
        try:
            query = query.filter(StockIn.status == int(status))
        except (TypeError, ValueError):
            # 参数异常（如中文状态名）不应当 500，忽略该筛选
            current_app.logger.warning(f"[导出入库] status 参数非法：{status!r}，已忽略")
    # 导出时间范围：最近一周/一月/半年/一年/自定义起止
    start, end = parse_export_range()
    if start:
        query = query.filter(db.func.date(StockIn.create_time) >= start)
    if end:
        query = query.filter(db.func.date(StockIn.create_time) <= end)
    rows = []
    for si in query.order_by(StockIn.create_time.asc(), StockIn.id.asc()).all():
        rows.append([
            si.order_no or "", si.reagent.name if si.reagent else "",
            (si.reagent.registrant or "") if si.reagent else "",
            (si.reagent.spec or "") if si.reagent else "",
            si.batch_no or "",
            si.serial_no or "",
            si.produce_date.strftime("%Y-%m-%d") if si.produce_date else "",
            si.expire_date.strftime("%Y-%m-%d") if si.expire_date else "",
            si.quantity,
            (si.reagent.unit or "盒") if si.reagent else "盒",
            float(si.unit_price or 0), float(si.amount or 0),
            si.supplier.name if si.supplier else "",
            si.accept_status or "合格",
            si.status_text,
            (si.operator_id, si.operator.real_name or "") if si.operator_id else "",
            (si.verifier_id, si.verifier.real_name or "") if si.verifier_id else "",
            si.create_time.strftime("%Y-%m-%d %H:%M") if si.create_time else "",
        ])
    filename = f"入库记录_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    try:
        path = export_excel(
            "入库记录",
            ["入库单号", "试剂名称", "生产企业", "规格", "批号", "序列号", "生产日期", "有效期",
             "数量", "单位", "单价(元)", "金额",
             "供应商", "接收状态", "状态", "入库人", "验收人", "登记时间"],
            rows, filename)
    except Exception:
        current_app.logger.exception("[导出入库] 生成 Excel 失败")
        flash("导出入库记录失败，错误已写入 logs/error.log，请重试或联系管理员。", "danger")
        return redirect(url_for("stock_in.in_list"))
    log_operation("入库管理", f"导出入库记录 Excel（{range_label(start, end)}）")
    return send_excel(path, filename)
