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
