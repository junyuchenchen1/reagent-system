# -*- coding: utf-8 -*-
"""UDI 扫码识别服务

三件事：
1. parse_dm()      —— 解析试剂盒 Data Matrix（GS1 标准）里的产品标识、批号、有效期
2. lookup_product()—— 用 DI 查本地 UDI 产品库（由药监局发布文件导入）
3. match_reagents()—— 把 UDI 产品对应到本系统试剂档案（已绑定 > 注册证/名称 > 模糊候选）

说明：批号与有效期本来就在码里（AI 10 / 17），UDI 库只提供“这个 DI 是哪个产品”，
两者结合才能做到扫一下自动选试剂 + 填批号效期。
"""
import re
from datetime import date

from app.extensions import db
from app.models.reagent import Reagent
from app.models.udi import UdiProduct

# AI 定长字段：产品标识 14 位、生产日期/有效期 6 位、数量 8 位
_FIXED_AI = {"01": 14, "11": 6, "17": 6, "30": 8}
_VAR_AI = ("10", "21", "240")
_ALL_AI = tuple(_FIXED_AI) + _VAR_AI

# 变长字段后面“下一个 AI 的可信起点”：01 后必须 14 位纯数字，17/11 后必须 6 位合法日期。
# 不把 "21"/"240" 当起点：它们出现在批号内部（如 114260321 里的 21）的概率远高于真的下一个 AI。
_AI_NEXT = re.compile(r"(01)(\d{14})|(17|11)(\d{6})|(30)(\d{8})")

# 检验试剂关键字（导入发布文件时据此过滤，避免把几百万条耗材/设备都塞进本地库）
IVD_KEYWORDS = ("试剂", "校准品", "质控品", "抗体", "染色液", "溶血剂",
                "培养基", "血凝", "凝血", "检测试剂")

# 药监局数据里很多检验用的"液"类产品名称不带"试剂"二字（稀释液、清洗液、鞘液、
# 定标液、缓冲液、样本保存液、底物液……），只按上面的关键词会漏掉。补充这些：
IVD_KEYWORDS += ("稀释液", "清洗液", "鞘液", "定标液", "校准液", "缓冲液",
                 "保存液", "底物", "质控液", "参考品", "对照品", "检测盒",
                 "测定盒", "血型", "抗A", "抗B", "血清", "酶标", "标本",
                 "采样", "溶血")

# 名字里出现这些的，基本不是检验试剂（隐形眼镜护理液、输液器、口罩等），先排除再加关键词
IVD_EXCLUDE = ("隐形眼镜", "护理液", "润眼", "润滑", "输液", "注射", "口罩",
               "手套", "敷料", "口腔", "鼻腔", "耳道", "家用", "美容", "减肥")


def _gs1_date(yymmdd: str):
    """GS1 的 YYMMDD 换算成日期：00-49 → 2000-2049，50-99 → 1950-1999。"""
    if not yymmdd or len(yymmdd) != 6 or not yymmdd.isdigit():
        return None
    yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:])
    year = 2000 + yy if yy <= 49 else 1900 + yy
    try:
        return date(year, mm, dd)
    except ValueError:
        return None


def _next_ai_start(code: str, i: int) -> int:
    """变长字段（批号/序列号）后没有 GS 分隔符时，找下一个“可信的 AI 起点”。

    不能只按 "11"/"17" 两个字判断：批号本身可能以 11 开头（如 114260321），
    所以日期型 AI 必须连同 6 位一起校验成合法日期，01 必须后接 14 位纯数字。
    """
    for m in _AI_NEXT.finditer(code, i):
        if m.group(1):
            return m.start()
        if m.group(3):
            if _gs1_date(m.group(4)):
                return m.start()
            continue
        return m.start()
    return len(code)


def parse_dm(raw: str) -> dict:
    """解析 GS1 DataMatrix 内容。

    支持两种常见形态：
    - 标准码：`01`+GTIN14 `17`+YYMMDD `10`+批号（变长字段以 GS=\\x1d 结束）
    - 不规范码：变长批号后没有 GS，直接跟下一个 AI —— 用正则找下一个定长 AI 起点截断
    返回：{"di","batch_no","expire_date","produce_date","serial","qty"}
    """
    result = {}
    if not raw:
        return result
    code = (raw.strip()
            .replace("\x1d", "|").replace("\u001d", "|")
            .replace("\r", "").replace("\n", ""))
    code = code.replace("(", "").replace(")", "")   # 人可读括号格式 (01)…
    code = code.replace(" ", "|")                   # GS1 码内不会有空格，出现即当作分隔符

    if code.isdigit() and len(code) == 14:          # 只扫到 DI 本身
        return {"di": code}

    i, n = 0, len(code)
    while i < n:
        if code[i] == "|":
            i += 1
            continue
        ai = code[i:i + 2]
        if ai not in _ALL_AI:
            i += 1
            continue
        i += 2
        if ai in _FIXED_AI:
            val = code[i:i + _FIXED_AI[ai]]
            i += _FIXED_AI[ai]
            if not val:
                continue
            if ai == "01":
                # 只认真的 14 位产品标识：码尾噪音（如 "240130219004M" 里恰好出现的 01）不能覆盖
                if val.isdigit() and len(val) == 14:
                    result.setdefault("di", val)
            elif ai == "30":
                result["qty"] = val.lstrip("0") or "0"
            else:
                d = _gs1_date(val)
                if d:
                    result["expire_date" if ai == "17" else "produce_date"] = d
        else:                                       # 变长字段
            end = code.find("|", i)
            if end < 0:
                end = _next_ai_start(code, i)
            val = code[i:end].strip("|")
            i = end
            if ai == "10":
                result["batch_no"] = val
            elif ai == "21":
                result["serial"] = val
            elif ai == "240":
                result["extra"] = val

    # 兜底：没有 AI 开头但整串里存在 GTIN 形态
    if "di" not in result:
        m = re.search(r"01(\d{14})", code)
        if m:
            result["di"] = m.group(1)
    if "di" in result:
        result["di"] = result["di"].strip()
    return result


def lookup_product(di: str):
    """按 DI 查本地 UDI 产品库；未导入或未收录返回 None。"""
    di = (di or "").strip()
    if not di:
        return None
    return db.session.get(UdiProduct, di)


def _norm(s: str) -> str:
    if not s:
        return ""
    s = s.replace("（", "(").replace("）", ")").replace("　", " ")
    return re.sub(r"\s+", "", s).lower()


def _base_name(s: str) -> str:
    """去掉括号补充说明，只留主名称，便于“葡萄糖测定试剂盒(己糖激酶法)”这种差异对比。"""
    return re.sub(r"\([^)]*\)", "", _norm(s))


def match_reagents(product, limit: int = 5):
    """返回 (建议试剂 id, 候选列表)。已绑定关系优先，其次注册证/名称，最后名称包含。"""
    if product is None:
        return None, []
    if product.reagent_id:
        r = db.session.get(Reagent, product.reagent_id)
        if r is not None:
            return r.id, [{"id": r.id, "name": r.name, "spec": r.spec,
                           "registrant": r.registrant, "score": 200,
                           "reason": "该 DI 已绑定此试剂"}]

    pname = _base_name(product.name)
    pmfr = _norm(product.mfr)
    preg = _norm((product.reg_no or "").split(",")[0])

    scored = []
    for r in Reagent.query.filter_by(status=1).all():
        rname = _base_name(r.name)
        rreg = _norm(r.reg_no or "")
        rfull = _norm(r.name)
        score, reason = 0, ""
        reg_hit = bool(preg and rreg and (preg == rreg or preg in rreg or rreg in preg))
        name_score = 0
        if pname and rname:
            if pname == rname:
                name_score = 80
            elif len(pname) >= 4 and (pname in rname or rname in pname):
                name_score = 45
            elif pname in rfull:
                name_score = 30
        if reg_hit:
            # 一个注册证常覆盖同系列多个产品，名称对不上时只当线索，不能自动选定
            score += 100 if name_score else 55
            reason = "注册证编号一致" if name_score else "注册证一致但名称不同，请确认"
        if name_score:
            score += name_score
            reason = reason or {80: "产品名称一致", 45: "产品名称包含",
                                30: "名称相近"}[name_score]
        if pmfr and r.registrant and _norm(r.registrant) == pmfr:
            score += 20
            reason = reason or "生产企业一致"
        if score >= 45:
            scored.append({"id": r.id, "name": r.name, "spec": r.spec,
                           "registrant": r.registrant, "score": score,
                           "reason": reason})

    scored.sort(key=lambda x: (-x["score"], x["id"]))
    candidates = scored[:limit]
    best = None
    if candidates:
        # 只有“唯一最高分”才自动选中；并列最高（如高值/低值两条同名质控品）交给人工点选
        top = candidates[0]["score"]
        if top >= 75 and sum(1 for c in scored if c["score"] == top) == 1:
            best = candidates[0]
    return (best["id"] if best else None), candidates


def bind_reagent(di: str, reagent_id) -> None:
    """记住“这个 DI 对应本系统哪条试剂档案”（第一次手工选定后自动调用）。"""
    if not di or not reagent_id:
        return
    product = db.session.get(UdiProduct, str(di).strip())
    if product is None:
        product = UdiProduct(di=str(di).strip(), name="（本机扫码建档）",
                             source="扫码入库")
        db.session.add(product)
    product.reagent_id = int(reagent_id)
    db.session.commit()


def resolve(raw_code: str) -> dict:
    """扫描后的统一入口：解析 → 查库 → 匹配试剂，返回前端可直接使用的结构。"""
    parsed = parse_dm(raw_code)
    product = lookup_product(parsed.get("di"))
    reagent_id, candidates = match_reagents(product)
    return {
        "parsed": {
            "di": parsed.get("di"),
            "batch_no": parsed.get("batch_no"),
            "expire_date": (parsed.get("expire_date").isoformat()
                            if parsed.get("expire_date") else None),
            "produce_date": (parsed.get("produce_date").isoformat()
                             if parsed.get("produce_date") else None),
            "serial": parsed.get("serial"),
        },
        "product": product.to_dict() if product else None,
        "reagent_id": reagent_id,
        "candidates": candidates,
        "found": bool(product),
    }
