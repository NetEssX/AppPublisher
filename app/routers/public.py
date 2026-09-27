"""对外接口：应用介绍页 + 更新检查 + 公告。

约定：
- `{slug}` 是应用短链，例如 /QUTSchedule。
- 所有 JSON 接口都带 Cache-Control: no-cache，让客户端每次都能拿到最新版本。
- 应用被停用或不存在时一律 404，不区分原因，避免泄露内部状态。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse

from .. import analytics, config, db, storage
from ..serializers import (
    notice_list_payload,
    notice_payload,
    release_list_payload,
    release_payload,
)
from ..utils import escape, format_time, human_size

logger = logging.getLogger("apppublisher.public")

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.filters["human_size"] = human_size
templates.env.filters["format_time"] = format_time

_FALLBACK_MEDIA_TYPE = "application/octet-stream"
_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}
# 列表接口的返回上限。客户端只需要最近的若干条，不设上限会随着发布次数增长把响应撑爆。
_LIST_LIMIT = 100


# ---------------------------------------------------------------- 内部工具


def load_enabled_app(slug: str) -> Dict[str, Any]:
    app = db.query_one("SELECT * FROM apps WHERE slug = ? AND enabled = 1", (slug,))
    if app is None:
        raise HTTPException(status_code=404, detail="应用不存在或已下线")
    return app


def base_url_for(request: Request) -> str:
    """download 字段用的站点前缀。配置了 PUBLIC_BASE_URL 就用它，否则按请求推断。"""
    if config.PUBLIC_BASE_URL:
        return config.PUBLIC_BASE_URL
    return str(request.base_url).rstrip("/")


def find_latest_release(app_id: int) -> Optional[Dict[str, Any]]:
    """优先取被标记为 latest 的版本；没有任何标记时退化为 versionCode 最大的那个。"""
    row = db.query_one(
        "SELECT * FROM releases WHERE app_id = ? AND is_latest = 1 "
        "ORDER BY version_code DESC, id DESC LIMIT 1",
        (app_id,),
    )
    if row is None:
        row = db.query_one(
            "SELECT * FROM releases WHERE app_id = ? ORDER BY version_code DESC, id DESC LIMIT 1",
            (app_id,),
        )
    return row


def latest_releases_by_app() -> Dict[int, Dict[str, Any]]:
    """一次查出每个应用的最新版本，避免按应用逐个查的 N+1。

    「最新」= is_latest 优先，其次 versionCode 最大。SQL 侧排序，
    Python 侧 setdefault 取首行即为该应用的最新版本。
    """
    result: Dict[int, Dict[str, Any]] = {}
    for row in db.query_all(
        "SELECT id, app_id, version_name, version_code, is_latest, released_at, build_size "
        "FROM releases ORDER BY app_id, is_latest DESC, version_code DESC, id DESC"
    ):
        result.setdefault(row["app_id"], row)
    return result


def find_latest_notice(app_id: int) -> Optional[Dict[str, Any]]:
    return db.query_one(
        "SELECT * FROM notices WHERE app_id = ? ORDER BY published_at DESC, id DESC LIMIT 1",
        (app_id,),
    )


def _fallback_page(app: Dict[str, Any]) -> str:
    """介绍页未填写时的占位内容，避免游客看到空白页。"""
    name = escape(app["name"])
    slug = escape(app["slug"])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<style>
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:#0f1115; color:#e6e9ef; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
  .box {{ text-align:center; padding:48px 32px; }}
  h1 {{ font-size:28px; margin:0 0 12px; }}
  p {{ color:#8b93a7; margin:0 0 28px; }}
  code {{ background:#1a1e26; padding:2px 8px; border-radius:6px; color:#9ecbff; }}
  a.btn {{ display:inline-block; padding:10px 22px; border-radius:8px; background:#3b82f6; color:#fff;
           text-decoration:none; font-size:14px; }}
</style>
</head>
<body>
  <div class="box">
    <h1>{name}</h1>
    <p>介绍页尚未填写。更新检查接口已可用：<code>/{slug}/releases/latest</code></p>
    <a class="btn" href="/{slug}/releases/latest">查看最新版本 JSON</a>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------- 站点入口


def _accent_for(slug: str) -> int:
    """由短链稳定推导一个色相角，用作没有 banner 时的渐变占位底色。"""
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 360


@router.get("/", response_class=HTMLResponse)
def site_index(request: Request) -> HTMLResponse:
    """站点根页：卡片式列出所有已上线的应用。"""
    # 只取卡片要用的列：intro_html 可能有几 MB，SELECT * 会把每个应用的整页 HTML
    # 都读进内存，而根页一个字节都用不到。
    apps = db.query_all(
        "SELECT id, slug, name, tagline, banner_path FROM apps "
        "WHERE enabled = 1 ORDER BY name COLLATE NOCASE"
    )
    latest_by_app = latest_releases_by_app()

    cards = []
    for app in apps:
        latest = latest_by_app.get(app["id"])
        cards.append(
            {
                "slug": app["slug"],
                "name": app["name"],
                "tagline": app["tagline"] or "",
                "banner_url": f"/media/{app['banner_path']}" if app["banner_path"] else "",
                "accent": _accent_for(app["slug"]),
                "initial": (app["name"] or app["slug"])[:1].upper(),
                "latest_version": latest["version_name"] if latest else "",
                "latest_code": latest["version_code"] if latest else None,
                "released_at": latest["released_at"] if latest else None,
                "size": latest["build_size"] if latest else 0,
            }
        )

    return templates.TemplateResponse(
        request,
        "index.html",
        {"cards": cards, "total": len(cards)},
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"},
    )


@router.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@router.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "apps": db.query_value("SELECT COUNT(*) FROM apps") or 0}


# ---------------------------------------------------------------- 介绍页


@router.get("/{slug}/s/{code}")
def share_redirect(request: Request, slug: str, code: str) -> RedirectResponse:
    """分享链接入口：记一次点击、写下分享码，然后跳到目标页。

    分享码不存在或已停用时**照常跳转**，只是不归因 —— 已经发出去的链接
    不该因为后台删了记录就甩个 404 到别人脸上。
    """
    app = load_enabled_app(slug)
    link = db.query_one(
        "SELECT * FROM share_links WHERE app_id = ? AND code = ? AND enabled = 1",
        (app["id"], code.strip().lower()),
    )

    if link is not None:
        # 显式传 code：这一刻 Cookie 还没写下去，自动推断拿不到。
        analytics.record(
            request, app["id"], analytics.KIND_SHARE_CLICK, share_code=link["code"]
        )

    target = f"/{app['slug']}"
    if link is not None and link["target"] == "download":
        latest = find_latest_release(app["id"])
        if latest is not None:
            target = f"/{app['slug']}/build/{latest['version_code']}"

    # 302 而不是 301：链接的目标会随「最新版」变化，也便于日后停用某个码。
    response = RedirectResponse(target, status_code=302)
    if link is not None:
        analytics.set_share_cookie(response, link["code"])
    return response


@router.get("/{slug}", response_class=HTMLResponse)
def app_intro(request: Request, slug: str) -> HTMLResponse:
    """返回后台粘贴/上传的 HTML 原文。"""
    app = load_enabled_app(slug)
    analytics.record(request, app["id"], analytics.KIND_VIEW)
    html = app["intro_html"] or _fallback_page(app)
    response = HTMLResponse(
        html,
        headers={
            # 内容由站点管理员自行提供，这里只做基础的 MIME 嗅探防护。
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Cache-Control": "no-cache",
        },
    )

    # 有人直接用带 ?ref= 的地址（而不是走 /s/{code} 跳转）时，
    # 把分享码补写进 Cookie，后续的下载与更新检查才能继续归因。
    ref = (request.query_params.get("ref") or "").strip().lower()
    if ref and analytics.SHARE_CODE_RE.match(ref) and not analytics.share_code_from_cookie(request):
        analytics.set_share_cookie(response, ref)
    return response


# ---------------------------------------------------------------- 更新检查


@router.get("/{slug}/releases/latest")
def releases_latest(request: Request, slug: str) -> JSONResponse:
    app = load_enabled_app(slug)
    release = find_latest_release(app["id"])
    if release is None:
        raise HTTPException(status_code=404, detail="该应用还没有发布任何版本")
    release["app_name"] = app["name"]
    analytics.record(request, app["id"], analytics.KIND_UPDATE_CHECK)
    payload = release_payload(release, app["slug"], base_url_for(request))
    return JSONResponse(payload, headers=_NO_CACHE)


@router.get("/{slug}/releases")
def releases_list(request: Request, slug: str) -> JSONResponse:
    """最近的版本列表，最多 _LIST_LIMIT 条（按 versionCode 从新到旧）。"""
    app = load_enabled_app(slug)
    rows = db.query_all(
        "SELECT * FROM releases WHERE app_id = ? ORDER BY version_code DESC, id DESC LIMIT ?",
        (app["id"], _LIST_LIMIT),
    )
    for row in rows:
        row["app_name"] = app["name"]
    return JSONResponse(
        release_list_payload(rows, app["slug"], base_url_for(request), _LIST_LIMIT),
        headers=_NO_CACHE,
    )


@router.get("/{slug}/build/{version_code}")
def release_build(request: Request, slug: str, version_code: int) -> FileResponse:
    """按 versionCode 分发构建产物。用版本号而非版本名定位，改版本名不会让旧链接失效。"""
    app = load_enabled_app(slug)
    release = db.query_one(
        "SELECT * FROM releases WHERE app_id = ? AND version_code = ?",
        (app["id"], version_code),
    )
    if release is None:
        raise HTTPException(status_code=404, detail="版本不存在")

    path = storage.resolve(release["build_path"])
    if path is None:
        # 数据库有记录但文件丢了，属于运维问题，要能在日志里看见。
        logger.error(
            "构建产物缺失: slug=%s version_code=%s path=%s",
            slug,
            version_code,
            release["build_path"],
        )
        raise HTTPException(status_code=410, detail="安装包已不可用，请联系开发者")

    # 只在确认文件存在、真的要下发时才计数；410 那条路径不算一次下载。
    analytics.record(request, app["id"], analytics.KIND_DOWNLOAD, release["id"])

    return FileResponse(
        path,
        media_type=release["content_type"] or _FALLBACK_MEDIA_TYPE,
        filename=storage.download_filename(
            app["slug"], release["version_name"], release["build_name"]
        ),
        headers=_NO_CACHE,
    )


# ---------------------------------------------------------------- 公告


@router.get("/{slug}/notices/latest")
def notices_latest(slug: str) -> JSONResponse:
    app = load_enabled_app(slug)
    notice = find_latest_notice(app["id"])
    if notice is None:
        raise HTTPException(status_code=404, detail="该应用还没有发布任何公告")
    return JSONResponse(notice_payload(notice), headers=_NO_CACHE)


@router.get("/{slug}/notices")
def notices_list(slug: str) -> JSONResponse:
    """最近的公告列表，最多 _LIST_LIMIT 条（按发布时间从新到旧）。"""
    app = load_enabled_app(slug)
    rows = db.query_all(
        "SELECT * FROM notices WHERE app_id = ? ORDER BY published_at DESC, id DESC LIMIT ?",
        (app["id"], _LIST_LIMIT),
    )
    return JSONResponse(notice_list_payload(rows, _LIST_LIMIT), headers=_NO_CACHE)


@router.get("/{slug}/notices/{notice_id}")
def notice_detail(slug: str, notice_id: int) -> JSONResponse:
    app = load_enabled_app(slug)
    notice = db.query_one(
        "SELECT * FROM notices WHERE app_id = ? AND id = ?", (app["id"], notice_id)
    )
    if notice is None:
        raise HTTPException(status_code=404, detail="公告不存在")
    return JSONResponse(notice_payload(notice), headers=_NO_CACHE)
