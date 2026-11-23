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


# ------------------------- 最低库存预警口径（按盒 / 按瓶） -------------------------
def test_low_stock_warning_by_unit_counts_total_units(db_session, users, reagent):
    """min_stock_unit=unit：按在库最小单位数比较，而不是盒数。"""
    from app.services import warning_service

    reagent.pack_size = 10
    reagent.min_stock = 50
    reagent.min_stock_unit = reagent.MIN_UNIT_UNIT
    db_session.session.commit()

    _in_stock(reagent, users["mgr"], qty=2)        # 2 盒 = 20 瓶

    rows = warning_service.get_low_stock_warnings(99)
    row = next(x for x in rows if x["reagent"].id == reagent.id)
    assert row["current"] == 20
    assert row["min"] == 50
    assert row["gap"] == 30
    assert row["unit_text"] == "瓶"
    assert row["by_unit"] is True
    # 缺口 30 瓶按每盒 10 瓶折算 = 3 盒
    assert "约 3 盒" in row["suggestion"]


def test_low_stock_warning_by_pack_ignores_unit_count(db_session, users, reagent):
    """min_stock_unit=pack：只看在库盒数，瓶数再多也不影响判定。"""
    from app.services import warning_service

    reagent.pack_size = 12
    reagent.min_stock = 3
    reagent.min_stock_unit = reagent.MIN_UNIT_PACK
    db_session.session.commit()

    _in_stock(reagent, users["mgr"], qty=2)        # 2 盒 = 24 瓶

    rows = warning_service.get_low_stock_warnings(99)
    row = next(x for x in rows if x["reagent"].id == reagent.id)
    assert row["current"] == 2
    assert row["gap"] == 1
    assert row["unit_text"] == "盒"
    assert row["by_unit"] is False
    assert "30 瓶" not in row["suggestion"]


def test_low_stock_warning_by_unit_includes_issued_unused(db_session, users, reagent):
    """按瓶预警：科室已出库未用完的瓶也计入当前可用量。"""
    from app.models.reagent import Reagent
    from app.services import stock_service, warning_service

    reagent.pack_size = 10
    reagent.min_stock = 15
    reagent.min_stock_unit = reagent.MIN_UNIT_UNIT
    db_session.session.commit()

    _in_stock(reagent, users["mgr"], qty=2)      # 中心 2 盒 = 20 瓶
    out = _out(reagent, users["mgr"], qty=1)     # 领出 1 盒到科室
    stock_service.confirm_stock_out(out, users["mgr"])

    db_session.session.expire_all()
    r = db_session.session.get(Reagent, reagent.id)
    assert r.in_stock_units == 10
    assert r.in_use_units == 10
    assert r.available_units == 20

    # 若只算中心在库会误判 10 < 15 缺血；实际全科 20 瓶充足 → 不应预警
    assert all(x["reagent"].id != r.id
               for x in warning_service.get_low_stock_warnings(99))


def test_low_stock_zero_means_no_warning(db_session, users, reagent):
    """最低库存填 0 表示不参与预警（两种口径都成立）。"""
    from app.services import warning_service

    reagent.min_stock = 0
    reagent.min_stock_unit = reagent.MIN_UNIT_UNIT
    db_session.session.commit()
    _in_stock(reagent, users["mgr"], qty=1)

    rows = warning_service.get_low_stock_warnings(99)
    assert all(x["reagent"].id != reagent.id for x in rows)


def test_reagent_save_persists_min_stock_unit(client, db_session, users, reagent):
    """界面保存时按所选口径落库，非法值回落为按大包装。"""
    from app.models.reagent import Reagent

    base = {
        "id": reagent.id, "name": reagent.name,
        "unit": "盒", "small_unit": "瓶", "pack_size": 12,
        "min_stock": 40, "expire_warning_days": 90,
        "storage_condition": "冷藏", "status": "1",
        "csrf_token": "test-token",
    }
    client.post("/basic/reagent/save",
                data=dict(base, min_stock_unit="unit"), follow_redirects=True)
    saved = db_session.session.get(Reagent, reagent.id)
    assert saved.min_stock == 40
    assert saved.min_stock_unit == Reagent.MIN_UNIT_UNIT
    assert saved.min_stock_unit_text == "瓶"

    client.post("/basic/reagent/save",
                data=dict(base, min_stock_unit="bogus"), follow_redirects=True)
    db_session.session.expire_all()
    assert db_session.session.get(Reagent, reagent.id).min_stock_unit == Reagent.MIN_UNIT_PACK


def test_pagination_macro_renders_ellipsis_instead_of_none(app, db_session):
    """回归：页数较多时 iter_pages() 会返回 None 作省略占位，不能被直接渲染成 None。"""
    from flask import render_template_string

    class FakePagination:
        page = 5
        pages = 12
        has_prev = True
        has_next = True
        prev_num = 4
        next_num = 6

        def iter_pages(self, **kwargs):
            return [1, 2, None, 4, 5, 6, None, 11, 12]

    with app.test_request_context("/usage/records?page=5&keyword=abc"):
        html = render_template_string(
            "{% from '_macros.html' import pagination %}"
            "{{ pagination(pg, 'usage.records') }}",
            pg=FakePagination())

    assert "None" not in html               # 不再出现 None 字样
    assert "&hellip;" in html               # 省略号占位
    assert "page=4" in html and "page=6" in html   # 上/下一页仍带页码
    assert "keyword=abc" in html            # 查询条件被保留


def test_pagination_tolerates_bad_page_param(client, db_session, users, reagent):
    """回归：?page=None / 非数字 / 负数 不应 500，也不应把 None 显示到页码上。"""
    _in_stock(reagent, users["mgr"], qty=1)

    for bad in ["None", "abc", "-1", ""]:
        resp = client.get(f"/usage/records?page={bad}")
        assert resp.status_code == 200, bad
        assert b"None" not in resp.data, bad


def test_pagination_has_jump_box_and_clamps_overflow(client, db_session, users, reagent):
    """分页提供“跳至 N 页”输入框；页码超界自动落回末页，不出现空列表。"""
    from app.models.stock import PackUse

    pack = _in_stock(reagent, users["mgr"], qty=1)[0]
    for i in range(16):
        db_session.session.add(PackUse(
            use_no=f"SY-T-{i:03d}", pack_id=pack.id, reagent_id=reagent.id,
            units_used=1, use_type=PackUse.TYPE_USE, user_id=users["stf"].id))
    db_session.session.commit()

    html = client.get("/usage/records").get_data(as_text=True)
    assert 'name="page"' in html            # 跳转输入框
    assert "跳至" in html
    assert "共 2 页" in html                # 16 条 / 每页 15 → 2 页

    over = client.get("/usage/records?page=99")
    assert over.status_code == 200
    body = over.get_data(as_text=True)
    assert "暂无使用记录" not in body       # 超界被夹到末页，而不是空页
    assert "共 2 页" in body

    kw = client.get("/usage/records?keyword=葡萄糖&page=99").get_data(as_text=True)
    assert 'name="keyword" value="葡萄糖"' in kw   # 跳转/翻页保留查询条件


def test_main_js_injects_csrf_only_into_post_forms():
    """main.js 只给 POST 表单注入 CSRF token，GET 搜索/翻页表单保持 URL 干净。"""
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent
          / "app" / "static" / "js" / "main.js").read_text(encoding="utf-8")
    assert "meta[name='csrf-token']" in js
    assert "method !== \"post\"" in js or "!== 'post'" in js
    assert "input.name = \"csrf_token\"" in js or "csrf_token" in js


def test_reagent_page_filters_manufacturers_by_category(client, db_session, users):
    """分类 → 厂家联动：选定分类后，厂家下拉只显示该分类下有试剂的厂家。"""
    from app.models.reagent import Category, Reagent

    c1 = Category(name="血液试剂", sort=1)
    c2 = Category(name="生化试剂", sort=2)
    db_session.session.add_all([c1, c2])
    db_session.session.commit()
    db_session.session.add_all([
        Reagent(name="A 试剂", category_id=c1.id, registrant="厂家甲"),
        Reagent(name="B 试剂", category_id=c2.id, registrant="厂家乙"),
    ])
    db_session.session.commit()

    html = client.get(f"/basic/reagent?category_id={c1.id}").get_data(as_text=True)
    assert "厂家甲" in html and "厂家乙" in html   # 全集仍在下拉里，供切换分类时使用

    tag_yi = html[html.index('value="厂家乙"'):]
    tag_yi = tag_yi[:tag_yi.index(">") + 1]
    assert "hidden disabled" in tag_yi            # 不属该分类 → 初始隐藏

    tag_jia = html[html.index('value="厂家甲"'):]
    tag_jia = tag_jia[:tag_jia.index(">") + 1]
    assert "hidden disabled" not in tag_jia

    ok = client.get("/basic/reagent?category_id=abc")   # 非法分类参数不报错
    assert ok.status_code == 200


# ============================================================
# UDI 扫码识别（DM 码解析 / 产品库 / 试剂匹配 / 入库绑定）
# ============================================================
DI = "06972487720754"


def test_parse_dm_standard_without_separator():
    """标准 GS1 码：01 定长 14 位，17 定长 6 位，10 变长读到结尾。"""
    from app.services.udi_service import parse_dm

    d = parse_dm(f"01{DI}17" "281231" "10B2601A")
    assert d["di"] == DI
    assert d["batch_no"] == "B2601A"
    assert d["expire_date"].isoformat() == "2028-12-31"


def test_parse_dm_with_gs_separator_and_serial():
    from app.services.udi_service import parse_dm

    d = parse_dm(f"01{DI}\x1d17281231\x1d10B2601A\x1d21SN0001")
    assert d["di"] == DI
    assert d["batch_no"] == "B2601A"
    assert d["serial"] == "SN0001"


def test_parse_dm_batch_before_expire_without_gs():
    """批号在前且缺 GS 分隔时，应在下一个定长 AI（17）处截断。"""
    from app.services.udi_service import parse_dm

    d = parse_dm(f"01{DI}10LOT2026A17281231")
    assert d["batch_no"] == "LOT2026A"
    assert d["expire_date"].isoformat() == "2028-12-31"


def test_parse_dm_ai240_not_swallowed_by_batch():
    """真实管路清洗液码：批号后面直接跟 (240) 附加产品标识，批号不能被它污染。"""
    from app.services.udi_service import parse_dm

    d = parse_dm("0106947145500207112605151727051510305260501240130299007M")
    assert d["di"] == "06947145500207"
    assert d["batch_no"] == "305260501"
    assert d.get("extra") == "130299007M"
    assert "serial" not in d
    assert d["produce_date"].isoformat() == "2026-05-15"
    assert d["expire_date"].isoformat() == "2027-05-15"


def test_parse_dm_batch_with_ai_like_digits_backtracking():
    """真实罗氏电解液码：批号 93575501 里的 "01"+14 位数字不能被当成 GTIN。"""
    from app.services.udi_service import parse_dm

    d = parse_dm("01040156309102811093575501172804302401082065221611251120")
    assert d["di"] == "04015630910281"
    assert d["batch_no"] == "93575501"
    assert d["produce_date"].isoformat() == "2025-11-20"
    assert d["expire_date"].isoformat() == "2028-04-30"
    assert d.get("extra") == "10820652216"
    assert "serial" not in d


def test_gtin_check_digit_filters_fake_gtin():
    """真 DI 必过 Mod-10；批号里恰好 14 位的数字多半过不了，不能再被当成 01。"""
    from app.services.udi_service import _gtin_check_ok

    for di in ("06933289805346", "06954414706512", "06947145500207",
               "04015630910281", "06972487720754"):
        assert _gtin_check_ok(di), di
    assert not _gtin_check_ok("17280430240108")      # 罗氏码批号里的 14 位数字
    assert not _gtin_check_ok("12345678901234")


def test_parse_dm_short_serial_number():
    """真实支原体试剂码 (10)20260901(21)477：3 位短序列号也要切开，且不能把普通批号切坏。"""
    from app.services.udi_service import parse_dm

    d = parse_dm("01069722358405741126090117270831102026090121477")
    assert d["di"] == "06972235840574"
    assert d["batch_no"] == "20260901"
    assert d["serial"] == "477"
    assert d["produce_date"].isoformat() == "2026-09-01"
    assert d["expire_date"].isoformat() == "2027-08-31"

    # 批号不是 8 位日期时，尾部 21477 不能被当序列号
    d2 = parse_dm("0106972235840574112609011727083110ABCD21477")
    assert d2["batch_no"] == "ABCD21477"
    assert "serial" not in d2


def test_parse_dm_only_di():
    from app.services.udi_service import parse_dm

    assert parse_dm(DI)["di"] == DI


def test_gs1_date_century_rule():
    from app.services.udi_service import _gs1_date

    assert _gs1_date("490101").year == 2049
    assert _gs1_date("500101").year == 1950
    assert _gs1_date("271332") is None          # 非法月份


def test_udi_scan_api_matches_reagent(client, db_session):
    """DI 命中产品库 + 名称一致 → 自动给出 reagent_id 与码内批号效期。"""
    from app.models.reagent import Reagent
    from app.models.udi import UdiProduct

    db_session.session.add(UdiProduct(
        di=DI, coding="GS1", name="CD38检测试剂", spec="50人份/盒",
        mfr="北京同生时代生物技术有限公司", reg_no="京大械备20180038号",
        cat="耗材", source="test"))
    r = Reagent(name="CD38检测试剂", spec="50人份/盒", pack_size=1,
                unit="盒", small_unit="人份")
    db_session.session.add(r)
    db_session.session.commit()

    res = client.get(f"/stock-in/udi?code=01{DI}1728123110B2601A")
    data = res.get_json()
    assert data["found"] is True
    assert data["product"]["name"] == "CD38检测试剂"
    assert data["reagent_id"] == r.id
    assert data["parsed"]["batch_no"] == "B2601A"
    assert data["parsed"]["expire_date"] == "2028-12-31"


def test_udi_scan_api_unknown_di_keeps_code_fields(client, db_session):
    """DI 未收录时仍要回填码内批号与有效期，让用户只需手工选试剂。"""
    res = client.get("/stock-in/udi?code=01123456789012341726063010Z9")
    data = res.get_json()
    assert data["found"] is False
    assert data["product"] is None
    assert data["reagent_id"] is None
    assert data["parsed"]["batch_no"] == "Z9"
    assert data["parsed"]["expire_date"] == "2026-06-30"


def test_udi_scan_api_rejects_empty_code(client):
    assert client.get("/stock-in/udi?code=").status_code == 400


def test_udi_scan_brings_last_unit_price(client, db_session, users, reagent):
    """再次扫同一试剂时带出最近一次入库单价；作废单不参与（避免 0 元、避免沿用别的试剂）。"""
    from app.models.udi import UdiProduct

    db_session.session.add(UdiProduct(
        di=DI, coding="GS1", name="葡萄糖(GLU)测定试剂盒", spec="R1 40ml×4",
        mfr="测试厂家", reg_no="", cat="生化", source="test"))
    db_session.session.commit()
    _new_in(reagent, users["admin"], batch="B-OLD", price=128.5)
    voided = _new_in(reagent, users["admin"], batch="B-VOID", price=999.0)
    voided.status = 2                       # 已作废的单不能当作「上次单价」
    db_session.session.commit()

    data = client.get(f"/stock-in/udi?code=01{DI}1728123110B2601A").get_json()
    assert data["reagent_id"] == reagent.id
    assert data["last_price"] == pytest.approx(128.5)


def test_reagent_modal_categories_keep_all_under_mfr_filter(client, db_session):
    """厂家筛选不再裁分类选项；分类下拉全量，收敛交给前端双向联动。"""
    import re

    from app.models.reagent import Category, Reagent

    c1 = Category(name="生化试剂类")
    c2 = Category(name="免疫试剂类")
    db_session.session.add_all([c1, c2])
    db_session.session.commit()
    db_session.session.add_all([
        Reagent(name="甲厂家试剂", registrant="甲厂家", category_id=c1.id, pack_size=1),
        Reagent(name="乙厂家试剂", registrant="乙厂家", category_id=c2.id, pack_size=1),
    ])
    db_session.session.commit()

    html = client.get("/basic/reagent?mfr=甲厂家").get_data(as_text=True)
    filt = re.search(r'id="filter_category".*?</select>', html, re.S).group(0)
    modal = re.search(r'id="f_category".*?</select>', html, re.S).group(0)
    # 筛选区和编辑弹窗都保留全部分类，具体可见范围由前端联动决定
    for block in (filt, modal):
        assert "生化试剂类" in block and "免疫试剂类" in block
    # 双向联动：选分类收敛厂家（applyMfrFilter）、选厂家收敛分类（applyCatFilter）
    assert "applyMfrFilter" in html and "applyCatFilter" in html
    assert "mfrSel.addEventListener('change', applyCatFilter)" in html


def test_in_add_page_has_udi_scanner(client):
    html = client.get("/stock-in/add").get_data(as_text=True)
    assert 'id="udiInput"' in html
    assert 'name="udi_di"' in html
    assert "zxing.min.js" in html
    assert 'id="udiCamBtn"' in html
    # 拍照识别：手机/局域网（http 非安全上下文）下不必调摄像头也能扫
    assert 'id="udiPhotoBtn"' in html
    assert 'id="udiPhoto"' in html
    assert 'accept="image/*"' in html


def test_in_add_page_guides_to_create_reagent(client):
    """档案里没有该试剂时，结果卡片给出「新建试剂档案」入口，并把码带过去自动重扫。"""
    html = client.get("/stock-in/add").get_data(as_text=True)
    assert "新建试剂档案" in html
    # 多参数 URL 不能直接拼进 href（"&reg_no=" 会被当成实体 ®），改为 data-* + URL 对象
    assert "udi-new-reagent" in html
    assert "searchParams.set(pair[1], v)" in html
    assert 'const UDI_AUTO = "";' in html          # 未带 udi 参数时不自动重扫

    html2 = client.get(f"/stock-in/add?udi={DI}").get_data(as_text=True)
    assert DI in html2.split("const UDI_AUTO")[1][:40]


def test_reagent_list_prefills_from_udi(client):
    """从入库页扫码跳来建档：新增弹窗自动打开并预填名称/规格/注册证/注册人/UDI。"""
    html = client.get(
        "/basic/reagent?new=1&name=HIV抗原抗体试剂盒&spec=100测试/盒"
        f"&reg_no=国械注准20183400568&registrant=深圳新产业&udi_di={DI}&udi=010{DI}B2601A"
    ).get_data(as_text=True)
    assert "HIV抗原抗体试剂盒" in html
    assert "国械注准20183400568" in html
    assert "const PREFILL" in html
    assert 'id="f_udi"' in html


def test_reagent_list_unescapes_mangled_spec(client):
    """旧链接把 &reg_no= 当实体 ® 解析，注册证会并进规格里；服务端要还原成两个字段。"""
    html = client.get(
        "/basic/reagent?new=1&spec=100测试/盒®_no=国械注准20183400568".replace("®", "%C2%AE")
        + "&registrant=深圳市新产业"
    ).get_data(as_text=True)
    seg = html.split("const PREFILL")[-1][:300]
    assert '"spec": "100测试/盒"' in seg
    assert '"reg_no": "国械注准20183400568"' in seg
    assert "®" not in seg


def test_reagent_list_cleans_entity_leftover_in_spec(client):
    """链接二次转义后规格里会残留 "&#32;\\" 之类垃圾，预填前必须清掉。"""
    html = client.get(
        "/basic/reagent?new=1&name=HIV试剂盒"
        "&spec=100测试/盒%C2%AE_no=%E5%9B%B3%E5%AE%B6&#32;"
    ).get_data(as_text=True)
    seg = html.split("const PREFILL")[-1][:300]
    assert '"spec": "100测试/盒"' in seg
    assert "&#32;" not in seg
    assert "®" not in seg


def test_reagent_save_with_udi_binds_and_returns_to_stock_in(client, db_session):
    """扫码建档保存后：DI 绑定到新档案，并回跳到入库页自动重扫。"""
    from app.models.reagent import Reagent
    from app.models.udi import UdiProduct

    resp = client.post("/basic/reagent/save", data={
        "name": "HIV抗原抗体检测试剂盒", "spec": "100测试/盒",
        "reg_no": "国械注准20183400568", "registrant": "深圳新产业",
        "unit": "盒", "pack_size": 1, "small_unit": "瓶",
        "min_stock": 1, "min_stock_unit": "pack", "status": "on",
        "udi_di": DI, "udi": f"010{DI}B2601A", "csrf_token": "test-token",
    })
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "/stock-in/add" in location
    assert DI in location

    new = Reagent.query.filter_by(name="HIV抗原抗体检测试剂盒").first()
    assert new is not None
    assert db_session.session.get(UdiProduct, DI).reagent_id == new.id


def test_udi_import_filters_non_ivd(db_session, tmp_path):
    """导入器只保留检验试剂类产品，耗材/设备不进库。"""
    import zipfile

    from app.models.udi import UdiProduct
    from udi_import import import_package

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<udid version="1.0"><devices>
  <device><zxxsdycpbs>{DI}</zxxsdycpbs><cpbsbmtxmc>GS1</cpbsbmtxmc>
    <cpmctymc>CD38检测试剂</cpmctymc><ggxh>50人份/盒</ggxh>
    <ylqxzcrbarmc>北京同生时代生物技术有限公司</ylqxzcrbarmc>
    <zczbhhzbapzbh>京大械备20180038号</zczbhhzbapzbh><cplb>耗材</cplb></device>
  <device><zxxsdycpbs>06970000000001</zxxsdycpbs><cpbsbmtxmc>GS1</cpbsbmtxmc>
    <cpmctymc>一次性使用无菌注射器</cpmctymc><ggxh>5ml</ggxh>
    <ylqxzcrbarmc>某器械公司</ylqxzcrbarmc><cplb>耗材</cplb></device>
</devices></udid>"""
    zpath = tmp_path / "udi.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("UDID_TEST.xml", xml)

    stats = import_package(zpath)
    assert stats["scanned"] == 2
    assert stats["matched"] == 1                 # 只命中试剂那条
    assert UdiProduct.query.count() == 1
    assert UdiProduct.query.first().di == DI


def test_udi_import_keeps_liquid_reagents_and_drops_care_products(db_session, tmp_path):
    """名称不带"试剂"的液类产品（稀释液/清洗液/保存液）要收；隐形眼镜护理液、鼻腔清洗液要挡掉。"""
    import zipfile

    from app.models.udi import UdiProduct
    from udi_import import import_package

    def dev(di, name):
        return (f"<device><zxxsdycpbs>{di}</zxxsdycpbs><cpbsbmtxmc>GS1</cpbsbmtxmc>"
                f"<cpmctymc>{name}</cpmctymc><ggxh>1L</ggxh>"
                f"<ylqxzcrbarmc>某公司</ylqxzcrbarmc><cplb>耗材</cplb></device>")

    xml = ('<?xml version="1.0" encoding="UTF-8"?><udid version="1.0"><devices>'
           + dev("06970000000011", "尿液分析用稀释液 UF-CELLPACK")
           + dev("06970000000012", "样本保存液")
           + dev("06970000000013", "隐形眼镜护理液")
           + dev("06970000000014", "医用鼻腔清洗液")
           + "</devices></udid>")
    zpath = tmp_path / "udi2.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("UDID_LIQ.xml", xml)

    stats = import_package(zpath)
    assert stats["scanned"] == 4
    assert stats["matched"] == 2                  # 稀释液 + 保存液
    assert UdiProduct.query.count() == 2
    assert not UdiProduct.query.filter(UdiProduct.di.in_(
        ["06970000000013", "06970000000014"])).first()


def test_udi_import_force_di_bypasses_filter(db_session, tmp_path):
    """关键词没覆盖到的产品，可以按 DI 强制补录（--di）。"""
    import zipfile

    from app.models.udi import UdiProduct
    from udi_import import import_package

    di = "06970000000021"
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><udid version="1.0"><devices>'
           f'<device><zxxsdycpbs>{di}</zxxsdycpbs><cpmctymc>一次性使用无菌注射器</cpmctymc>'
           f'<zzbhhzbapzbh>国械注准20193141908</zzbhhzbapzbh><cplb>耗材</cplb></device>'
           f'</devices></udid>')
    zpath = tmp_path / "udi3.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("UDID_FORCE.xml", xml)

    stats = import_package(zpath, force_di={di})
    assert stats["forced"] == 1
    assert stats["written"] == 1
    obj = db_session.session.get(UdiProduct, di)
    assert obj is not None and obj.name == "一次性使用无菌注射器"


def test_stock_in_binds_udi_to_reagent(client, db_session, users, reagent):
    """提交入库时保存 udi_di，并把 DI 与所选试剂绑定；下次扫同款自动选中。"""
    from app.models.stock import StockIn
    from app.models.udi import UdiProduct

    db_session.session.add(UdiProduct(di=DI, coding="GS1", name="CD38检测试剂"))
    db_session.session.commit()

    resp = client.post("/stock-in/add", data={
        "reagent_id": reagent.id, "batch_no": "B2601A",
        "expire_date": "2028-12-31", "quantity": 1, "unit_price": 100,
        "verifier_id": users["mgr"].id, "accept_status": "合格",
        "udi_di": DI, "csrf_token": "test-token",
    })
    assert resp.status_code in (302, 200)

    si = StockIn.query.order_by(StockIn.id.desc()).first()
    assert si is not None and si.udi_di == DI
    assert db_session.session.get(UdiProduct, DI).reagent_id == reagent.id


def test_udi_import_cleans_duplicated_reg_no(tmp_path, db_session):
    """药监局发布文件里注册证号常写成 "X号,X"，导入时按去掉"号"的归一值去重。"""
    import zipfile

    from app.models.udi import UdiProduct
    from udi_import import _clean_reg, import_package

    assert _clean_reg("京大械备20180038号,京大械备20180038") == "京大械备20180038号"
    assert _clean_reg("豫郑械备20200152号;豫郑械备20200152号") == "豫郑械备20200152号"
    assert _clean_reg("") == ""

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<udid version="1.0"><devices>
  <device><zxxsdycpbs>{DI}</zxxsdycpbs><cpbsbmtxmc>GS1</cpbsbmtxmc>
    <cpmctymc>CD38检测试剂</cpmctymc><ggxh>50人份/盒</ggxh>
    <ylqxzcrbarmc>北京同生时代生物技术有限公司</ylqxzcrbarmc>
    <zczbhhzbapzbh>京大械备20180038号,京大械备20180038</zczbhhzbapzbh>
    <cplb>耗材</cplb></device>
</devices></udid>"""
    zpath = tmp_path / "udi_reg.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("UDID_TEST.xml", xml)

    import_package(zpath)
    assert UdiProduct.query.first().reg_no == "京大械备20180038号"


def test_udi_import_reads_every_xml_in_package(tmp_path, db_session):
    """药监局月度/全量包内是多个 XML，导入时要全部扫到，不能只看第一个。"""
    import zipfile

    from app.models.udi import UdiProduct
    from udi_import import import_package

    def device(di, name):
        return f"""  <device><zxxsdycpbs>{di}</zxxsdycpbs><cpbsbmtxmc>GS1</cpbsbmtxmc>
    <cpmctymc>{name}</cpmctymc><ggxh>100人份/盒</ggxh>
    <ylqxzcrbarmc>某公司</ylqxzcrbarmc><cplb>耗材</cplb></device>"""

    part1 = ('<?xml version="1.0" encoding="UTF-8"?>\n<udid version="1.0"><devices>\n'
             + device("06970000000002", "丙氨酸氨基转移酶测定试剂盒") + "\n</devices></udid>")
    part2 = ('<?xml version="1.0" encoding="UTF-8"?>\n<udid version="1.0"><devices>\n'
             + device("06970000000003", "葡萄糖测定试剂盒") + "\n</devices></udid>")

    zpath = tmp_path / "multi.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("PART1_Of_2.xml", part1)
        zf.writestr("PART2_Of_2.xml", part2)

    stats = import_package(zpath)
    assert stats["files"] == 2
    assert stats["matched"] == 2
    assert UdiProduct.query.count() == 2


def test_udi_import_accepts_directory(tmp_path, db_session):
    """全量包解压后的目录可直接作为导入参数。"""
    from app.models.udi import UdiProduct
    from udi_import import import_package

    for idx, name in enumerate(["尿酸测定试剂盒", "总胆红素测定试剂盒"], start=1):
        (tmp_path / f"PART{idx}.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<udid version="1.0"><devices>'
            f"<device><zxxsdycpbs>0697000000000{idx + 3}</zxxsdycpbs>"
            f"<cpmctymc>{name}</cpmctymc></device>"
            "</devices></udid>", encoding="utf-8")

    stats = import_package(tmp_path)
    assert stats["files"] == 2
    assert stats["matched"] == 2
    assert UdiProduct.query.count() == 2


def test_udi_match_reg_same_name_diff_gives_candidate_only(db_session, reagent):
    """一个注册证常覆盖同系列多个产品：注册证一致但名称完全不同时只给候选，不自动选中。"""
    from app.models.udi import UdiProduct
    from app.services import udi_service

    reagent.reg_no = "京大械备20180038号"
    db_session.session.commit()

    prod = UdiProduct(di="06970000000021", name="CD38检测试剂",
                      mfr="北京同生时代生物技术有限公司",
                      reg_no="京大械备20180038号")
    rid, cands = udi_service.match_reagents(prod)
    assert rid is None
    assert cands and cands[0]["id"] == reagent.id
    assert "请确认" in cands[0]["reason"]


def test_udi_match_tied_scores_need_manual_pick(db_session, reagent):
    """同名的两条试剂（高值/低值只差括号）分数并列时，不自动选中，交给人工点选。"""
    from app.models.reagent import Reagent
    from app.models.udi import UdiProduct
    from app.services import udi_service

    twin = Reagent(name="葡萄糖(GLU)测定试剂盒（低值）", spec="R1 20ml×2",
                   pack_size=6, small_unit="瓶", unit="盒", min_stock=2,
                   expire_warning_days=90, storage_condition="冷藏")
    db_session.session.add(twin)
    db_session.session.commit()

    prod = UdiProduct(di="06970000000022", name="葡萄糖(GLU)测定试剂盒")
    rid, cands = udi_service.match_reagents(prod)
    assert rid is None
    assert len(cands) >= 2


def test_parse_dm_real_code_batch_starting_with_11():
    """真实盒码：批号以 11 开头（114260321），码尾还带 "240130219004M" 噪音，DI 不能被带歪。"""
    from app.services.udi_service import parse_dm

    p = parse_dm("0106947145509262112607091727070910114260321 240130219004M")
    assert p["di"] == "06947145509262"
    assert p["batch_no"] == "114260321"
    assert p["expire_date"].isoformat() == "2027-07-09"


def test_parse_dm_with_gs1_symbol_prefix():
    """]C1（FNC1 符号前缀）+ 无 GS 分隔的真实码。"""
    from app.services.udi_service import parse_dm

    p = parse_dm("]C10106947145509262112607091727070910114260321")
    assert p["di"] == "06947145509262"
    assert p["batch_no"] == "114260321"
    assert p["expire_date"].isoformat() == "2027-07-09"


def test_parse_dm_splits_serial_from_batch():
    """真实盒码：(21) 序列号紧跟在批号后面（无 GS），要分出来，批号不能被吞。

    标签：批号 W041606007 / 生产 2026-06-24 / 失效 2028-06-24 / 序列号 31012305014743
    """
    from app.services.udi_service import parse_dm

    forms = [
        "(01)06933289830348(11)260624(17)280624(10)W041606007(21)31012305014743",
        "0106933289830348112606241728062410W0416060072131012305014743",
        "0106933289830348\x1d11260624\x1d17280624\x1d10W041606007\x1d2131012305014743",
    ]
    for code in forms:
        p = parse_dm(code)
        assert p["di"] == "06933289830348", code
        assert p["batch_no"] == "W041606007", code
        assert p["serial"] == "31012305014743", code
        assert p["produce_date"].isoformat() == "2026-06-24", code
        assert p["expire_date"].isoformat() == "2028-06-24", code


def test_parse_dm_batch_number_without_serial_stays_intact():
    """批号里本来就含 "21"、后面没有序列号时，不能把批号切坏。"""
    from app.services.udi_service import parse_dm

    p = parse_dm("0106933289830348112606241728062410A214560017")
    assert p["batch_no"] == "A214560017"
    assert "serial" not in p


def test_parse_dm_batch_with_30_and_01_like_digits_stays_intact():
    """真实梅毒盒码：批号 W034603011 里的 "30+8位数字""01+14位数字" 不能当成下一个 AI。

    码：DI 06933289805346 / 生产 2026-03-20 / 失效 2028-03-20 /
    批号 W034603011 / 序列号 31012500005511。
    旧实现会把批号截成 W03460、把后面的 11213101 当成数量，序列号也丢。
    """
    from app.services.udi_service import parse_dm

    code = ("0106933289805346112603201728032010W0346030112131012500005511")
    p = parse_dm(code)
    assert p["di"] == "06933289805346"
    assert p["batch_no"] == "W034603011"
    assert p["serial"] == "31012500005511"
    assert "qty" not in p
    assert p["produce_date"].isoformat() == "2026-03-20"
    assert p["expire_date"].isoformat() == "2028-03-20"


def test_parse_dm_no_serial_with_11_right_after_batch():
    """真实 HIV 盒码没有 (21) 序列号，批号末尾的 2 和 (11) 的 1 不能凑成假 21。

    码：(01)06936498602812(10)2510032(11)251009(17)271009
    —— 批号 2510032、生产 2025-10-09、失效 2027-10-09，没有序列号。
    旧实现把 125100917271009 整段（含 17 日期）当成序列号，批号还被截成 251003。
    """
    from app.services.udi_service import parse_dm

    p = parse_dm("01069364986028121025100321125100917271009")
    assert p["di"] == "06936498602812"
    assert p["batch_no"] == "2510032"
    assert "serial" not in p
    assert p["produce_date"].isoformat() == "2025-10-09"
    assert p["expire_date"].isoformat() == "2027-10-09"


def test_parse_dm_short_serial_is_split_from_batch():
    """真实基蛋降钙素原盒码：序列号只有 7 位（H000362），不能被并进批号。

    标签：(01)06954414706512 (11)260601 (17)271201 (10)P01826044 (21)H000362
    旧实现要求 21 后至少 8 位，会把 P0182604421H000362 整段当批号、序列号丢失。
    """
    from app.services.udi_service import parse_dm

    p = parse_dm("0106954414706512112606011727120110P0182604421H000362")
    assert p["di"] == "06954414706512"
    assert p["batch_no"] == "P01826044"
    assert p["serial"] == "H000362"
    assert p["produce_date"].isoformat() == "2026-06-01"
    assert p["expire_date"].isoformat() == "2027-12-01"


def test_in_add_page_has_serial_and_produce_fields(client):
    """入库页要有序列号、生产日期两个非必填框；批号、有效期仍是必填。"""
    html = client.get("/stock-in/add").get_data(as_text=True)
    assert 'name="serial_no"' in html and 'id="serialNo"' in html
    assert 'name="produce_date"' in html and 'id="produceDate"' in html
    assert 'name="batch_no"' in html and 'name="expire_date"' in html
    # 识别序列号开关默认关闭：按批号整批入库时不用每次手动删序列号
    assert 'id="serialToggle"' in html
    toggle_line = [ln for ln in html.splitlines() if 'id="serialToggle"' in ln][0]
    assert "checked" not in toggle_line
    assert "wantSerial()" in html
    # 布局：左 4 列扫码卡片 + 待入库清单，右 8 列表单
    assert 'class="col-lg-4' in html and 'class="col-lg-8' in html
    assert 'id="pendingList"' in html and "一次提交" in html
    # 提交按钮只在卡片标题栏，页面底部不再重复一枚
    assert 'class="btn btn-primary btn-sm js-submit' in html
    assert 'btn btn-primary js-submit" type="submit"><i class="bi bi-check-lg"></i> 提交入库' not in html


def test_stock_in_saves_serial_and_produce_date(client, db_session, users, reagent):
    """提交入库时把序列号、生产日期一起存下来（两者都可以不填）。"""
    from app.models.stock import StockIn

    resp = client.post("/stock-in/add", data={
        "reagent_id": reagent.id, "batch_no": "W041606007",
        "expire_date": "2028-06-24", "quantity": 1, "unit_price": 100,
        "verifier_id": users["mgr"].id, "accept_status": "合格",
        "serial_no": "31012305014743", "produce_date": "2026-06-24",
        "csrf_token": "test-token",
    })
    assert resp.status_code in (302, 200)
    si = StockIn.query.order_by(StockIn.id.desc()).first()
    assert si.serial_no == "31012305014743"
    assert si.produce_date.isoformat() == "2026-06-24"

    # 不填这两项也能入库
    resp2 = client.post("/stock-in/add", data={
        "reagent_id": reagent.id, "batch_no": "W041606007",
        "expire_date": "2028-06-24", "quantity": 1,
        "verifier_id": users["mgr"].id, "accept_status": "合格",
        "csrf_token": "test-token",
    })
    assert resp2.status_code in (302, 200)
    latest = StockIn.query.order_by(StockIn.id.desc()).first()
    assert latest.serial_no is None and latest.produce_date is None


def test_stock_in_rejects_duplicate_serial(client, db_session, users, reagent):
    """同一盒（同序列号）不能重复入库；不填序列号即可按批多次入库。"""
    from app.models.stock import StockIn

    base = {
        "reagent_id": reagent.id, "batch_no": "W041701001",
        "expire_date": "2028-06-24", "quantity": 1,
        "verifier_id": users["mgr"].id, "accept_status": "合格",
        "csrf_token": "test-token",
    }
    r1 = client.post("/stock-in/add", data=dict(base, serial_no="99990000111122"))
    assert r1.status_code in (302, 200)
    first = StockIn.query.order_by(StockIn.id.desc()).first()
    assert first.serial_no == "99990000111122"
    n_after_first = StockIn.query.count()

    # 同序列号再提交：拦下，不新增单据，页面给出提示
    r2 = client.post("/stock-in/add", data=dict(base, serial_no="99990000111122"))
    html = r2.get_data(as_text=True)
    assert StockIn.query.count() == n_after_first
    assert "同一盒不能重复入库" in html
    assert first.order_no in html

    # 不填序列号（= 不勾选「识别序列号」，按批入库）：同批号可以连续入
    client.post("/stock-in/add", data=base)
    client.post("/stock-in/add", data=base)
    assert StockIn.query.count() == n_after_first + 2


def test_udi_scan_reports_duplicate_serial(client, db_session, users, reagent):
    """扫同一个码第二次时，接口要告诉前端这盒序列号已入库。"""
    from app.models.stock import StockIn

    code = ("0106933289830348112606241728062410W041606007"
            "2131012305014743")
    client.post("/stock-in/add", data={
        "reagent_id": reagent.id, "batch_no": "W041606007",
        "expire_date": "2028-06-24", "quantity": 1,
        "verifier_id": users["mgr"].id, "accept_status": "合格",
        "serial_no": "31012305014743", "produce_date": "2026-06-24",
        "csrf_token": "test-token",
    })
    si = StockIn.query.filter_by(serial_no="31012305014743").first()
    assert si is not None

    d = client.get("/stock-in/udi?code=" + code).get_json()
    assert d["parsed"]["serial"] == "31012305014743"
    assert d["serial_dup"] is not None
    assert d["serial_dup"]["order_no"] == si.order_no
    assert d["serial_dup"]["batch_no"] == "W041606007"


def test_stock_in_rejects_produce_date_after_expire(client, db_session, users, reagent):
    """生产日期晚于有效期属于录错，直接拒绝。"""
    resp = client.post("/stock-in/add", data={
        "reagent_id": reagent.id, "batch_no": "X1", "expire_date": "2026-12-31",
        "quantity": 1, "verifier_id": users["mgr"].id,
        "produce_date": "2027-01-02", "csrf_token": "test-token",
    }, follow_redirects=True)
    assert "生产日期不能晚于有效期" in resp.get_data(as_text=True)


def test_batch_stock_in_creates_multiple_and_labels(client, db_session, users, reagent):
    """待入库清单一次提交：多试剂一起落库、直接生成条码，并跳到批量标签页。"""
    from app.models.stock import ReagentPack

    payload = {
        "items": [
            # 勾了序列号就是一盒，数量只能是 1
            {"reagent_id": reagent.id, "batch_no": "B2601", "expire_date": "2028-06-30",
             "quantity": 1, "unit_price": 12.5, "serial_no": "SN0001"},
            # 没有序列号可以按批号一次入多盒
            {"reagent_id": reagent.id, "batch_no": "B2602", "expire_date": "2028-07-31",
             "quantity": 2, "produce_date": "2026-07-01"},
        ],
        "verifier_id": users["mgr"].id,
        "storage_location": "2号冰箱",
        "accept_status": "合格",
    }
    r = client.post("/stock-in/add-batch", json=payload,
                    headers={"X-CSRF-Token": "test-token"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True
    assert len(d["sids"]) == 2
    assert d["labels_url"].startswith("/stock-in/labels-batch?sids=")
    packs = ReagentPack.query.filter(ReagentPack.stock_in_id.in_(d["sids"])).count()
    assert packs == 3                      # 2 盒 + 1 盒

    html = client.get(d["labels_url"]).get_data(as_text=True)
    assert "批量入库条码标签" in html
    assert "3 枚" in html


def test_batch_stock_in_rejects_bad_rows(client, db_session, users, reagent):
    """整批校验：缺验收人、重复序列号都整批拒绝，一条都不落库。"""
    from app.models.stock import StockIn

    n0 = StockIn.query.count()

    # 缺验收人
    r = client.post("/stock-in/add-batch", json={
        "items": [{"reagent_id": reagent.id, "batch_no": "B1",
                   "expire_date": "2028-06-30", "quantity": 1}],
    }, headers={"X-CSRF-Token": "test-token"})
    assert r.status_code == 400
    assert any("验收人" in e for e in r.get_json()["errors"])
    assert StockIn.query.count() == n0

    # 先单条入库一个带序列号的，再批量提交同一序列号 → 拒绝
    client.post("/stock-in/add", data={
        "reagent_id": reagent.id, "batch_no": "B9", "expire_date": "2028-06-30",
        "quantity": 1, "verifier_id": users["mgr"].id,
        "serial_no": "SN9", "csrf_token": "test-token",
    })
    n1 = StockIn.query.count()
    r = client.post("/stock-in/add-batch", json={
        "items": [{"reagent_id": reagent.id, "batch_no": "B9", "expire_date": "2028-06-30",
                   "quantity": 1, "serial_no": "SN9"}],
        "verifier_id": users["mgr"].id,
    }, headers={"X-CSRF-Token": "test-token"})
    assert r.status_code == 400
    assert any("重复入库" in e for e in r.get_json()["errors"])
    assert StockIn.query.count() == n1


def test_serial_reagent_quantity_must_be_one(client, db_session, users, reagent):
    """有序列号就是一盒：批量与单条都不许把数量写成 2，否则同一序列号会变成两盒。"""
    from app.models.stock import StockIn

    # 批量：序列号 + 数量 2 → 整批拒绝
    r = client.post("/stock-in/add-batch", json={
        "items": [{"reagent_id": reagent.id, "batch_no": "SNQ",
                   "expire_date": "2028-06-30", "quantity": 2, "serial_no": "SNQ0001"}],
        "verifier_id": users["mgr"].id,
    }, headers={"X-CSRF-Token": "test-token"})
    assert r.status_code == 400
    assert any("数量只能为 1" in e for e in r.get_json()["errors"])
    assert StockIn.query.filter_by(serial_no="SNQ0001").count() == 0

    # 单条：序列号 + 盒数 2 → 不入库
    client.post("/stock-in/add", data={
        "reagent_id": reagent.id, "batch_no": "SNQ", "expire_date": "2028-06-30",
        "quantity": 2, "serial_no": "SNQ0002", "verifier_id": users["mgr"].id,
        "csrf_token": "test-token",
    })
    assert StockIn.query.filter_by(serial_no="SNQ0002").count() == 0

    # 数量 1 正常入库
    client.post("/stock-in/add", data={
        "reagent_id": reagent.id, "batch_no": "SNQ", "expire_date": "2028-06-30",
        "quantity": 1, "serial_no": "SNQ0003", "verifier_id": users["mgr"].id,
        "csrf_token": "test-token",
    })
    assert StockIn.query.filter_by(serial_no="SNQ0003").count() == 1


def test_in_export_and_detail_include_serial(client, db_session, users, reagent):
    """入库单详情页与导出的 xlsx 都要带序列号（和生产日期）。"""
    import io
    from openpyxl import load_workbook
    from app.models.stock import StockIn

    client.post("/stock-in/add", data={
        "reagent_id": reagent.id, "batch_no": "W041606007",
        "expire_date": "2028-06-24", "quantity": 1,
        "verifier_id": users["mgr"].id,
        "serial_no": "31012305014743", "produce_date": "2026-06-24",
        "csrf_token": "test-token",
    })
    si = StockIn.query.filter_by(serial_no="31012305014743").first()
    assert si is not None

    detail = client.get("/stock-in/%d" % si.id).get_data(as_text=True)
    assert "序列号" in detail
    assert "31012305014743" in detail
    assert "2026-06-24" in detail

    resp = client.get("/stock-in/export")
    assert resp.status_code == 200
    wb = load_workbook(io.BytesIO(resp.data))
    ws = wb.active
    headers = [c.value for c in ws[1]]
    assert "序列号(UDI 21)" in headers
    assert "生产日期" in headers
    col = headers.index("序列号(UDI 21)")
    values = [ws.cell(row=i, column=col + 1).value for i in range(2, ws.max_row + 1)]
    assert "31012305014743" in values
