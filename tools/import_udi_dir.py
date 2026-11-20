# -*- coding: utf-8 -*-
"""扫一个目录，把里面所有 UDI 发布包（.zip / .xml）逐个导入本地 DI 产品库。

用法：
    python tools\\import_udi_dir.py                     # 扫项目根目录（默认）
    python tools\\import_udi_dir.py D:\\下载\\UDI包     # 扫指定目录
    python tools\\import_udi_dir.py D:\\下载 --recursive # 连子目录一起扫
    python tools\\import_udi_dir.py --dry-run           # 只统计不写库
    python tools\\import_udi_dir.py --yes               # 不询问，直接导

重复导入同一个 DI 只更新产品信息，已建立的 DI↔试剂绑定关系保留。
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SKIP_DIRS = {"dist", "backups", "instance", "logs", "exports", ".git", "__pycache__", ".venv", "venv"}


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def collect(root: Path, recursive: bool, keyword: str):
    pattern = "**/*" if recursive else "*"
    found = []
    for p in root.glob(pattern):
        if not p.is_file() or p.suffix.lower() not in (".zip", ".xml"):
            continue
        rel = p.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel[:-1]):
            continue
        if keyword and keyword.lower() not in p.name.lower():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        found.append((p, size))
    return sorted(found, key=lambda x: (str(x[0]).lower(), x[1]))


def main() -> int:
    ap = argparse.ArgumentParser(description="把一个目录下的 UDI 发布包全部导入")
    ap.add_argument("dir", nargs="?", default="", help="要扫描的目录（默认项目根目录）")
    ap.add_argument("--recursive", action="store_true", help="连子目录一起扫")
    ap.add_argument("--keyword", default="", help="只导入文件名含该关键字的包")
    ap.add_argument("--dry-run", action="store_true", help="只统计命中多少条，不写库")
    ap.add_argument("--yes", action="store_true", help="不询问，直接开始")
    ap.add_argument("--no-backup", action="store_true", help="跳过导入前的数据库备份")
    args = ap.parse_args()

    root = Path(args.dir).resolve() if args.dir else ROOT
    if not root.is_dir():
        print(f"[错误] 目录不存在：{root}")
        return 1

    files = collect(root, args.recursive, args.keyword)
    print("=" * 60)
    print(f" 扫描目录：{root}（{'含子目录' if args.recursive else '仅本层'}）")
    print("=" * 60)
    if not files:
        print("[提示] 这个目录下没有 .zip / .xml 文件。")
        print("       把药监局下载的 UDI 发布包放到这里，或直接把这个文件夹拖到脚本上。")
        return 1

    total_size = sum(s for _, s in files)
    print(f"找到 {len(files)} 个文件，合计 {human_size(total_size)}：")
    for i, (p, s) in enumerate(files, 1):
        tip = ""
        if s > 200 * 1024 * 1024:
            tip = "  ← 全量包，约 15~20 分钟"
        elif s > 20 * 1024 * 1024:
            tip = "  ← 较大的包，可能几分钟"
        print(f"  {i}. {p.name}（{human_size(s)}）{tip}")
    print()

    if not args.yes:
        if args.dry_run:
            ans = input("按回车开始预检（只统计，不写库；输入 n 取消）：")
        else:
            ans = input("导入前会先备份数据库。按回车开始导入（输入 n 取消）：")
        if ans.strip().lower() == "n":
            print("已取消，未做任何写入。")
            return 0

    if not args.dry_run and not args.no_backup:
        print("[1/2] 备份数据库 ...")
        try:
            import backup
            ok = backup.backup_sqlite()
            if not ok:
                print("[警告] 备份未成功，继续导入（如需回滚请自行处理）")
        except Exception as exc:                       # noqa: BLE001
            print(f"[警告] 备份失败：{exc}，继续导入")

    print("[2/2] 开始导入 ...\n")
    from app import create_app
    from udi_import import import_package

    app = create_app()
    summary = {"files": 0, "scanned": 0, "matched": 0, "created": 0,
               "updated": 0, "skipped": 0, "failed": 0}
    started = time.time()

    with app.app_context():
        for idx, (p, size) in enumerate(files, 1):
            print(f"--- [{idx}/{len(files)}] {p.name} ---")
            t0 = time.time()
            try:
                stats = import_package(p, dry_run=args.dry_run, source=f"目录导入:{p.name}")
            except SystemExit as exc:
                print(f"[跳过] {p.name}：{exc}")
                summary["failed"] += 1
                print()
                continue
            except Exception as exc:                    # noqa: BLE001
                print(f"[失败] {p.name}：{exc}")
                summary["failed"] += 1
                print()
                continue
            summary["files"] += 1
            for k in ("scanned", "matched", "created", "updated", "skipped"):
                summary[k] += int(stats.get(k, 0) or 0)
            print(f"    扫描 {stats['scanned']} 条，命中 {stats['matched']} 条，"
                  f"新增 {stats['created']}，更新 {stats['updated']}，"
                  f"耗时 {time.time() - t0:.1f}s")
            print()

    print("=" * 60)
    print(f" 完成：{summary['files']} 个文件导入成功，{summary['failed']} 个失败/跳过")
    print(f" 合计扫描 {summary['scanned']} 条 → 命中 {summary['matched']} 条"
          f"（新增 {summary['created']}，更新 {summary['updated']}）")
    if summary["skipped"]:
        print(f" 有 {summary['skipped']} 个文件解析失败被跳过")
    print(f" 总耗时 {time.time() - started:.1f}s")
    if args.dry_run:
        print(" 这是预检，未写入数据库。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
