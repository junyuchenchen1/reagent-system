# -*- coding: utf-8 -*-
"""库存管理：实时库存、效期预警、盘点"""
from datetime import datetime, date, timedelta
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort, jsonify)
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models.reagent import Reagent, Category
from app.models.stock import Inventory, StockCheck, StockIn, ReagentPack
from app.services import stock_service, warning_service
from app.utils.permission import manager_required
from app.utils.common import log_operation, export_excel, send_excel
from app.utils.paging import page_arg, paginate_query, clamp_page

inventory_bp = Blueprint("inventory", __name__)


class SimplePagination:
    """内存分页：实时库存需按"状态"在 Python 侧过滤后再分页，无法直接用 SQL 分页。"""

    def __init__(self, page, per_page, total, items):
        self.page, self.per_page, self.total, self.items = page, per_page, total, items

    @property
    def pages(self):
        return max(1, (self.total + self.per_page - 1) // self.per_page)

    @property
    def has_prev(self):
        return self.page > 1

    @property
    def has_next(self):
        return self.page < self.pages

    @property
    def prev_num(self):
        return self.page - 1

    @property
    def next_num(self):
        return self.page + 1

    def iter_pages(self, left_edge=2, left_current=2, right_current=2, right_edge=2):
        """与 Flask-SQLAlchemy 的 iter_pages 一致：远距离页码用 None 占位（渲染成省略号）"""
        last = 0
        for n in range(1, self.pages + 1):
            if (n <= left_edge
                    or (self.page - left_current - 1 < n < self.page + right_current)
                    or n > self.pages - right_edge):
                if last + 1 != n:
                    yield None
                yield n
                last = n


# ===================== 实时库存（中心在库） =====================
@inventory_bp.route("/")
@login_required
def stock_list():
    page = page_arg()
    keyword = request.args.get("keyword", "").strip()
    category_id = request.args.get("category_id", "")
    storage = request.args.get("storage", "")
    status = request.args.get("status", "")
    mfr = request.args.get("mfr", "")

    query = Inventory.query.options(joinedload(Inventory.reagent)).join(
        Reagent, Inventory.reagent_id == Reagent.id)
    if keyword:
        query = query.filter(
            db.or_(Reagent.name.like(f"%{keyword}%"),
                   Inventory.batch_no.like(f"%{keyword}%")))
    if category_id:
        query = query.filter(Reagent.category_id == int(category_id))
    if storage:
        query = query.filter(Reagent.storage_condition == storage)
    if mfr:
        query = query.filter(Reagent.registrant == mfr)

    items = query.order_by(Inventory.expire_date.asc()).all()
    if status:
        items = [i for i in items if i.stock_status == status]

    per_page = 15
    total = len(items)
    page = clamp_page(page, max(1, (total + per_page - 1) // per_page))
    paged = items[(page - 1) * per_page: page * per_page]
    pagination = SimplePagination(page, per_page, total, paged)

    # 页面内试剂的"已出库未用瓶数"与批次验证状态：各查一次，避免模板逐行懒加载
    page_reagent_ids = {i.reagent_id for i in paged}
    used_map, verify_map = {}, {}
    if page_reagent_ids:
        rows = db.session.query(
            ReagentPack.reagent_id,
            db.func.coalesce(
                db.func.sum(ReagentPack.total_units - ReagentPack.used_units), 0),
        ).filter(
            ReagentPack.reagent_id.in_(page_reagent_ids),
            ReagentPack.status.in_([ReagentPack.STATUS_ISSUED, ReagentPack.STATUS_USING]),
        ).group_by(ReagentPack.reagent_id).all()
        used_map = {rid: int(units or 0) for rid, units in rows}

        keys = {(i.reagent_id, i.batch_no) for i in paged}
        rows = StockIn.query.filter(
            StockIn.status == StockIn.STATUS_DONE,
            StockIn.reagent_id.in_({rid for rid, _ in keys}),
            StockIn.batch_no.in_({bn for _, bn in keys}),
        ).order_by(StockIn.id.asc()).all()
        for si in rows:
            key = (si.reagent_id, si.batch_no)
            if key in keys:
                # 同批号多次入库：全部验证过才算"已验证"
                verify_map[key] = verify_map.get(key, True) and bool(si.is_verified)

    categories = Category.query.order_by(Category.sort).all()
    # 根据已选分类筛选厂家列表
    mfr_query = db.session.query(Reagent.registrant).filter(
        Reagent.registrant != None, Reagent.registrant != '')
    if category_id:
        mfr_query = mfr_query.filter(Reagent.category_id == int(category_id))
    manufacturers = [r[0] for r in mfr_query.distinct().order_by(Reagent.registrant).all()]
    # 根据已选厂家筛选分类列表
    cat_query = db.session.query(Reagent.category_id).filter(
        Reagent.category_id != None)
    if mfr:
        cat_query = cat_query.filter(Reagent.registrant == mfr)
        valid_cat_ids = set(r[0] for r in cat_query.distinct().all())
        filtered_categories = [c for c in categories if c.id in valid_cat_ids]
    else:
        filtered_categories = categories
    return render_template("inventory/list.html",
                           pagination=pagination,
                           categories=filtered_categories, manufacturers=manufacturers,
                           keyword=keyword, category_id=category_id, mfr=mfr,
                           storage=storage, status=status,
                           used_map=used_map, verify_map=verify_map,
                           storage_choices=Reagent.STORAGE_CHOICES)


@inventory_bp.route("/packs/<int:rid>")
@login_required
def reagent_packs(rid):
    """实时库存「明细」：某试剂当前在库的每一盒（条形码级）信息"""
    reagent = db.session.get(Reagent, rid) or abort(404)
    packs = ReagentPack.query.filter_by(
        reagent_id=rid, status=ReagentPack.STATUS_IN_STOCK
    ).order_by(ReagentPack.expire_date.asc(), ReagentPack.id.asc()).all()
    issued_unused = ReagentPack.query.filter(
        ReagentPack.reagent_id == rid,
        ReagentPack.status.in_([ReagentPack.STATUS_ISSUED, ReagentPack.STATUS_USING]),
    ).count()
    return jsonify({
        "found": True,
        "reagent": {
            "id": reagent.id,
            "name": reagent.name,
            "spec": reagent.spec or "",
            "category": reagent.category.name if reagent.category else "未分类",
            "registrant": reagent.registrant or "",
            "storage_condition": reagent.storage_condition or "",
            "unit": reagent.unit or "盒",
            "small_unit": reagent.small_unit or "瓶",
            "pack_size": reagent.pack_size or 1,
            "min_stock": reagent.min_stock or 0,
            "min_stock_unit": reagent.min_stock_unit or "pack",
            "min_stock_unit_text": reagent.min_stock_unit_text,
            "in_stock_units": reagent.in_stock_units,
            "in_use_units": reagent.in_use_units,
            "available_units": reagent.available_units,
            "reg_no": reagent.reg_no or "",
        },
        "in_stock": len(packs),
        "issued_unused": issued_unused,
        "packs": [{
            "id": p.id,
            "barcode": p.barcode,
            "batch_no": p.batch_no,
            "expire_date": p.expire_date.strftime("%Y-%m-%d"),
            "days_left": p.days_left,
            "expired": p.days_left < 0,
            "storage_location": p.storage_location or "",
            "total_units": p.total_units,
            "used_units": p.used_units,
            "remaining_units": p.remaining_units,
        } for p in packs],
    })


@inventory_bp.route("/verify-batch", methods=["POST"])
@login_required
@manager_required
def verify_batch():
    """按试剂+批号标记性能验证完成"""
    reagent_id = request.form.get("reagent_id", type=int)
    batch_no = request.form.get("batch_no", "")
    # 性能验证针对"试剂+批号"：同一批号可能分多次入库，需全部标记
    n = StockIn.query.filter_by(reagent_id=reagent_id, batch_no=batch_no,
                                status=StockIn.STATUS_DONE).update(
        {StockIn.is_verified: True}, synchronize_session=False)
    if n:
        db.session.commit()
        log_operation("库存管理", f"批号 {batch_no} 性能验证完成")
        flash(f"批号 {batch_no} 已标记为验证合格", "success")
    else:
        flash("未找到对应入库记录", "danger")
    return redirect(url_for("inventory.stock_list"))


@inventory_bp.route("/export")
@login_required
@manager_required
def stock_export():
    items = Inventory.query.join(Reagent).order_by(
        Inventory.expire_date.asc()).all()
    rows = []
    for i in items:
        r = i.reagent
        si = StockIn.query.filter_by(reagent_id=r.id, batch_no=i.batch_no,
                                      status=StockIn.STATUS_DONE).first()
        verify_text = "已验证" if si and si.is_verified else ("未验证" if si else "")
        rows.append([
            r.name,
            r.category.name if r.category else "",
            i.batch_no,
            i.expire_date.strftime("%Y-%m-%d"),
            i.quantity,
            r.unit or "盒",
            r.pack_size,
            r.small_unit or "瓶",
            i.quantity * r.pack_size,
            r.in_use_units,
            verify_text,
            i.stock_status_text,
            i.days_left,
            r.storage_condition,
            i.storage_location or "",
        ])
    filename = f"实时库存_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    path = export_excel(
        "实时库存",
        ["试剂名称", "分类", "批号", "有效期", "在库数量", "单位", "每包装数量", "最小单位",
         "在库最小单位数", "已出库未用最小单位数", "性能验证", "状态", "剩余天数", "存储条件", "存放位置"],
        rows, filename)
    log_operation("库存管理", "导出实时库存 Excel")
    return send_excel(path, filename)


# ===================== 效期预警 =====================
@inventory_bp.route("/warning")
@login_required
def warning():
    warn_items = warning_service.get_expire_warnings(999)
    low_items = warning_service.get_low_stock_warnings(999)
    return render_template("inventory/warning.html",
                           warn_items=warn_items, low_items=low_items)


# ===================== 盘点 =====================
@inventory_bp.route("/check")
@login_required
def check_list():
    items = StockCheck.query.order_by(StockCheck.id.desc()).all()
    return render_template("inventory/check_list.html", items=items)


@inventory_bp.route("/check/add", methods=["GET", "POST"])
@login_required
@manager_required
def check_add():
    if request.method == "POST":
        if StockCheck.query.filter_by(status=StockCheck.STATUS_DRAFT).first():
            flash("当前已有未完成的盘点单，请先完成", "warning")
            return redirect(url_for("inventory.check_list"))
        data = {
            "title": request.form.get("title", "").strip(),
            "remark": request.form.get("remark", "").strip(),
        }
        check = stock_service.start_check(data, current_user)
        log_operation("库存盘点",
                      f"创建盘点单 {check.check_no}，共 {len(check.items)} 个批次")
        flash("盘点单已创建，请核对实际库存", "success")
        return redirect(url_for("inventory.check_detail", cid=check.id))
    return render_template("inventory/check_add.html")


@inventory_bp.route("/check/<int:cid>")
@login_required
def check_detail(cid):
    check = db.session.get(StockCheck, cid) or abort(404)
    return render_template("inventory/check_detail.html", check=check)


@inventory_bp.route("/check/<int:cid>/finish", methods=["POST"])
@login_required
@manager_required
def check_finish(cid):
    check = db.session.get(StockCheck, cid) or abort(404)
    if check.status == StockCheck.STATUS_DONE:
        flash("该盘点单已完成", "warning")
        return redirect(url_for("inventory.check_detail", cid=cid))

    for item in check.items:
        raw = request.form.get(f"actual_{item.id}")
        try:
            item.actual_qty = int(raw)
        except (TypeError, ValueError):
            flash("实际盘点盒数必须为整数，请检查", "danger")
            return redirect(url_for("inventory.check_detail", cid=cid))

    stock_service.finish_check(check)
    log_operation("库存盘点", f"完成盘点单 {check.check_no}")
    flash("盘点完成，库存已按实际数量调整", "success")
    return redirect(url_for("inventory.check_detail", cid=cid))


# ---------- 温湿度记录 ----------
from app.models.stock import TempRecord, TempLocation


def _month_bounds(month: str):
    """'YYYY-MM' -> [月初, 下月初) 日期区间；非法返回 None。

    用日期区间过滤而不是 strftime()：strftime 是 SQLite 专用函数，
    切到 MySQL 会直接报错；日期比较两边都支持，还能用上索引。
    """
    try:
        start = datetime.strptime((month or "").strip(), "%Y-%m").date().replace(day=1)
    except (TypeError, ValueError):
        return None
    return start, (start + timedelta(days=32)).replace(day=1)


@inventory_bp.route("/temp")
@login_required
def temp_list():
    page = page_arg()
    loc_filter = request.args.get("location", "").strip()
    # 默认当前月
    month = request.args.get("month", "").strip()
    if not month:
        month = date.today().strftime("%Y-%m")
    query = TempRecord.query
    if loc_filter:
        query = query.filter(TempRecord.location == loc_filter)
    bounds = _month_bounds(month)
    if bounds:
        query = query.filter(TempRecord.record_date >= bounds[0],
                             TempRecord.record_date < bounds[1])
    pagination = paginate_query(
        query.options(joinedload(TempRecord.recorder)).order_by(
            TempRecord.record_date.desc(), TempRecord.id.desc()), per_page=20)
    # 图表数据：按冰箱分组
    chart_recs = query.order_by(TempRecord.record_date.asc()).all()
    chart_data = {}
    for r in chart_recs:
        chart_data.setdefault(r.location, {"labels": [], "temp": [], "humidity": []})
        chart_data[r.location]["labels"].append(r.record_date.strftime("%m-%d"))
        chart_data[r.location]["temp"].append(r.am_temp)
        chart_data[r.location]["humidity"].append(r.humidity)
    all_locs = [r[0] for r in db.session.query(TempRecord.location).distinct().order_by(TempRecord.location).all()]
    return render_template("inventory/temp_list.html", pagination=pagination,
                           chart_data=chart_data,
                           all_locs=all_locs, loc_filter=loc_filter, month=month)


@inventory_bp.route("/temp/add", methods=["GET", "POST"])
@login_required
def temp_add():
    from datetime import date
    if request.method == "POST":
        record_date = request.form.get("record_date", "").strip()
        if not record_date:
            flash("请选择日期", "danger")
            return redirect(url_for("inventory.temp_add"))
        rd = datetime.strptime(record_date, "%Y-%m-%d").date()
        locations = request.form.getlist("location[]")
        am_temps = request.form.getlist("am_temp[]")
        pm_temps = request.form.getlist("pm_temp[]")
        humidities = request.form.getlist("humidity[]")
        notes = request.form.getlist("note[]")
        count = 0
        for i, loc in enumerate(locations):
            loc = loc.strip()
            if not loc:
                continue
            def fv(v):
                try: return float(v) if v else None
                except: return None
            rec = TempRecord(
                location=loc, record_date=rd,
                am_temp=fv(am_temps[i]) if i < len(am_temps) else None,
                pm_temp=fv(pm_temps[i]) if i < len(pm_temps) else None,
                humidity=fv(humidities[i]) if i < len(humidities) else None,
                note=notes[i].strip() if i < len(notes) else "",
                recorder_id=current_user.id)
            db.session.add(rec)
            count += 1
        db.session.commit()
        log_operation("温湿度记录", f"批量录入 {count} 条温湿度记录（{record_date}）")
        flash(f"已保存 {count} 条记录", "success")
        return redirect(url_for("inventory.temp_list"))
    # GET: 从存放位置取位置列表
    today = date.today().strftime("%Y-%m-%d")
    from app.models.reagent import StorageLocation
    locations = [l.name for l in StorageLocation.query.filter_by(status=1).order_by(StorageLocation.sort).all()]
    if not locations:
        locations = ["2-8℃试剂冰箱1", "-20℃冷冻冰箱", "室温"]
    return render_template("inventory/temp_add.html", today=today, locations=locations)


@inventory_bp.route("/temp/export")
@login_required
@manager_required
def temp_export():
    from app.utils.common import export_excel
    loc_filter = request.args.get("location", "").strip()
    month = request.args.get("month", "").strip()
    query = TempRecord.query
    if loc_filter:
        query = query.filter(TempRecord.location == loc_filter)
    bounds = _month_bounds(month)
    if bounds:
        query = query.filter(TempRecord.record_date >= bounds[0],
                             TempRecord.record_date < bounds[1])
    recs = query.order_by(TempRecord.record_date.asc()).all()
    rows = []
    for t in recs:
        rows.append([
            t.record_date.strftime('%Y-%m-%d'), t.location,
            t.am_temp if t.am_temp is not None else '',
            t.humidity if t.humidity is not None else '',
            t.recorder.real_name if t.recorder else '',
            t.note or ''
        ])
    path = export_excel("温湿度记录",
        ["日期", "位置", "温度℃", "湿度%", "记录人", "备注"],
        rows, "温湿度记录.xlsx")
    return send_excel(path, "温湿度记录.xlsx")


@inventory_bp.route("/temp/<int:tid>/edit", methods=["GET", "POST"])
@login_required
def temp_edit(tid):
    rec = db.session.get(TempRecord, tid) or abort(404)
    if not current_user.is_manager:
        abort(403)
    if request.method == "POST":
        before = f"上午{rec.am_temp}，下午{rec.pm_temp}，湿度{rec.humidity}"
        rec.am_temp = request.form.get("am_temp", type=float)
        rec.pm_temp = request.form.get("pm_temp", type=float)
        rec.humidity = request.form.get("humidity", type=float)
        rec.note = request.form.get("note", "").strip()
        db.session.commit()
        log_operation("温湿度记录", f"更正记录 #{rec.id}（{rec.record_date} {rec.location}）：原值 {before}")
        flash("记录已更新", "success")
        return redirect(url_for("inventory.temp_list"))
    return render_template("inventory/temp_edit.html", rec=rec)


@inventory_bp.route("/temp/<int:tid>/delete", methods=["POST"])
@login_required
def temp_delete(tid):
    rec = db.session.get(TempRecord, tid) or abort(404)
    if not current_user.is_manager:
        abort(403)
    # 温湿度属于质量记录，禁止物理删除，避免破坏审计追溯。
    flash("质量记录不允许删除；如录入有误，请使用“编辑”更正，系统会保留操作日志", "warning")
    return redirect(url_for("inventory.temp_list"))
