"""对外接口：应用介绍页 + 更新检查 + 公告。

约定：
- `{slug}` 是应用短链，例如 /QUTSchedule。
- 所有 JSON 接口都带 Cache-Control: no-cache，让客户端每次都能拿到最新版本。
- 应用被停用或不存在时一律 404，不区分原因，避免泄露内部状态。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.responses import HTMLResponse

from .. import config, db, storage
from ..serializers import (
    notice_list_payload,
    notice_payload,
    release_list_payload,
    release_payload,
)
from ..utils import escape

logger = logging.getLogger("apppublisher.public")

router = APIRouter()

_FALLBACK_MEDIA_TYPE = "application/octet-stream"
_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


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


@router.get("/", response_class=HTMLResponse)
def site_index() -> HTMLResponse:
    """站点根页：列出所有已上线的应用，方便自己点进去看。"""
    apps = db.query_all(
        "SELECT slug, name, updated_at FROM apps WHERE enabled = 1 ORDER BY name COLLATE NOCASE"
    )
    if apps:
        items = "\n".join(
            f'<li><a href="/{escape(row["slug"])}">{escape(row["name"])}</a>'
            f'<code>/{escape(row["slug"])}</code></li>'
            for row in apps
        )
        body = f'<ul class="apps">{items}</ul>'
    else:
        body = '<p class="empty">还没有已上线的应用。</p>'

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>应用列表</title>
<style>
  body {{ margin:0; background:#0f1115; color:#e6e9ef; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
  .wrap {{ max-width:720px; margin:0 auto; padding:56px 24px; }}
  h1 {{ font-size:24px; margin:0 0 24px; }}
  ul.apps {{ list-style:none; padding:0; margin:0; }}
  ul.apps li {{ display:flex; align-items:center; justify-content:space-between; gap:16px;
                padding:16px 20px; background:#171a21; border:1px solid #232833; border-radius:12px; margin-bottom:10px; }}
  ul.apps a {{ color:#e6e9ef; text-decoration:none; font-size:16px; }}
  ul.apps a:hover {{ color:#60a5fa; }}
  code {{ color:#7d8698; font-size:12px; }}
  .empty {{ color:#8b93a7; }}
</style>
</head>
<body><div class="wrap"><h1>应用列表</h1>{body}</div></body>
</html>"""
    return HTMLResponse(html, headers={"X-Content-Type-Options": "nosniff"})


@router.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@router.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "apps": db.query_value("SELECT COUNT(*) FROM apps") or 0}


# ---------------------------------------------------------------- 介绍页


@router.get("/{slug}", response_class=HTMLResponse)
def app_intro(slug: str) -> HTMLResponse:
    """返回后台粘贴/上传的 HTML 原文。"""
    app = load_enabled_app(slug)
    html = app["intro_html"] or _fallback_page(app)
    return HTMLResponse(
        html,
        headers={
            # 内容由站点管理员自行提供，这里只做基础的 MIME 嗅探防护。
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Cache-Control": "no-cache",
        },
    )


# ---------------------------------------------------------------- 更新检查


@router.get("/{slug}/releases/latest")
def releases_latest(request: Request, slug: str) -> JSONResponse:
    app = load_enabled_app(slug)
    release = find_latest_release(app["id"])
    if release is None:
        raise HTTPException(status_code=404, detail="该应用还没有发布任何版本")
    release["app_name"] = app["name"]
    payload = release_payload(release, app["slug"], base_url_for(request))
    return JSONResponse(payload, headers=_NO_CACHE)


@router.get("/{slug}/releases")
def releases_list(request: Request, slug: str) -> JSONResponse:
    app = load_enabled_app(slug)
    rows = db.query_all(
        "SELECT * FROM releases WHERE app_id = ? ORDER BY version_code DESC, id DESC",
        (app["id"],),
    )
    for row in rows:
        row["app_name"] = app["name"]
    return JSONResponse(
        release_list_payload(rows, app["slug"], base_url_for(request)), headers=_NO_CACHE
    )


@router.get("/{slug}/build/{version_code}")
def release_build(slug: str, version_code: int) -> FileResponse:
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
    app = load_enabled_app(slug)
    rows = db.query_all(
        "SELECT * FROM notices WHERE app_id = ? ORDER BY published_at DESC, id DESC",
        (app["id"],),
    )
    return JSONResponse(notice_list_payload(rows), headers=_NO_CACHE)


@router.get("/{slug}/notices/{notice_id}")
def notice_detail(slug: str, notice_id: int) -> JSONResponse:
    app = load_enabled_app(slug)
    notice = db.query_one(
        "SELECT * FROM notices WHERE app_id = ? AND id = ?", (app["id"], notice_id)
    )
    if notice is None:
        raise HTTPException(status_code=404, detail="公告不存在")
    return JSONResponse(notice_payload(notice), headers=_NO_CACHE)
