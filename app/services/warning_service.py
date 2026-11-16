# -*- coding: utf-8 -*-
"""预警与统计服务：效期预警、低库存、首页统计、趋势数据"""
from datetime import datetime, timedelta, date, time as dtime
from sqlalchemy.orm import joinedload, selectinload

from app.extensions import db
from app.models.reagent import Reagent
from app.models.stock import (
    StockIn, StockOut, Inventory, ReagentPack, PackUse,
)


def summary_cards() -> dict:
    """首页统计卡片"""
    today = date.today()
    start = datetime.combine(today, dtime.min)

    in_stock_boxes = db.session.query(db.func.count(ReagentPack.id)).filter_by(
        status=ReagentPack.STATUS_IN_STOCK).scalar() or 0
    # 预警阈值因试剂而异；遍历“批次汇总”而非所有单盒，数据量小得多。
    warning_batches = 0
    expired_batches = 0
    for inv in Inventory.query.options(joinedload(Inventory.reagent)).filter(
            Inventory.quantity > 0).all():
        if inv.expire_date < today:
            expired_batches += 1
        elif inv.expire_date <= today + timedelta(days=inv.reagent.expire_warning_days):
            warning_batches += 1

    today_in = db.session.query(db.func.coalesce(db.func.sum(StockIn.quantity), 0)).filter(
        StockIn.status == StockIn.STATUS_DONE, StockIn.audit_time >= start).scalar()
    today_out = db.session.query(db.func.coalesce(db.func.sum(StockOut.quantity), 0)).filter(
        StockOut.status == StockOut.STATUS_DONE, StockOut.confirm_time >= start).scalar()

    return {
        "reagent_count": Reagent.query.filter_by(status=1).count(),
        "in_stock_boxes": int(in_stock_boxes),
        "in_use_units": db.session.query(
            db.func.coalesce(
                db.func.sum(ReagentPack.total_units - ReagentPack.used_units), 0)).filter(
            ReagentPack.status.in_([ReagentPack.STATUS_ISSUED, ReagentPack.STATUS_USING])
        ).scalar(),
        "today_in_qty": int(today_in or 0),
        "today_out_qty": int(today_out or 0),
        "warning_count": int(warning_batches),
        "expire_count": int(expired_batches),
    }


def get_expire_warnings(limit: int = 20):
    """在库批次效期预警（临期/过期）"""
    # 仅取可能预警的批次；关系一次性加载，避免模板访问 reagent 时逐行查询。
    invs = Inventory.query.options(joinedload(Inventory.reagent)).filter(
        Inventory.quantity > 0).order_by(Inventory.expire_date.asc()).all()
    rows = []
    for inv in invs:
        st = inv.stock_status
        if st == "normal":
            continue
        rows.append((st, inv))
    # 过期优先，再按效期早
    order = {"expired": 0, "warning": 1}
    rows.sort(key=lambda x: (order.get(x[0], 9), x[1].expire_date))
    result = []
    for st, inv in rows[:limit]:
        days = inv.days_left
        if days < 0:
            suggestion = "已过期，建议立即报废处理"
        else:
            suggestion = "即将到期，建议尽快出库使用"
        result.append({
            "reagent": inv.reagent,
            "batch_no": inv.batch_no,
            "expire_date": inv.expire_date,
            "boxes": inv.quantity,
            "days": days,
            "status": st,
            "suggestion": suggestion,
        })
    return result


def get_low_stock_warnings(limit: int = 10):
    """库存不足预警（口径由试剂档案的 min_stock_unit 决定）：
    - pack（默认）：比较中心在库盒数，与历史行为一致；
    - unit：比较全科可用瓶数 = 中心在库未开封 + 科室已出库未用完。
    min_stock 为 0 表示该试剂不参与预警。
    """
    low = []
    for r in Reagent.query.options(selectinload(Reagent.packs)).filter(
            Reagent.status == 1).all():
        minv = r.min_stock or 0
        if minv <= 0:
            continue
        current = r.current_stock_for_min
        if current >= minv:
            continue
        gap = minv - current
        unit_text = r.min_stock_unit_text
        if r.min_stock_unit == Reagent.MIN_UNIT_UNIT and (r.pack_size or 1) > 1:
            boxes = -(-gap // (r.pack_size or 1))   # 向上取整，折算需补的盒数
            suggestion = f"建议补货 {gap} {unit_text}（约 {boxes} {r.unit or '盒'}）"
        else:
            suggestion = f"建议补货 {gap} {unit_text}以上"
        low.append({
            "reagent": r,
            "current": current,
            "min": minv,
            "gap": gap,
            "unit_text": unit_text,
            "by_unit": r.min_stock_unit == Reagent.MIN_UNIT_UNIT,
            "center_qty": r.in_stock_units if r.min_stock_unit == Reagent.MIN_UNIT_UNIT else r.in_stock_boxes,
            "issued_qty": r.in_use_units if r.min_stock_unit == Reagent.MIN_UNIT_UNIT else 0,
            "suggestion": suggestion,
        })
    low.sort(key=lambda x: x["current"] - x["min"])
    return low[:limit]


def trend_data(days: int = 30):
    """近 N 天出入库趋势（盒数）：两次分组查询取回全部日期，替代逐日两次查询。"""
    today = date.today()
    start = datetime.combine(today - timedelta(days=days - 1), dtime.min)
    end = datetime.combine(today + timedelta(days=1), dtime.min)

    def daily_sum(model, time_col):
        rows = db.session.query(
            db.func.date(time_col), db.func.sum(model.quantity)
        ).filter(
            model.status == model.STATUS_DONE,
            time_col >= start, time_col < end,
        ).group_by(db.func.date(time_col)).all()
        return {str(day): int(qty or 0) for day, qty in rows}

    ins = daily_sum(StockIn, StockIn.audit_time)
    outs = daily_sum(StockOut, StockOut.confirm_time)

    labels, in_list, out_list = [], [], []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        key = d.strftime("%Y-%m-%d")
        labels.append(d.strftime("%m-%d"))
        in_list.append(ins.get(key, 0))
        out_list.append(outs.get(key, 0))
    return {"labels": labels, "in": in_list, "out": out_list}
