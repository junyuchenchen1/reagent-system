# -*- coding: utf-8 -*-
"""角色权限装饰器"""
from functools import wraps
from flask import abort
from flask_login import current_user


def roles_required(*roles):
    """要求当前用户属于指定角色之一，否则 403"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if not current_user.has_role(*roles):
                abort(403)
            return func(*args, **kwargs)
        return wrapper
    return decorator


def manager_required(func):
    """管理员 / 试剂管理员 可访问"""
    return roles_required("admin", "manager")(func)


def admin_required(func):
    """仅系统管理员可访问"""
    return roles_required("admin")(func)
