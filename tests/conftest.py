# -*- coding: utf-8 -*-
"""pytest 公共夹具。

所有测试都跑在临时 SQLite 库上（SQLITE_PATH 指向系统临时目录），
绝不会碰到真实库 instance/reagent.db。
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须在 import app 之前设置：Config 在导入时就读取环境变量
_TMP_DIR = Path(tempfile.mkdtemp(prefix="reagent_test_"))
os.environ["DB_TYPE"] = "sqlite"
os.environ["SQLITE_PATH"] = str(_TMP_DIR / "test.db")

CRED_FILE = ROOT / "instance" / "initial_admin_password.txt"


@pytest.fixture(scope="session")
def app():
    from app import create_app

    # 只清理本次测试新建的一次性密码文件，绝不删除真实部署里原有的文件
    existed = CRED_FILE.exists()
    application = create_app()
    application.config.update(TESTING=True)
    if not existed and CRED_FILE.exists():
        CRED_FILE.unlink()
    return application


@pytest.fixture()
def db_session(app):
    """每个用例一套干净的表结构。"""
    from app.extensions import db

    with app.app_context():
        db.drop_all()
        db.create_all()
        yield db
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def users(db_session):
    from app.models.user import User

    made = {}
    for username, role, real_name in [
        ("admin", "admin", "管理员"),
        ("mgr", "manager", "李试剂"),
        ("stf", "staff", "王检验"),
    ]:
        u = User(username=username, real_name=real_name, role=role, status=1)
        u.set_password("pass123")
        db_session.session.add(u)
        made[username] = u
    db_session.session.commit()
    return made


@pytest.fixture()
def reagent(db_session):
    from app.models.reagent import Reagent

    r = Reagent(name="葡萄糖(GLU)测定试剂盒", spec="R1 40ml×4",
                pack_size=12, small_unit="瓶", unit="盒",
                min_stock=5, expire_warning_days=90, storage_condition="冷藏")
    db_session.session.add(r)
    db_session.session.commit()
    return r


@pytest.fixture()
def client(app, users):
    """已登录 admin 的测试客户端，并预置 CSRF token。"""
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(users["admin"].id)
        sess["_fresh"] = True
        sess["_csrf_token"] = "test-token"
    return c
