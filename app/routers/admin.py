"""后台管理。

全部写操作都依赖 security.require_csrf（同时校验登录 + CSRF）。
表单里的提示消息走查询串（?ok= / ?err=），不占用会话存储。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import analytics, apikeys, config, db, security, services, storage
from ..serializers import notice_payload, release_payload
from ..utils import format_time, human_size, now_ms, redirect_with
from .public import (
    base_url_for,
    find_latest_notice,
    find_latest_release,
    latest_releases_by_app,
)

logger = logging.getLogger("apppublisher.admin")

router = APIRouter(prefix="/admin")

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.filters["human_size"] = human_size
templates.env.filters["format_time"] = format_time

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
_DATETIME_FORMATS = ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


# ---------------------------------------------------------------- 工具


def render(request: Request, template: str, **context: Any) -> HTMLResponse:
    context.setdefault("user", None)
    context.setdefault("ok", request.query_params.get("ok"))
    context.setdefault("err", request.query_params.get("err"))
    return templates.TemplateResponse(request, template, context)


def safe_next(target: Optional[str]) -> str:
    """只允许站内相对跳转，挡掉 //evil.com 这类开放重定向。"""
    value = (target or "").strip()
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/admin"


def parse_local_datetime(value: str) -> Optional[int]:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return int(time.mktime(time.strptime(text, fmt)) * 1000)
        except ValueError:
            continue
    return None


def to_local_input(ms: Optional[int]) -> str:
    """毫秒时间戳 -> <input type="datetime-local"> 需要的本地时间字符串。"""
    if not ms:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M", time.localtime(ms / 1000))


templates.env.filters["local_input"] = to_local_input


def normalize_tagline(raw: str) -> str:
    """一句话简介：折叠空白并截断，避免有人往单行字段里塞一大段。"""
    return " ".join((raw or "").split())[:200]


def normalize_note(raw: str) -> str:
    """分享链接的备注，同样折叠空白并限长。"""
    return " ".join((raw or "").split())[:120]


def parse_days(raw: Optional[str]) -> int:
    """统计区间。委托给 config.clamp_days()，与 JSON API 共用同一份白名单逻辑。"""
    return config.clamp_days(raw)


def pretty_json(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, indent=2)


def require_app(app_id: int, user: Dict[str, Any]) -> Dict[str, Any]:
    """取应用并校验归属。规则与 JSON API 完全一致 —— 都走 services.require_app()。

    无权时按 404 处理而不是 403：不向对方泄露「这个应用存在，只是不属于你」。
    """
    try:
        return services.require_app(app_id, user)
    except services.ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


def read_intro_text(intro_html: str, intro_file: Optional[UploadFile]) -> str:
    """介绍页正文：上传的 HTML 文件优先于文本框内容。"""
    if intro_file is not None and intro_file.filename:
        try:
            # accept 属性只是给文件选择框用的提示，不是安全边界，服务端必须自己校验。
            if storage.match_extension(intro_file.filename, config.HTML_EXTENSIONS) is None:
                raise HTTPException(
                    status_code=400, detail="介绍页文件只接受 .html / .htm / .txt"
                )
            raw = intro_file.file.read(config.MAX_INTRO_BYTES + 1)
        finally:
            try:
                intro_file.file.close()
            except Exception:  # noqa: BLE001
                pass
        if len(raw) > config.MAX_INTRO_BYTES:
            raise HTTPException(
                status_code=413, detail=f"HTML 文件超过 {config.MAX_INTRO_MB} MB 上限"
            )
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            # 国内编辑器导出的文件常见 GBK 编码。
            return raw.decode("gbk", errors="replace")

    text = intro_html or ""
    if len(text.encode("utf-8")) > config.MAX_INTRO_BYTES:
        raise HTTPException(status_code=413, detail=f"HTML 内容超过 {config.MAX_INTRO_MB} MB 上限")
    return text


# ---------------------------------------------------------------- 登录登出


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/admin") -> HTMLResponse:
    # 已登录的直接送进去，省一次输入。
    token = security.get_session_token(request)
    if token and security.read_session_token(token) is not None:
        return RedirectResponse(safe_next(next), status_code=303)

    # 登录页还没有会话可用，改用双提交：同一个随机值既进 Cookie 也进表单。
    # 攻击者读不到受害者的 Cookie，所以没法把「对得上」的值塞进跨站表单。
    csrf = security.csrf_cookie_value(request) or security.new_csrf_secret()
    response = render(request, "login.html", next_url=safe_next(next), csrf_token=csrf)
    if security.csrf_cookie_value(request) != csrf:
        security.set_csrf_cookie(response, csrf)
    return response


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/admin"),
    csrf_token: str = Form(""),
) -> RedirectResponse:
    if not security.double_submit_ok(request, csrf_token):
        return redirect_with("/admin/login", err="表单已过期，请重新提交")

    username = username.strip()
    target = safe_next(next)

    locked = security.login_locked_for(username)
    if locked:
        return redirect_with("/admin/login", err=f"失败次数过多，请 {locked} 秒后再试")

    user = db.query_one("SELECT * FROM users WHERE username = ?", (username,))
    if user is None or not security.verify_password(password, user["password_hash"]):
        security.record_login_failure(username)
        return redirect_with("/admin/login", err="用户名或密码错误")

    security.clear_login_failures(username)
    response = RedirectResponse(target, status_code=303)
    security.set_session_cookie(
        response, security.create_session_token(user["id"], user["password_hash"])
    )
    return response


@router.post("/logout")
def logout(user: Dict[str, Any] = Depends(security.require_csrf)) -> RedirectResponse:
    response = RedirectResponse("/admin/login", status_code=303)
    security.clear_session_cookie(response)
    return response


# ---------------------------------------------------------------- 概览


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request, user: Dict[str, Any] = Depends(security.get_current_user)
) -> HTMLResponse:
    # 超管看全部，其他人只看自己创建的（与 JSON API 同一条规则）。
    apps = services.visible_apps(user)

    # 三条聚合查询搞定，避免每个应用各查 3 次的 N+1。
    release_counts = {
        row["app_id"]: row["total"]
        for row in db.query_all("SELECT app_id, COUNT(*) AS total FROM releases GROUP BY app_id")
    }
    notice_counts = {
        row["app_id"]: row["total"]
        for row in db.query_all("SELECT app_id, COUNT(*) AS total FROM notices GROUP BY app_id")
    }
    latest_by_app = latest_releases_by_app()

    # 统计同样是一次查询取全，不要再按应用逐个查。
    totals = analytics.app_totals_map(config.STATS_DEFAULT_RANGE_DAYS)
    for app in apps:
        app["release_count"] = release_counts.get(app["id"], 0)
        app["notice_count"] = notice_counts.get(app["id"], 0)
        app["latest"] = latest_by_app.get(app["id"])
        app_stats = totals.get(app["id"], {})
        app["pv"] = (app_stats.get(analytics.KIND_VIEW) or {}).get("pv", 0)
        app["uv"] = (app_stats.get(analytics.KIND_VIEW) or {}).get("uv", 0)
        app["downloads"] = (app_stats.get(analytics.KIND_DOWNLOAD) or {}).get("pv", 0)

    return render(
        request,
        "apps.html",
        user=user,
        apps=apps,
        stats_days=config.STATS_DEFAULT_RANGE_DAYS,
    )


# ---------------------------------------------------------------- 应用


@router.get("/apps/new", response_class=HTMLResponse)
def app_new(
    request: Request, user: Dict[str, Any] = Depends(security.get_current_user)
) -> HTMLResponse:
    return render(request, "app_form.html", user=user, app=None)


@router.post("/apps")
def app_create(
    user: Dict[str, Any] = Depends(security.require_csrf),
    slug: str = Form(...),
    name: str = Form(...),
    tagline: str = Form(""),
    intro_html: str = Form(""),
    intro_file: UploadFile = File(None),
    enabled: str = Form(""),
) -> RedirectResponse:
    try:
        html = read_intro_text(intro_html, intro_file)
    except HTTPException as exc:
        return redirect_with("/admin/apps/new", err=str(exc.detail))

    try:
        # 校验、查重、入库都在 services 里，与 JSON API 共用同一份实现。
        app_id = services.create_app(
            slug=slug,
            name=name,
            tagline=tagline,
            intro_html=html,
            enabled=bool(enabled),
            owner_id=user["id"],
        )
    except services.ServiceError as exc:
        return redirect_with("/admin/apps/new", err=exc.message)

    return redirect_with(f"/admin/apps/{app_id}", ok=f"应用「{name}」已创建")


@router.get("/apps/{app_id}", response_class=HTMLResponse)
def app_detail(
    request: Request,
    app_id: int,
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> HTMLResponse:
    app = require_app(app_id, user)
    releases = db.query_all(
        "SELECT * FROM releases WHERE app_id = ? ORDER BY version_code DESC, id DESC",
        (app_id,),
    )
    notices = db.query_all(
        "SELECT * FROM notices WHERE app_id = ? ORDER BY published_at DESC, id DESC",
        (app_id,),
    )

    base = base_url_for(request)
    latest = find_latest_release(app_id)
    preview = None
    if latest is not None:
        latest = dict(latest)
        latest["app_name"] = app["name"]
        preview = release_payload(latest, app["slug"], base)

    latest_notice = find_latest_notice(app_id)
    notice_preview = notice_payload(latest_notice) if latest_notice else None

    # 顺手做一次过期明细清理；内部按天去重，不会每个请求都真的删。
    # 清理是附加动作，失败不该把整个详情页变成 500（record() 也是同样的态度）。
    try:
        analytics.purge_old_events()
    except Exception:  # noqa: BLE001
        logger.warning("统计明细清理失败", exc_info=True)

    days = parse_days(request.query_params.get("days"))
    include_bots = request.query_params.get("bots") == "1"
    stats = {
        "days": days,
        "include_bots": include_bots,
        "choices": config.STATS_RANGE_CHOICES,
        "summary": analytics.summary(app_id, days, include_bots),
        "series": analytics.daily_series(app_id, days, include_bots),
        "referrers": analytics.referrers(app_id, days, include_bots=include_bots),
        "ua_rows": analytics.ua_breakdown(app_id, days, include_bots),
        "recent": analytics.recent_downloads(app_id),
        "retention_days": config.STATS_RETENTION_DAYS,
    }

    assets = [
        {
            "id": row["id"],
            "path": row["path"],
            "url": f"{base}/media/{row['path']}",
            "name": row["original_name"] or row["path"].rsplit("/", 1)[-1],
            "size": row["size"],
            "created_at": row["created_at"],
        }
        for row in db.query_all("SELECT * FROM assets WHERE app_id = ? ORDER BY id DESC", (app_id,))
    ]

    # 已经落盘但没有登记归属的图片（本次升级前上传的，或手工放进目录的）。
    # 不列出来的话它们在界面上等于不存在，用户只会困惑「我传的图去哪了」。
    known = {row["path"] for row in db.query_all("SELECT path FROM assets")}
    known.update(
        row["banner_path"]
        for row in db.query_all("SELECT banner_path FROM apps WHERE banner_path IS NOT NULL")
    )
    unregistered = []
    for stored in storage.list_files("images"):
        rel_path = storage.relative_of(stored)
        if not rel_path or rel_path in known:
            continue
        try:
            size = stored.stat().st_size
        except OSError:
            continue
        unregistered.append(
            {"path": rel_path, "url": f"{base}/media/{rel_path}", "name": stored.name, "size": size}
        )

    share_links = analytics.share_link_stats(app_id, days, include_bots)
    for item in share_links:
        item["url"] = f"{base}/{app['slug']}/s/{item['code']}"
        item["target_label"] = analytics.SHARE_TARGET_LABELS.get(item["target"], item["target"])

    owner = None
    if app["owner_id"]:
        owner = db.query_one(
            "SELECT id, username, display_name FROM users WHERE id = ?", (app["owner_id"],)
        )

    return render(
        request,
        "app_detail.html",
        user=user,
        app=app,
        owner=owner,
        stats=stats,
        assets=assets,
        unregistered=unregistered,
        share_links=share_links,
        share_target_labels=analytics.SHARE_TARGET_LABELS,
        # 相对地址给 <img> 用（本机调试时也能直接显示），绝对地址给复制用。
        banner_url=f"/media/{app['banner_path']}" if app["banner_path"] else "",
        banner_link=f"{base}/media/{app['banner_path']}" if app["banner_path"] else "",
        releases=releases,
        notices=notices,
        base_url=base,
        preview_json=pretty_json(preview),
        notice_json=pretty_json(notice_preview),
        # 文件选择框的 accept 属性，由后端配置生成，避免两边各写一份扩展名清单。
        accept_extensions=",".join(sorted(config.BUILD_EXTENSIONS)),
    )


@router.post("/apps/{app_id}")
def app_update(
    app_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
    slug: str = Form(...),
    name: str = Form(...),
    tagline: str = Form(""),
    intro_html: str = Form(""),
    intro_file: UploadFile = File(None),
    enabled: str = Form(""),
) -> RedirectResponse:
    require_app(app_id, user)
    target = f"/admin/apps/{app_id}"

    try:
        slug = config.validate_slug(slug)
    except ValueError as exc:
        return redirect_with(target, err=str(exc))

    name = name.strip()
    if not name:
        return redirect_with(target, err="应用名称不能为空")

    if db.query_one("SELECT id FROM apps WHERE slug = ? AND id <> ?", (slug, app_id)):
        return redirect_with(target, err=f"短链 /{slug} 已被其它应用占用")

    try:
        html = read_intro_text(intro_html, intro_file)
    except HTTPException as exc:
        return redirect_with(target, err=str(exc.detail))

    db.execute(
        "UPDATE apps SET slug = ?, name = ?, tagline = ?, intro_html = ?, enabled = ?, "
        "updated_at = ? WHERE id = ?",
        (slug, name, normalize_tagline(tagline), html, 1 if enabled else 0, now_ms(), app_id),
    )
    return redirect_with(target, ok="已保存")


@router.post("/apps/{app_id}/delete")
def app_delete(
    app_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
) -> RedirectResponse:
    app = require_app(app_id, user)
    build_paths = [
        row["build_path"]
        for row in db.query_all("SELECT build_path FROM releases WHERE app_id = ?", (app_id,))
    ]
    # banner 和其它图片是共享 uploads/images 的，只删具体文件，不要动目录。
    if app["banner_path"]:
        build_paths.append(app["banner_path"])
    # 该应用登记过的图片也要清掉（assets 表里的行会随外键级联删除）。
    build_paths.extend(
        row["path"] for row in db.query_all("SELECT path FROM assets WHERE app_id = ?", (app_id,))
    )
    db.execute("DELETE FROM apps WHERE id = ?", (app_id,))
    # 数据库记录先删干净，再清磁盘；反之若中途失败会留下悬空记录。
    # 单个文件删不掉（权限等）不该让整个请求 500，记日志继续。
    for path in build_paths:
        try:
            storage.delete(path)
        except OSError:
            logger.warning("删除产物文件失败，磁盘上可能残留 %s", path, exc_info=True)
    return redirect_with("/admin", ok=f"应用「{app['name']}」及其发行版已删除")


@router.post("/apps/{app_id}/images")
def image_upload(
    request: Request,
    app_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
    image: UploadFile = File(...),
) -> RedirectResponse:
    target = f"/admin/apps/{app_id}#media"
    require_app(app_id, user)
    try:
        rel_path, size, _ = storage.save_upload(
            image, "images", config.IMAGE_EXTENSIONS, config.MAX_IMAGE_BYTES
        )
    except HTTPException as exc:
        return redirect_with(target, err=str(exc.detail))

    try:
        db.execute(
            "INSERT INTO assets (app_id, path, original_name, size, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (app_id, rel_path, storage.safe_display_name(image.filename, "image"), size, now_ms()),
        )
    except Exception:
        # 登记失败就把刚落盘的文件删掉。否则它是一个永远不出现在列表里、
        # 但确实占着磁盘也对外可访问的隐身文件。
        storage.delete(rel_path)
        logger.exception("登记图片资源失败 app_id=%s path=%s", app_id, rel_path)
        return redirect_with(target, err="保存失败，请查看服务端日志")

    url = f"{base_url_for(request)}/media/{rel_path}"
    return redirect_with(target, ok=f"图片已上传（{human_size(size)}）：{url}")


@router.post("/apps/{app_id}/images/{asset_id}/delete")
def asset_delete(
    app_id: int,
    asset_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
) -> RedirectResponse:
    target = f"/admin/apps/{app_id}#media"
    require_app(app_id, user)
    asset = db.query_one("SELECT * FROM assets WHERE id = ? AND app_id = ?", (asset_id, app_id))
    if asset is None:
        # 别的应用的资源不允许从这里删。
        return redirect_with(target, err="图片不存在或不属于该应用")

    db.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    try:
        storage.delete(asset["path"])
    except OSError:
        logger.warning("删除图片文件失败，磁盘上可能残留 %s", asset["path"], exc_info=True)
    return redirect_with(target, ok=f"已删除 {asset['original_name']}")


@router.post("/apps/{app_id}/banner")
def banner_upload(
    app_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
    banner: UploadFile = File(...),
) -> RedirectResponse:
    """上传根页卡片顶部的大图。"""
    target = f"/admin/apps/{app_id}#media"
    app = require_app(app_id, user)
    try:
        rel_path, size, _ = storage.save_upload(
            banner, "images", config.IMAGE_EXTENSIONS, config.MAX_IMAGE_BYTES
        )
    except HTTPException as exc:
        return redirect_with(target, err=str(exc.detail))

    previous = app["banner_path"]
    db.execute(
        "UPDATE apps SET banner_path = ?, updated_at = ? WHERE id = ?",
        (rel_path, now_ms(), app_id),
    )
    # 先换引用再删旧文件：顺序反过来时，删失败会让 banner_path 指向一个已不存在的文件，
    # 前台就成了裂图。留着孤儿文件只是浪费一点磁盘。
    if previous:
        try:
            storage.delete(previous)
        except OSError:
            logger.warning("删除旧 banner 失败，磁盘上可能残留 %s", previous, exc_info=True)
    return redirect_with(target, ok=f"封面已更新（{human_size(size)}）")


@router.post("/apps/{app_id}/banner/delete")
def banner_delete(
    app_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
) -> RedirectResponse:
    target = f"/admin/apps/{app_id}#media"
    app = require_app(app_id, user)
    if not app["banner_path"]:
        return redirect_with(target, err="当前没有封面图")

    db.execute(
        "UPDATE apps SET banner_path = NULL, updated_at = ? WHERE id = ?", (now_ms(), app_id)
    )
    try:
        storage.delete(app["banner_path"])
    except OSError:
        logger.warning("删除 banner 文件失败，磁盘上可能残留 %s", app["banner_path"], exc_info=True)
    return redirect_with(target, ok="封面已移除")


# ---------------------------------------------------------------- 分享链接


@router.post("/apps/{app_id}/share")
def share_create(
    app_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
    note: str = Form(""),
    target: str = Form("intro"),
) -> RedirectResponse:
    anchor = f"/admin/apps/{app_id}#share"
    require_app(app_id, user)

    note_value = normalize_note(note)
    target_value = target if target in analytics.SHARE_TARGETS else "intro"

    # 直接靠 code 上的 UNIQUE 约束避碰，不做「先查再插」——
    # 那两步之间有窗口，并发请求可能同时选中同一个码。
    # 撞了就换一个重试，比先查一次少一条查询，也没有竞态。
    for _ in range(10):
        candidate = analytics.generate_share_code()
        try:
            db.execute(
                "INSERT INTO share_links (app_id, code, note, target, enabled, created_at) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (app_id, candidate, note_value, target_value, now_ms()),
            )
        except sqlite3.IntegrityError:
            continue
        return redirect_with(anchor, ok=f"分享链接已生成（{candidate}）")

    logger.error("生成分享码连续冲突 app_id=%s", app_id)
    return redirect_with(anchor, err="生成分享码失败，请重试")


@router.post("/apps/{app_id}/share/{link_id}/toggle")
def share_toggle(
    app_id: int,
    link_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
) -> RedirectResponse:
    anchor = f"/admin/apps/{app_id}#share"
    require_app(app_id, user)
    try:
        enabled = services.toggle_share_link(app_id, user, link_id)
    except services.ServiceError as exc:
        return redirect_with(anchor, err=exc.message)
    return redirect_with(anchor, ok="已启用" if enabled else "已停用，链接仍会跳转但不再归因")


@router.post("/apps/{app_id}/share/{link_id}/delete")
def share_delete(
    app_id: int,
    link_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
) -> RedirectResponse:
    anchor = f"/admin/apps/{app_id}#share"
    require_app(app_id, user)
    link = db.query_one("SELECT * FROM share_links WHERE id = ? AND app_id = ?", (link_id, app_id))
    if link is None:
        return redirect_with(anchor, err="分享链接不存在")

    # 只删链接本身，**保留** events 里已记录的 share_code：
    # 历史归因数据不该因为清理链接而消失，统计查询 join 不上时自然忽略。
    db.execute("DELETE FROM share_links WHERE id = ?", (link_id,))
    return redirect_with(anchor, ok=f"已删除分享链接「{link['note'] or link['code']}」")


# ---------------------------------------------------------------- 发行版


@router.post("/apps/{app_id}/releases")
def release_create(
    app_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
    version_name: str = Form(...),
    version_code: str = Form(...),
    description: str = Form(""),
    force_update: str = Form(""),
    make_latest: str = Form(""),
    released_at: str = Form(""),
    build: UploadFile = File(...),
) -> RedirectResponse:
    target = f"/admin/apps/{app_id}#releases"
    require_app(app_id, user)

    version_name = version_name.strip()
    if not version_name:
        return redirect_with(target, err="版本名不能为空")

    try:
        code = int(str(version_code).strip())
        if code <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return redirect_with(target, err="版本号必须是正整数")

    if db.query_one(
        "SELECT id FROM releases WHERE app_id = ? AND version_code = ?", (app_id, code)
    ):
        return redirect_with(target, err=f"版本号 {code} 已存在，请先删除旧记录")

    try:
        rel_path, size, sha256 = storage.save_upload(
            build, "build", config.BUILD_EXTENSIONS, config.MAX_BUILD_BYTES
        )
    except HTTPException as exc:
        return redirect_with(target, err=str(exc.detail))

    existing = db.query_value("SELECT COUNT(*) FROM releases WHERE app_id = ?", (app_id,)) or 0
    mark_latest = bool(make_latest) or existing == 0
    timestamp = now_ms()

    try:
        with db.transaction() as conn:
            if mark_latest:
                db.execute_tx(
                    conn, "UPDATE releases SET is_latest = 0 WHERE app_id = ?", (app_id,)
                )
            db.execute_tx(
                conn,
                "INSERT INTO releases (app_id, version_name, version_code, description, build_path,"
                " build_name, build_size, build_sha256, content_type, force_update, is_latest,"
                " released_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    app_id,
                    version_name,
                    code,
                    description or "",
                    rel_path,
                    storage.safe_display_name(build.filename, f"{code}.bin"),
                    size,
                    sha256,
                    storage.content_type_for(build.filename),
                    1 if force_update else 0,
                    1 if mark_latest else 0,
                    parse_local_datetime(released_at) or timestamp,
                    timestamp,
                ),
            )
    except Exception:
        # 入库失败就把刚落盘的构建产物清掉，避免留下孤儿文件。
        storage.delete(rel_path)
        logger.exception("写入发行版失败 app_id=%s version_code=%s", app_id, code)
        return redirect_with(target, err="保存失败，请查看服务端日志")

    if released_at.strip() and parse_local_datetime(released_at) is None:
        return redirect_with(
            target, ok=f"{version_name}（{code}）已发布，但发布时间格式无法识别，已使用当前时间"
        )
    return redirect_with(target, ok=f"{version_name}（{code}）已发布，{human_size(size)}")


@router.post("/apps/{app_id}/releases/{release_id}")
def release_update(
    app_id: int,
    release_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
    version_name: str = Form(...),
    description: str = Form(""),
    force_update: str = Form(""),
    released_at: str = Form(""),
) -> RedirectResponse:
    target = f"/admin/apps/{app_id}#releases"
    require_app(app_id, user)
    release = db.query_one(
        "SELECT * FROM releases WHERE id = ? AND app_id = ?", (release_id, app_id)
    )
    if release is None:
        return redirect_with(target, err="发行版不存在")

    version_name = version_name.strip()
    if not version_name:
        return redirect_with(target, err="版本名不能为空")

    parsed_time = parse_local_datetime(released_at)
    db.execute(
        "UPDATE releases SET version_name = ?, description = ?, force_update = ?, released_at = ? "
        "WHERE id = ?",
        (
            version_name,
            description or "",
            1 if force_update else 0,
            parsed_time or release["released_at"],
            release_id,
        ),
    )
    if released_at.strip() and parsed_time is None:
        return redirect_with(target, ok="发行版已更新，但发布时间无法识别，已保留原时间")
    return redirect_with(target, ok="发行版已更新")


@router.post("/apps/{app_id}/releases/{release_id}/latest")
def release_set_latest(
    app_id: int,
    release_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
) -> RedirectResponse:
    target = f"/admin/apps/{app_id}#releases"
    require_app(app_id, user)
    release = db.query_one(
        "SELECT * FROM releases WHERE id = ? AND app_id = ?", (release_id, app_id)
    )
    if release is None:
        return redirect_with(target, err="发行版不存在")

    with db.transaction() as conn:
        db.execute_tx(conn, "UPDATE releases SET is_latest = 0 WHERE app_id = ?", (app_id,))
        db.execute_tx(conn, "UPDATE releases SET is_latest = 1 WHERE id = ?", (release_id,))
    return redirect_with(target, ok=f"已将 {release['version_name']} 设为最新版本")


@router.post("/apps/{app_id}/releases/{release_id}/delete")
def release_delete(
    app_id: int,
    release_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
) -> RedirectResponse:
    target = f"/admin/apps/{app_id}#releases"
    require_app(app_id, user)
    release = db.query_one(
        "SELECT * FROM releases WHERE id = ? AND app_id = ?", (release_id, app_id)
    )
    if release is None:
        return redirect_with(target, err="发行版不存在")

    # 补 latest 和删记录放进同一个事务：若先删文件再补，文件删除一失败就会留下
    # 「应用没有任何 latest」的坏状态；磁盘上多个孤儿文件则可以事后清理。
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

    return redirect_with(target, ok=f"已删除 {release['version_name']}")


# ---------------------------------------------------------------- 公告


@router.post("/apps/{app_id}/notices")
def notice_create(
    app_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
    title: str = Form(...),
    content: str = Form(""),
    published_at: str = Form(""),
) -> RedirectResponse:
    target = f"/admin/apps/{app_id}#notices"
    require_app(app_id, user)

    title = title.strip()
    if not title:
        return redirect_with(target, err="公告标题不能为空")

    timestamp = now_ms()
    db.execute(
        "INSERT INTO notices (app_id, title, content, published_at, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (app_id, title, content or "", parse_local_datetime(published_at) or timestamp, timestamp),
    )
    return redirect_with(target, ok=f"公告「{title}」已发布")


@router.post("/apps/{app_id}/notices/{notice_id}")
def notice_update(
    app_id: int,
    notice_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
    title: str = Form(...),
    content: str = Form(""),
    published_at: str = Form(""),
) -> RedirectResponse:
    target = f"/admin/apps/{app_id}#notices"
    require_app(app_id, user)
    notice = db.query_one(
        "SELECT * FROM notices WHERE id = ? AND app_id = ?", (notice_id, app_id)
    )
    if notice is None:
        return redirect_with(target, err="公告不存在")

    title = title.strip()
    if not title:
        return redirect_with(target, err="公告标题不能为空")

    parsed_time = parse_local_datetime(published_at)
    db.execute(
        "UPDATE notices SET title = ?, content = ?, published_at = ? WHERE id = ?",
        (
            title,
            content or "",
            parsed_time or notice["published_at"],
            notice_id,
        ),
    )
    if published_at.strip() and parsed_time is None:
        return redirect_with(target, ok="公告已更新，但发布时间无法识别，已保留原时间")
    return redirect_with(target, ok="公告已更新")


@router.post("/apps/{app_id}/notices/{notice_id}/delete")
def notice_delete(
    app_id: int,
    notice_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
) -> RedirectResponse:
    target = f"/admin/apps/{app_id}#notices"
    require_app(app_id, user)
    notice = db.query_one(
        "SELECT * FROM notices WHERE id = ? AND app_id = ?", (notice_id, app_id)
    )
    if notice is None:
        return redirect_with(target, err="公告不存在")

    db.execute("DELETE FROM notices WHERE id = ?", (notice_id,))
    return redirect_with(target, ok=f"已删除公告「{notice['title']}」")


# ---------------------------------------------------------------- 个人设置


@router.get("/profile", response_class=HTMLResponse)
def profile_page(
    request: Request, user: Dict[str, Any] = Depends(security.get_current_user)
) -> HTMLResponse:
    return render(request, "profile.html", user=user)


@router.post("/profile/password")
def profile_change_password(
    user: Dict[str, Any] = Depends(security.require_csrf),
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
) -> RedirectResponse:
    target = "/admin/profile"
    if not security.verify_password(current_password, user["password_hash"]):
        return redirect_with(target, err="当前密码不正确")
    if len(new_password) < 8:
        return redirect_with(target, err="新密码至少 8 位")
    if new_password != confirm_password:
        return redirect_with(target, err="两次输入的新密码不一致")

    new_hash = security.hash_password(new_password)
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user["id"]))

    # 初始口令文件属于初始管理员（config.ADMIN_USERNAME），只有本人在「密码」页改密
    # 才让它作废。别的账号改自己的密码跟它无关——无差别删除会让初始管理员在忘记口令、
    # 会话又过期之后，既进不去后台、也没文件可查。
    # 按用户名比对：若 ADMIN_USERNAME 在初始化之后被改过，两边对不上，文件会留下。
    # 宁可留一个陈旧的 0600 文件，也不要误删仍然有效的凭据。
    if user["username"].lower() == config.ADMIN_USERNAME.lower():
        try:
            (config.DATA_DIR / ".initial_admin_password").unlink(missing_ok=True)
        except OSError:
            logger.warning("删除初始密码文件失败", exc_info=True)

    # 换发新 token，否则当前会话会被自己的改密逻辑踢掉。
    response = redirect_with(target, ok="密码已更新")
    security.set_session_cookie(response, security.create_session_token(user["id"], new_hash))
    return response


# ---------------------------------------------------------------- 账号（超级管理员）


@router.get("/accounts", response_class=HTMLResponse)
def accounts_page(
    request: Request, user: Dict[str, Any] = Depends(security.require_super)
) -> HTMLResponse:
    users = db.query_all("SELECT * FROM users ORDER BY id")
    super_count = db.query_value("SELECT COUNT(*) FROM users WHERE is_super = 1") or 0
    return render(request, "accounts.html", user=user, users=users, super_count=super_count)


@router.post("/accounts")
def account_create(
    user: Dict[str, Any] = Depends(security.require_super_csrf),
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    is_super: str = Form(""),
) -> RedirectResponse:
    target = "/admin/accounts"
    username = username.strip()

    if not _USERNAME_RE.match(username):
        return redirect_with(target, err="用户名只能包含字母、数字、下划线、点和连字符，长度 3-32")
    if len(password) < 8:
        return redirect_with(target, err="密码至少 8 位")
    if db.query_one("SELECT id FROM users WHERE username = ?", (username,)):
        return redirect_with(target, err=f"用户名 {username} 已存在")

    db.execute(
        "INSERT INTO users (username, password_hash, display_name, is_super, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            username,
            security.hash_password(password),
            (display_name or "").strip(),
            1 if is_super else 0,
            now_ms(),
        ),
    )
    return redirect_with(target, ok=f"账号 {username} 已创建")


@router.post("/accounts/{user_id}/password")
def account_reset_password(
    user_id: int,
    user: Dict[str, Any] = Depends(security.require_super_csrf),
    new_password: str = Form(...),
) -> RedirectResponse:
    target = "/admin/accounts"
    target_user = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if target_user is None:
        return redirect_with(target, err="账号不存在")
    if len(new_password) < 8:
        return redirect_with(target, err="密码至少 8 位")

    new_hash = security.hash_password(new_password)
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user_id))

    response = redirect_with(target, ok=f"已重置 {target_user['username']} 的密码")
    if user_id == user["id"]:
        # 重置的是自己：会话 token 与口令哈希绑定，不换发就会被自己踢下线，
        # 而 accounts 页不像 profile 页那样会重新签发。
        security.set_session_cookie(response, security.create_session_token(user_id, new_hash))
    return response


@router.post("/accounts/{user_id}/delete")
def account_delete(
    user_id: int,
    user: Dict[str, Any] = Depends(security.require_super_csrf),
) -> RedirectResponse:
    target = "/admin/accounts"
    target_user = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if target_user is None:
        return redirect_with(target, err="账号不存在")
    if user_id == user["id"]:
        return redirect_with(target, err="不能删除当前登录的账号")

    # 校验与删除必须在同一个写事务里：两个并发的删除会各自读到 super_count == 2，
    # 双双通过校验后都执行删除，最终一个超管都不剩，谁也进不了后台。
    with db.transaction() as conn:
        if target_user["is_super"]:
            super_count = db.query_value("SELECT COUNT(*) FROM users WHERE is_super = 1") or 0
            if super_count <= 1:
                return redirect_with(target, err="至少要保留一个超级管理员")
        db.execute_tx(conn, "DELETE FROM users WHERE id = ?", (user_id,))

    return redirect_with(target, ok=f"账号 {target_user['username']} 已删除")


# ---------------------------------------------------------------- API 密钥


@router.get("/api-keys", response_class=HTMLResponse)
def api_keys_page(
    request: Request, user: Dict[str, Any] = Depends(security.get_current_user)
) -> HTMLResponse:
    # 超管能看到所有人的密钥，便于排查；普通账号只看自己的。
    keys = apikeys.list_all() if user["is_super"] else apikeys.list_for_user(user["id"])
    return render(request, "api_keys.html", user=user, keys=keys, new_key=None)


@router.post("/api-keys", response_class=HTMLResponse)
def api_key_create(
    request: Request,
    user: Dict[str, Any] = Depends(security.require_csrf),
    name: str = Form(""),
) -> HTMLResponse:
    """创建密钥。

    这里刻意**用渲染而不是跳转**来展示明文密钥：跳转意味着把它塞进 URL，
    会被浏览器历史、反代访问日志和 Referer 一路记录下来。代价是刷新会重复提交
    表单（浏览器会提示），比泄露一把长期有效的凭据划算得多。
    """
    created = apikeys.create(user["id"], normalize_note(name))
    keys = apikeys.list_all() if user["is_super"] else apikeys.list_for_user(user["id"])
    return render(
        request,
        "api_keys.html",
        user=user,
        keys=keys,
        new_key=created,
        ok=f"密钥已创建：{created['name'] or '（未命名）'}",
    )


@router.post("/api-keys/{key_id}/revoke")
def api_key_revoke(
    key_id: int, user: Dict[str, Any] = Depends(security.require_csrf)
) -> RedirectResponse:
    target = "/admin/api-keys"
    # 非超管只能动自己的密钥
    owner_filter = apikeys.owner_filter(user)
    if not apikeys.revoke(key_id, owner_filter):
        return redirect_with(target, err="密钥不存在或无权操作")
    return redirect_with(target, ok="密钥已撤销，使用它的请求会立即失败")


@router.post("/api-keys/{key_id}/delete")
def api_key_delete(
    key_id: int, user: Dict[str, Any] = Depends(security.require_csrf)
) -> RedirectResponse:
    target = "/admin/api-keys"
    owner_filter = apikeys.owner_filter(user)
    if not apikeys.delete(key_id, owner_filter):
        return redirect_with(target, err="密钥不存在或无权操作")
    return redirect_with(target, ok="密钥已删除")

