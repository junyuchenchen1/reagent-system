# -*- coding: utf-8 -*-
"""首页仪表盘"""
from flask import Blueprint, render_template
from flask_login import login_required

from app.extensions import db
from app.models.stock import ReagentPack
from app.services import warning_service as ws

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@login_required
def index():
    stats = ws.summary_cards()
    trend = ws.trend_data(30)
    warnings = ws.get_expire_warnings(10)
    low_stock = ws.get_low_stock_warnings(10)

    # 各分类在库盒数占比
    from app.models.reagent import Category, Reagent
    category_rows = db.session.query(
        db.func.coalesce(Category.name, "未分类"), db.func.count(ReagentPack.id)
    ).join(Reagent, ReagentPack.reagent_id == Reagent.id).outerjoin(
        Category, Reagent.category_id == Category.id
    ).filter(ReagentPack.status == ReagentPack.STATUS_IN_STOCK).group_by(Category.name).all()
    category_stock = [{"name": name, "value": int(total)}
                      for name, total in category_rows if total > 0]

    return render_template(
        "dashboard/index.html",
        stats=stats,
        trend=trend,
        category_stock=category_stock,
        warnings=warnings,
        low_stock=low_stock,
    )
