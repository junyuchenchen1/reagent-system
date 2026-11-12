# -*- coding: utf-8 -*-
"""轻量安全辅助：CSRF 防护与站内跳转校验。"""
import secrets
from urllib.parse import urljoin, urlparse

from flask import abort, request, session


def csrf_token() -> str:
    """为当前会话生成并复用 CSRF token。"""
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def validate_csrf() -> None:
    """验证所有会改变服务器状态的浏览器请求。"""
    expected = session.get("_csrf_token")
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        abort(400, description="请求校验失败，请刷新页面后重试")


def is_safe_next_url(target: str | None) -> bool:
    if not target:
        return False
    base = urlparse(request.host_url)
    candidate = urlparse(urljoin(request.host_url, target))
    return candidate.scheme in ("http", "https") and candidate.netloc == base.netloc
