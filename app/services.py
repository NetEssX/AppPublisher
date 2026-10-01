"""网页后台与 JSON API 共用的领域操作。

两套入口（`/admin` 的表单、`/api/v1` 的 JSON）**刻意共用这里的实现**。
各写一份的话，「网页里能改、API 里不能改」这类规则漂移迟早会发生 ——
这个项目已经因为双份状态吃过一次亏（apps.banner_path 与 assets 表）。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, UploadFile

from . import config, db, storage
from .utils import now_ms

logger = logging.getLogger("apppublisher.services")


class ServiceError(Exception):
    """领域校验失败。

    status 供 JSON API 直接用作 HTTP 状态码；网页端只取 message 做提示。
    """

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def normalize_text(raw: str, limit: int) -> str:
    """折叠空白并截断，用于 tagline / 备注这类单行短文本。"""
    return " ".join((raw or "").split())[:limit]


# ---------------------------------------------------------------- 归属与权限


def can_manage(user: Dict[str, Any], app: Dict[str, Any]) -> bool:
    """超管可管所有应用；其他人只能管自己创建的。

    归属为 NULL 的历史应用只有超管能动 —— 用 scripts/claim_apps.py 认领归属。
    """
    if user.get("is_super"):
        return True
    return app.get("owner_id") is not None and app["owner_id"] == user["id"]


def visible_apps(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    """当前账号能看到的应用列表。"""
    if user.get("is_super"):
        return db.query_all("SELECT * FROM apps ORDER BY id DESC")
    return db.query_all("SELECT * FROM apps WHERE owner_id = ? ORDER BY id DESC", (user["id"],))


def require_app(app_id: int, user: Dict[str, Any]) -> Dict[str, Any]:
    """取应用并校验权限。

    无权时按 404 处理而不是 403：不向对方泄露「这个应用存在，只是不属于你」。
    """
    app = db.query_one("SELECT * FROM apps WHERE id = ?", (app_id,))
    if app is None or not can_manage(user, app):
        raise ServiceError("应用不存在或无权访问", status=404)
    return app


def find_app_by_slug(slug: str, user: Dict[str, Any]) -> Dict[str, Any]:
    app = db.query_one("SELECT * FROM apps WHERE slug = ?", (slug,))
    if app is None or not can_manage(user, app):
        raise ServiceError("应用不存在或无权访问", status=404)
    return app


# ---------------------------------------------------------------- 应用


def create_app(
    *,
    slug: str,
    name: str,
    tagline: str = "",
    intro_html: str = "",
    enabled: bool = True,
    owner_id: Optional[int] = None,
) -> int:
    try:
        slug = config.validate_slug(slug)
    except ValueError as exc:
        raise ServiceError(str(exc))

    name = (name or "").strip()
    if not name:
        raise ServiceError("应用名称不能为空")
    if db.query_one("SELECT id FROM apps WHERE slug = ?", (slug,)):
        raise ServiceError(f"短链 /{slug} 已被占用")

    intro_html = intro_html or ""
    if len(intro_html.encode("utf-8")) > config.MAX_INTRO_BYTES:
        raise ServiceError(f"介绍页 HTML 超过 {config.MAX_INTRO_MB} MB 上限", status=413)

    timestamp = now_ms()
    try:
        return db.execute(
            "INSERT INTO apps (slug, name, tagline, intro_html, banner_path, owner_id,"
            " enabled, created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (
                slug,
                name,
                normalize_text(tagline, 200),
                intro_html,
                owner_id,
                1 if enabled else 0,
                timestamp,
                timestamp,
            ),
        )
    except sqlite3.IntegrityError:
        # 上面的查重与这里的插入之间有窗口，真正兜底的是 slug 上的 UNIQUE 约束。
        raise ServiceError(f"短链 /{slug} 已被占用")


def update_app(
    app_id: int,
    user: Dict[str, Any],
    *,
    slug: str,
    name: str,
    tagline: str = "",
    intro_html: str = "",
    enabled: bool = True,
) -> None:
    require_app(app_id, user)

    try:
        slug = config.validate_slug(slug)
    except ValueError as exc:
        raise ServiceError(str(exc))

    name = (name or "").strip()
    if not name:
        raise ServiceError("应用名称不能为空")

    if db.query_one("SELECT id FROM apps WHERE slug = ? AND id <> ?", (slug, app_id)):
        raise ServiceError(f"短链 /{slug} 已被其它应用占用")

    intro_html = intro_html or ""
    if len(intro_html.encode("utf-8")) > config.MAX_INTRO_BYTES:
        raise ServiceError(f"介绍页 HTML 超过 {config.MAX_INTRO_MB} MB 上限", status=413)

    db.execute(
        "UPDATE apps SET slug = ?, name = ?, tagline = ?, intro_html = ?, enabled = ?, "
        "updated_at = ? WHERE id = ?",
        (
            slug,
            name,
            normalize_text(tagline, 200),
            intro_html,
            1 if enabled else 0,
            now_ms(),
            app_id,
        ),
    )


def delete_app(app_id: int, user: Dict[str, Any]) -> str:
    """删除应用及其全部产物文件。返回被删应用的名字。"""
    app = require_app(app_id, user)

    # 需要清理的磁盘文件：构建产物 + 封面 + 该应用登记的图片。
    # assets 表里的行会随外键级联删除，但文件系统不受外键管辖。
    paths = [
        row["build_path"]
        for row in db.query_all("SELECT build_path FROM releases WHERE app_id = ?", (app_id,))
    ]
    if app["banner_path"]:
        paths.append(app["banner_path"])
    paths.extend(
        row["path"] for row in db.query_all("SELECT path FROM assets WHERE app_id = ?", (app_id,))
    )

    db.execute("DELETE FROM apps WHERE id = ?", (app_id,))
    # 记录先删干净再清磁盘；反过来若中途失败会留下悬空记录。
    # 单个文件删不掉（权限等）不该让整个请求失败，记日志继续。
    for path in paths:
        try:
            storage.delete(path)
        except OSError:
            logger.warning("删除文件失败，磁盘上可能残留 %s", path, exc_info=True)
    return app["name"]


# ---------------------------------------------------------------- 媒体


def _save(upload: UploadFile, subdir: str, extensions, max_bytes: int):
    try:
        return storage.save_upload(upload, subdir, extensions, max_bytes)
    except HTTPException as exc:
        raise ServiceError(str(exc.detail), status=exc.status_code)


def add_image(app_id: int, user: Dict[str, Any], upload: UploadFile) -> Dict[str, Any]:
    require_app(app_id, user)
    rel_path, size, _ = _save(upload, "images", config.IMAGE_EXTENSIONS, config.MAX_IMAGE_BYTES)
    try:
        asset_id = db.execute(
            "INSERT INTO assets (app_id, path, original_name, size, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (app_id, rel_path, storage.safe_display_name(upload.filename, "image"), size, now_ms()),
        )
    except Exception:
        # 登记失败就把刚落盘的文件清掉，否则它永远不出现在列表里，变成看不见的孤儿。
        storage.delete(rel_path)
        logger.exception("登记图片资源失败 app_id=%s path=%s", app_id, rel_path)
        raise ServiceError("保存失败，请查看服务端日志", status=500)
    return {"id": asset_id, "path": rel_path, "size": size}


def delete_image(app_id: int, user: Dict[str, Any], asset_id: int) -> Dict[str, Any]:
    require_app(app_id, user)
    asset = db.query_one("SELECT * FROM assets WHERE id = ? AND app_id = ?", (asset_id, app_id))
    if asset is None:
        raise ServiceError("图片不存在", status=404)
    db.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    try:
        storage.delete(asset["path"])
    except OSError:
        logger.warning("删除图片文件失败，磁盘上可能残留 %s", asset["path"], exc_info=True)
    return asset


def set_banner(app_id: int, user: Dict[str, Any], upload: UploadFile) -> Dict[str, Any]:
    app = require_app(app_id, user)
    rel_path, size, _ = _save(upload, "images", config.IMAGE_EXTENSIONS, config.MAX_IMAGE_BYTES)
    previous = app["banner_path"]
    try:
        db.execute(
            "UPDATE apps SET banner_path = ?, updated_at = ? WHERE id = ?",
            (rel_path, now_ms(), app_id),
        )
    except Exception:
        # 入库失败就把刚落盘的文件清掉，否则没人引用它，成了看不见的孤儿文件。
        storage.delete(rel_path)
        logger.exception("更新封面失败 app_id=%s path=%s", app_id, rel_path)
        raise ServiceError("保存失败，请查看服务端日志", status=500)
    # 先换引用再删旧文件：反过来若删失败，banner_path 会指向已不存在的文件，
    # 前台就成了裂图。留个孤儿文件只是浪费一点磁盘。
    if previous:
        try:
            storage.delete(previous)
        except OSError:
            logger.warning("删除旧 banner 失败，磁盘上可能残留 %s", previous, exc_info=True)
    return {"path": rel_path, "size": size}


def clear_banner(app_id: int, user: Dict[str, Any]) -> None:
    app = require_app(app_id, user)
    if not app["banner_path"]:
        raise ServiceError("当前没有封面图")
    db.execute("UPDATE apps SET banner_path = NULL, updated_at = ? WHERE id = ?", (now_ms(), app_id))
    try:
        storage.delete(app["banner_path"])
    except OSError:
        logger.warning("删除 banner 文件失败，磁盘上可能残留 %s", app["banner_path"], exc_info=True)


# ---------------------------------------------------------------- 发行版


def parse_version_code(raw: Any) -> int:
    try:
        code = int(str(raw).strip())
        if code <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ServiceError("版本号必须是正整数")
    return code


def create_release(
    app_id: int,
    user: Dict[str, Any],
    *,
    version_name: str,
    version_code: Any,
    description: str = "",
    force_update: bool = False,
    make_latest: bool = False,
    released_at_ms: Optional[int] = None,
    upload: UploadFile,
) -> Dict[str, Any]:
    require_app(app_id, user)

    version_name = (version_name or "").strip()
    if not version_name:
        raise ServiceError("版本名不能为空")
    code = parse_version_code(version_code)

    if db.query_one("SELECT id FROM releases WHERE app_id = ? AND version_code = ?", (app_id, code)):
        raise ServiceError(f"版本号 {code} 已存在，请先删除旧记录")

    # 查重必须发生在落盘之前，否则被拒的请求会留下孤儿文件。
    rel_path, size, sha256 = _save(upload, "build", config.BUILD_EXTENSIONS, config.MAX_BUILD_BYTES)

    existing = db.query_value("SELECT COUNT(*) FROM releases WHERE app_id = ?", (app_id,)) or 0
    mark_latest = bool(make_latest) or existing == 0
    timestamp = now_ms()

    try:
        with db.transaction() as conn:
            if mark_latest:
                db.execute_tx(conn, "UPDATE releases SET is_latest = 0 WHERE app_id = ?", (app_id,))
            release_id = db.execute_tx(
                conn,
                "INSERT INTO releases (app_id, version_name, version_code, description, build_path,"
                " build_name, build_size, build_sha256, content_type, force_update, is_latest,"
                " released_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    app_id,
                    version_name,
                    code,
                    description or "",
                    rel_path,
                    storage.safe_display_name(upload.filename, f"{code}.bin"),
                    size,
                    sha256,
                    storage.content_type_for(upload.filename),
                    1 if force_update else 0,
                    1 if mark_latest else 0,
                    released_at_ms if released_at_ms is not None else timestamp,
                    timestamp,
                ),
            )
    except sqlite3.IntegrityError:
        # 查重与插入之间隔着一整次文件上传，窗口很宽（CI 重试、表单重复提交）。
        # 真正的兜底是 UNIQUE(app_id, version_code)，这属于普通校验失败，不是 500。
        storage.delete(rel_path)
        raise ServiceError(f"版本号 {code} 已存在，请先删除旧记录")
    except Exception:
        # 入库失败就把刚落盘的产物清掉，避免留下孤儿文件。
        storage.delete(rel_path)
        logger.exception("写入发行版失败 app_id=%s version_code=%s", app_id, code)
        raise ServiceError("保存失败，请查看服务端日志", status=500)

    return {
        "id": release_id,
        "version_name": version_name,
        "version_code": code,
        "size": size,
        "sha256": sha256,
        "is_latest": mark_latest,
    }


def update_release(
    app_id: int,
    user: Dict[str, Any],
    release_id: int,
    *,
    version_name: str,
    description: str = "",
    force_update: bool = False,
    released_at_ms: Optional[int] = None,
) -> None:
    require_app(app_id, user)
    release = db.query_one(
        "SELECT * FROM releases WHERE id = ? AND app_id = ?", (release_id, app_id)
    )
    if release is None:
        raise ServiceError("发行版不存在", status=404)

    version_name = (version_name or "").strip()
    if not version_name:
        raise ServiceError("版本名不能为空")

    db.execute(
        "UPDATE releases SET version_name = ?, description = ?, force_update = ?, released_at = ? "
        "WHERE id = ?",
        (
            version_name,
            description or "",
            1 if force_update else 0,
            released_at_ms if released_at_ms is not None else release["released_at"],
            release_id,
        ),
    )


def set_latest_release(app_id: int, user: Dict[str, Any], release_id: int) -> str:
    require_app(app_id, user)
    release = db.query_one(
        "SELECT * FROM releases WHERE id = ? AND app_id = ?", (release_id, app_id)
    )
    if release is None:
        raise ServiceError("发行版不存在", status=404)
    with db.transaction() as conn:
        db.execute_tx(conn, "UPDATE releases SET is_latest = 0 WHERE app_id = ?", (app_id,))
        db.execute_tx(conn, "UPDATE releases SET is_latest = 1 WHERE id = ?", (release_id,))
    return release["version_name"]


def delete_release(app_id: int, user: Dict[str, Any], release_id: int) -> str:
    require_app(app_id, user)
    release = db.query_one(
        "SELECT * FROM releases WHERE id = ? AND app_id = ?", (release_id, app_id)
    )
    if release is None:
        raise ServiceError("发行版不存在", status=404)

    # 补 latest 与删记录放进同一个事务：若先删文件再补，文件删除一失败就会留下
    # 「应用没有任何 latest」的坏状态；磁盘多一个孤儿文件则可以事后清理。
    with db.transaction() as conn:
        db.execute_tx(conn, "DELETE FROM releases WHERE id = ?", (release_id,))
        if release["is_latest"]:
            fallback = db.query_one(
                "SELECT id FROM releases WHERE app_id = ? "
                "ORDER BY version_code DESC, id DESC LIMIT 1",
                (app_id,),
            )
            if fallback:
                db.execute_tx(
                    conn, "UPDATE releases SET is_latest = 1 WHERE id = ?", (fallback["id"],)
                )

    try:
        storage.delete(release["build_path"])
    except OSError:
        logger.warning("删除产物文件失败，磁盘上可能残留 %s", release["build_path"], exc_info=True)
    return release["version_name"]


# ---------------------------------------------------------------- 公告


def create_notice(
    app_id: int,
    user: Dict[str, Any],
    *,
    title: str,
    content: str = "",
    published_at_ms: Optional[int] = None,
) -> Dict[str, Any]:
    require_app(app_id, user)
    title = (title or "").strip()
    if not title:
        raise ServiceError("公告标题不能为空")

    timestamp = now_ms()
    notice_id = db.execute(
        "INSERT INTO notices (app_id, title, content, published_at, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            app_id,
            title,
            content or "",
            published_at_ms if published_at_ms is not None else timestamp,
            timestamp,
        ),
    )
    return {"id": notice_id, "title": title}


def update_notice(
    app_id: int,
    user: Dict[str, Any],
    notice_id: int,
    *,
    title: str,
    content: str = "",
    published_at_ms: Optional[int] = None,
) -> None:
    require_app(app_id, user)
    notice = db.query_one("SELECT * FROM notices WHERE id = ? AND app_id = ?", (notice_id, app_id))
    if notice is None:
        raise ServiceError("公告不存在", status=404)

    title = (title or "").strip()
    if not title:
        raise ServiceError("公告标题不能为空")

    db.execute(
        "UPDATE notices SET title = ?, content = ?, published_at = ? WHERE id = ?",
        (
            title,
            content or "",
            published_at_ms if published_at_ms is not None else notice["published_at"],
            notice_id,
        ),
    )


def delete_notice(app_id: int, user: Dict[str, Any], notice_id: int) -> str:
    require_app(app_id, user)
    notice = db.query_one("SELECT * FROM notices WHERE id = ? AND app_id = ?", (notice_id, app_id))
    if notice is None:
        raise ServiceError("公告不存在", status=404)
    db.execute("DELETE FROM notices WHERE id = ?", (notice_id,))
    return notice["title"]


# ---------------------------------------------------------------- 分享链接


def create_share_link(
    app_id: int, user: Dict[str, Any], *, note: str = "", target: str = "intro"
) -> Dict[str, Any]:
    from . import analytics

    require_app(app_id, user)
    normalized_note = normalize_text(note, 120)
    normalized_target = target if target in analytics.SHARE_TARGETS else "intro"
    timestamp = now_ms()

    for _ in range(10):
        code = analytics.generate_share_code()
        try:
            link_id = db.execute(
                "INSERT INTO share_links (app_id, code, note, target, enabled, created_at) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (app_id, code, normalized_note, normalized_target, timestamp),
            )
        except sqlite3.IntegrityError:
            # 预检与插入之间有窗口；真正兜底的是 code 上的 UNIQUE 约束。
            continue
        return {"id": link_id, "code": code, "note": normalized_note, "target": normalized_target}
    raise ServiceError("生成分享码失败，请重试", status=500)


def delete_share_link(app_id: int, user: Dict[str, Any], link_id: int) -> Dict[str, Any]:
    require_app(app_id, user)
    link = db.query_one("SELECT * FROM share_links WHERE id = ? AND app_id = ?", (link_id, app_id))
    if link is None:
        raise ServiceError("分享链接不存在", status=404)
    # 只删链接本身，保留 events 里已记录的 share_code：历史归因不该随链接消失。
    db.execute("DELETE FROM share_links WHERE id = ?", (link_id,))
    return link


def toggle_share_link(app_id: int, user: Dict[str, Any], link_id: int) -> bool:
    require_app(app_id, user)
    link = db.query_one("SELECT * FROM share_links WHERE id = ? AND app_id = ?", (link_id, app_id))
    if link is None:
        raise ServiceError("分享链接不存在", status=404)
    enabled = 0 if link["enabled"] else 1
    db.execute("UPDATE share_links SET enabled = ? WHERE id = ?", (enabled, link_id))
    return bool(enabled)
