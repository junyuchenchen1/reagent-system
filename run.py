# -*- coding: utf-8 -*-
"""
检验科试剂管理系统 - 启动入口（生产模式）
================================
使用 Waitress 生产级服务器（多线程，适合局域网多台电脑同时访问）。
默认使用 MySQL；若未检测到可用的 MySQL，会自动降级为 SQLite，
保证项目“开箱即用”。设置环境变量 DB_TYPE 可强制指定：
    DB_TYPE=mysql   强制使用 MySQL
    DB_TYPE=sqlite  强制使用 SQLite

可通过环境变量自定义监听地址与端口：
    HOST=0.0.0.0  PORT=8080  python run.py
"""
import os
import sys

from waitress import serve
from app import create_app

# 日志实时落盘：重定向到 logs\server.log 时默认有缓冲，出错堆栈可能一直卡在内存里
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

app = create_app()

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 5000))
THREADS = int(os.environ.get("THREADS", 8))

if __name__ == "__main__":
    print("=" * 60)
    print("  检验科试剂管理系统已启动（Waitress 生产服务器）")
    print(f"  局域网访问地址： http://{HOST}:{PORT}  （同网段电脑用本机IP访问）")
    print("  当前数据库：", app.config.get("DB_TYPE_ACTUAL", "未知").upper())
    print("=" * 60)
    # debug 模式仅用于开发调试，生产/局域网访问必须关闭
    serve(app, host=HOST, port=PORT, threads=THREADS)
