"""分页参数容错与统一分页。

历史分页宏在页码超限时会生成 `?page=None` 之类的链接，直接 `int()` 解析会抛
ValueError 导致 500；这里统一把非法页码回落为第 1 页，并把超出末页的页码夹到末页。
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


def clamp_page(page: int, pages: int) -> int:
    """把页码夹到 [1, pages]，pages 至少按 1 计。"""
    pages = max(1, pages)
    return min(max(1, page), pages)


def paginate_query(query, per_page: int = 15):
    """统一分页：非法页码回落第 1 页，超出末页自动夹到末页（避免空列表）。"""
    page = page_arg()
    pg = query.paginate(page=page, per_page=per_page, error_out=False)
    if page > pg.pages >= 1:
        pg = query.paginate(page=pg.pages, per_page=per_page, error_out=False)
    return pg
