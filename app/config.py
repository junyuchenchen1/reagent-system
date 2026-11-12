# -*- coding: utf-8 -*-
"""系统配置文件：数据库连接、会话、上传/导出路径等"""
import os
import secrets
from sqlalchemy.engine import URL
from datetime import timedelta

# 项目根目录
BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))


def _load_or_create_secret_key() -> str:
    """读取 instance/secret_key；不存在则生成随机密钥并写入文件。
    这样重启服务后登录态不丢失，且密钥不硬编码在源码里。"""
    key_file = os.path.join(BASE_DIR, "instance", "secret_key")
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    if os.path.exists(key_file):
        with open(key_file, "r", encoding="utf-8") as f:
            k = f.read().strip()
            if k:
                return k
    # 生成新密钥
    os.makedirs(os.path.dirname(key_file), exist_ok=True)
    new_key = secrets.token_hex(32)
    with open(key_file, "w", encoding="utf-8") as f:
        f.write(new_key)
    return new_key


class Config:
    # 项目根目录（类属性，便于 Config.BASE_DIR 引用）
    BASE_DIR = BASE_DIR

    # ---------------- MySQL 连接配置（请按本机实际修改） ----------------
    MYSQL_USER = os.environ.get("MYSQL_USER", "root")
    # 生产环境必须通过环境变量提供密码；不再把 root/root 当作可用默认值。
    MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
    MYSQL_HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT = int(os.environ.get("MYSQL_PORT", 3306))
    MYSQL_DB = os.environ.get("MYSQL_DB", "reagent_db")

    # 数据库类型：auto（自动探测）/ mysql / sqlite
    DB_TYPE = os.environ.get("DB_TYPE", "auto")

    # ---------------- Flask 基础配置 ----------------
    SECRET_KEY = _load_or_create_secret_key()
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,   # 连接前检测有效性，避免断连
        "pool_recycle": 3600,
        "pool_size": 10,
        "max_overflow": 20,
    }

    # ---------------- 文件 / 导出 ----------------
    EXPORT_FOLDER = os.path.join(BASE_DIR, "exports")
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 单文件最大 16MB

    # ---------------- 业务默认参数 ----------------
    DEFAULT_EXPIRE_WARNING_DAYS = 90   # 默认效期预警天数
    DEFAULT_LOW_STOCK = 10             # 默认最低库存

    @classmethod
    def mysql_uri(cls, with_db: bool = True) -> str:
        return URL.create(
            "mysql+pymysql", username=cls.MYSQL_USER,
            password=cls.MYSQL_PASSWORD, host=cls.MYSQL_HOST,
            port=cls.MYSQL_PORT, database=cls.MYSQL_DB if with_db else None,
            query={"charset": "utf8mb4"},
        ).render_as_string(hide_password=False)

    @classmethod
    def sqlite_uri(cls) -> str:
        # 默认 instance/reagent.db；环境变量 SQLITE_PATH 可覆盖（自动化测试、临时实例用）
        path = os.environ.get("SQLITE_PATH") or os.path.join(
            BASE_DIR, "instance", "reagent.db")
        return "sqlite:///" + path.replace("\\", "/")
