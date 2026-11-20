# -*- coding: utf-8 -*-
"""药监局 UDI 发布文件导入器（只导检验试剂子集）

用法：
    python udi_import.py D:\\下载\\UDID_MONTH_UPDATE_202610.zip
    python udi_import.py D:\\下载\\UDID_DAY_UPDATE_20261119.zip --dry-run
    python udi_import.py <文件> --all          # 不过滤，导入全部产品（含耗材/设备）
    python udi_import.py <文件> --limit 1000   # 只导入前 N 条符合条件的产品

发布文件来自 https://udi.nmpa.gov.cn/download.html （每日/每周/每月/全量），
ZIP 内是 XML。默认按 IVD_KEYWORDS 过滤，只保留检验试剂类产品，库体积小、查得快。
重复导入同一 DI 会更新产品信息（保留已绑定的试剂关系）。
"""
import argparse
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.extensions import db                       # noqa: E402
from app.models.udi import UdiProduct               # noqa: E402
from app.services.udi_service import IVD_KEYWORDS   # noqa: E402

# XML 字段（药监局发布文件的拼音缩写字段名）
F_DI = "zxxsdycpbs"
F_CODING = "cpbsbmtxmc"
F_NAME = "cpmctymc"
F_SPEC = "ggxh"
F_MFR = "ylqxzcrbarmc"
F_REG = "zczbhhzbapzbh"
F_CAT = "cplb"


def _field(elem, tag: str) -> str:
    return (elem.findtext(tag) or "").strip()


def _clean_reg(value: str) -> str:
    """清洗注册证/备案号：药监局数据里常见 "京大械备20180038号,京大械备20180038" 这类同号重复，
    按去掉尾部"号"的归一值去重，优先保留带"号"的写法。"""
    parts = [p.strip() for p in re.split(r"[,，;；/]", value or "") if p.strip()]
    out, index = [], {}
    for part in parts:
        key = part.rstrip("号").strip().lower()
        if not key:
            continue
        if key in index:
            if part.endswith("号") and not out[index[key]].endswith("号"):
                out[index[key]] = part
            continue
        index[key] = len(out)
        out.append(part)
    return ",".join(out)


def _is_ivd(name: str, reg_no: str, keywords=IVD_KEYWORDS) -> bool:
    if not name:
        return False
    return any(k in name for k in keywords) or "试剂" in (reg_no or "")


def _open_xml(path: Path):
    """返回 (需要关闭的对象, 字节流)。ZIP 里取第一个 XML。"""
    if path.suffix.lower() == ".zip":
        zf = zipfile.ZipFile(path)
        names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if not names:
            zf.close()
            raise SystemExit("ZIP 内没有 XML 文件")
        return zf, zf.open(names[0])
    return None, open(path, "rb")


def import_package(path, only_ivd: bool = True, dry_run: bool = False,
                   limit: int = 0, source: str = "") -> dict:
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"文件不存在：{path}")
    source = source or path.name

    handle, stream = _open_xml(path)
    stats = {"scanned": 0, "matched": 0, "written": 0, "no_di": 0,
             "updated": 0, "created": 0}
    seen = set()
    try:
        # 用 start 事件抓根元素，便于定期释放内存（全量 XML 可能上 GB）
        context = ET.iterparse(stream, events=("start", "end"))
        root = None
        pending = 0
        for event, elem in context:
            if root is None and event == "start":
                root = elem
            if event != "end" or elem.tag != "device":
                continue
            stats["scanned"] += 1
            di = _field(elem, F_DI)
            name = _field(elem, F_NAME)
            reg = _clean_reg(_field(elem, F_REG))
            if (not di) or (only_ivd and not _is_ivd(name, reg)):
                if not di:
                    stats["no_di"] += 1
                if limit and stats["matched"] >= limit:
                    elem.clear()
                    break
                elem.clear()
                if root is not None and stats["scanned"] % 5000 == 0:
                    root.clear()
                continue

            stats["matched"] += 1
            if not dry_run and di not in seen:
                seen.add(di)
                obj = UdiProduct(
                    di=di, coding=_field(elem, F_CODING), name=name[:200],
                    spec=_field(elem, F_SPEC)[:255],
                    mfr=_field(elem, F_MFR)[:200], reg_no=reg[:200],
                    cat=_field(elem, F_CAT)[:20], source=source[:80])
                existed = db.session.get(UdiProduct, di) is not None
                if existed:
                    # 更新产品信息，保留已绑定的 reagent_id
                    db.session.merge(obj)
                    stats["updated"] += 1
                else:
                    db.session.add(obj)
                    stats["created"] += 1
                stats["written"] += 1
                pending += 1
                if pending >= 300:
                    db.session.commit()
                    pending = 0

            elem.clear()
            if root is not None and stats["scanned"] % 5000 == 0:
                root.clear()
            if limit and stats["matched"] >= limit:
                break
        if not dry_run and pending:
            db.session.commit()
    finally:
        stream.close()
        if handle is not None:
            handle.close()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="导入药监局 UDI 发布文件")
    parser.add_argument("file", help="ZIP 或 XML 路径")
    parser.add_argument("--all", action="store_true",
                        help="不过滤，导入全部产品（耗材/设备也导入）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    parser.add_argument("--limit", type=int, default=0, help="最多导入 N 条")
    parser.add_argument("--source", default="", help="来源备注（默认用文件名）")
    args = parser.parse_args()

    from app import create_app
    app = create_app()
    with app.app_context():
        if not args.dry_run:
            before = UdiProduct.query.count()
            print(f"[UDI] 导入前已有 {before} 条")
        stats = import_package(args.file, only_ivd=not args.all,
                               dry_run=args.dry_run, limit=args.limit,
                               source=args.source)
        print(f"[UDI] 扫描 {stats['scanned']} 条，命中检验试剂 {stats['matched']} 条")
        if args.dry_run:
            print("[UDI] 试运行，未写入数据库")
        else:
            print(f"[UDI] 写入 {stats['written']} 条（新增 {stats['created']}，"
                  f"更新 {stats['updated']}），累计 "
                  f"{UdiProduct.query.count()} 条")


if __name__ == "__main__":
    main()
