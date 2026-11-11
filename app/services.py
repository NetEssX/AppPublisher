"""网页后台与 JSON API 共用的领域操作。

两套入口（`/admin` 的表单、`/api/v1` 的 JSON）**刻意共用这里的实现**。
各写一份的话，「网页里能改、API 里不能改」这类规则漂移迟早会发生 ——
这个项目已经因为双份状态吃过一次亏（apps.banner_path 与 assets 表）。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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
    # 附加产物（手机包之外的手表包等）也要一并清理。
    paths.extend(
        row["path"]
        for row in db.query_all(
            "SELECT f.path FROM release_files f JOIN releases r ON r.id = f.release_id "
            "WHERE r.app_id = ?",
            (app_id,),
        )
    )
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


def _default_file_label(filename: Optional[str], index: int) -> str:
    """用户没写标识时，用文件名（不含扩展名）当默认标识。"""
    stem = Path(filename or "").stem
    return normalize_text(stem, 40) or f"文件 {index + 1}"


def normalize_file_labels(raw: Any, count: int) -> List[str]:
    """把「手机,手表」或重复表单字段整理成与文件顺序对应的标识列表。

    没传标识时返回等长的空字符串，由 create_release 从文件名推导默认值。
    ``raw`` 可以是 None、单个字符串或字符串列表；逗号（中英文）都当分隔符。
    """
    if raw is None:
        parts: List[str] = []
    elif isinstance(raw, str):
        parts = [raw]
    else:
        parts = [str(item) for item in raw]

    labels: List[str] = []
    for item in parts:
        labels.extend(part.strip()[:40] for part in re.split(r"[,，]", item) if part.strip())

    if not labels:
        return [""] * count
    if len(labels) != count:
        raise ServiceError(f"文件标识有 {len(labels)} 个，但上传了 {count} 个文件，数量必须一致")
    return labels


def release_files_for(release_ids: Iterable[int]) -> Dict[int, List[Dict[str, Any]]]:
    """批量取发行版的附加产物，按 release_id 分组。

    列表接口一次取完，避免每个版本再查一次库（N+1）。
    """
    ids = sorted({int(item) for item in release_ids})
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = db.query_all(
        f"SELECT * FROM release_files WHERE release_id IN ({placeholders}) "
        "ORDER BY release_id, position, id",
        tuple(ids),
    )
    grouped: Dict[int, List[Dict[str, Any]]] = {release_id: [] for release_id in ids}
    for row in rows:
        grouped[row["release_id"]].append(row)
    return grouped


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
    upload: Optional[UploadFile] = None,
    uploads: Optional[List[UploadFile]] = None,
    labels: Any = None,
) -> Dict[str, Any]:
    """发布一个版本。

    ``upload`` 是历史入口（单文件）；``uploads`` 可以按「主产物在前」的顺序传多个文件。
    两者同时给时 upload 排在最前面，仍然只产生一个版本记录。
    """
    require_app(app_id, user)

    version_name = (version_name or "").strip()
    if not version_name:
        raise ServiceError("版本名不能为空")
    code = parse_version_code(version_code)

    if db.query_one("SELECT id FROM releases WHERE app_id = ? AND version_code = ?", (app_id, code)):
        raise ServiceError(f"版本号 {code} 已存在，请先删除旧记录")

    candidates: List[UploadFile] = []
    if upload is not None and upload.filename:
        candidates.append(upload)
    for item in uploads or []:
        if item is not None and item.filename:
            candidates.append(item)
    if not candidates:
        raise ServiceError("请选择要上传的文件")
    if len(candidates) > config.MAX_RELEASE_FILES:
        raise ServiceError(
            f"一个版本最多上传 {config.MAX_RELEASE_FILES} 个文件，当前 {len(candidates)} 个"
        )

    label_list = normalize_file_labels(labels, len(candidates))
    resolved_labels = [
        label or _default_file_label(item.filename, index)
        for index, (item, label) in enumerate(zip(candidates, label_list))
    ]

    # 先把所有文件落盘，再开始事务；任何一步失败都把已落盘的文件清掉，避免孤儿文件。
    stored = []
    try:
        for item in candidates:
            rel_path, size, sha256 = _save(
                item, "build", config.BUILD_EXTENSIONS, config.MAX_BUILD_BYTES
            )
            stored.append(
                {
                    "path": rel_path,
                    "name": storage.safe_display_name(item.filename, f"{code}.bin"),
                    "size": size,
                    "sha256": sha256,
                    "content_type": storage.content_type_for(item.filename),
                }
            )
    except Exception:
        for item in stored:
            storage.delete(item["path"])
        raise

    existing = db.query_value("SELECT COUNT(*) FROM releases WHERE app_id = ?", (app_id,)) or 0
    mark_latest = bool(make_latest) or existing == 0
    timestamp = now_ms()
    primary = stored[0]

    try:
        with db.transaction() as conn:
            if mark_latest:
                db.execute_tx(conn, "UPDATE releases SET is_latest = 0 WHERE app_id = ?", (app_id,))
            release_id = db.execute_tx(
                conn,
                "INSERT INTO releases (app_id, version_name, version_code, description, build_path,"
                " build_name, build_label, build_size, build_sha256, content_type, force_update,"
                " is_latest, released_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    app_id,
                    version_name,
                    code,
                    description or "",
                    primary["path"],
                    primary["name"],
                    resolved_labels[0],
                    primary["size"],
                    primary["sha256"],
                    primary["content_type"],
                    1 if force_update else 0,
                    1 if mark_latest else 0,
                    released_at_ms if released_at_ms is not None else timestamp,
                    timestamp,
                ),
            )
            files = [
                {
                    # 主产物没有独立记录，序列化时 id 为 null；这里保持一致。
                    "id": None,
                    "label": resolved_labels[0],
                    "name": primary["name"],
                    "size": primary["size"],
                    "sha256": primary["sha256"],
                    "content_type": primary["content_type"],
                    "primary": True,
                }
            ]
            for position, extra in enumerate(stored[1:], start=1):
                file_id = db.execute_tx(
                    conn,
                    "INSERT INTO release_files (release_id, label, path, original_name, size,"
                    " sha256, content_type, position, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        release_id,
                        resolved_labels[position],
                        extra["path"],
                        extra["name"],
                        extra["size"],
                        extra["sha256"],
                        extra["content_type"],
                        position,
                        timestamp,
                    ),
                )
                files.append(
                    {
                        "id": file_id,
                        "label": resolved_labels[position],
                        "name": extra["name"],
                        "size": extra["size"],
                        "sha256": extra["sha256"],
                        "content_type": extra["content_type"],
                        "primary": False,
                    }
                )
    except sqlite3.IntegrityError:
        # 查重与插入之间隔着一整次文件上传，窗口很宽（CI 重试、表单重复提交）。
        # 真正的兜底是 UNIQUE(app_id, version_code)，这属于普通校验失败，不是 500。
        for item in stored:
            storage.delete(item["path"])
        raise ServiceError(f"版本号 {code} 已存在，请先删除旧记录")
    except Exception:
        # 入库失败就把刚落盘的文件清掉，避免留下孤儿文件。
        for item in stored:
            storage.delete(item["path"])
        logger.exception("写入发行版失败 app_id=%s version_code=%s", app_id, code)
        raise ServiceError("保存失败，请查看服务端日志", status=500)

    return {
        "id": release_id,
        "version_name": version_name,
        "version_code": code,
        "size": primary["size"],
        "sha256": primary["sha256"],
        "is_latest": mark_latest,
        "files": files,
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

    extra_paths = [
        row["path"]
        for row in db.query_all(
            "SELECT path FROM release_files WHERE release_id = ?", (release_id,)
        )
    ]

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

    for path in [release["build_path"], *extra_paths]:
        try:
            storage.delete(path)
        except OSError:
            logger.warning("删除产物文件失败，磁盘上可能残留 %s", path, exc_info=True)
    return release["version_name"]


def delete_release_file(
    app_id: int, user: Dict[str, Any], release_id: int, file_id: int
) -> str:
    """删除发行版里的一个附加产物。主产物不能单独删，只能删整个发行版。"""
    require_app(app_id, user)
    release = db.query_one(
        "SELECT id FROM releases WHERE id = ? AND app_id = ?", (release_id, app_id)
    )
    if release is None:
        raise ServiceError("发行版不存在", status=404)

    row = db.query_one(
        "SELECT * FROM release_files WHERE id = ? AND release_id = ?", (file_id, release_id)
    )
    if row is None:
        raise ServiceError("文件不存在", status=404)

    db.execute("DELETE FROM release_files WHERE id = ?", (file_id,))
    try:
        storage.delete(row["path"])
    except OSError:
        logger.warning("删除产物文件失败，磁盘上可能残留 %s", row["path"], exc_info=True)
    return row["label"] or row["original_name"] or f"#{file_id}"


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
