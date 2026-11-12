# -*- coding: utf-8 -*-
"""
数据库备份 + 临时导出文件清理（供 Windows 任务计划程序每日调用）
- SQLite 在线备份：使用 sqlite3 backup API，服务运行中也能安全备份
- 备份目录：backups/，文件名 reagent_YYYYMMDD_HHMMSS.db
- 保留最近 30 天的备份，自动删除更早的
- 顺便清理 exports/ 里 7 天前导出的 xlsx
"""
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "instance", "reagent.db")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
EXPORT_DIR = os.path.join(BASE_DIR, "exports")

KEEP_DAYS_BACKUP = 30      # 备份保留天数
KEEP_DAYS_EXPORT = 7       # 导出文件保留天数
KEEP_DAYS_LOG = 365        # 操作日志保留天数


def backup_sqlite():
    """用 SQLite 在线备份 API，避免复制到一半数据库被写入"""
    if not os.path.exists(DB_PATH):
        print(f"[备份] 未找到数据库文件：{DB_PATH}")
        return False
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"reagent_{stamp}.db")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
        print(f"[备份] 成功 → {dest}  ({os.path.getsize(dest)//1024} KB)")
    except Exception as e:
        print(f"[备份] 失败：{e}")
        return False
    finally:
        dst.close()
        src.close()
    return True


def clean_old_backups():
    """删除超过保留期的旧备份"""
    if not os.path.exists(BACKUP_DIR):
        return
    cutoff = datetime.now() - timedelta(days=KEEP_DAYS_BACKUP)
    removed = 0
    for name in os.listdir(BACKUP_DIR):
        if not (name.startswith("reagent_") and name.endswith(".db")):
            continue
        path = os.path.join(BACKUP_DIR, name)
        mtime = datetime.fromtimestamp(os.path.getmtime(path))
        if mtime < cutoff:
            try:
                os.remove(path)
                removed += 1
            except Exception as e:
                print(f"[清理备份] 删除 {name} 失败：{e}")
    if removed:
        print(f"[清理备份] 删除了 {removed} 个过期备份（>{KEEP_DAYS_BACKUP}天）")


def clean_old_exports():
    """删除 exports/ 里超过保留期的 xlsx"""
    if not os.path.exists(EXPORT_DIR):
        return
    cutoff = datetime.now() - timedelta(days=KEEP_DAYS_EXPORT)
    removed = 0
    for name in os.listdir(EXPORT_DIR):
        if not name.endswith(".xlsx"):
            continue
        path = os.path.join(EXPORT_DIR, name)
        mtime = datetime.fromtimestamp(os.path.getmtime(path))
        if mtime < cutoff:
            try:
                os.remove(path)
                removed += 1
            except Exception as e:
                print(f"[清理导出] 删除 {name} 失败：{e}")
    if removed:
        print(f"[清理导出] 删除了 {removed} 个过期导出文件（>{KEEP_DAYS_EXPORT}天）")


def clean_old_logs():
    """删除 sys_oper_log 表中超过保留期的操作日志"""
    if not os.path.exists(DB_PATH):
        return
    cutoff = (datetime.now() - timedelta(days=KEEP_DAYS_LOG)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("DELETE FROM sys_oper_log WHERE create_time < ?", (cutoff,))
        removed = cur.rowcount
        conn.commit()
        conn.close()
        if removed:
            print(f"[清理日志] 删除了 {removed} 条过期操作日志（>{KEEP_DAYS_LOG}天）")
    except Exception as e:
        print(f"[清理日志] 失败：{e}")


if __name__ == "__main__":
    print(f"=== 备份任务开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    ok = backup_sqlite()
    clean_old_backups()
    clean_old_exports()
    clean_old_logs()
    print("=== 备份任务完成 ===")
    sys.exit(0 if ok else 1)
