# -*- coding: utf-8 -*-
"""统计报表：出入库趋势、分类消耗、供应商采购、类型占比、导出"""
from datetime import datetime, date, timedelta
from flask import Blueprint, render_template, request
from flask_login import login_required

from app.extensions import db
from app.models.stock import StockIn, StockOut, PackUse
from app.services.warning_service import trend_data
from app.utils.common import log_operation, export_excel, send_excel

report_bp = Blueprint("report", __name__)


def _period_start(days):
    return date.today() - timedelta(days=days - 1)


@report_bp.route("/")
@login_required
def report_index():
    days = request.args.get("days", default=30, type=int)
    days = max(7, min(days, 365))
    start = _period_start(days)

    trend = trend_data(days)
    ins = StockIn.query.filter(
        StockIn.status == StockIn.STATUS_DONE,
        db.func.date(StockIn.audit_time) >= start).all()
    outs = StockOut.query.filter(
        StockOut.status == StockOut.STATUS_DONE,
        db.func.date(StockOut.confirm_time) >= start).all()
    uses = PackUse.query.filter(
        db.func.date(PackUse.create_time) >= start).all()

    in_amount = round(sum(x.amount for x in ins), 2)
    in_qty = sum(x.quantity for x in ins)
    out_qty = sum(x.quantity for x in outs)
    scrap_qty = sum(x.quantity for x in outs if x.out_type == StockOut.TYPE_SCRAP)
    used_units = sum(x.units_used for x in uses)

    # 分类出库盒数
    cat = {}
    for x in outs:
        name = x.reagent.category.name if x.reagent.category else "未分类"
        cat[name] = cat.get(name, 0) + x.quantity
    cat_data = [{"name": k, "value": v} for k, v in
                sorted(cat.items(), key=lambda kv: -kv[1])]

    # 供应商采购金额 TOP10
    sup = {}
    for x in ins:
        name = x.supplier.name if x.supplier else "未知供应商"
        sup[name] = round(sup.get(name, 0) + x.amount, 2)
    sup_sorted = sorted(sup.items(), key=lambda kv: -kv[1])[:10]
    sup_data = {"names": [k for k, _ in sup_sorted],
                "values": [v for _, v in sup_sorted]}

    # 出库类型占比
    typ = {}
    for x in outs:
        t = StockOut.TYPE_TEXT.get(x.out_type, x.out_type)
        typ[t] = typ.get(t, 0) + x.quantity
    type_data = [{"name": k, "value": v} for k, v in typ.items()]

    totals = {"in_amount": in_amount, "in_qty": in_qty,
              "out_qty": out_qty, "scrap_qty": scrap_qty,
              "used_units": used_units}

    return render_template("report/index.html", days=days, totals=totals,
                           trend=trend, cat_data=cat_data,
                           sup_data=sup_data, type_data=type_data)


@report_bp.route("/export-consume")
@login_required
def export_consume():
    days = request.args.get("days", default=30, type=int)
    start = _period_start(days)
    outs = StockOut.query.filter(
        StockOut.status == StockOut.STATUS_DONE,
        db.func.date(StockOut.confirm_time) >= start).all()

    agg = {}
    for x in outs:
        key = x.reagent.name
        a = agg.setdefault(key, {
            "category": x.reagent.category.name if x.reagent.category else "未分类",
            "use": 0, "qc": 0, "scrap": 0, "total": 0})
        if x.out_type in ("use", "qc", "scrap"):
            a[x.out_type] += x.quantity
        a["total"] += x.quantity

    rows = [[name, v["category"], v["use"], v["qc"],
             v["scrap"], v["total"]] for name, v in
            sorted(agg.items(), key=lambda kv: -kv[1]["total"])]
    filename = f"试剂出库汇总_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    path = export_excel(
        "试剂出库汇总",
        ["试剂名称", "分类", "检验领用(包装)", "质控消耗(包装)", "报废(包装)", "合计(包装)"],
        rows, filename)
    log_operation("统计报表", "导出试剂出库汇总")
    return send_excel(path, filename)
