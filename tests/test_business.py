# -*- coding: utf-8 -*-
"""业务规则回归测试：覆盖盒/瓶两级库存的关键约束。

运行： python -m pytest -q
"""
import datetime

import pytest


# ------------------------- 辅助 -------------------------
def _new_in(reagent, user, qty=3, batch="B202610", expire=None, price=380.0):
    from app.services import stock_service

    return stock_service.create_stock_in({
        "reagent_id": reagent.id,
        "batch_no": batch,
        "expire_date": expire or datetime.date(2027, 10, 1),
        "quantity": qty,
        "unit_price": price,
    }, user)


def _in_stock(reagent, user, qty=3, batch="B202610", expire=None):
    from app.services import stock_service

    si = _new_in(reagent, user, qty=qty, batch=batch, expire=expire)
    return stock_service.confirm_stock_in(si, user)


def _out(reagent, user, qty=1, receiver=None):
    from app.services import stock_service

    out = stock_service.create_stock_out({
        "reagent_id": reagent.id, "quantity": qty, "out_type": "use",
    }, user)
    if receiver is not None:
        out.receiver_id = receiver.id
        out.receiver = receiver.real_name
        stock_service.db.session.commit()
    return out


# ------------------------- 条码 -------------------------
def test_barcode_is_10_digits_and_never_duplicated(db_session):
    from app.utils.common import gen_barcodes

    codes = gen_barcodes(None, 5)
    assert len(codes) == 5
    assert len(set(codes)) == 5
    assert all(len(c) == 10 and c.isdigit() for c in codes)
    # 当天第一盒必须是 0001（序列初值不能相差一位）
    assert codes[0].endswith("0001")
    assert codes[-1].endswith("0005")
    # 连续取号不得与上一批重叠（旧实现依赖行锁，SQLite 下会失效）
    more = gen_barcodes(None, 2)
    assert set(codes).isdisjoint(more)
    assert more[0].endswith("0006")


# ------------------------- 入库 -------------------------
def test_stock_in_creates_one_pack_per_box(db_session, users, reagent):
    from app.models.stock import Inventory, ReagentPack

    packs = _in_stock(reagent, users["mgr"], qty=3)
    assert len(packs) == 3
    assert len({p.barcode for p in packs}) == 3
    assert all(p.status == ReagentPack.STATUS_IN_STOCK for p in packs)
    assert all(p.total_units == reagent.pack_size for p in packs)

    inv = Inventory.query.filter_by(reagent_id=reagent.id, batch_no="B202610").one()
    assert inv.quantity == 3


# ------------------------- 出库 -------------------------
def test_stock_out_picks_earliest_expiry_first(db_session, users, reagent):
    from app.services import stock_service

    _in_stock(reagent, users["mgr"], qty=2, batch="B-EARLY",
              expire=datetime.date(2027, 1, 1))
    _in_stock(reagent, users["mgr"], qty=2, batch="B-LATE",
              expire=datetime.date(2027, 12, 1))

    out = _out(reagent, users["stf"], qty=2)
    packs = stock_service.confirm_stock_out(out, users["mgr"])
    assert {p.batch_no for p in packs} == {"B-EARLY"}


def test_out_records_receiver_not_confirmer(db_session, users, reagent):
    from app.services import stock_service

    _in_stock(reagent, users["mgr"], qty=1)
    out = _out(reagent, users["stf"], qty=1, receiver=users["stf"])
    packs = stock_service.confirm_stock_out(out, users["mgr"])  # 确认人=李试剂

    # 曾经的 bug：这里记成了确认人(mgr.id)，追溯时领用人全错
    assert all(p.receiver_id == users["stf"].id for p in packs)
    assert all(p.receiver == "王检验" for p in packs)


def test_expired_pack_only_goes_through_scrap(db_session, users, reagent):
    from app.models.stock import ReagentPack
    from app.services import stock_service

    _in_stock(reagent, users["mgr"], qty=1, batch="B-OLD",
              expire=datetime.date(2020, 1, 1))

    # 过期盒不计入"可用整盒"，普通领用在建单时就被拦下
    with pytest.raises(ValueError):
        _out(reagent, users["stf"], qty=1)

    # 报废流程允许选过期盒，出库后状态为"已报废"
    scrap = stock_service.create_stock_out({
        "reagent_id": reagent.id, "quantity": 1,
        "out_type": "scrap", "reason": "过期报废",
    }, users["stf"])
    packs = stock_service.confirm_stock_out(scrap, users["mgr"])
    assert packs[0].status == ReagentPack.STATUS_SCRAPPED


def test_expired_in_stock_pack_can_be_scrapped_via_scan(client, users, reagent, db_session):
    """回归：在库但已过期的盒，扫码时不能被“状态不能出库”挡住，报废应能走通。"""
    from app.models.stock import ReagentPack, StockOut

    packs = _in_stock(reagent, users["mgr"], qty=1, batch="B-EXPIRED",
                      expire=datetime.date(2020, 1, 1))
    pack = packs[0]

    d = client.get(f"/stock-out/scan?barcode={pack.barcode}").get_json()
    assert d["found"] is True
    assert d["can_out"] is True          # 在库，允许出库
    assert d["can_use"] is False         # 但已过期，不能领用/质控
    assert d["expired"] is True

    resp = client.post("/stock-out/add", data={
        "scan_barcode": pack.barcode, "quantity": 1,
        "out_type": "scrap", "reason": "过期报废",
        "csrf_token": "test-token",
    })
    assert resp.status_code == 302

    out = StockOut.query.filter_by(scan_barcode=pack.barcode).one()
    client.post(f"/stock-out/{out.id}/confirm", data={
        "receiver_id": users["stf"].id, "force": "yes",
        "csrf_token": "test-token",
    })
    db_session.session.expire_all()
    assert db_session.session.get(ReagentPack, pack.id).status == ReagentPack.STATUS_SCRAPPED


def test_batches_api_separates_expired_boxes(client, users, reagent):
    """回归：过期盒单独统计，报废时优先推荐效期最早的（哪怕已过期）。"""
    _in_stock(reagent, users["mgr"], qty=1, batch="B-VALID",
              expire=datetime.date(2027, 1, 1))
    _in_stock(reagent, users["mgr"], qty=2, batch="B-EXPIRED",
              expire=datetime.date(2020, 1, 1))

    use = client.get(f"/stock-out/batches?reagent_id={reagent.id}").get_json()
    assert use["valid_boxes"] == 1
    assert use["expired_boxes"] == 2
    assert use["recommend"]["batch_no"] == "B-VALID"

    scrap = client.get(
        f"/stock-out/batches?reagent_id={reagent.id}&out_type=scrap").get_json()
    assert scrap["recommend"]["batch_no"] == "B-EXPIRED"
    assert scrap["recommend"]["expired"] is True


# ------------------------- 库存明细（条形码级） -------------------------
def test_scan_api_suggests_earlier_expiry_box(client, users, reagent):
    """回归：扫到的盒不是效期最早时，要提示更早到期的在库盒（先进先出）。"""
    late = _in_stock(reagent, users["mgr"], qty=1, batch="B-LATE",
                     expire=datetime.date(2027, 12, 1))[0]
    early = _in_stock(reagent, users["mgr"], qty=1, batch="B-EARLY",
                      expire=datetime.date(2026, 12, 1))[0]

    d = client.get(f"/stock-out/scan?barcode={late.barcode}").get_json()
    assert d["earlier_count"] == 1
    assert d["earlier"][0]["batch_no"] == "B-EARLY"
    assert d["earlier"][0]["barcode"] == early.barcode

    # 扫的本来就是最早的盒 → 不需要提醒
    d2 = client.get(f"/stock-out/scan?barcode={early.barcode}").get_json()
    assert d2["earlier_count"] == 0
    assert d2["earlier"] == []

    # 更早的盒已经过期：不能建议“改用这盒”（过期不能领用），只单独计数
    _in_stock(reagent, users["mgr"], qty=1, batch="B-EXPIRED",
              expire=datetime.date(2020, 1, 1))
    d3 = client.get(f"/stock-out/scan?barcode={late.barcode}").get_json()
    assert d3["earlier_count"] == 1
    assert d3["earlier_expired_count"] == 1
    assert all(not p["expired"] for p in d3["earlier"])


def test_inventory_pack_detail_lists_barcodes(client, users, reagent):
    """回归：实时库存的「明细」要能列出每一盒的条形码，并标出过期盒。"""
    good = _in_stock(reagent, users["mgr"], qty=2, batch="B-DETAIL",
                     expire=datetime.date(2027, 1, 1))
    bad = _in_stock(reagent, users["mgr"], qty=1, batch="B-OLD",
                    expire=datetime.date(2020, 1, 1))

    d = client.get(f"/inventory/packs/{reagent.id}").get_json()
    assert d["found"] is True
    assert d["in_stock"] == 3
    assert {p["barcode"] for p in d["packs"]} == {p.barcode for p in good + bad}
    flags = {p["batch_no"]: p["expired"] for p in d["packs"]}
    assert flags == {"B-DETAIL": False, "B-OLD": True}

    # 库存页上确实有「明细」按钮（点击打开上面这份数据）
    html = client.get("/inventory/").get_data(as_text=True)
    assert "openPacks(this)" in html
    # 出库页支持带条码跳转并自动锁定
    out_html = client.get("/stock-out/add").get_data(as_text=True)
    assert "params.get('barcode')" in out_html


# ------------------------- 库存汇总校准 -------------------------
def test_inventory_aggregate_self_heals(db_session, users, reagent):
    """回归：汇总库存与实际在库盒不一致时要能自动校准（否则库存页会“显示还有”）。"""
    from app.models.stock import Inventory, ReagentPack
    from app.services import stock_service

    packs = _in_stock(reagent, users["mgr"], qty=2, batch="B-DRIFT")
    inv = Inventory.query.filter_by(reagent_id=reagent.id, batch_no="B-DRIFT").one()
    assert inv.quantity == 2

    inv.quantity = 9                     # 模拟历史漂移
    db_session.session.commit()
    assert stock_service.reconcile_inventory() == 1
    assert Inventory.query.filter_by(
        reagent_id=reagent.id, batch_no="B-DRIFT").one().quantity == 2

    # 两盒都报废后，汇总行应消失，不能继续显示“在库”
    for p in packs:
        db_session.session.get(ReagentPack, p.id).status = ReagentPack.STATUS_SCRAPPED
    db_session.session.commit()
    assert stock_service.reconcile_inventory() == 1
    assert Inventory.query.filter_by(
        reagent_id=reagent.id, batch_no="B-DRIFT").first() is None


# ------------------------- 使用登记 -------------------------
def test_use_requires_issue_and_respects_remaining(db_session, users, reagent):
    from app.models.stock import ReagentPack
    from app.services import stock_service

    packs = _in_stock(reagent, users["mgr"], qty=1)
    with pytest.raises(ValueError):
        stock_service.register_use(packs[0], 1, "use", users["stf"])

    issued = stock_service.confirm_stock_out(_out(reagent, users["stf"], 1), users["mgr"])
    pack = issued[0]

    stock_service.register_use(pack, 5, "use", users["stf"])
    assert pack.remaining_units == 7
    assert pack.status == ReagentPack.STATUS_USING

    with pytest.raises(ValueError):          # 超量保护
        stock_service.register_use(pack, 99, "use", users["stf"])

    stock_service.register_use(pack, 7, "use", users["stf"])
    assert pack.status == ReagentPack.STATUS_EMPTY


# ------------------------- 盘点 -------------------------
def test_stock_check_reconciles_inventory(db_session, users, reagent):
    from app.models.stock import Inventory, ReagentPack
    from app.services import stock_service

    _in_stock(reagent, users["mgr"], qty=3)
    check = stock_service.start_check({"title": "月末盘点"}, users["mgr"])
    assert len(check.items) == 1

    check.items[0].actual_qty = 2            # 盘亏 1 盒
    db_session.session.commit()
    stock_service.finish_check(check)

    inv = Inventory.query.filter_by(reagent_id=reagent.id, batch_no="B202610").one()
    assert inv.quantity == 2
    assert ReagentPack.query.filter_by(status=ReagentPack.STATUS_SCRAPPED).count() == 1


# ------------------------- 页面 / 权限 -------------------------
def test_main_pages_render(client):
    for url in ["/", "/stock-in/", "/stock-in/add", "/stock-out/", "/stock-out/add",
                "/usage/", "/usage/add", "/usage/records", "/inventory/",
                "/inventory/warning", "/inventory/check", "/inventory/temp",
                "/inventory/temp/add", "/basic/reagent", "/basic/supplier",
                "/basic/category", "/basic/location", "/report/", "/admin/user",
                "/admin/log", "/databackup/"]:
        resp = client.get(url)
        assert resp.status_code == 200, url


def test_temp_records_filtered_by_month(client, db_session):
    from app.models.stock import TempRecord

    db_session.session.add_all([
        TempRecord(location="2-8℃冰箱", record_date=datetime.date(2026, 9, 15),
                   am_temp=5.0, pm_temp=5.5, humidity=50),
        TempRecord(location="2-8℃冰箱", record_date=datetime.date(2026, 10, 2),
                   am_temp=4.8, pm_temp=5.2, humidity=48),
    ])
    db_session.session.commit()

    body = client.get("/inventory/temp?month=2026-10").get_data(as_text=True)
    assert "2026-10-02" in body
    assert "2026-09-15" not in body
    assert client.get("/inventory/temp/export?month=2026-10").status_code == 200


def test_verify_batch_covers_all_receipts_of_same_batch(client, db_session, users, reagent):
    from app.models.stock import StockIn

    _in_stock(reagent, users["mgr"], qty=1, batch="B-V")
    _in_stock(reagent, users["mgr"], qty=1, batch="B-V")   # 同批号第二次入库

    resp = client.post("/inventory/verify-batch",
                       data={"reagent_id": reagent.id, "batch_no": "B-V",
                             "csrf_token": "test-token"}, follow_redirects=True)
    assert resp.status_code == 200
    rows = StockIn.query.filter_by(reagent_id=reagent.id, batch_no="B-V").all()
    assert len(rows) == 2 and all(r.is_verified for r in rows)
    assert "已验证" in client.get("/inventory/").get_data(as_text=True)


def test_reagent_with_history_cannot_be_deleted(client, db_session, users, reagent):
    from app.models.reagent import Reagent

    _in_stock(reagent, users["mgr"], qty=1)
    resp = client.post("/basic/reagent/delete",
                       data={"id": reagent.id, "csrf_token": "test-token"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert db_session.session.get(Reagent, reagent.id) is not None


def test_user_with_history_cannot_be_deleted(client, db_session, users, reagent):
    from app.models.user import User

    _new_in(reagent, users["mgr"], qty=1)          # 李试剂 是登记人
    resp = client.post("/admin/user/delete",
                       data={"id": users["mgr"].id, "csrf_token": "test-token"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert db_session.session.get(User, users["mgr"].id) is not None
