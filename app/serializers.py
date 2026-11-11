"""对外 JSON 结构。

后台的「API 预览」与公开接口共用这里的函数，保证界面显示的就是真实返回。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .utils import now_ms


def _stem(filename: str) -> str:
    name = (filename or "").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def release_file_entries(
    release: Dict[str, Any],
    slug: str,
    base_url: str,
    extras: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """一个版本的全部产物：主产物（历史 build 字段）+ 附加产物。

    单文件版本的 files 恰好一条，download 与旧的 download 字段完全一致；
    多文件版本里第一条是主产物，其余各带独立下载地址。
    """
    version_code = int(release["version_code"])
    primary_name = release.get("build_name") or ""
    primary_label = (release.get("build_label") or "").strip() or _stem(primary_name) or "主产物"
    entries: List[Dict[str, Any]] = [
        {
            "id": None,
            "label": primary_label,
            "name": primary_name,
            "size": int(release["build_size"] or 0),
            "sha256": release["build_sha256"] or "",
            "contentType": release["content_type"] or "application/octet-stream",
            "primary": True,
            "download": f"{base_url}/{slug}/build/{version_code}",
        }
    ]
    for row in extras or []:
        entries.append(
            {
                "id": int(row["id"]),
                "label": row["label"] or _stem(row["original_name"] or "") or f"文件 {row['id']}",
                "name": row["original_name"] or "",
                "size": int(row["size"] or 0),
                "sha256": row["sha256"] or "",
                "contentType": row["content_type"] or "application/octet-stream",
                "primary": False,
                "download": f"{base_url}/{slug}/build/{version_code}/files/{row['id']}",
            }
        )
    return entries


def release_payload(
    release: Dict[str, Any],
    slug: str,
    base_url: str,
    extras: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
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
        # 多文件版本（手机 + 手表等）。单文件版本里恰好一条，即上面的 download。
        "files": release_file_entries(release, slug, base_url, extras),
    }


def notice_payload(notice: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "title": notice["title"] or "",
        "content": notice["content"] or "",
        "timestamp": int(notice["published_at"]),
        "id": int(notice["id"]),
    }


def release_list_payload(
    releases: list,
    slug: str,
    base_url: str,
    limit: int,
    files_by_release: Optional[Dict[int, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """列表响应。count 是本次实际返回条数，limit 是服务端上限；
    两者相等时说明可能还有更早的记录没返回。"""
    files_by_release = files_by_release or {}
    return {
        "slug": slug,
        "count": len(releases),
        "limit": limit,
        "generatedAt": now_ms(),
        "releases": [
            release_payload(row, slug, base_url, files_by_release.get(row["id"]))
            for row in releases
        ],
    }


def notice_list_payload(notices: list, limit: int) -> Dict[str, Any]:
    return {
        "count": len(notices),
        "limit": limit,
        "generatedAt": now_ms(),
        "notices": [notice_payload(row) for row in notices],
    }
