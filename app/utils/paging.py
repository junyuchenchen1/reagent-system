"""分页参数容错。

历史分页宏在页码超限时会生成 `?page=None` 之类的链接，直接 `int()` 解析会抛
ValueError 导致 500；这里统一把非法页码回落为第 1 页。
"""

from flask import request


def page_arg(name: str = "page") -> int:
    """取页码参数，非法值（空/None/非数字/小于 1）一律返回 1。"""
    raw = request.args.get(name, "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 1
    return value if value >= 1 else 1
