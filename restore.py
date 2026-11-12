# -*- coding: utf-8 -*-
"""
一键恢复数据库（WAL 安全版）
1. 停掉正在运行的 Flask 服务
2. 列出 backups/ 里所有备份文件，并标出空库/损坏的文件
3. 用户输入编号选择要恢复的备份
4. 用 SQLite 在线备份 API 把当前库整库快照一份（WAL 里没合并的写入也会包含）
5. 用备份 API 整库写回当前库，写回后校验；校验不通过自动回滚
6. 重新启动服务

为什么不用 shutil.copy2：
  SQLite 开了 WAL 以后，最近的写入可能还只在 reagent.db-wal 里，主库文件
  本身是旧的、甚至只有表结构甚至空白。直接复制 .db 会漏数据（本项目就
  出现过 pre_restore_xxxx.db 里 0 张表的情况），直接覆盖后再删 -wal 更会
  把没合并的写入彻底丢掉。backup API 由 SQLite 自己保证一致性快照。
"""
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "instance", "reagent.db")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")


# ============================================================
# 服务控制
# ============================================================
def stop_server():
    """杀掉所有 run.py 进程"""
    print(">>> 正在停止服务...")
    subprocess.run(
        ["powershell", "-Command",
         "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*run.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
        capture_output=True)
    time.sleep(2)
    print("    服务已停止")


def start_server():
    print(">>> 正在启动服务...")
    subprocess.Popen(
        [sys.executable, "run.py"],
        cwd=BASE_DIR,
        creationflags=0x08000000  # CREATE_NO_WINDOW
    )
    time.sleep(4)
    print("    服务已启动（http://127.0.0.1:5000）")


# ============================================================
# SQLite 工具：WAL 安全
# ============================================================
def _connect(path):
    """带锁等待的连接：别的进程占着库时先等 15 秒，而不是立刻报错。"""
    conn = sqlite3.connect(path, timeout=15)
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def checkpoint(path):
    """把 WAL 里未合并的写入合并回主库文件并清空 WAL。"""
    if not os.path.exists(path):
        return
    conn = _connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()


def count_tables(path):
    """库里有多少张业务表（0 说明是空库/坏库）。"""
    try:
        conn = _connect(path)
        try:
            row = conn.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()
            return row[0] if row else 0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def integrity_ok(path):
    """PRAGMA integrity_check 是否为 ok。"""
    try:
        conn = _connect(path)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return bool(row) and str(row[0]).lower() == "ok"
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def snapshot(src_path, dst_path):
    """用 backup API 把 src_path 完整快照到 dst_path（含 WAL 内容）。"""
    if not os.path.exists(src_path):
        raise FileNotFoundError(f"源数据库不存在：{src_path}")
    if os.path.exists(dst_path):
        os.remove(dst_path)
    src = _connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()


def restore_from(backup_path):
    """把备份整库写回当前库。不删 -wal 文件，避免其它正打开该库的程序读到坏页。"""
    checkpoint(DB_PATH)          # 先把当前库的 WAL 落地并清空
    src = _connect(backup_path)
    dst = _connect(DB_PATH)
    try:
        src.backup(dst)          # 整库覆盖，SQLite 自己处理页与 WAL
        dst.commit()
    finally:
        dst.close()
        src.close()
    checkpoint(DB_PATH)          # 覆盖后清一次，避免残留旧 WAL


# ============================================================
# 备份列表
# ============================================================
def list_backups():
    if not os.path.exists(BACKUP_DIR):
        return []
    files = [f for f in os.listdir(BACKUP_DIR)
             if f.startswith("reagent_") and f.endswith(".db")]
    files.sort(reverse=True)
    return files


def backup_info(path):
    """返回 (表数量, 是否完整)；文件打不开时返回 (0, False)。"""
    tables = count_tables(path)
    if tables <= 0:
        return 0, False
    return tables, integrity_ok(path)


# ============================================================
# 主流程
# ============================================================
def main():
    print("=" * 50)
    print("  检验科试剂管理系统 - 数据库恢复工具（WAL 安全版）")
    print("=" * 50)
    backups = list_backups()
    if not backups:
        print("未找到任何备份文件！")
        input("按回车键退出...")
        return

    print("\n可用备份（最新在最前）：")
    info = {}
    for i, f in enumerate(backups, 1):
        path = os.path.join(BACKUP_DIR, f)
        tables, ok = backup_info(path)
        info[f] = (tables, ok)
        size = os.path.getsize(path) // 1024
        mtime = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
        tag = f"{tables} 张表" if ok else "不可用（空库或文件损坏）"
        print(f"  [{i}] {f}   ({size} KB, {mtime}, {tag})")

    choice = input("\n请输入要恢复的备份编号（输入 q 取消）：").strip()
    if choice.lower() == "q":
        print("已取消")
        return
    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(backups):
            print("编号无效！")
            return
    except ValueError:
        print("请输入数字！")
        return

    chosen = backups[idx]
    chosen_path = os.path.join(BACKUP_DIR, chosen)
    tables, ok = info[chosen]
    if not ok:
        print(f"\n{chosen} 里没有任何业务表或文件已损坏，恢复它只会得到空库/坏库。")
        print("请换一个备份，或先用其它备份文件。已取消。")
        input("按回车键退出...")
        return

    print(f"\n你选择恢复：{chosen}（{tables} 张表）")
    confirm = input("确认恢复？这会覆盖当前数据库！输入 yes 继续：").strip().lower()
    if confirm != "yes":
        print("已取消")
        return

    stop_server()

    # 先把当前库安全快照一份（backup API：WAL 里没合并的写入也在内）
    safety = None
    if os.path.exists(DB_PATH):
        safety = os.path.join(
            BACKUP_DIR, f"pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
        try:
            snapshot(DB_PATH, safety)
            print(f"    当前数据库已另存为：{os.path.basename(safety)}"
                  f"（{count_tables(safety)} 张表）")
        except Exception as exc:
            print(f"    警告：当前数据库备份失败：{exc}")
            safety = None
            if input("备份失败，仍要继续恢复？输入 yes 继续：").strip().lower() != "yes":
                print("已取消，正在重启服务...")
                start_server()
                return

    # 覆盖
    try:
        restore_from(chosen_path)
    except Exception as exc:
        print(f"    恢复失败：{exc}")
        _rollback(safety)
        start_server()
        input("按回车键退出...")
        return

    # 校验，不通过就回滚
    restored_tables = count_tables(DB_PATH)
    if restored_tables <= 0 or not integrity_ok(DB_PATH):
        print(f"    恢复后校验失败（表数量 {restored_tables}）")
        _rollback(safety)
        start_server()
        input("按回车键退出...")
        return

    print(f"    已用 {chosen} 恢复完成，当前数据库 {restored_tables} 张表，完整性检查通过")

    start_server()
    print("\n=== 恢复完成 ===")
    input("按回车键退出...")


def _rollback(safety):
    """恢复失败时，用刚做的安全快照还原，避免留下半截数据库。"""
    if not safety or not os.path.exists(safety):
        print("    没有可用的安全快照，无法自动回滚！")
        return
    try:
        restore_from(safety)
        print("    已自动回滚到恢复前的数据库")
    except Exception as exc:
        print(f"    自动回滚也失败了：{exc}")
        print(f"    请手动把 {safety} 复制为 instance/reagent.db")


if __name__ == "__main__":
    main()
