# -*- coding: utf-8 -*-
"""通用工具：操作日志、单号生成、Excel 导出"""
import os
import uuid
from datetime import datetime, date, timedelta
from flask import current_app, request, send_file
from flask_login import current_user
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from app.extensions import db
from app.models.log import OperLog


def parse_export_range():
    """解析导出时间范围参数。
    range=week/month/half/year 对应最近一周/一月/半年/一年；
    range=custom 时使用 start/end 自定义日期；
    无参数时返回 (None, None) 表示全部时间。
    返回 (start_date, end_date)，均为 date 或 None。
    """
    rng = request.args.get("range", "")
    start = _parse_ymd(request.args.get("start", ""))
    end = _parse_ymd(request.args.get("end", ""))
    if not start:
        days = {"week": 7, "month": 30, "half": 183, "year": 365}.get(rng)
        if days:
            start = date.today() - timedelta(days=days)
    return start, end


def _parse_ymd(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def range_label(start, end) -> str:
    """导出日志里可读的时间范围描述"""
    if not start and not end:
        return "全部时间"
    s = start.strftime("%Y-%m-%d") if start else "最早"
    e = end.strftime("%Y-%m-%d") if end else "今天"
    return f"{s} ~ {e}"


def log_operation(module: str, content: str):
    """记录一条操作日志（在请求上下文中调用）"""
    try:
        log = OperLog(
            user_id=current_user.id if current_user.is_authenticated else None,
            username=current_user.username if current_user.is_authenticated else "系统",
            module=module,
            content=content,
            ip=request.remote_addr,
        )
        db.session.add(log)
        db.session.commit()
    except Exception as exc:  # 日志失败不应影响主流程
        db.session.rollback()
        current_app.logger.warning(f"记录操作日志失败：{exc}")


def gen_order_no(prefix: str) -> str:
    """生成业务单号，如 RK20261019153012-4821"""
    now = datetime.now()
    # 随机 UUID 片段使多终端同秒创建单据也保持足够低的碰撞概率。
    return f"{prefix}{now.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:10].upper()}"


def gen_barcodes(pack_model, n: int):
    """批量生成 10 位纯数字条码：6 位日期(yyMMdd) + 4 位当日序号(0001 起)
    采用 Code128 编码，无需 EAN-13 校验位，扫码枪可直接读取。
    例：2026-10-20 第一盒 -> 2610200001

    取号方式：先一条 UPDATE 原子自增，再读回当前值。MySQL 下这条 UPDATE 会锁行，
    SQLite 下会锁库，因此不会出现"两个请求读到同一个序号"而生成重复条码。
    """
    from sqlalchemy.exc import IntegrityError
    from app.models.stock import BarcodeSequence

    if n <= 0:
        return []
    prefix = datetime.now().strftime("%y%m%d")

    # 当天首次取号时创建序列行；并发下别人可能先创建，用保存点隔离唯一键冲突
    if db.session.query(BarcodeSequence.prefix).filter_by(prefix=prefix).first() is None:
        try:
            with db.session.begin_nested():
                db.session.add(BarcodeSequence(prefix=prefix, next_value=1))
                db.session.flush()
        except IntegrityError:
            pass  # 已由其他请求创建，直接复用

    db.session.query(BarcodeSequence).filter_by(prefix=prefix).update(
        {BarcodeSequence.next_value: BarcodeSequence.next_value + n},
        synchronize_session=False)
    next_value = db.session.query(BarcodeSequence.next_value).filter_by(
        prefix=prefix).scalar()
    if not next_value:
        raise RuntimeError("条码序列初始化失败，请重试")
    last = next_value - 1              # 本次发出的最后一个序号
    if last > 9999:
        raise ValueError("当日条码数量已超过 9999，请联系管理员")
    start = last - n + 1
    return [prefix + f"{value:04d}" for value in range(start, last + 1)]


def export_excel(sheet_name: str, headers: list, rows: list, filename: str) -> str:
    """
    导出 Excel
    :param headers: 表头列表
    :param rows: 二维列表数据
    :param filename: 保存文件名
    :return: 文件完整路径
    """
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name

    # 样式
    header_font = Font(name="微软雅黑", bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="165DFF")
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left = Alignment(horizontal="left", vertical="center")
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    MAX_ROWS = 50000
    if len(rows) > MAX_ROWS:
        print(f"[导出警告] 数据 {len(rows)} 行超过上限 {MAX_ROWS}，已截断。建议缩小时间范围。")
        rows = rows[:MAX_ROWS]

    ws.append(headers)
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
        cell.border = border

    # 先写入所有行（tuple 类型先转空字符串占位，后面再处理签名）
    for row in rows:
        ws.append(["" if isinstance(v, tuple) else v for v in row])

    # 处理签名单元格：值为 (user_id, 显示名) 的，若该用户有签名则插入图片，否则显示文字
    SIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "instance", "signatures")
    sig_cache = {}
    def get_sig(user_id):
        if user_id in sig_cache:
            return sig_cache[user_id]
        path = None
        try:
            from app.models.user import User
            u = db.session.get(User, user_id)
            if u and u.signature_image:
                p = os.path.join(SIG_DIR, u.signature_image)
                if os.path.exists(p):
                    path = p
        except Exception:
            pass
        sig_cache[user_id] = path
        return path

    for r in ws.iter_rows(min_row=2):
        for cell in r:
            cell.border = border
            cell.alignment = left if isinstance(cell.value, str) and len(str(cell.value)) > 4 else center

    # 自动列宽（粗略）
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            if cell.value is not None:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max(max_len * 1.8 + 4, 10), 40)

    # 签名单元格：没有签名的写姓名，有签名的插入浮动图片
    sig_cells = []  # (cell_ref, sig_path, fallback_name)
    # 签名图片尺寸（像素）——按 240x100 原图比例，适配 11字符列宽 / 20.2磅行高
    SIG_IMG_W = 60   # 图片宽 60px
    SIG_IMG_H = 25   # 图片高 25px
    SIG_ROW_H = 22   # 行高（磅）
    for row_idx, row in enumerate(rows, start=2):
        for col_idx, val in enumerate(row):
            if isinstance(val, tuple) and len(val) == 2 and isinstance(val[0], int):
                user_id, fallback = val
                sig_path = get_sig(user_id)
                cell_ref = f"{ws.cell(row=row_idx, column=col_idx+1).column_letter}{row_idx}"
                if sig_path:
                    sig_cells.append((cell_ref, sig_path, fallback))
                    ws[cell_ref] = None
                    ws.row_dimensions[row_idx].height = SIG_ROW_H
                else:
                    ws[cell_ref] = fallback

    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 24

    # 插入签名浮动图片（OneCellAnchor 锚定到单元格并居中）
    # 注意：AnchorMarker 只在 openpyxl 3.1+ 的 spreadsheet_drawing 里有；
    # 老版本（2.x / 3.0）上这行 import 会直接 ImportError，把整个导出打成 500。
    # 所以这里软导入：拿不到就退回「字符串锚点」写法（所有版本都支持）。
    from openpyxl.drawing.image import Image as XLImage

    _ANCHOR_API = True
    try:
        from openpyxl.drawing.spreadsheet_drawing import OneCellAnchor, AnchorMarker
        from openpyxl.drawing.xdr import XDRPositiveSize2D
        from openpyxl.utils.units import pixels_to_EMU
        AnchorMarker(col=0, colOff=0, row=0, rowOff=0)   # 试构造一次，确认版本真支持
    except Exception as _imp_err:
        _ANCHOR_API = False
        print(f"[导出签名] 当前 openpyxl 不支持精确锚点（{_imp_err}），改用单元格锚定")

    for cell_ref, sig_path, fallback in sig_cells:
        try:
            img = XLImage(sig_path)
            img.width = SIG_IMG_W
            img.height = SIG_IMG_H
            cell = ws[cell_ref]
            if _ANCHOR_API:
                col_letter = cell.column_letter
                col_width_px = ws.column_dimensions[col_letter].width * 7 + 5
                off_x = max(0, int((col_width_px - SIG_IMG_W) / 2))
                off_y = max(0, int((SIG_ROW_H * 96 / 72 - SIG_IMG_H) / 2))
                marker = AnchorMarker(col=cell.column - 1, colOff=pixels_to_EMU(off_x),
                                      row=cell.row - 1, rowOff=pixels_to_EMU(off_y))
                size = XDRPositiveSize2D(pixels_to_EMU(SIG_IMG_W), pixels_to_EMU(SIG_IMG_H))
                img.anchor = OneCellAnchor(_from=marker, ext=size)
            else:
                # 老版本：图片就锚在这个单元格（左上角对齐，位置够用）
                img.anchor = cell.coordinate
            ws.add_image(img)
        except Exception as e:
            print(f"[导出签名] 图片插入失败 {cell_ref}: {e}")
            ws[cell_ref] = fallback

    folder = current_app.config["EXPORT_FOLDER"]
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, filename)
    # Windows 下同名文件可能还被上一次下载的响应占用（连点两次导出就会撞名），
    # 直接覆盖会 PermissionError → 页面 500。这里撞名就换一个唯一名字。
    base, ext = os.path.splitext(filename)
    for _ in range(20):
        if not os.path.exists(path):
            break
        path = os.path.join(folder, f"{base}_{uuid.uuid4().hex[:8]}{ext}")
    try:
        wb.save(path)
    except PermissionError:
        path = os.path.join(folder, f"{base}_{uuid.uuid4().hex[:8]}{ext}")
        wb.save(path)
    return path


def send_excel(path: str, download_name: str):
    """返回 Excel 文件下载响应（而不是只存到服务器）"""
    return send_file(
        path,
        as_attachment=True,
        download_name=download_name,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
