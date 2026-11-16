# -*- coding: utf-8 -*-
"""基础数据导入导出：分类/供应商/位置/试剂档案/用户"""
import os
from datetime import datetime, date
from io import BytesIO

from flask import Blueprint, render_template, request, flash, send_file, redirect, url_for
from flask_login import login_required
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from app.extensions import db
from app.models.reagent import Category, Supplier, StorageLocation, Reagent
from app.models.user import User
from app.utils.permission import admin_required
from app.utils.common import log_operation

databackup_bp = Blueprint("databackup", __name__)


# ============ 导出 ============
@databackup_bp.route("/export")
@login_required
@admin_required
def export_all():
    wb = Workbook()
    wb.remove(wb.active)

    header_font = Font(name="微软雅黑", bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="165DFF")
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    def add_sheet(title, headers, rows):
        ws = wb.create_sheet(title)
        ws.append(headers)
        for c in ws[1]:
            c.font = header_font
            c.fill = header_fill
            c.alignment = center
            c.border = border
        for r in rows:
            ws.append(r)
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.border = border
                c.alignment = Alignment(vertical="center")
        for col in ws.columns:
            ml = max((len(str(c.value)) if c.value is not None else 0) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(max(ml * 1.8 + 4, 10), 40)
        ws.freeze_panes = "A2"

    # 1. 试剂分类
    cats = Category.query.order_by(Category.sort, Category.id).all()
    add_sheet("试剂分类",
              ["分类名称", "排序"],
              [[c.name, c.sort or 0] for c in cats])

    # 2. 供应商
    sups = Supplier.query.order_by(Supplier.id).all()
    add_sheet("供应商",
              ["供应商名称", "联系人", "电话", "地址", "许可证号", "许可证有效期", "类型", "状态", "备注"],
              [[s.name, s.contact or "", s.phone or "", s.address or "",
                s.license_no or "",
                s.license_expire_date.strftime("%Y-%m-%d") if s.license_expire_date else "",
                s.supplier_type or "经销商",
                "启用" if s.status == 1 else "停用",
                s.remark or ""] for s in sups])

    # 3. 存放位置
    locs = StorageLocation.query.order_by(StorageLocation.sort, StorageLocation.id).all()
    add_sheet("存放位置",
              ["位置名称", "排序", "状态", "备注"],
              [[l.name, l.sort or 0,
                "启用" if l.status == 1 else "停用",
                l.remark or ""] for l in locs])

    # 4. 试剂档案
    regs = Reagent.query.order_by(Reagent.id).all()
    add_sheet("试剂档案",
              ["试剂名称", "分类", "规格", "大包装单位", "每包数量", "最小单位",
               "最低库存", "最低库存单位", "效期预警天数", "存储条件", "是否危险品",
               "注册证号", "注册证有效期", "注册人/生产企业", "代理人",
               "状态", "备注"],
              [[r.name,
                r.category.name if r.category else "",
                r.spec or "", r.unit or "盒", r.pack_size or 1, r.small_unit or "瓶",
                r.min_stock or 0,
                "最小单位" if r.min_stock_unit == Reagent.MIN_UNIT_UNIT else "大包装",
                r.expire_warning_days or 90,
                r.storage_condition or "冷藏",
                "是" if r.is_hazard == 1 else "否",
                r.reg_no or "",
                r.reg_expire_date.strftime("%Y-%m-%d") if r.reg_expire_date else "",
                r.registrant or "", r.agent or "",
                "启用" if r.status == 1 else "停用",
                r.remark or ""] for r in regs])

    # 5. 用户
    users = User.query.order_by(User.id).all()
    add_sheet("用户",
              ["登录账号", "姓名", "科室", "电话", "角色", "状态"],
              [[u.username, u.real_name, u.department or "检验科", u.phone or "",
                u.role_name,
                "正常" if u.status == 1 else "禁用"] for u in users])

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    log_operation("数据导入导出", "导出全部基础数据")
    fname = f"基础数据导出_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ============ 导入 ============
@databackup_bp.route("/import", methods=["POST"])
@login_required
@admin_required
def import_all():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("请选择要导入的 xlsx 文件", "danger")
        return redirect(url_for("databackup.page"))

    # 导入前自动备份数据库
    import shutil
    db_path = os.path.join("instance", "reagent.db")
    backup_dir = os.path.join("instance", "backups")
    os.makedirs(backup_dir, exist_ok=True)
    if os.path.exists(db_path):
        backup_name = f"pre_import_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        backup_path = os.path.join(backup_dir, backup_name)
        shutil.copy2(db_path, backup_path)

    try:
        wb = load_workbook(f, data_only=True)
    except Exception as e:
        flash(f"文件读取失败：{e}", "danger")
        return redirect(url_for("databackup.page"))

    stats = {"分类": 0, "供应商": 0, "位置": 0, "试剂": 0, "用户": 0}
    errors = []

    def row_dict(ws, row_idx):
        """把一行读成 dict（按表头）"""
        headers = [c.value for c in ws[1]]
        vals = [c.value for c in ws[row_idx]]
        return {headers[i]: vals[i] if i < len(vals) else None for i in range(len(headers))}

    # 1. 试剂分类
    if "试剂分类" in wb.sheetnames:
        ws = wb["试剂分类"]
        for i in range(2, ws.max_row + 1):
            d = row_dict(ws, i)
            name = (d.get("分类名称") or "").strip()
            if not name:
                continue
            obj = Category.query.filter_by(name=name).first()
            if not obj:
                obj = Category(name=name)
                db.session.add(obj)
            obj.sort = int(d.get("排序") or 0)
            stats["分类"] += 1

    # 2. 供应商
    if "供应商" in wb.sheetnames:
        ws = wb["供应商"]
        for i in range(2, ws.max_row + 1):
            d = row_dict(ws, i)
            name = (d.get("供应商名称") or "").strip()
            if not name:
                continue
            obj = Supplier.query.filter_by(name=name).first()
            if not obj:
                obj = Supplier(name=name)
                db.session.add(obj)
            obj.contact = (d.get("联系人") or "").strip()
            obj.phone = (d.get("电话") or "").strip()
            obj.address = (d.get("地址") or "").strip()
            obj.license_no = (d.get("许可证号") or "").strip()
            obj.supplier_type = (d.get("类型") or "经销商").strip()
            obj.status = 0 if (d.get("状态") or "").strip() == "停用" else 1
            obj.remark = (d.get("备注") or "").strip()
            ed = (d.get("许可证有效期") or "").strip()
            if ed:
                try:
                    obj.license_expire_date = datetime.strptime(ed[:10], "%Y-%m-%d").date()
                except Exception:
                    pass
            stats["供应商"] += 1

    # 3. 存放位置
    if "存放位置" in wb.sheetnames:
        ws = wb["存放位置"]
        for i in range(2, ws.max_row + 1):
            d = row_dict(ws, i)
            name = (d.get("位置名称") or "").strip()
            if not name:
                continue
            obj = StorageLocation.query.filter_by(name=name).first()
            if not obj:
                obj = StorageLocation(name=name)
                db.session.add(obj)
            obj.sort = int(d.get("排序") or 0)
            obj.status = 0 if (d.get("状态") or "").strip() == "停用" else 1
            obj.remark = (d.get("备注") or "").strip()
            stats["位置"] += 1

    # 4. 试剂档案
    if "试剂档案" in wb.sheetnames:
        ws = wb["试剂档案"]
        for i in range(2, ws.max_row + 1):
            d = row_dict(ws, i)
            name = (d.get("试剂名称") or "").strip()
            if not name:
                continue
            obj = Reagent.query.filter_by(name=name).first()
            if not obj:
                obj = Reagent(name=name)
                db.session.add(obj)
            cat_name = (d.get("分类") or "").strip()
            if cat_name:
                cat = Category.query.filter_by(name=cat_name).first()
                if cat:
                    obj.category_id = cat.id
            obj.spec = (d.get("规格") or "").strip()
            obj.unit = (d.get("大包装单位") or "盒").strip()
            try:
                obj.pack_size = int(d.get("每包数量") or 1)
            except Exception:
                obj.pack_size = 1
            obj.small_unit = (d.get("最小单位") or "瓶").strip()
            try:
                obj.min_stock = int(d.get("最低库存") or 0)
            except Exception:
                obj.min_stock = 0
            # 旧导出文件没有"最低库存单位"列：缺省按大包装，与历史口径一致
            obj.min_stock_unit = (Reagent.MIN_UNIT_UNIT
                                  if (d.get("最低库存单位") or "").strip() == "最小单位"
                                  else Reagent.MIN_UNIT_PACK)
            try:
                obj.expire_warning_days = int(d.get("效期预警天数") or 90)
            except Exception:
                obj.expire_warning_days = 90
            sc = (d.get("存储条件") or "冷藏").strip()
            if sc in Reagent.STORAGE_CHOICES:
                obj.storage_condition = sc
            obj.is_hazard = 1 if (d.get("是否危险品") or "").strip() == "是" else 0
            obj.reg_no = (d.get("注册证号") or "").strip()
            obj.registrant = (d.get("注册人/生产企业") or "").strip()
            obj.agent = (d.get("代理人") or "").strip()
            obj.status = 0 if (d.get("状态") or "").strip() == "停用" else 1
            obj.remark = (d.get("备注") or "").strip()
            ed = (d.get("注册证有效期") or "").strip()
            if ed:
                try:
                    obj.reg_expire_date = datetime.strptime(ed[:10], "%Y-%m-%d").date()
                except Exception:
                    pass
            stats["试剂"] += 1

    # 角色反向映射：中文 → code
    ROLE_LABEL_TO_CODE = {label: code for code, label in User.ROLE_MAP.items()}

    # 5. 用户（只更新基础信息，不改密码，已存在的用户跳过）
    if "用户" in wb.sheetnames:
        ws = wb["用户"]
        for i in range(2, ws.max_row + 1):
            d = row_dict(ws, i)
            uname = (str(d.get("登录账号") or "")).strip()
            if not uname:
                continue
            obj = User.query.filter_by(username=uname).first()
            rname = (str(d.get("角色") or "")).strip()
            role_code = ROLE_LABEL_TO_CODE.get(rname, User.ROLE_STAFF)
            status_val = 0 if (str(d.get("状态") or "")).strip() == "禁用" else 1
            if obj:
                # 已存在：更新基础信息和角色，不改密码
                obj.real_name = (str(d.get("姓名") or "")).strip() or obj.real_name
                obj.department = (str(d.get("科室") or "检验科")).strip()
                obj.phone = (str(d.get("电话") or "")).strip()
                obj.role = role_code
                obj.status = status_val
            else:
                # 新用户：默认密码 123456
                real = (str(d.get("姓名") or "")).strip() or uname
                obj = User(username=uname, real_name=real)
                obj.department = (str(d.get("科室") or "检验科")).strip()
                obj.phone = (str(d.get("电话") or "")).strip()
                obj.role = role_code
                obj.status = status_val
                obj.set_password("123456")
                db.session.add(obj)
            stats["用户"] += 1

    db.session.commit()
    log_operation("数据导入导出",
                  f"导入：分类{stats['分类']} 供应商{stats['供应商']} 位置{stats['位置']} 试剂{stats['试剂']} 用户{stats['用户']}")
    flash(f"导入完成：分类{stats['分类']}条、供应商{stats['供应商']}条、位置{stats['位置']}条、"
          f"试剂{stats['试剂']}条、用户{stats['用户']}条。新用户默认密码 123456。"
          f"（导入前已自动备份到 instance/backups/）", "success")
    return redirect(url_for("databackup.page"))


@databackup_bp.route("/")
@login_required
@admin_required
def page():
    return render_template("admin/databackup.html")
