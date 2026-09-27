"""对外 JSON 结构。

后台的「API 预览」与公开接口共用这里的函数，保证界面显示的就是真实返回。
"""

from __future__ import annotations

from typing import Any, Dict

from .utils import now_ms


def release_payload(release: Dict[str, Any], slug: str, base_url: str) -> Dict[str, Any]:
    """更新检查接口的响应体。

    versionName / versionCode / desc / timestamp / download 是对外承诺的稳定字段，
    其余字段为增量补充，客户端可忽略。
    """
    version_code = int(release["version_code"])
    return {
        "versionName": release["version_name"],
        "versionCode": version_code,
        "desc": release["description"] or "",
        "timestamp": int(release["released_at"]),
        "download": f"{base_url}/{slug}/build/{version_code}",
        # ---------- 以下为增量字段 ----------
        "appName": release.get("app_name") or None,
        "size": int(release["build_size"] or 0),
        "sha256": release["build_sha256"] or "",
        "forceUpdate": bool(release["force_update"]),
    }


def notice_payload(notice: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "title": notice["title"] or "",
        "content": notice["content"] or "",
        "timestamp": int(notice["published_at"]),
        "id": int(notice["id"]),
    }


def release_list_payload(releases: list, slug: str, base_url: str) -> Dict[str, Any]:
    return {
        "slug": slug,
        "count": len(releases),
        "generatedAt": now_ms(),
        "releases": [release_payload(row, slug, base_url) for row in releases],
    }


def notice_list_payload(notices: list) -> Dict[str, Any]:
    return {
        "count": len(notices),
        "generatedAt": now_ms(),
        "notices": [notice_payload(row) for row in notices],
    }
