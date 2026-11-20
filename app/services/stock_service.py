# -*- coding: utf-8 -*-
"""库存业务服务：入库生盒、整盒出库、瓶级使用、盘点"""
from datetime import datetime, date
from app.extensions import db
from app.models.stock import (
    StockIn, StockOut, Inventory, ReagentPack, PackUse,
    StockCheck, StockCheckItem,
)
from app.utils.common import gen_order_no, gen_barcodes


# ============================================================
# 库存汇总校准
# ============================================================
def reconcile_inventory() -> int:
    """按“实际在库盒”重算汇总库存（Inventory），修复历史漂移。

    正常流程下 Inventory.quantity 恒等于该试剂+批号的在库盒数。但历史版本、
    手工改库、恢复旧备份等都可能让两者对不上（例如某盒早就出库/报废了，
    汇总却没减，实时库存就一直显示“还有 1 盒”）。实时库存、效期预警、
    导出都读汇总表，所以启动时校准一次。返回被修正的批次数。
    """
    packs = ReagentPack.query.filter_by(
        status=ReagentPack.STATUS_IN_STOCK).all()
    counts, first_pack = {}, {}
    for p in packs:
        key = (p.reagent_id, p.batch_no)
        counts[key] = counts.get(key, 0) + 1
        cur = first_pack.get(key)
        if cur is None or (p.expire_date and cur.expire_date
                           and p.expire_date < cur.expire_date):
            first_pack[key] = p

    changed = 0
    for inv in Inventory.query.all():
        key = (inv.reagent_id, inv.batch_no)
        n = counts.pop(key, 0)
        if n == 0:
            db.session.delete(inv)          # 该批次已无在库盒，汇总行应删除
            changed += 1
        elif inv.quantity != n:
            inv.quantity = n
            changed += 1

    # 有在库盒但汇总行缺失（例如恢复旧备份后）
    for key, n in counts.items():
        p = first_pack[key]
        db.session.add(Inventory(
            reagent_id=p.reagent_id, batch_no=p.batch_no,
            expire_date=p.expire_date, quantity=n,
            storage_location=p.storage_location or ""))
        changed += 1

    if changed:
        db.session.commit()
    return changed


# ============================================================
# 入库
# ============================================================
def create_stock_in(data: dict, user) -> StockIn:
    """创建待审核入库单，quantity 为盒数"""
    stock_in = StockIn(
        order_no=gen_order_no("RK"),
        reagent_id=data["reagent_id"],
        batch_no=data["batch_no"].strip(),
        expire_date=data["expire_date"],
        quantity=int(data["quantity"]),
        unit_price=data.get("unit_price") or 0,
        supplier_id=data.get("supplier_id") or None,
        storage_location=data.get("storage_location"),
        operator_id=user.id,
        verifier_id=data.get("verifier_id"),
        accept_status=data.get("accept_status", "合格"),
        is_verified=data.get("is_verified", False),
        udi_di=data.get("udi_di") or None,
        remark=data.get("remark"),
    )
    db.session.add(stock_in)
    db.session.commit()
    return stock_in


def confirm_stock_in(stock_in: StockIn, user):
    """审核入库：增库存，并为每一盒生成独立条码盒"""
    if stock_in.status != StockIn.STATUS_PENDING:
        raise ValueError("该入库单已处理")

    now = datetime.now()
    stock_in.status = StockIn.STATUS_DONE
    stock_in.auditor_id = user.id
    stock_in.audit_time = now

    # 维护中心在库批次汇总
    inv = Inventory.query.filter_by(
        reagent_id=stock_in.reagent_id, batch_no=stock_in.batch_no).first()
    if inv:
        inv.quantity += stock_in.quantity
        inv.expire_date = stock_in.expire_date
        inv.storage_location = stock_in.storage_location or inv.storage_location
    else:
        inv = Inventory(
            reagent_id=stock_in.reagent_id, batch_no=stock_in.batch_no,
            expire_date=stock_in.expire_date, quantity=stock_in.quantity,
            storage_location=stock_in.storage_location)
        db.session.add(inv)

    # 为每盒生成条码盒
    reagent = stock_in.reagent
    barcodes = gen_barcodes(ReagentPack, stock_in.quantity)
    packs = []
    for bc in barcodes:
        pack = ReagentPack(
            barcode=bc,
            reagent_id=stock_in.reagent_id,
            batch_no=stock_in.batch_no,
            expire_date=stock_in.expire_date,
            stock_in_id=stock_in.id,
            unit_price=stock_in.unit_price,
            total_units=reagent.pack_size or 1,
            storage_location=stock_in.storage_location,
            status=ReagentPack.STATUS_IN_STOCK,
        )
        db.session.add(pack)
        packs.append(pack)

    db.session.commit()
    return packs


def cancel_stock_in(stock_in: StockIn):
    if stock_in.status != StockIn.STATUS_PENDING:
        raise ValueError("仅待审核单据可取消")
    stock_in.status = StockIn.STATUS_CANCEL
    db.session.commit()


# ============================================================
# 出库（整盒）
# ============================================================
def available_in_stock_boxes(reagent_id: int, allow_expired: bool = False) -> int:
    """中心在库可出库盒数"""
    q = ReagentPack.query.filter_by(
        reagent_id=reagent_id, status=ReagentPack.STATUS_IN_STOCK)
    if not allow_expired:
        q = q.filter(ReagentPack.expire_date >= date.today())
    return q.count()


def pick_in_stock_packs(reagent_id: int, quantity: int, allow_expired: bool = False):
    """先进先出：按效期从早到晚选出在库盒；数量不足返回 None"""
    q = ReagentPack.query.filter_by(
        reagent_id=reagent_id, status=ReagentPack.STATUS_IN_STOCK
    )
    if not allow_expired:
        q = q.filter(ReagentPack.expire_date >= date.today())
    packs = q.order_by(ReagentPack.expire_date.asc(), ReagentPack.id.asc()).with_for_update().limit(quantity).all()
    if len(packs) < quantity:
        return None
    return packs[:quantity]


def create_stock_out(data: dict, user) -> StockOut:
    """创建待确认出库单（整盒）。支持扫码锁定单盒，或按试剂+盒数 FIFO"""
    out_type = data.get("out_type", "use")
    scan_barcode = (data.get("scan_barcode") or "").strip()
    if out_type == StockOut.TYPE_SCRAP and not data.get("reason", "").strip():
        raise ValueError("报废出库必须填写报废原因")

    if scan_barcode:
        pack = get_pack_by_barcode(scan_barcode)
        if not pack:
            raise ValueError("未找到该条码对应的试剂盒，请核对后重试")
        if pack.status != ReagentPack.STATUS_IN_STOCK:
            raise ValueError(f"该盒当前状态为「{pack.status_text}」，无法出库")
        if out_type != StockOut.TYPE_SCRAP and pack.days_left < 0:
            raise ValueError("该盒已过期，不能领用/质控，请走报废流程")
        reagent_id, quantity = pack.reagent_id, 1
    else:
        reagent_id = data["reagent_id"]
        quantity = int(data["quantity"])
        allow_expired = out_type == StockOut.TYPE_SCRAP
        if not reagent_id:
            raise ValueError("请选择试剂")
        if quantity <= 0:
            raise ValueError("出库盒数必须大于 0")
        available = available_in_stock_boxes(reagent_id, allow_expired)
        if quantity > available:
            raise ValueError(
                f"在库可用整盒不足（当前 {available} 盒，申请 {quantity} 盒）")

    out = StockOut(
        order_no=gen_order_no("CK"),
        reagent_id=reagent_id,
        quantity=quantity,
        out_type=out_type,
        scan_barcode=scan_barcode or None,
        receiver=data.get("receiver"),
        applicant_id=user.id,
        reason=data.get("reason"),
    )
    db.session.add(out)
    db.session.commit()
    return out


def confirm_stock_out(out: StockOut, user):
    """确认出库：扫码锁定盒或 FIFO 分配，在库→已出库(或报废)，扣减中心在库"""
    if out.status != StockOut.STATUS_PENDING:
        raise ValueError("该出库单已处理")

    allow_expired = out.out_type == StockOut.TYPE_SCRAP
    if out.scan_barcode:
        pack = (ReagentPack.query.filter_by(barcode=out.scan_barcode)
                .with_for_update().first())
        if (not pack or pack.status != ReagentPack.STATUS_IN_STOCK
                or pack.reagent_id != out.reagent_id):
            raise ValueError("扫码锁定的盒已不在库或与试剂不匹配")
        if not allow_expired and pack.days_left < 0:
            raise ValueError("该盒已过期，不能领用/质控")
        packs = [pack]
    else:
        packs = pick_in_stock_packs(out.reagent_id, out.quantity, allow_expired)
        if not packs:
            raise ValueError("在库可用整盒不足，无法出库")

    now = datetime.now()
    for p in packs:
        p.stock_out_id = out.id
        p.receiver = out.receiver
        # 接收人取单据上的接收人（领用人），不能记成确认出库的人
        p.receiver_id = out.receiver_id
        p.issued_time = now
        if out.out_type == StockOut.TYPE_SCRAP:
            p.status = ReagentPack.STATUS_SCRAPPED
            p.close_time = now
        else:
            p.status = ReagentPack.STATUS_ISSUED

    # 按批次扣减中心在库汇总
    batch_count = {}
    for p in packs:
        batch_count[p.batch_no] = batch_count.get(p.batch_no, 0) + 1
    for bn, cnt in batch_count.items():
        inv = Inventory.query.filter_by(reagent_id=out.reagent_id, batch_no=bn).first()
        if inv:
            inv.quantity = max(0, inv.quantity - cnt)
            if inv.quantity <= 0:
                db.session.delete(inv)

    out.status = StockOut.STATUS_DONE
    out.confirmer_id = user.id
    out.confirm_time = now
    db.session.commit()
    return packs


def cancel_stock_out(out: StockOut):
    if out.status != StockOut.STATUS_PENDING:
        raise ValueError("仅待确认单据可取消")
    out.status = StockOut.STATUS_CANCEL
    db.session.commit()


# ============================================================
# 盒 / 条码
# ============================================================
def get_pack_by_barcode(barcode: str):
    if not barcode:
        return None
    return ReagentPack.query.filter_by(barcode=barcode.strip()).first()


def get_issued_packs_for_use(reagent_id: int):
    """科室已出库、可登记使用的盒；优先已开盒(using)，再按效期早"""
    packs = ReagentPack.query.filter(
        ReagentPack.reagent_id == reagent_id,
        ReagentPack.status.in_([ReagentPack.STATUS_ISSUED, ReagentPack.STATUS_USING])
    ).all()
    rank = {ReagentPack.STATUS_USING: 0, ReagentPack.STATUS_ISSUED: 1}
    return sorted(packs, key=lambda p: (rank.get(p.status, 9), p.expire_date, p.id))


# ============================================================
# 使用登记（开盒后按瓶）
# ============================================================
def register_use(pack: ReagentPack, units: int, use_type: str, user, remark: str = None):
    """在某盒上登记使用瓶数；返回使用记录"""
    if pack.status in (ReagentPack.STATUS_EMPTY, ReagentPack.STATUS_SCRAPPED):
        raise ValueError("该盒已用完或已报废，无法登记使用")
    if pack.status == ReagentPack.STATUS_IN_STOCK:
        raise ValueError("该盒尚在中心库，请先在【试剂出库】整盒出库到科室")
    units = int(units)
    if units <= 0:
        raise ValueError("使用数量必须大于 0")
    if units > pack.remaining_units:
        raise ValueError(f"使用数量超过该盒剩余瓶数（剩余 {pack.remaining_units} 瓶）")

    record = PackUse(
        use_no=gen_order_no("SY"),
        pack_id=pack.id,
        reagent_id=pack.reagent_id,
        units_used=units,
        use_type=use_type,
        user_id=user.id,
        remark=remark,
    )
    db.session.add(record)

    pack.used_units += units
    if pack.status == ReagentPack.STATUS_ISSUED:
        pack.status = ReagentPack.STATUS_USING
    if pack.remaining_units == 0:
        pack.status = ReagentPack.STATUS_EMPTY
        pack.close_time = datetime.now()

    db.session.commit()
    return record


# ============================================================
# 盘点
# ============================================================
def start_check(data: dict, user) -> StockCheck:
    """开始盘点：快照中心在库（按试剂+批号）"""
    check = StockCheck(
        check_no=gen_order_no("PD"),
        title=data.get("title") or f"盘点-{datetime.now().strftime('%Y%m%d')}",
        operator_id=user.id,
        remark=data.get("remark"),
    )
    db.session.add(check)
    db.session.flush()

    invs = Inventory.query.filter(Inventory.quantity > 0).order_by(
        Inventory.reagent_id, Inventory.expire_date).all()
    for inv in invs:
        item = StockCheckItem(
            check_id=check.id,
            reagent_id=inv.reagent_id,
            batch_no=inv.batch_no,
            expire_date=inv.expire_date,
            system_qty=inv.quantity,
        )
        db.session.add(item)

    db.session.commit()
    return check


def finish_check(check: StockCheck):
    """完成盘点：按差异调整。盘亏盒报废，盘盈补建盒并同步汇总"""
    if check.status != StockCheck.STATUS_DRAFT:
        raise ValueError("该盘点单已完成")

    diff_count = 0
    for item in check.items:
        if item.actual_qty is None:
            raise ValueError("请填写完整的实际盘点数量")
        item.diff_qty = item.actual_qty - item.system_qty
        if item.diff_qty != 0:
            diff_count += 1

        if item.diff_qty < 0:
            # 盘亏：把该批次在库盒标记报废
            loss = -item.diff_qty
            packs = ReagentPack.query.filter_by(
                reagent_id=item.reagent_id, batch_no=item.batch_no,
                status=ReagentPack.STATUS_IN_STOCK
            ).order_by(ReagentPack.expire_date.asc()).limit(loss).all()
            for p in packs:
                p.status = ReagentPack.STATUS_SCRAPPED
                p.close_time = datetime.now()
        elif item.diff_qty > 0:
            # 盘盈：补建在库盒
            reagent = item.reagent
            bcs = gen_barcodes(ReagentPack, item.diff_qty)
            for bc in bcs:
                p = ReagentPack(
                    barcode=bc, reagent_id=item.reagent_id,
                    batch_no=item.batch_no, expire_date=item.expire_date,
                    total_units=reagent.pack_size or 1,
                    status=ReagentPack.STATUS_IN_STOCK,
                    storage_location="盘点盘盈补建")
                db.session.add(p)

        inv = Inventory.query.filter_by(
            reagent_id=item.reagent_id, batch_no=item.batch_no).first()
        if item.actual_qty <= 0:
            if inv:
                db.session.delete(inv)
        elif inv:
            inv.quantity = item.actual_qty
        else:
            db.session.add(Inventory(
                reagent_id=item.reagent_id, batch_no=item.batch_no,
                expire_date=item.expire_date, quantity=item.actual_qty,
                storage_location="盘点补建"))

    check.status = StockCheck.STATUS_DONE
    check.difference_count = diff_count
    check.finish_time = datetime.now()
    db.session.commit()
    return check
