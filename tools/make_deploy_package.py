# -*- coding: utf-8 -*-
"""生成“干净的一键部署包”ZIP：不含数据库、日志、备份、密钥与 git 历史。

用法：
    python tools/make_deploy_package.py                     # 版本号取当天日期
    python tools/make_deploy_package.py --version 20261116  # 指定版本号

产物：dist/试剂管理系统_部署包_<版本>.zip
"""
import argparse
import os
import shutil
import zipfile
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "dist"

# 整目录剔除（运行时数据、缓存、版本库）
EXCLUDE_DIRS = {".git", ".pytest_cache", "__pycache__", "instance",
                "logs", "backups", "exports", "dist", ".idea", ".vscode"}
# 按目录名前缀剔除（药监局 UDI 原始发布文件解压目录，单包可达 300MB+）
EXCLUDE_DIR_PREFIX = ("UDID_", "udi_data")
# 按后缀剔除（数据库及其 WAL/SHM、字节码、日志、压缩包）
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".sqlite",
                  ".log", ".zip", ".tmp")
# 按文件名剔除（会话密钥、首启凭据）
EXCLUDE_NAMES = {"secret_key", "initial_admin_password.txt",
                 "Thumbs.db", "desktop.ini"}

# 包里必须保留的空目录（首次启动前由 install.ps1 建，但 zip 里要有结构）
EMPTY_DIRS = ["instance", "instance/signatures", "instance/certs",
              "logs", "backups", "exports"]

PACKAGE_README = """检验科试剂管理系统 —— 一键部署包
版本：{version}

一、环境要求
  Windows 10 / 11（64 位），已安装 Python 3.10 或更高版本
  （安装 Python 时务必勾选 “Add Python to PATH”）

二、三步完成部署
  1) 把整个文件夹解压到本地磁盘，例如 D:\\检验科\\试剂
     （不要在压缩包里直接双击运行）
  2) 右键 install.ps1 → “使用 PowerShell 运行”
     若提示“禁止运行脚本”，先在 PowerShell 执行一次：
       Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
  3) 脚本会自动完成：校验 Python 版本 → 安装依赖 → 建目录 → 初始化数据库
     → 生成启动/停止/看门狗脚本 → 注册计划任务 → 启动服务
     首次运行会生成管理员密码，位置：instance\\initial_admin_password.txt
     用账号 admin + 该密码登录，登录成功后该文件会自动删除

三、部署完成后的日常使用
  - 开机自动启动，无需手动操作；服务崩溃后 5 分钟内自动拉起
  - 每天 23:00 自动备份数据库到 backups\\ 目录（保留 30 天）
  - 桌面会生成“试剂管理系统”“停止试剂管理系统”两个快捷方式
  - 本机访问 http://127.0.0.1:5000，局域网访问 http://本机IP:5000

四、这个包不含业务数据
  不含数据库、WAL/SHM 文件、日志、备份、密钥文件；
  首次运行自动建库（16 张表 + 管理员账号 + 4 个默认存放位置），
  基础数据可用“数据导入导出”模块批量导入。

五、可选操作
  - 想先体验演示数据：python init_db.py
    （写入演示试剂与演示账号，并把 admin 密码重置为 admin123，仅限体验环境）
  - 卸载：uninstall.ps1
  - 详细文档：README.md、使用说明.txt、数据库操作步骤.txt
"""


def _excluded(path: Path) -> bool:
    return path.name in EXCLUDE_NAMES or path.suffix.lower() in EXCLUDE_SUFFIX


def build(version: str) -> Path:
    top = f"试剂管理系统_部署包_{version}"
    stage = OUT_DIR / ("_stage_" + version)
    if stage.exists():
        shutil.rmtree(stage)
    root = stage / top
    root.mkdir(parents=True)

    copied = 0
    for dirpath, dirnames, filenames in os.walk(BASE_DIR):
        here = Path(dirpath)
        rel = here.relative_to(BASE_DIR)
        # 原地裁剪，os.walk 不再进入被排除的目录
        dirnames[:] = [d for d in dirnames
                       if d not in EXCLUDE_DIRS
                       and not d.startswith(EXCLUDE_DIR_PREFIX)]
        for name in filenames:
            src = here / name
            if _excluded(src):
                continue
            (root / rel).mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, root / rel / name)
            copied += 1

    for d in EMPTY_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)

    (root / "部署步骤（请先看这里）.txt").write_text(
        PACKAGE_README.format(version=version), encoding="utf-8-sig",
        newline="\r\n")

    zip_path = OUT_DIR / f"{top}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=9) as zf:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in sorted(filenames):
                f = Path(dirpath) / name
                zf.write(f, (Path(top) / f.relative_to(root)).as_posix())
        # 空目录必须显式写条目，否则解压后不存在
        for d in EMPTY_DIRS:
            zf.writestr(zipfile.ZipInfo(f"{top}/{d}/"), "")
    shutil.rmtree(stage)
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description="生成干净部署包")
    parser.add_argument("--version", default=date.today().strftime("%Y%m%d"),
                        help="版本号，默认当天日期 YYYYMMDD")
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = build(args.version)
    print(f"[打包完成] {zip_path}")
    print(f"大小: {zip_path.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
