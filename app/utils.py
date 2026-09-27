"""通用小工具。"""

from __future__ import annotations

import html
import time
import urllib.parse
from typing import Any, Optional

from fastapi.responses import RedirectResponse


def now_ms() -> int:
    """当前时间戳（毫秒）。所有对客户端暴露的 timestamp 字段都用毫秒。"""
    return int(time.time() * 1000)


def human_size(num_bytes: Optional[int]) -> str:
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def format_time(ms: Optional[int]) -> str:
    if not ms:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))


def redirect_with(url: str, ok: Optional[str] = None, err: Optional[str] = None) -> RedirectResponse:
    """303 跳转，并附带一条提示消息（走查询串，无需 session 存储）。"""
    params = {}
    if ok:
        params["ok"] = ok
    if err:
        params["err"] = err
    if params:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urllib.parse.urlencode(params)}"
    return RedirectResponse(url, status_code=303)


def escape(text: Any) -> str:
    return html.escape("" if text is None else str(text))
