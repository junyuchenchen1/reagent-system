# -*- coding: utf-8 -*-
"""
应用工厂：创建并配置 Flask 应用
- 自动探测 MySQL，不可用时降级 SQLite（保证开箱即用）
- 注册蓝图、登录管理、全局上下文
"""
import os
import secrets
import pymysql
from flask import Flask, request, render_template
from app.config import Config
from app.extensions import db, login_manager
from app.utils.security import csrf_token, validate_csrf


def _ensure_mysql() -> bool:
    """连接 MySQL 服务，创建数据库（如不存在）。成功返回 True。"""
    try:
        conn = pymysql.connect(
            host=Config.MYSQL_HOST,
            port=Config.MYSQL_PORT,
            user=Config.MYSQL_USER,
            password=Config.MYSQL_PASSWORD,
            charset="utf8mb4",
            connect_timeout=3,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{Config.MYSQL_DB}` "
                    f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
                )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:  # 连接失败：账号密码错误 / 服务未启动等
        print(f"[数据库] MySQL 不可用：{exc}")
        return False


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    os.makedirs(app.config["EXPORT_FOLDER"], exist_ok=True)
    os.makedirs(os.path.join(Config.BASE_DIR, "instance"), exist_ok=True)

    # ---------- 错误日志：把 500 的完整堆栈写入 logs/error.log ----------
    # 用 VBS / restore.py 静默启动时 stderr 不会落盘，没有这份日志就只
    # 能看到“Internal Server Error”，查不到真正原因。
    import logging
    from logging.handlers import RotatingFileHandler

    _log_dir = os.path.join(Config.BASE_DIR, "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _err_handler = RotatingFileHandler(
        os.path.join(_log_dir, "error.log"),
        maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    _err_handler.setLevel(logging.ERROR)
    _err_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"))
    app.logger.addHandler(_err_handler)

    # ---------- 决定数据库 ----------
    db_type = app.config["DB_TYPE"]
    if db_type == "auto":
        db_type = "mysql" if _ensure_mysql() else "sqlite"
    elif db_type == "mysql":
        # 强制 mysql，也尝试确保数据库存在；失败则报错提示
        if not _ensure_mysql():
            raise RuntimeError(
                "已强制 DB_TYPE=mysql，但无法连接 MySQL，请检查服务与账号密码，"
                "或改用环境变量 DB_TYPE=sqlite 体验。"
            )

    if db_type == "mysql":
        if not app.config["MYSQL_PASSWORD"]:
            raise RuntimeError("DB_TYPE=mysql 时必须设置 MYSQL_PASSWORD 环境变量")
        app.config["SQLALCHEMY_DATABASE_URI"] = Config.mysql_uri()
    else:
        app.config["SQLALCHEMY_DATABASE_URI"] = Config.sqlite_uri()
    app.config["DB_TYPE_ACTUAL"] = db_type
    if db_type == "sqlite":
        # SQLite 仅适合单机轻量使用；延长锁等待，减少多人同时提交时报错。
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"connect_args": {"timeout": 15}}

    # ---------- 初始化扩展 ----------
    db.init_app(app)
    login_manager.init_app(app)

    # ---------- SQLite 连接参数（单机 / 局域网稳定性）----------
    # 默认 SQLite 不启用外键约束、写时锁整库。以下参数让单机部署更稳：
    #   foreign_keys=ON  外键真正生效，避免删数据留下孤儿记录
    #   journal_mode=WAL 读写不互相阻塞（查询时不挡写入）
    #   busy_timeout     写锁冲突时等待而不是立刻报 database is locked
    if db_type == "sqlite":
        from sqlalchemy import event

        with app.app_context():
            _sqlite_engine = db.engine

        @event.listens_for(_sqlite_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _connection_record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=15000")
            cur.close()

    @app.before_request
    def protect_csrf():
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            validate_csrf()

    from app.models.user import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    # ---------- 注册蓝图 ----------
    from app.routes.auth import auth_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.basic import basic_bp
    from app.routes.stock_in import stock_in_bp
    from app.routes.stock_out import stock_out_bp
    from app.routes.usage import usage_bp
    from app.routes.inventory import inventory_bp
    from app.routes.admin import admin_bp
    from app.routes.report import report_bp
    from app.routes.databackup import databackup_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(basic_bp, url_prefix="/basic")
    app.register_blueprint(stock_in_bp, url_prefix="/stock-in")
    app.register_blueprint(stock_out_bp, url_prefix="/stock-out")
    app.register_blueprint(usage_bp)
    app.register_blueprint(inventory_bp, url_prefix="/inventory")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(report_bp, url_prefix="/report")
    app.register_blueprint(databackup_bp, url_prefix="/databackup")

    # ---------- 静态资源缓存（提升局域网访问速度） ----------
    # css/js/字体等静态文件在浏览器端缓存 7 天，Win7 客户端第二次访问
    # 无需重新下载，页面加载显著加快。
    import re as _re

    _CACHE_TYPES = {
        "css": "text/css; charset=utf-8",
        "js": "application/javascript; charset=utf-8",
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "svg": "image/svg+xml",
        "woff": "font/woff", "woff2": "font/woff2", "ttf": "font/ttf",
        "ico": "image/x-icon",
    }

    @app.after_request
    def add_static_cache_headers(response):
        # 仅对静态文件生效，动态页面仍走默认（不缓存）
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=604800, immutable"
            m = _re.search(r"\.([a-zA-Z0-9]+)$", request.path)
            if m and m.group(1).lower() in _CACHE_TYPES:
                response.headers["Content-Type"] = _CACHE_TYPES[m.group(1).lower()]
        return response

    # ---------- 全局上下文（模板可直接使用） ----------
    @app.context_processor
    def inject_globals():
        from datetime import datetime
        from app.models.stock import StockIn, StockOut
        return {
            "APP_NAME": "检验科试剂管理系统",
            "DB_TYPE_ACTUAL": app.config.get("DB_TYPE_ACTUAL", ""),
            "NOW": datetime.now(),
            # 静态资源版本号：修改 css/js 后把数字 +1，浏览器即强制刷新缓存
            "STATIC_VERSION": "5",
            "csrf_token": csrf_token,
            "pending_in": StockIn.query.filter_by(
                status=StockIn.STATUS_PENDING).count(),
            "pending_out": StockOut.query.filter_by(
                status=StockOut.STATUS_PENDING).count(),
        }

    # ---------- 简易全局错误处理 ----------
    @app.errorhandler(403)
    def forbidden(e):
        from flask import render_template
        return render_template("errors.html", code=403,
                               message="抱歉，您没有权限访问该页面"), 403

    @app.errorhandler(404)
    def not_found(e):
        from flask import render_template
        return render_template("errors.html", code=404,
                               message="抱歉，您访问的页面不存在"), 404

    @app.errorhandler(400)
    def bad_request(e):
        return render_template("errors.html", code=400,
                               message=getattr(e, "description", "请求无效")), 400

    # 自动建表（首次运行）
    with app.app_context():
        db.create_all()

        # 自动补列：旧数据库升级时新增的字段自动 ALTER TABLE 加上
        import sqlite3 as _sq
        _db_path = app.config.get("SQLALCHEMY_DATABASE_URI", "").replace("sqlite:///", "")
        if _db_path and os.path.exists(_db_path):
            _conn = _sq.connect(_db_path)
            _c = _conn.cursor()
            def _ensure_col(table, col, ddl):
                _c.execute(f"PRAGMA table_info({table})")
                existing = [r[1] for r in _c.fetchall()]
                if col not in existing:
                    _c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                    print(f"[自动迁移] {table} 新增列 {col}")
            _ensure_col("sys_user", "signature_image", "VARCHAR(255)")
            _ensure_col("reagent_info", "reg_no", "VARCHAR(100)")
            _ensure_col("reagent_info", "reg_expire_date", "DATE")
            _ensure_col("reagent_info", "registrant", "VARCHAR(100)")
            _ensure_col("reagent_info", "agent", "VARCHAR(100)")
            _ensure_col("reagent_info", "cert_image", "VARCHAR(255)")
            _ensure_col("reagent_info", "min_stock_unit", "VARCHAR(10) DEFAULT 'pack'")
            _ensure_col("stock_in", "udi_di", "VARCHAR(32)")
            _ensure_col("supplier", "license_expire_date", "DATE")
            _ensure_col("supplier", "supplier_type", "VARCHAR(20) DEFAULT '经销商'")
            _ensure_col("supplier", "cert_image", "VARCHAR(255)")

            # 修复历史数据里遗留的空字符串日期：SQLite 的 DATE/DATETIME 列
            # 一旦存了 ''，SQLAlchemy 读取时会抛 ValueError: Invalid
            # isoformat string ''，对应页面直接 500（恢复旧备份后最常见）。
            for (_tbl,) in _c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                if _tbl.startswith("sqlite_"):
                    continue
                _cols = _c.execute(f'PRAGMA table_info("{_tbl}")').fetchall()
                for _col, _type in [(r[1], (r[2] or "").upper()) for r in _cols]:
                    if _type not in ("DATE", "DATETIME", "TIMESTAMP"):
                        continue
                    _fixed = _c.execute(
                        f'UPDATE "{_tbl}" SET "{_col}"=NULL '
                        f'WHERE "{_col}" IS NOT NULL AND ('
                        f'TRIM("{_col}")=\'\' OR "{_col}" LIKE \'0000-00-00%\')'
                    ).rowcount
                    if _fixed:
                        print(f"[数据修复] {_tbl}.{_col}：{_fixed} 条空日期已置为 NULL")
            _conn.commit()
            _conn.close()

        # 库存汇总自校准：按实际在库盒重算 Inventory.quantity
        # （历史版本/恢复旧备份后可能出现“汇总还有 1 盒、实际一盒都不在库”）
        from app.services.stock_service import reconcile_inventory
        _fixed = reconcile_inventory()
        if _fixed:
            print(f"[库存校准] 已按实际在库盒修正 {_fixed} 个批次的库存汇总")

        # 存放位置字典：首次运行且为空时插入常用位置，便于入库直接选择
        from app.models.reagent import StorageLocation
        if StorageLocation.query.count() == 0:
            defaults = [
                ("常温试剂柜", 1, 1, "室温 15~30℃"),
                ("2-8℃ 冷藏冰箱", 2, 1, "冷藏保存"),
                ("-20℃ 冷冻冰柜", 3, 1, "冷冻保存"),
                ("阴凉柜", 4, 1, "阴凉避光"),
            ]
            for name, sort, status, remark in defaults:
                db.session.add(
                    StorageLocation(name=name, sort=sort,
                                    status=status, remark=remark))
            db.session.commit()

        # 首次运行创建管理员。密码为随机一次性密码，避免固定默认口令。
        from app.models.user import User
        if User.query.count() == 0:
            admin = User(
                username="admin", real_name="系统管理员",
                role=User.ROLE_ADMIN, status=1,
            )
            initial_password = secrets.token_urlsafe(12)
            admin.set_password(initial_password)
            db.session.add(admin)
            db.session.commit()
            # VBS 隐藏启动时 stdout 可能缓冲，不能只依赖 server.log。
            # 凭据文件仅在首次建库时生成，并在首次成功登录后自动删除。
            credential_file = os.path.join(Config.BASE_DIR, "instance", "initial_admin_password.txt")
            with open(credential_file, "w", encoding="utf-8") as f:
                f.write("检验科试剂管理系统 - 首次部署管理员凭据\n")
                f.write("账号：admin / admin\n")
                f.write(f"密码：{initial_password}\n\n")
                f.write("请立即登录并修改密码。首次登录成功后，本文件会自动删除。\n")
            print(f"[初始化] 已创建管理员账号 admin / {initial_password}，请立即登录并修改密码", flush=True)

    return app
