"""JSON 管理接口 `/api/v1`。

鉴权走 API 密钥（`Authorization: Bearer <key>`，也接受 `X-API-Key`），
**不需要 CSRF** —— 密钥是显式携带的凭据，不像 Cookie 那样会被浏览器自动带上，
天然不受跨站请求伪造影响。

权限与网页后台完全一致：密钥继承创建者账号的角色，且只能操作该账号创建的应用。
所有写操作都委托给 `services.py`，与网页后台共用同一份实现。

字段命名约定：**本接口统一用 snake_case**；公开的读取接口
（`/{slug}/releases/latest` 等）沿用 camelCase。两者是刻意区分的两套 API。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from .. import analytics, apikeys, config, db, services
from ..serializers import notice_payload
from .public import base_url_for, find_latest_release, latest_releases_by_app

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------- 鉴权


def current_key(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """解析 API 密钥。Authorization: Bearer 为主，X-API-Key 作为兼容写法。"""
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    # 用 if 而不是 elif：Authorization 存在但没解析出 token 时（Basic 认证、
    # 空的 Bearer），文档承诺的 X-API-Key 回退仍然要生效。
    if not token and x_api_key:
        token = x_api_key.strip()

    if not token:
        raise HTTPException(
            status_code=401, detail="缺少 API 密钥", headers={"WWW-Authenticate": "Bearer"}
        )

    key = apikeys.resolve(token)
    if key is None:
        raise HTTPException(
            status_code=401, detail="API 密钥无效或已撤销", headers={"WWW-Authenticate": "Bearer"}
        )
    return key


def actor_of(key: Dict[str, Any]) -> Dict[str, Any]:
    """把密钥行转成 services 需要的「用户」身份。"""
    return {"id": key["user_id"], "username": key["username"], "is_super": key["is_super"]}


# ---------------------------------------------------------------- 请求体


class AppCreate(BaseModel):
    slug: str = Field(..., description="短链，例如 QUTSchedule")
    name: str
    tagline: str = ""
    intro_html: str = ""
    enabled: bool = True


class AppUpdate(BaseModel):
    """PATCH 语义：只传要改的字段，未传的保持原值。"""

    slug: Optional[str] = None
    name: Optional[str] = None
    tagline: Optional[str] = None
    intro_html: Optional[str] = None
    enabled: Optional[bool] = None


class NoticeCreate(BaseModel):
    title: str
    content: str = ""
    published_at: Optional[int] = Field(None, description="epoch 毫秒，留空取当前时间")


class NoticeUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    published_at: Optional[int] = None


class ReleaseUpdate(BaseModel):
    version_name: Optional[str] = None
    description: Optional[str] = None
    force_update: Optional[bool] = None
    released_at: Optional[int] = None


class ShareLinkCreate(BaseModel):
    note: str = ""
    target: str = Field("intro", description="intro 或 download")


# ---------------------------------------------------------------- 序列化


def release_json(slug: str, base_url: str, row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "version_name": row["version_name"],
        "version_code": row["version_code"],
        "description": row["description"] or "",
        "size": row["build_size"],
        "sha256": row["build_sha256"],
        "content_type": row["content_type"],
        "force_update": bool(row["force_update"]),
        "is_latest": bool(row["is_latest"]),
        "released_at": row["released_at"],
        "created_at": row["created_at"],
        "download_url": f"{base_url}/{slug}/build/{row['version_code']}",
    }


def notice_json(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = notice_payload(row)
    return {
        "id": payload["id"],
        "title": payload["title"],
        "content": payload["content"],
        "published_at": payload["timestamp"],
    }


def app_json(app: Dict[str, Any], base_url: str, *, with_intro: bool = True) -> Dict[str, Any]:
    latest = find_latest_release(app["id"])
    data = {
        "id": app["id"],
        "slug": app["slug"],
        "name": app["name"],
        "tagline": app["tagline"] or "",
        "enabled": bool(app["enabled"]),
        "owner_id": app["owner_id"],
        "banner_url": f"{base_url}/media/{app['banner_path']}" if app["banner_path"] else None,
        "intro_url": f"{base_url}/{app['slug']}",
        "created_at": app["created_at"],
        "updated_at": app["updated_at"],
        "latest_release": release_json(app["slug"], base_url, latest) if latest else None,
    }
    if with_intro:
        data["intro_html"] = app["intro_html"] or ""
    return data


def _releases_of(app_id: int) -> List[Dict[str, Any]]:
    return db.query_all(
        "SELECT * FROM releases WHERE app_id = ? ORDER BY version_code DESC, id DESC", (app_id,)
    )


def _notices_of(app_id: int) -> List[Dict[str, Any]]:
    return db.query_all(
        "SELECT * FROM notices WHERE app_id = ? ORDER BY published_at DESC, id DESC", (app_id,)
    )


def _release_by_code(app_id: int, version_code: int) -> Dict[str, Any]:
    row = db.query_one(
        "SELECT * FROM releases WHERE app_id = ? AND version_code = ?", (app_id, version_code)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    return row


# ---------------------------------------------------------------- 账号


@router.get("/me", summary="验证密钥并返回所属账号")
def api_me(key: Dict[str, Any] = Depends(current_key)) -> Dict[str, Any]:
    return {
        "user_id": key["user_id"],
        "username": key["username"],
        "display_name": key["display_name"] or "",
        "is_super": bool(key["is_super"]),
        "key_name": key["key_name"] or "",
        "key_prefix": key["key_prefix"],
    }


# ---------------------------------------------------------------- 应用


@router.get("/apps", summary="列出可管理的应用")
def list_apps(key: Dict[str, Any] = Depends(current_key)) -> Dict[str, Any]:
    actor = actor_of(key)
    apps = services.visible_apps(actor)
    latest_by_app = latest_releases_by_app()
    items = []
    for app in apps:
        latest = latest_by_app.get(app["id"])
        items.append(
            {
                "id": app["id"],
                "slug": app["slug"],
                "name": app["name"],
                "tagline": app["tagline"] or "",
                "enabled": bool(app["enabled"]),
                "owner_id": app["owner_id"],
                "latest_version_code": latest["version_code"] if latest else None,
                "latest_version_name": latest["version_name"] if latest else None,
            }
        )
    return {"count": len(items), "apps": items}


@router.post("/apps", status_code=201, summary="创建应用")
def create_app(
    request: Request, payload: AppCreate, key: Dict[str, Any] = Depends(current_key)
) -> Dict[str, Any]:
    actor = actor_of(key)
    app_id = services.create_app(
        slug=payload.slug,
        name=payload.name,
        tagline=payload.tagline,
        intro_html=payload.intro_html,
        enabled=payload.enabled,
        owner_id=actor["id"],
    )
    # 调用方刚把 intro_html 发上来，没必要再回传一遍
    return app_json(services.require_app(app_id, actor), base_url_for(request), with_intro=False)


@router.get("/apps/{slug}", summary="查看应用（含发行版与公告）")
def get_app(
    request: Request, slug: str, key: Dict[str, Any] = Depends(current_key)
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    base = base_url_for(request)
    data = app_json(app, base)
    data["releases"] = [release_json(app["slug"], base, row) for row in _releases_of(app["id"])]
    data["notices"] = [notice_json(row) for row in _notices_of(app["id"])]
    return data


@router.patch("/apps/{slug}", summary="修改应用（只传要改的字段）")
def patch_app(
    request: Request, slug: str, payload: AppUpdate, key: Dict[str, Any] = Depends(current_key)
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    services.update_app(
        app["id"],
        actor,
        slug=payload.slug if payload.slug is not None else app["slug"],
        name=payload.name if payload.name is not None else app["name"],
        tagline=payload.tagline if payload.tagline is not None else app["tagline"],
        intro_html=payload.intro_html if payload.intro_html is not None else app["intro_html"],
        enabled=bool(payload.enabled) if payload.enabled is not None else bool(app["enabled"]),
    )
    return app_json(services.require_app(app["id"], actor), base_url_for(request), with_intro=False)


@router.delete("/apps/{slug}", summary="删除应用及其全部产物")
def delete_app(slug: str, key: Dict[str, Any] = Depends(current_key)) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    name = services.delete_app(app["id"], actor)
    return {"deleted": True, "slug": slug, "name": name}


# ---------------------------------------------------------------- 媒体


@router.get("/apps/{slug}/images", summary="列出已上传的图片")
def list_images(
    request: Request, slug: str, key: Dict[str, Any] = Depends(current_key)
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    base = base_url_for(request)
    rows = db.query_all("SELECT * FROM assets WHERE app_id = ? ORDER BY id DESC", (app["id"],))
    return {
        "count": len(rows),
        "images": [
            {
                "id": row["id"],
                "name": row["original_name"],
                "size": row["size"],
                "created_at": row["created_at"],
                "url": f"{base}/media/{row['path']}",
            }
            for row in rows
        ],
    }


@router.post("/apps/{slug}/images", status_code=201, summary="上传介绍页图片")
def upload_image(
    request: Request,
    slug: str,
    file: UploadFile = File(..., description="图片文件（不接受 .svg）"),
    key: Dict[str, Any] = Depends(current_key),
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    result = services.add_image(app["id"], actor, file)
    return {
        "id": result["id"],
        "size": result["size"],
        "url": f"{base_url_for(request)}/media/{result['path']}",
    }


@router.delete("/apps/{slug}/images/{image_id}", summary="删除图片")
def delete_image(
    slug: str, image_id: int, key: Dict[str, Any] = Depends(current_key)
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    asset = services.delete_image(app["id"], actor, image_id)
    return {"deleted": True, "id": image_id, "name": asset["original_name"]}


@router.post("/apps/{slug}/banner", summary="上传封面图")
def upload_banner(
    request: Request,
    slug: str,
    file: UploadFile = File(...),
    key: Dict[str, Any] = Depends(current_key),
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    result = services.set_banner(app["id"], actor, file)
    return {"size": result["size"], "url": f"{base_url_for(request)}/media/{result['path']}"}


@router.delete("/apps/{slug}/banner", summary="移除封面图")
def delete_banner(slug: str, key: Dict[str, Any] = Depends(current_key)) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    services.clear_banner(app["id"], actor)
    return {"deleted": True}


# ---------------------------------------------------------------- 发行版


@router.get("/apps/{slug}/releases", summary="列出发行版")
def list_releases(
    request: Request, slug: str, key: Dict[str, Any] = Depends(current_key)
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    base = base_url_for(request)
    rows = _releases_of(app["id"])
    return {"count": len(rows), "releases": [release_json(app["slug"], base, row) for row in rows]}


@router.post("/apps/{slug}/releases", status_code=201, summary="上传并发布新版本")
def create_release(
    request: Request,
    slug: str,
    file: UploadFile = File(..., description="构建产物，扩展名需在白名单内"),
    version_name: str = Form(...),
    version_code: int = Form(..., description="正整数，应用内唯一"),
    description: str = Form(""),
    force_update: bool = Form(False),
    make_latest: bool = Form(True),
    released_at: Optional[int] = Form(None, description="epoch 毫秒，留空取当前时间"),
    key: Dict[str, Any] = Depends(current_key),
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    result = services.create_release(
        app["id"],
        actor,
        version_name=version_name,
        version_code=version_code,
        description=description,
        force_update=force_update,
        make_latest=make_latest,
        released_at_ms=released_at,
        upload=file,
    )
    row = db.query_one("SELECT * FROM releases WHERE id = ?", (result["id"],))
    return release_json(app["slug"], base_url_for(request), row)


@router.patch("/apps/{slug}/releases/{version_code}", summary="修改发行版元信息")
def patch_release(
    request: Request,
    slug: str,
    version_code: int,
    payload: ReleaseUpdate,
    key: Dict[str, Any] = Depends(current_key),
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    release = _release_by_code(app["id"], version_code)

    services.update_release(
        app["id"],
        actor,
        release["id"],
        version_name=(
            payload.version_name if payload.version_name is not None else release["version_name"]
        ),
        description=(
            payload.description if payload.description is not None else release["description"]
        ),
        force_update=(
            bool(payload.force_update)
            if payload.force_update is not None
            else bool(release["force_update"])
        ),
        released_at_ms=payload.released_at,
    )
    row = db.query_one("SELECT * FROM releases WHERE id = ?", (release["id"],))
    # 与 list/create/get 用同一份结构：客户端不该因为换了方法就少拿到 size/sha256/download_url
    return release_json(app["slug"], base_url_for(request), row)


@router.post("/apps/{slug}/releases/{version_code}/latest", summary="设为最新版本")
def mark_latest(
    slug: str, version_code: int, key: Dict[str, Any] = Depends(current_key)
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    release = _release_by_code(app["id"], version_code)
    name = services.set_latest_release(app["id"], actor, release["id"])
    return {"is_latest": True, "version_code": version_code, "version_name": name}


@router.delete("/apps/{slug}/releases/{version_code}", summary="删除发行版及其产物文件")
def delete_release(
    slug: str, version_code: int, key: Dict[str, Any] = Depends(current_key)
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    release = _release_by_code(app["id"], version_code)
    name = services.delete_release(app["id"], actor, release["id"])
    return {"deleted": True, "version_code": version_code, "version_name": name}


# ---------------------------------------------------------------- 公告


@router.get("/apps/{slug}/notices", summary="列出公告")
def list_notices(slug: str, key: Dict[str, Any] = Depends(current_key)) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    rows = _notices_of(app["id"])
    return {"count": len(rows), "notices": [notice_json(row) for row in rows]}


@router.post("/apps/{slug}/notices", status_code=201, summary="发布公告")
def create_notice(
    slug: str, payload: NoticeCreate, key: Dict[str, Any] = Depends(current_key)
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    result = services.create_notice(
        app["id"],
        actor,
        title=payload.title,
        content=payload.content,
        published_at_ms=payload.published_at,
    )
    return notice_json(db.query_one("SELECT * FROM notices WHERE id = ?", (result["id"],)))


@router.patch("/apps/{slug}/notices/{notice_id}", summary="修改公告")
def patch_notice(
    slug: str,
    notice_id: int,
    payload: NoticeUpdate,
    key: Dict[str, Any] = Depends(current_key),
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    notice = db.query_one(
        "SELECT * FROM notices WHERE id = ? AND app_id = ?", (notice_id, app["id"])
    )
    if notice is None:
        raise HTTPException(status_code=404, detail="公告不存在")

    services.update_notice(
        app["id"],
        actor,
        notice_id,
        title=payload.title if payload.title is not None else notice["title"],
        content=payload.content if payload.content is not None else notice["content"],
        published_at_ms=payload.published_at,
    )
    return notice_json(db.query_one("SELECT * FROM notices WHERE id = ?", (notice_id,)))


@router.delete("/apps/{slug}/notices/{notice_id}", summary="删除公告")
def delete_notice(
    slug: str, notice_id: int, key: Dict[str, Any] = Depends(current_key)
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    title = services.delete_notice(app["id"], actor, notice_id)
    return {"deleted": True, "id": notice_id, "title": title}


# ---------------------------------------------------------------- 分享链接


@router.get("/apps/{slug}/share-links", summary="列出分享链接及其效果")
def list_share_links(
    request: Request,
    slug: str,
    days: int = config.STATS_DEFAULT_RANGE_DAYS,
    key: Dict[str, Any] = Depends(current_key),
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    base = base_url_for(request)
    days = config.clamp_days(days)
    links = analytics.share_link_stats(app["id"], days)
    for item in links:
        item["url"] = f"{base}/{app['slug']}/s/{item['code']}"
    return {"days": days, "count": len(links), "share_links": links}


@router.post("/apps/{slug}/share-links", status_code=201, summary="生成分享链接")
def create_share_link(
    request: Request,
    slug: str,
    payload: ShareLinkCreate,
    key: Dict[str, Any] = Depends(current_key),
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    result = services.create_share_link(app["id"], actor, note=payload.note, target=payload.target)
    result["url"] = f"{base_url_for(request)}/{app['slug']}/s/{result['code']}"
    return result


@router.delete("/apps/{slug}/share-links/{link_id}", summary="删除分享链接")
def delete_share_link(
    slug: str, link_id: int, key: Dict[str, Any] = Depends(current_key)
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    link = services.delete_share_link(app["id"], actor, link_id)
    return {"deleted": True, "id": link_id, "code": link["code"]}


# ---------------------------------------------------------------- 统计


@router.get("/apps/{slug}/stats", summary="访问统计")
def app_stats(
    slug: str,
    days: int = config.STATS_DEFAULT_RANGE_DAYS,
    include_bots: bool = False,
    key: Dict[str, Any] = Depends(current_key),
) -> Dict[str, Any]:
    actor = actor_of(key)
    app = services.find_app_by_slug(slug, actor)
    days = config.clamp_days(days)
    return {
        "days": days,
        "include_bots": include_bots,
        "summary": analytics.summary(app["id"], days, include_bots),
        "daily": analytics.daily_series(app["id"], days, include_bots),
        "referrers": analytics.referrers(app["id"], days, include_bots=include_bots),
        "clients": analytics.ua_breakdown(app["id"], days, include_bots),
        "recent_downloads": analytics.recent_downloads(app["id"]),
    }
