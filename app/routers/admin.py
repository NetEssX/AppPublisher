"""后台管理。

全部写操作都依赖 security.require_csrf（同时校验登录 + CSRF）。
表单里的提示消息走查询串（?ok= / ?err=），不占用会话存储。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import config, db, security, storage
from ..serializers import notice_payload, release_payload
from ..utils import format_time, human_size, now_ms, redirect_with
from .public import base_url_for, find_latest_notice, find_latest_release

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


def pretty_json(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, indent=2)


def require_app(app_id: int) -> Dict[str, Any]:
    app = db.query_one("SELECT * FROM apps WHERE id = ?", (app_id,))
    if app is None:
        raise HTTPException(status_code=404, detail="应用不存在")
    return app


def read_intro_text(intro_html: str, intro_file: Optional[UploadFile]) -> str:
    """介绍页正文：上传的 HTML 文件优先于文本框内容。"""
    if intro_file is not None and intro_file.filename:
        try:
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
    return render(request, "login.html", next_url=safe_next(next))


@router.post("/login")
def login_submit(
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/admin"),
) -> RedirectResponse:
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
def logout() -> RedirectResponse:
    response = RedirectResponse("/admin/login", status_code=303)
    security.clear_session_cookie(response)
    return response


# ---------------------------------------------------------------- 概览


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request, user: Dict[str, Any] = Depends(security.get_current_user)
) -> HTMLResponse:
    apps = db.query_all("SELECT * FROM apps ORDER BY id DESC")
    for app in apps:
        app["release_count"] = (
            db.query_value("SELECT COUNT(*) FROM releases WHERE app_id = ?", (app["id"],)) or 0
        )
        app["notice_count"] = (
            db.query_value("SELECT COUNT(*) FROM notices WHERE app_id = ?", (app["id"],)) or 0
        )
        app["latest"] = find_latest_release(app["id"])
    return render(request, "apps.html", user=user, apps=apps)


# ---------------------------------------------------------------- 应用


@router.get("/apps/new", response_class=HTMLResponse)
def app_new(
    request: Request, user: Dict[str, Any] = Depends(security.get_current_user)
) -> HTMLResponse:
    return render(request, "app_form.html", user=user, app=None)


@router.post("/apps")
def app_create(
    request: Request,
    user: Dict[str, Any] = Depends(security.require_csrf),
    slug: str = Form(...),
    name: str = Form(...),
    intro_html: str = Form(""),
    intro_file: UploadFile = File(None),
    enabled: str = Form(""),
) -> RedirectResponse:
    try:
        slug = config.validate_slug(slug)
    except ValueError as exc:
        return redirect_with("/admin/apps/new", err=str(exc))

    name = name.strip()
    if not name:
        return redirect_with("/admin/apps/new", err="应用名称不能为空")
    if db.query_one("SELECT id FROM apps WHERE slug = ?", (slug,)):
        return redirect_with("/admin/apps/new", err=f"短链 /{slug} 已被占用")

    try:
        html = read_intro_text(intro_html, intro_file)
    except HTTPException as exc:
        return redirect_with("/admin/apps/new", err=str(exc.detail))

    timestamp = now_ms()
    app_id = db.execute(
        "INSERT INTO apps (slug, name, intro_html, enabled, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (slug, name, html, 1 if enabled else 0, timestamp, timestamp),
    )
    return redirect_with(f"/admin/apps/{app_id}", ok=f"应用「{name}」已创建")


@router.get("/apps/{app_id}", response_class=HTMLResponse)
def app_detail(
    request: Request,
    app_id: int,
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> HTMLResponse:
    app = require_app(app_id)
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

    return render(
        request,
        "app_detail.html",
        user=user,
        app=app,
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
    request: Request,
    app_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
    slug: str = Form(...),
    name: str = Form(...),
    intro_html: str = Form(""),
    intro_file: UploadFile = File(None),
    enabled: str = Form(""),
) -> RedirectResponse:
    require_app(app_id)
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
        "UPDATE apps SET slug = ?, name = ?, intro_html = ?, enabled = ?, updated_at = ? "
        "WHERE id = ?",
        (slug, name, html, 1 if enabled else 0, now_ms(), app_id),
    )
    return redirect_with(target, ok="已保存")


@router.post("/apps/{app_id}/delete")
def app_delete(
    app_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
) -> RedirectResponse:
    app = require_app(app_id)
    build_paths = [
        row["build_path"]
        for row in db.query_all("SELECT build_path FROM releases WHERE app_id = ?", (app_id,))
    ]
    db.execute("DELETE FROM apps WHERE id = ?", (app_id,))
    # 数据库记录先删干净，再清磁盘；反之若中途失败会留下悬空记录。
    for path in build_paths:
        storage.delete(path)
    return redirect_with("/admin", ok=f"应用「{app['name']}」及其发行版已删除")


@router.post("/apps/{app_id}/images")
def image_upload(
    request: Request,
    app_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
    image: UploadFile = File(...),
) -> RedirectResponse:
    target = f"/admin/apps/{app_id}"
    require_app(app_id)
    try:
        rel_path, size, _ = storage.save_upload(
            image, "images", config.IMAGE_EXTENSIONS, config.MAX_IMAGE_BYTES
        )
    except HTTPException as exc:
        return redirect_with(target, err=str(exc.detail))
    url = f"{base_url_for(request)}/media/{rel_path}"
    return redirect_with(target, ok=f"图片已上传（{human_size(size)}）：{url}")


# ---------------------------------------------------------------- 发行版


@router.post("/apps/{app_id}/releases")
def release_create(
    request: Request,
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
    target = f"/admin/apps/{app_id}"
    require_app(app_id)

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
    target = f"/admin/apps/{app_id}"
    release = db.query_one(
        "SELECT * FROM releases WHERE id = ? AND app_id = ?", (release_id, app_id)
    )
    if release is None:
        return redirect_with(target, err="发行版不存在")

    version_name = version_name.strip()
    if not version_name:
        return redirect_with(target, err="版本名不能为空")

    db.execute(
        "UPDATE releases SET version_name = ?, description = ?, force_update = ?, released_at = ? "
        "WHERE id = ?",
        (
            version_name,
            description or "",
            1 if force_update else 0,
            parse_local_datetime(released_at) or release["released_at"],
            release_id,
        ),
    )
    return redirect_with(target, ok="发行版已更新")


@router.post("/apps/{app_id}/releases/{release_id}/latest")
def release_set_latest(
    app_id: int,
    release_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
) -> RedirectResponse:
    target = f"/admin/apps/{app_id}"
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
    target = f"/admin/apps/{app_id}"
    release = db.query_one(
        "SELECT * FROM releases WHERE id = ? AND app_id = ?", (release_id, app_id)
    )
    if release is None:
        return redirect_with(target, err="发行版不存在")

    db.execute("DELETE FROM releases WHERE id = ?", (release_id,))
    storage.delete(release["build_path"])

    # 删掉的正好是 latest，就补一个（versionCode 最大的那个）。
    if release["is_latest"]:
        fallback = db.query_one(
            "SELECT id FROM releases WHERE app_id = ? ORDER BY version_code DESC, id DESC LIMIT 1",
            (app_id,),
        )
        if fallback:
            db.execute("UPDATE releases SET is_latest = 1 WHERE id = ?", (fallback["id"],))

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
    target = f"/admin/apps/{app_id}"
    require_app(app_id)

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
    target = f"/admin/apps/{app_id}"
    notice = db.query_one(
        "SELECT * FROM notices WHERE id = ? AND app_id = ?", (notice_id, app_id)
    )
    if notice is None:
        return redirect_with(target, err="公告不存在")

    title = title.strip()
    if not title:
        return redirect_with(target, err="公告标题不能为空")

    db.execute(
        "UPDATE notices SET title = ?, content = ?, published_at = ? WHERE id = ?",
        (
            title,
            content or "",
            parse_local_datetime(published_at) or notice["published_at"],
            notice_id,
        ),
    )
    return redirect_with(target, ok="公告已更新")


@router.post("/apps/{app_id}/notices/{notice_id}/delete")
def notice_delete(
    app_id: int,
    notice_id: int,
    user: Dict[str, Any] = Depends(security.require_csrf),
) -> RedirectResponse:
    target = f"/admin/apps/{app_id}"
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

    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (security.hash_password(new_password), user_id),
    )
    return redirect_with(target, ok=f"已重置 {target_user['username']} 的密码")


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

    if target_user["is_super"]:
        super_count = db.query_value("SELECT COUNT(*) FROM users WHERE is_super = 1") or 0
        if super_count <= 1:
            return redirect_with(target, err="至少要保留一个超级管理员")

    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return redirect_with(target, ok=f"账号 {target_user['username']} 已删除")
