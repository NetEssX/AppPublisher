"""端到端冒烟测试：整个后台流程 + 对外接口。

用独立临时数据目录，不会碰 ./data。直接 python3 tests/smoke_test.py 运行。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from datetime import date, datetime, timedelta

DATA_DIR = tempfile.mkdtemp(prefix="apppublisher-smoke-")
os.environ["APPPUBLISHER_DATA_DIR"] = DATA_DIR
os.environ["APPPUBLISHER_DB"] = os.path.join(DATA_DIR, "test.db")
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "smoke-test-password"
os.environ["PUBLIC_BASE_URL"] = "https://example.com"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

SLUG = "QUTSchedule"
INTRO_HTML = "<!doctype html><html><body><h1>QUT课表介绍页</h1></body></html>"
APK_BYTES = b"FAKE-APK-PAYLOAD" * 512
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64
ZIP_BYTES = b"PK\x03\x04FAKE-ZIP" * 256
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')

passed = 0
failed = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed.append(label)
        print(f"  FAIL {label} {detail}")


def csrf_of(client: TestClient, url: str) -> str:
    response = client.get(url, follow_redirects=False)
    match = CSRF_RE.search(response.text)
    assert match, f"{url} 页面里找不到 csrf_token"
    return match.group(1)


def main() -> int:
    with TestClient(app) as client:
        print("\n[1] 登录")
        # 登录页用双提交 CSRF：先 GET 拿到 Cookie 与表单里的同一个随机值。
        login_token = csrf_of(client, "/admin/login")
        check(
            "登录页下发 CSRF Cookie 且与表单一致",
            client.cookies.get("apppublisher_csrf") == login_token,
        )

        no_csrf_login = client.post(
            "/admin/login",
            data={"username": "admin", "password": "smoke-test-password"},
            follow_redirects=False,
        )
        check(
            "登录缺少 CSRF 被拒",
            no_csrf_login.status_code == 303
            and "err=" in no_csrf_login.headers.get("location", ""),
            f"status={no_csrf_login.status_code}",
        )

        bad = client.post(
            "/admin/login",
            data={
                "username": "admin",
                "password": "wrong-password",
                "csrf_token": login_token,
            },
            follow_redirects=False,
        )
        check("错误密码被拒", bad.status_code == 303 and "err=" in bad.headers.get("location", ""))

        ok = client.post(
            "/admin/login",
            data={
                "username": "admin",
                "password": "smoke-test-password",
                "csrf_token": login_token,
            },
            follow_redirects=False,
        )
        check("正确密码登录成功", ok.status_code == 303, f"status={ok.status_code}")
        check("下发了会话 Cookie", "apppublisher_session" in client.cookies)

        print("\n[2] 未登录时的防护")
        anon = TestClient(app)
        redirected = anon.get("/admin", follow_redirects=False)
        check("未登录访问后台被跳转", redirected.status_code == 303)

        print("\n[3] 创建应用")
        token = csrf_of(client, "/admin/apps/new")
        created = client.post(
            "/admin/apps",
            data={
                "csrf_token": token,
                "slug": SLUG,
                "name": "QUT课表",
                "intro_html": INTRO_HTML,
                "enabled": "1",
            },
            follow_redirects=False,
        )
        check("创建应用返回跳转", created.status_code == 303, f"status={created.status_code}")
        detail_url = created.headers["location"].split("?")[0]
        app_id = int(detail_url.rstrip("/").split("/")[-1])

        conflict = client.post(
            "/admin/apps",
            data={"csrf_token": token, "slug": SLUG, "name": "重复", "enabled": "1"},
            follow_redirects=False,
        )
        check("重复短链被拒绝", "err=" in conflict.headers.get("location", ""))

        reserved = client.post(
            "/admin/apps",
            data={"csrf_token": token, "slug": "admin", "name": "保留字", "enabled": "1"},
            follow_redirects=False,
        )
        check("保留字短链被拒绝", "err=" in reserved.headers.get("location", ""))

        print("\n[4] 介绍页")
        page = client.get(f"/{SLUG}")
        check("介绍页 200", page.status_code == 200, f"status={page.status_code}")
        check("介绍页返回粘贴的 HTML", "QUT课表介绍页" in page.text)
        check("介绍页带 nosniff", page.headers.get("x-content-type-options") == "nosniff")

        print("\n[5] 上传发行版")
        token = csrf_of(client, detail_url)
        upload = client.post(
            f"/admin/apps/{app_id}/releases",
            data={
                "csrf_token": token,
                "version_name": "v1.2.0",
                "version_code": "26092711",
                "description": "更新描述",
                "make_latest": "1",
                "force_update": "1",
            },
            files={
                "build": ("QUTSchedule-v1.2.0.apk", APK_BYTES, "application/octet-stream")
            },
            follow_redirects=False,
        )
        check(
            "上传 APK 成功",
            upload.status_code == 303 and "ok=" in upload.headers.get("location", ""),
            upload.headers.get("location", ""),
        )

        duplicate = client.post(
            f"/admin/apps/{app_id}/releases",
            data={
                "csrf_token": token,
                "version_name": "v1.2.1",
                "version_code": "26092711",
                "description": "重复版本号",
            },
            files={"build": ("dup.apk", APK_BYTES, "application/octet-stream")},
            follow_redirects=False,
        )
        check("重复 versionCode 被拒绝", "err=" in duplicate.headers.get("location", ""))

        rejected = client.post(
            f"/admin/apps/{app_id}/releases",
            data={
                "csrf_token": token,
                "version_name": "v1.2.2",
                "version_code": "26092712",
                "description": "不支持的扩展名",
            },
            files={"build": ("evil.sh", b"#!/bin/sh\n", "text/x-shellscript")},
            follow_redirects=False,
        )
        check("非白名单扩展名被拒绝", "err=" in rejected.headers.get("location", ""))

        zip_release = client.post(
            f"/admin/apps/{app_id}/releases",
            data={
                "csrf_token": token,
                "version_name": "v1.3.0-win",
                "version_code": "26092713",
                "description": "Windows 桌面版",
            },
            files={"build": ("QUTSchedule-win.zip", ZIP_BYTES, "application/zip")},
            follow_redirects=False,
        )
        check(
            "上传非 APK 产物（zip）成功",
            zip_release.status_code == 303 and "ok=" in zip_release.headers.get("location", ""),
            zip_release.headers.get("location", ""),
        )

        print("\n[6] 更新检查接口")
        latest = client.get(f"/{SLUG}/releases/latest")
        check("releases/latest 200", latest.status_code == 200)
        payload = latest.json()
        check(
            "versionName 正确", payload.get("versionName") == "v1.2.0", str(payload.get("versionName"))
        )
        check("versionCode 是整数", payload.get("versionCode") == 26092711)
        check("desc 正确", payload.get("desc") == "更新描述")
        check(
            "timestamp 是毫秒整数",
            isinstance(payload.get("timestamp"), int) and payload["timestamp"] > 10**12,
        )
        check(
            "download 用了 PUBLIC_BASE_URL",
            payload.get("download") == f"https://example.com/{SLUG}/build/26092711",
            str(payload.get("download")),
        )
        check("size 与上传字节数一致", payload.get("size") == len(APK_BYTES))
        check("forceUpdate 透传", payload.get("forceUpdate") is True)
        check("no-cache 头存在", "no-cache" in latest.headers.get("cache-control", ""))

        print("\n[7] 构建产物下载")
        apk = client.get(f"/{SLUG}/build/26092711")
        check("APK 下载 200", apk.status_code == 200)
        check("APK 内容一致", apk.content == APK_BYTES)
        check(
            "APK MIME 按扩展名判定",
            apk.headers.get("content-type") == "application/vnd.android.package-archive",
            apk.headers.get("content-type", ""),
        )
        check(
            "下载文件名为服务端生成",
            f"{SLUG}-v1.2.0.apk" in apk.headers.get("content-disposition", ""),
            apk.headers.get("content-disposition", ""),
        )

        zip_file = client.get(f"/{SLUG}/build/26092713")
        check("zip 下载 200", zip_file.status_code == 200)
        check("zip 内容一致", zip_file.content == ZIP_BYTES)
        check(
            "zip MIME 与 APK 不同",
            zip_file.headers.get("content-type") == "application/zip",
            zip_file.headers.get("content-type", ""),
        )
        check(
            "zip 下载文件名保留 .zip",
            f"{SLUG}-v1.3.0-win.zip" in zip_file.headers.get("content-disposition", ""),
            zip_file.headers.get("content-disposition", ""),
        )

        missing = client.get(f"/{SLUG}/build/99999999")
        check("不存在的版本返回 404", missing.status_code == 404)
        legacy = client.get(f"/{SLUG}/releases/apk/26092711")
        check("旧地址 /releases/apk 已移除", legacy.status_code == 404, f"status={legacy.status_code}")

        print("\n[8] 公告")
        token = csrf_of(client, detail_url)
        notice = client.post(
            f"/admin/apps/{app_id}/notices",
            data={"csrf_token": token, "title": "维护通知", "content": "今晚 23:00 维护"},
            follow_redirects=False,
        )
        check("发布公告成功", notice.status_code == 303 and "ok=" in notice.headers.get("location", ""))

        latest_notice = client.get(f"/{SLUG}/notices/latest")
        check("notices/latest 200", latest_notice.status_code == 200)
        notice_payload = latest_notice.json()
        check(
            "公告字段齐全",
            set(notice_payload) == {"title", "content", "timestamp", "id"},
            str(sorted(notice_payload)),
        )
        check("公告标题正确", notice_payload.get("title") == "维护通知")
        check("公告 id 是整数", isinstance(notice_payload.get("id"), int))

        print("\n[9] 安全防护")
        no_csrf = client.post(
            f"/admin/apps/{app_id}/notices",
            data={"title": "无 CSRF", "content": "x"},
            follow_redirects=False,
        )
        check("缺少 CSRF 被拒 400", no_csrf.status_code == 400, f"status={no_csrf.status_code}")

        bad_csrf = client.post(
            f"/admin/apps/{app_id}/notices",
            data={"csrf_token": "0" * 40, "title": "错 CSRF", "content": "x"},
            follow_redirects=False,
        )
        check("错误 CSRF 被拒 400", bad_csrf.status_code == 400, f"status={bad_csrf.status_code}")

        traversal = client.get(f"/{SLUG}/build/../../etc/passwd")
        check("路径穿越被拦", traversal.status_code in (404, 400), f"status={traversal.status_code}")

        logout_no_csrf = client.post("/admin/logout", follow_redirects=False)
        check(
            "登出缺少 CSRF 被拒 400",
            logout_no_csrf.status_code == 400,
            f"status={logout_no_csrf.status_code}",
        )
        check("登出被拒后仍是登录态", client.get("/admin", follow_redirects=False).status_code == 200)

        svg = client.post(
            f"/admin/apps/{app_id}/images",
            data={"csrf_token": token},
            files={
                "image": ("x.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml")
            },
            follow_redirects=False,
        )
        check("SVG 图片被拒（同源内联渲染会执行脚本）", "err=" in svg.headers.get("location", ""))

        bad_intro = client.post(
            f"/admin/apps/{app_id}",
            data={"csrf_token": token, "slug": SLUG, "name": "QUT课表", "enabled": "1"},
            files={"intro_file": ("page.md", b"# hi", "text/markdown")},
            follow_redirects=False,
        )
        check("非 HTML 的介绍页文件被拒", "err=" in bad_intro.headers.get("location", ""))

        check(
            "账号页不给自己提供重置/删除入口",
            "改自己的密码请走" in client.get("/admin/accounts").text,
        )

        limited = client.get(f"/{SLUG}/releases").json()
        check(
            "releases 列表带 limit 且条数正确",
            limited.get("limit") == 100 and limited.get("count") == 2,
            str(limited.get("limit")),
        )

        print("\n[9.5] 事务与工具函数")
        from app import db as dbmod
        from app.utils import format_time, redirect_with

        with dbmod.transaction() as outer:
            dbmod.execute_tx(
                outer,
                "INSERT INTO notices (app_id, title, content, published_at, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (app_id, "嵌套1", "x", 1, 1),
            )
            with dbmod.transaction() as inner:
                dbmod.execute_tx(
                    inner,
                    "INSERT INTO notices (app_id, title, content, published_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (app_id, "嵌套2", "x", 1, 1),
                )
        nested = dbmod.query_value(
            "SELECT COUNT(*) FROM notices WHERE app_id = ? AND title LIKE '嵌套%'", (app_id,)
        )
        check("嵌套事务可重入（内层用 SAVEPOINT）", nested == 2, f"count={nested}")

        check("format_time(0) 不当成空值", format_time(0) != "-", format_time(0))
        check("format_time(None) 返回占位符", format_time(None) == "-")

        frag = redirect_with("/admin/x#tab", ok="1").headers["location"]
        check("redirect_with 把查询串放在 fragment 之前", frag == "/admin/x?ok=1#tab", frag)
        empty = redirect_with("/admin/x?", ok="1").headers["location"]
        check("redirect_with 不产生空查询段", empty == "/admin/x?ok=1", empty)

        print("\n[9.6] 非 ASCII 输入与节流表淘汰")
        from app import security as secmod

        # hmac.compare_digest 对 str 要求纯 ASCII，非 ASCII 会抛 TypeError → 500。
        # 两个入口都要挡住：写操作的 require_csrf 与登录页的 double_submit_ok。
        weird = client.post(
            f"/admin/apps/{app_id}/notices",
            data={"csrf_token": "é", "title": "x", "content": "y"},
            follow_redirects=False,
        )
        check(
            "写操作收到非 ASCII csrf_token 返回 400 而非 500",
            weird.status_code == 400,
            f"status={weird.status_code}",
        )

        weird_login = client.post(
            "/admin/login",
            data={"username": "admin", "password": "x", "csrf_token": "é"},
            follow_redirects=False,
        )
        check(
            "登录页收到非 ASCII csrf_token 不 500",
            weird_login.status_code == 303,
            f"status={weird_login.status_code}",
        )

        # 节流表超容量时必须定向淘汰：整体 clear() 会把正在生效的锁定一起抹掉。
        now = time.time()
        with secmod._failures_lock:
            secmod._failures.clear()
            # victim 的 seen 最旧，排序后第一个被考虑，只有「跳过已锁定条目」才能保住它
            secmod._failures["victim"] = (secmod._LOCK_AFTER, now + 60, now - 100)
            for i in range(secmod._MAX_TRACKED + 10):
                secmod._failures[f"spray{i}"] = (1, 0.0, now)
            secmod._sweep_locked(now)
            remaining = len(secmod._failures)
        check("超容量后表收敛到上限附近", remaining <= secmod._MAX_TRACKED + 1, f"size={remaining}")
        check("超容量淘汰不会解除已有锁定", secmod.login_locked_for("victim") > 0)

        # 未锁定的条目仍然要被清掉，否则淘汰没意义
        with secmod._failures_lock:
            spray_left = sum(1 for k in secmod._failures if k.startswith("spray"))
            secmod._failures.clear()
        check("超容量淘汰确实丢弃了最旧的未锁定条目", spray_left < secmod._MAX_TRACKED + 10, f"left={spray_left}")

        print("\n[9.7] 分页 / 封面 / 统计")
        from app import analytics as anamod
        from app import config as cfgmod
        from app import db as dbmod2

        token = csrf_of(client, detail_url)

        detail = client.get(f"/admin/apps/{app_id}?days=7")
        check(
            "详情页含 6 个分页入口",
            all(
                marker in detail.text
                for marker in (
                    'data-tab="intro"',
                    'data-tab="media"',
                    'data-tab="releases"',
                    'data-tab="notices"',
                    'data-tab="stats"',
                    'data-tab="danger"',
                )
            ),
        )
        check("统计区间可切换", 'href="?days=180#stats"' in detail.text)

        # ---- 封面 ----
        check("未设封面时根页用占位块", "placeholder" in client.get("/").text)
        check("根页含应用名与简介", "QUT课表" in client.get("/").text)
        from app.utils import human_size as human_size_fn

        check(
            "根页卡片显示最新版体积",
            human_size_fn(len(APK_BYTES)) in client.get("/").text,
            human_size_fn(len(APK_BYTES)),
        )
        check(
            "分页在禁用 JS 时有展开回退",
            "<noscript>" in detail.text and ".panel[hidden]" in detail.text,
        )

        up = client.post(
            f"/admin/apps/{app_id}/banner",
            data={"csrf_token": token},
            files={"banner": ("b.png", PNG_BYTES, "image/png")},
            follow_redirects=False,
        )
        check("上传封面成功", "ok=" in up.headers.get("location", ""), up.headers.get("location", ""))
        banner_rel = dbmod2.query_value("SELECT banner_path FROM apps WHERE id = ?", (app_id,))
        check("封面路径已入库", bool(banner_rel) and banner_rel.endswith(".png"), str(banner_rel))
        check("根页卡片显示封面", f'src="/media/{banner_rel}"' in client.get("/").text)

        old_file = pathlib.Path(DATA_DIR) / "uploads" / str(banner_rel)
        client.post(
            f"/admin/apps/{app_id}/banner",
            data={"csrf_token": token},
            files={"banner": ("b2.png", PNG_BYTES, "image/png")},
            follow_redirects=False,
        )
        banner_rel2 = dbmod2.query_value("SELECT banner_path FROM apps WHERE id = ?", (app_id,))
        check("换封面后旧文件被清理", banner_rel2 != banner_rel and not old_file.exists())

        client.post(
            f"/admin/apps/{app_id}/banner/delete", data={"csrf_token": token}, follow_redirects=False
        )
        check(
            "移除封面成功",
            dbmod2.query_value("SELECT banner_path FROM apps WHERE id = ?", (app_id,)) is None,
        )

        # ---- 媒体资源列表 ----
        up_img = client.post(
            f"/admin/apps/{app_id}/images",
            data={"csrf_token": token},
            files={"image": ("screenshot.png", PNG_BYTES, "image/png")},
            follow_redirects=False,
        )
        check("上传介绍页图片成功", "ok=" in up_img.headers.get("location", ""))
        asset = dbmod2.query_one(
            "SELECT * FROM assets WHERE app_id = ? ORDER BY id DESC LIMIT 1", (app_id,)
        )
        check("图片登记入库", asset is not None)
        check(
            "记录了原始文件名",
            asset is not None and asset["original_name"] == "screenshot.png",
            str(asset and asset["original_name"]),
        )

        media_page = client.get(f"/admin/apps/{app_id}#media").text
        check(
            "媒体页列出已上传的图片",
            f"/media/{asset['path']}" in media_page and "screenshot.png" in media_page,
        )
        check(
            "媒体页给出可直接复制的完整地址",
            f'value="https://example.com/media/{asset["path"]}"' in media_page,
        )

        # 模拟「升级前上传、没有归属记录」的遗留文件
        (pathlib.Path(DATA_DIR) / "uploads" / "images" / "legacy-orphan.png").write_bytes(PNG_BYTES)
        media_page2 = client.get(f"/admin/apps/{app_id}#media").text
        check(
            "未登记的遗留图片也会列出",
            "legacy-orphan.png" in media_page2 and "未登记的图片" in media_page2,
        )

        # 不能跨应用删除图片
        other_app = client.post(
            "/admin/apps",
            data={"csrf_token": token, "slug": "OtherApp", "name": "另一个", "enabled": "1"},
            follow_redirects=False,
        )
        other_id = int(other_app.headers["location"].split("?")[0].rstrip("/").split("/")[-1])
        cross = client.post(
            f"/admin/apps/{other_id}/images/{asset['id']}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        check("不能通过别的应用删除本应用的图片", "err=" in cross.headers.get("location", ""))
        check(
            "跨应用删除被拒后记录仍在",
            dbmod2.query_one("SELECT id FROM assets WHERE id = ?", (asset["id"],)) is not None,
        )
        client.post(
            f"/admin/apps/{other_id}/delete", data={"csrf_token": token}, follow_redirects=False
        )

        asset_file = pathlib.Path(DATA_DIR) / "uploads" / asset["path"]
        rm_img = client.post(
            f"/admin/apps/{app_id}/images/{asset['id']}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        check("删除图片成功", "ok=" in rm_img.headers.get("location", ""))
        check(
            "删除后记录与磁盘文件都清理了",
            dbmod2.query_one("SELECT id FROM assets WHERE id = ?", (asset["id"],)) is None
            and not asset_file.exists(),
        )

        # 留一张给 [10] 验证「删除应用会连登记的图片一起清理」
        client.post(
            f"/admin/apps/{app_id}/images",
            data={"csrf_token": token},
            files={"image": ("kept.png", PNG_BYTES, "image/png")},
            follow_redirects=False,
        )
        kept = dbmod2.query_one(
            "SELECT path FROM assets WHERE app_id = ? ORDER BY id DESC LIMIT 1", (app_id,)
        )
        check("待验证的图片已登记", kept is not None)

        # ---- 采集 ----
        dbmod2.execute("DELETE FROM events WHERE app_id = ?", (app_id,))
        client.get(f"/{SLUG}")
        client.get(f"/{SLUG}/releases/latest")
        client.get(f"/{SLUG}/build/26092711")

        kinds = {
            row["kind"]: row["total"]
            for row in dbmod2.query_all(
                "SELECT kind, COUNT(*) AS total FROM events WHERE app_id = ? GROUP BY kind",
                (app_id,),
            )
        }
        check("介绍页访问被记录", kinds.get("view") == 1, str(kinds))
        check("更新检查被记录", kinds.get("update_check") == 1, str(kinds))
        check("下载被记录", kinds.get("download") == 1, str(kinds))
        check(
            "下载事件关联到具体版本",
            dbmod2.query_value(
                "SELECT release_id FROM events WHERE app_id = ? AND kind = 'download' "
                "ORDER BY id DESC LIMIT 1",
                (app_id,),
            )
            is not None,
        )

        client.get(f"/{SLUG}", headers={"Referer": "https://news.ycombinator.com/item?id=1"})
        check(
            "外部来源被记录",
            dbmod2.query_value(
                "SELECT referrer_host FROM events WHERE app_id = ? AND kind = 'view' "
                "ORDER BY id DESC LIMIT 1",
                (app_id,),
            )
            == "news.ycombinator.com",
        )
        client.get(f"/{SLUG}", headers={"Referer": "http://testserver/elsewhere"})
        check(
            "站内跳转不计为来源",
            dbmod2.query_value(
                "SELECT referrer_host FROM events WHERE app_id = ? AND kind = 'view' "
                "ORDER BY id DESC LIMIT 1",
                (app_id,),
            )
            == "",
        )

        # ---- 机器人判定：宁可漏判，也绝不能误判真实客户端 ----

        def last_event() -> dict:
            return dbmod2.query_one(
                "SELECT is_bot, ua_class FROM events WHERE app_id = ? ORDER BY id DESC LIMIT 1",
                (app_id,),
            )

        CRAWLERS = [
            "Googlebot/2.1 (+http://www.google.com/bot.html)",
            "Mozilla/5.0 (compatible; bingbot/2.0; +http://bing.com/bingbot.htm)",
            "Baiduspider/2.0",
            "Mozilla/5.0 (compatible; UptimeRobot/2.0)",
            "facebookexternalhit/1.1",
        ]
        missed = []
        for ua in CRAWLERS:
            client.get(f"/{SLUG}", headers={"User-Agent": ua})
            if not last_event()["is_bot"]:
                missed.append(ua)
        check("明确的爬虫仍能被识别", not missed, str(missed))

        # 这些都是真实客户端或通用 HTTP 库，判成机器人就会把用户流量吃掉
        CLIENTS = [
            "",  # 裸 socket / Qt QNetworkAccessManager 默认不发 UA
            "okhttp/4.12.0",
            "Dalvik/2.1.0 (Linux; U; Android 13; Pixel 7 Build/TQ2A)",
            "AndroidDownloadManager/13 (Linux; U; Android 13)",
            "CFNetwork/1494.0.7 Darwin/23.4.0",
            "Dart/3.3 (dart:io)",
            "Go-http-client/1.1",
            "curl/8.4.0",
            "python-requests/2.31.0",
            "httpx/0.27.0",
            "aiohttp/3.9.5",
            "MyRobot/1.0",  # 旧的 "bot\b" 会误伤这一类的
        ]
        misjudged = []
        for ua in CLIENTS:
            client.get(f"/{SLUG}", headers={"User-Agent": ua})
            if last_event()["is_bot"]:
                misjudged.append(ua or "(空 UA)")
        check("真实客户端与通用库都不被判为机器人", not misjudged, str(misjudged))

        client.get(f"/{SLUG}", headers={"User-Agent": "Dalvik/2.1.0 (Linux; U; Android 13)"})
        check("Android 客户端归到 android 类", last_event()["ua_class"] == "android")
        client.get(f"/{SLUG}", headers={"User-Agent": "okhttp/4.12.0"})
        check("无平台信息的库 UA 归到 lib 类", last_event()["ua_class"] == "lib")
        client.get(f"/{SLUG}", headers={"User-Agent": ""})
        check("空 UA 归到 unknown 类", last_event()["ua_class"] == "unknown")

        # ---- 关键：客户端接口永不因机器人判定而丢记录 ----
        dbmod2.execute("DELETE FROM events WHERE app_id = ?", (app_id,))
        bot_ua = {"User-Agent": "Googlebot/2.1"}
        client.get(f"/{SLUG}", headers=bot_ua)
        client.get(f"/{SLUG}/releases/latest", headers=bot_ua)
        client.get(f"/{SLUG}/build/26092711", headers=bot_ua)

        sums = anamod.summary(app_id, 30)
        check("机器人浏览默认不计入介绍页 PV", sums["kinds"]["view"]["pv"] == 0, str(sums["kinds"]["view"]))
        check("被挡下的机器人浏览数可查", sums["bot_views"] == 1, str(sums["bot_views"]))
        check(
            "更新检查从不做机器人过滤",
            sums["kinds"]["update_check"]["pv"] == 1,
            str(sums["kinds"]["update_check"]),
        )
        check("下载从不做机器人过滤", sums["kinds"]["download"]["pv"] == 1, str(sums["kinds"]["download"]))
        check("累计下载同样包含此类请求", sums["total_download"] == 1, str(sums["total_download"]))

        sums_all = anamod.summary(app_id, 30, include_bots=True)
        check(
            "切到包含机器人后介绍页 PV 恢复",
            sums_all["kinds"]["view"]["pv"] == 1,
            str(sums_all["kinds"]["view"]),
        )

        bots_page = client.get(f"/admin/apps/{app_id}?days=30&bots=1")
        check(
            "统计页的 bots=1 开关可用",
            bots_page.status_code == 200 and "已包含" in bots_page.text,
            f"status={bots_page.status_code}",
        )
        check("默认统计页提示有机器人被挡下", "机器人访问未计入" in client.get(f"/admin/apps/{app_id}?days=30").text)

        dbmod2.execute("DELETE FROM events WHERE app_id = ?", (app_id,))
        android_ua = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit"}
        client.get(f"/{SLUG}", headers=android_ua)
        client.get(f"/{SLUG}", headers=android_ua)
        client.get(f"/{SLUG}", headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)"})
        uv = dbmod2.query_value("SELECT COUNT(DISTINCT visitor) FROM events WHERE app_id = ?", (app_id,))
        check("同 IP+UA 记一个访客，换 UA 记两个", uv == 2, f"uv={uv}")
        visitor = dbmod2.query_value("SELECT visitor FROM events WHERE app_id = ? LIMIT 1", (app_id,))
        check("访客标识不是原始 IP", visitor not in ("testclient", "", None), str(visitor))

        stats_page = client.get(f"/admin/apps/{app_id}?days=7")
        check(
            "统计页渲染出各项指标",
            "数据统计" in stats_page.text
            and "独立访客 (UV)" in stats_page.text
            and "访问来源" in stats_page.text
            and "每日趋势" in stats_page.text,
        )

        # ---- 过期清理 ----
        old_ms = anamod.now_ms() - (cfgmod.STATS_RETENTION_DAYS + 1) * 86400 * 1000
        dbmod2.execute(
            "INSERT INTO events (app_id, kind, day, created_at, visitor, is_bot) VALUES (?,?,?,?,?,0)",
            (app_id, "view", "2000-01-01", old_ms, "deadbeefdeadbeef"),
        )
        purged = anamod.purge_old_events(force=True)
        check("过期明细被清理", purged >= 1, f"purged={purged}")
        check(
            "清理后旧行不再存在",
            dbmod2.query_value("SELECT COUNT(*) FROM events WHERE created_at <= ?", (old_ms,)) == 0,
        )

        print("\n[9.8] 分享链接与归因")
        token = csrf_of(client, detail_url)

        made = client.post(
            f"/admin/apps/{app_id}/share",
            data={"csrf_token": token, "note": "B 站动态", "target": "intro"},
            follow_redirects=False,
        )
        check("生成分享链接成功", "ok=" in made.headers.get("location", ""), made.headers.get("location", ""))
        link = dbmod2.query_one("SELECT * FROM share_links WHERE app_id = ? ORDER BY id DESC LIMIT 1", (app_id,))
        check("分享码已入库", link is not None and len(link["code"]) == 8, str(link and link["code"]))
        check("备注已保存", link and link["note"] == "B 站动态", str(link and link["note"]))

        detail_share = client.get(f"/admin/apps/{app_id}#share").text
        check("后台列出分享链接", f"/{SLUG}/s/{link['code']}" in detail_share and "B 站动态" in detail_share)
        check("页面给出完整可复制地址", f'value="https://example.com/{SLUG}/s/{link["code"]}"' in detail_share)

        dbmod2.execute("DELETE FROM events WHERE app_id = ?", (app_id,))
        client.cookies.delete("apppublisher_share")

        jump = client.get(f"/{SLUG}/s/{link['code']}", follow_redirects=False)
        check("分享链接 302 跳转", jump.status_code == 302, f"status={jump.status_code}")
        check("跳转到介绍页", jump.headers.get("location") == f"/{SLUG}", str(jump.headers.get("location")))
        check("写下了分享码 Cookie", client.cookies.get("apppublisher_share") == link["code"])
        check(
            "分享点击被记录",
            dbmod2.query_value(
                "SELECT COUNT(*) FROM events WHERE app_id = ? AND kind = 'share_click' AND share_code = ?",
                (app_id, link["code"]),
            )
            == 1,
        )

        # 带 Cookie 的后续访问与下载应当继续归因
        client.get(f"/{SLUG}")
        client.get(f"/{SLUG}/build/26092711")
        check(
            "后续介绍页访问带上分享码",
            dbmod2.query_value(
                "SELECT COUNT(*) FROM events WHERE app_id = ? AND kind = 'view' AND share_code = ?",
                (app_id, link["code"]),
            )
            == 1,
        )
        check(
            "后续下载也归因到该链接",
            dbmod2.query_value(
                "SELECT COUNT(*) FROM events WHERE app_id = ? AND kind = 'download' AND share_code = ?",
                (app_id, link["code"]),
            )
            == 1,
        )

        per_link = anamod.share_link_stats(app_id, 30)
        row = next((item for item in per_link if item["code"] == link["code"]), None)
        check("统计里能按链接看到点击/访问/下载",
              row is not None and row["clicks"] == 1 and row["views"] == 1 and row["downloads"] == 1,
              str(row))
        check("概览里有「分享带来访问」", anamod.summary(app_id, 30)["share_views"] == 1)

        # ?ref= 直接进介绍页也要归因并补写 Cookie
        client.cookies.delete("apppublisher_share")
        dbmod2.execute("DELETE FROM events WHERE app_id = ?", (app_id,))
        ref_hit = client.get(f"/{SLUG}?ref=weibo", follow_redirects=False)
        check("?ref= 访问也写入 Cookie", client.cookies.get("apppublisher_share") == "weibo")
        check("?ref= 访问带上分享码", dbmod2.query_value(
            "SELECT share_code FROM events WHERE app_id = ? ORDER BY id DESC LIMIT 1", (app_id,)) == "weibo")
        client.cookies.delete("apppublisher_share")

        # 停用 / 未知短码都不该砸 404 给已经发出去的链接
        dbmod2.execute("DELETE FROM events WHERE app_id = ?", (app_id,))
        unknown = client.get(f"/{SLUG}/s/zzzzzzzz", follow_redirects=False)
        check("未知分享码照常跳转", unknown.status_code == 302 and unknown.headers.get("location") == f"/{SLUG}")
        check("未知分享码不留下点击记录",
              dbmod2.query_value("SELECT COUNT(*) FROM events WHERE app_id = ? AND kind='share_click'", (app_id,)) == 0)

        client.post(f"/admin/apps/{app_id}/share/{link['id']}/toggle", data={"csrf_token": token},
                    follow_redirects=False)
        check("停用后 enabled=0", dbmod2.query_value("SELECT enabled FROM share_links WHERE id = ?", (link["id"],)) == 0)
        client.get(f"/{SLUG}/s/{link['code']}", follow_redirects=False)
        check("停用的链接仍跳转但不归因",
              dbmod2.query_value("SELECT COUNT(*) FROM events WHERE app_id = ? AND kind='share_click'", (app_id,)) == 0)

        # 指向「最新版下载」的链接
        dl = client.post(f"/admin/apps/{app_id}/share",
                         data={"csrf_token": token, "note": "QQ 群", "target": "download"},
                         follow_redirects=False)
        dl_link = dbmod2.query_one("SELECT * FROM share_links WHERE app_id = ? ORDER BY id DESC LIMIT 1", (app_id,))
        jump2 = client.get(f"/{SLUG}/s/{dl_link['code']}", follow_redirects=False)
        latest_code = dbmod2.query_value(
            "SELECT version_code FROM releases WHERE app_id = ? AND is_latest = 1", (app_id,)
        )
        check("下载型链接直接跳到当前最新版产物",
              jump2.headers.get("location") == f"/{SLUG}/build/{latest_code}",
              str(jump2.headers.get("location")))
        client.cookies.delete("apppublisher_share")

        # 删链接不清归因数据
        client.post(f"/admin/apps/{app_id}/share/{dl_link['id']}/delete", data={"csrf_token": token},
                    follow_redirects=False)
        check("分享链接已删除",
              dbmod2.query_one("SELECT id FROM share_links WHERE id = ?", (dl_link["id"],)) is None)
        check("删除链接后历史归因仍在",
              dbmod2.query_value("SELECT COUNT(*) FROM events WHERE app_id = ? AND share_code = ?",
                                 (app_id, dl_link["code"])) >= 1)
        client.cookies.delete("apppublisher_share")

        # 分享 Cookie 是全局的：从 A 的链接进来后再看 B 的页面，
        # 事件上会带 A 的码，但 B 的「分享带来访问」不能把它算进来。
        other = client.post(
            "/admin/apps",
            data={"csrf_token": token, "slug": "OtherApp", "name": "另一个应用", "enabled": "1"},
            follow_redirects=False,
        )
        other_id = int(other.headers["location"].split("?")[0].rstrip("/").split("/")[-1])
        dbmod2.execute("DELETE FROM events WHERE app_id = ?", (other_id,))

        client.cookies.set("apppublisher_share", link["code"])
        client.get("/OtherApp")
        check(
            "跨应用的分享码仍原样记在事件上",
            dbmod2.query_value(
                "SELECT COUNT(*) FROM events WHERE app_id = ? AND kind = 'view' AND share_code = ?",
                (other_id, link["code"]),
            )
            == 1,
        )
        check(
            "但别的应用的「分享带来访问」不把它算进来",
            anamod.summary(other_id, 30)["share_views"] == 0,
            str(anamod.summary(other_id, 30)["share_views"]),
        )
        client.cookies.delete("apppublisher_share")
        client.post(f"/admin/apps/{other_id}/delete", data={"csrf_token": token}, follow_redirects=False)

        # 来源统计只算介绍页浏览：更新检查是客户端 API 调用，不带 Referer，
        # 混进来会把「直接访问」灌满机器请求。
        dbmod2.execute("DELETE FROM events WHERE app_id = ?", (app_id,))
        client.get(f"/{SLUG}/releases/latest")
        client.get(f"/{SLUG}/releases/latest")
        client.get(f"/{SLUG}", headers={"Referer": "https://example.org/post/1"})
        ref = anamod.referrers(app_id, 30)
        check(
            "来源统计不含更新检查，只如实反映介绍页",
            ref["direct"] == 0
            and len(ref["rows"]) == 1
            and ref["rows"][0]["host"] == "example.org",
            str(ref),
        )

        # 图表与概览必须自洽：柱子总和 == 概览 total_pv
        dbmod2.execute("DELETE FROM events WHERE app_id = ?", (app_id,))
        for _ in range(3):
            client.get(f"/{SLUG}")
        client.get(f"/{SLUG}/releases/latest")
        client.get(f"/{SLUG}/notices/latest")
        series = anamod.daily_series(app_id, 7)
        summary7 = anamod.summary(app_id, 7)
        check("趋势图天数正确", len(series) == 7, str(len(series)))
        check(
            "柱子总和与概览 total_pv 一致（同一窗口口径）",
            sum(item["total"] for item in series) == summary7["total_pv"],
            f"chart={sum(item['total'] for item in series)} summary={summary7['total_pv']}",
        )

        # 直接测窗口不变量：旧实现（now - days*86400）不是自然日起点，
        # 会让「最近 N 天」横跨 N+1 个自然日，那半天的事件落不进图表。
        start_dt = datetime.fromtimestamp(anamod._since_ms(7) / 1000)
        check(
            "_since_ms 对齐到自然日 0 点",
            (start_dt.hour, start_dt.minute, start_dt.second) == (0, 0, 0),
            str(start_dt),
        )
        check(
            "_since_ms(7) 正好覆盖 7 个自然日（含今天）",
            (date.today() - start_dt.date()).days == 6,
            f"{start_dt.date()} -> {date.today()}",
        )
        check(
            "series 的窗口与 _since_ms 一致",
            anamod.daily_series(app_id, 30)[0]["day"] == str(
                (date.today() - timedelta(days=29))
            ),
            anamod.daily_series(app_id, 30)[0]["day"],
        )

        # 每个分区的表单提交后都要跳回自己的分页，不能一律落回「介绍页」
        notice_back = client.post(
            f"/admin/apps/{app_id}/notices",
            data={"csrf_token": token, "title": "分页跳转测试", "content": "x"},
            follow_redirects=False,
        )
        check(
            "发布公告后跳回公告分页",
            notice_back.headers.get("location", "").endswith("#notices"),
            notice_back.headers.get("location", ""),
        )
        # 用重复的 versionCode 触发错误分支——同样走 target，且不会真的建出版本
        release_back = client.post(
            f"/admin/apps/{app_id}/releases",
            data={
                "csrf_token": token,
                "version_name": "dup",
                "version_code": "26092711",
                "description": "重复版本号",
            },
            files={"build": ("d.apk", APK_BYTES, "application/octet-stream")},
            follow_redirects=False,
        )
        check(
            "发行版表单出错时也跳回发行版分页",
            release_back.headers.get("location", "").endswith("#releases"),
            release_back.headers.get("location", ""),
        )

        # ?ref= 的取值校验：常见写法要能用，非法值要安静丢弃
        client.cookies.delete("apppublisher_share")
        dbmod2.execute("DELETE FROM events WHERE app_id = ?", (app_id,))
        client.get(f"/{SLUG}?ref=weibo_2024", follow_redirects=False)
        check(
            "?ref= 接受下划线与连字符这类常见写法",
            dbmod2.query_value(
                "SELECT share_code FROM events WHERE app_id = ? ORDER BY id DESC LIMIT 1", (app_id,)
            )
            == "weibo_2024",
        )
        client.cookies.delete("apppublisher_share")
        client.get(f"/{SLUG}?ref=中文标记", follow_redirects=False)
        check(
            "?ref= 的非法取值被安静丢弃",
            dbmod2.query_value(
                "SELECT share_code FROM events WHERE app_id = ? ORDER BY id DESC LIMIT 1", (app_id,)
            )
            == "",
        )
        client.cookies.delete("apppublisher_share")

        # bots=1 但区间内没有机器人记录时，仍要留一个切回去的入口
        dbmod2.execute("DELETE FROM events WHERE app_id = ?", (app_id,))
        client.get(f"/{SLUG}")
        bots_only = client.get(f"/admin/apps/{app_id}?days=30&bots=1").text
        check("bots=1 且无机器人记录时仍能切回「只看真实访客」", "只看真实访客" in bots_only)
        check("区间链接里的 & 已转义为 &amp;", "&amp;bots=1" in bots_only)

        print("\n[10] 下线与删除")
        token = csrf_of(client, detail_url)
        client.post(
            f"/admin/apps/{app_id}",
            data={"csrf_token": token, "slug": SLUG, "name": "QUT课表", "intro_html": INTRO_HTML},
            follow_redirects=False,
        )
        offline = client.get(f"/{SLUG}")
        check("取消上线后介绍页 404", offline.status_code == 404, f"status={offline.status_code}")

        build_files = sorted((pathlib.Path(DATA_DIR) / "uploads" / "build").iterdir())
        check("构建产物确实落在磁盘上", len(build_files) == 2, str(build_files))

        token = csrf_of(client, detail_url)
        client.post(f"/admin/apps/{app_id}/delete", data={"csrf_token": token}, follow_redirects=False)
        check("删除应用后产物文件被清理", all(not p.exists() for p in build_files))
        check(
            "删除应用后登记的图片文件也被清理",
            kept is not None and not (pathlib.Path(DATA_DIR) / "uploads" / kept["path"]).exists(),
        )
        check(
            "删除应用后 assets 记录被级联删除",
            dbmod2.query_value("SELECT COUNT(*) FROM assets") == 0,
        )
        check("删除应用后列表为空", json.loads(client.get("/health").text)["apps"] == 0)

    print("\n[11] 冷启动并发（未设 ADMIN_PASSWORD）")
    root = str(pathlib.Path(__file__).resolve().parent.parent)
    cold = tempfile.mkdtemp(prefix="apppublisher-cold-")
    try:
        env = {**os.environ, "APPPUBLISHER_DATA_DIR": cold}
        env.pop("ADMIN_PASSWORD", None)
        # 必须一并清掉 DB 路径：只改 DATA_DIR 的话子进程仍会连到上面那个已有账号的库，
        # 于是 bootstrap 直接走「已存在」分支，测不到冷启动。
        env.pop("APPPUBLISHER_DB", None)
        boot = textwrap.dedent(
            f"""
            import sys; sys.path.insert(0, {root!r})
            from app import db
            from app.main import bootstrap_admin
            db.init_db(); bootstrap_admin()
            """
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", boot],
                env=env,
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        outputs = [p.communicate() for p in procs]
        check("冷启动不把口令打到 stdout", all(o[0].strip() == "" for o in outputs))
        crashed = [(p.returncode, o[1].strip().splitlines()[-1:]) for p, o in zip(procs, outputs) if p.returncode]
        check("并发冷启动无进程崩溃", not crashed, str(crashed))

        pw_file = pathlib.Path(cold) / ".initial_admin_password"
        key_file = pathlib.Path(cold) / ".secret_key"

        def mode_of(path: pathlib.Path) -> str:
            return oct(stat.S_IMODE(path.stat().st_mode)) if path.exists() else "(不存在)"

        check("初始口令文件权限 0600", mode_of(pw_file) == "0o600", mode_of(pw_file))
        check("密钥文件权限 0600", mode_of(key_file) == "0o600", mode_of(key_file))
        check("无 .tmp 残留", not list(pathlib.Path(cold).glob("*.tmp")))

        probe = textwrap.dedent(
            f"""
            import sys; sys.path.insert(0, {root!r})
            from app import config, db, security
            row = db.query_one("SELECT password_hash FROM users")
            pw = (config.DATA_DIR / ".initial_admin_password").read_text(encoding="utf-8").strip()
            print("USERS", db.query_value("SELECT COUNT(*) FROM users"))
            print("VERIFY", security.verify_password(pw, row["password_hash"]))
            print("KEY", config.SECRET_KEY)
            """
        )
        results = [
            subprocess.run(
                [sys.executable, "-c", probe], env=env, cwd=root, capture_output=True, text=True
            ).stdout
            for _ in range(2)
        ]

        def field(text: str, name: str) -> str:
            for line in text.splitlines():
                if line.startswith(name + " "):
                    return line.split(" ", 1)[1].strip()
            return ""

        check("并发冷启动只创建一个账号", field(results[0], "USERS") == "1", field(results[0], "USERS"))
        check(
            "口令文件里的口令与库中账号一致",
            field(results[0], "VERIFY") == "True",
            field(results[0], "VERIFY"),
        )
        keys = {field(r, "KEY") for r in results}
        check("多个进程收敛到同一把 SECRET_KEY", len(keys) == 1 and "" not in keys, str(len(keys)))

        from app import config as cfgmod

        check("SECRET_KEY 非空（空密钥会话可伪造）", bool(cfgmod.SECRET_KEY))
        started = time.time()
        cfgmod._read_key_file(pathlib.Path(cold) / "definitely-absent")
        elapsed = time.time() - started
        check("缺失的密钥文件不触发重试空等", elapsed < 0.2, f"{elapsed:.2f}s")

        # 只有初始管理员本人改密才该删除初始口令文件。
        # 别的账号（甚至普通管理员）改自己的密码跟这个文件无关，
        # 无差别删除会让初始管理员忘记口令后既进不去、也没文件可查。
        scenario = textwrap.dedent(
            """
            import re, sys
            sys.path.insert(0, %r)
            from starlette.testclient import TestClient
            from app import config
            from app.main import app

            def token(client, url):
                return re.search(r'name="csrf_token" value="([^"]+)"', client.get(url).text).group(1)

            pw_file = config.DATA_DIR / ".initial_admin_password"
            pw = pw_file.read_text(encoding="utf-8").strip()

            with TestClient(app) as admin:
                admin.post("/admin/login", data={"username": "admin", "password": pw,
                                                 "csrf_token": token(admin, "/admin/login")})
                admin.post("/admin/accounts",
                           data={"csrf_token": token(admin, "/admin/accounts"),
                                 "username": "someone", "password": "someone-password-1"},
                           follow_redirects=False)

            with TestClient(app) as other:
                other.post("/admin/login",
                           data={"username": "someone", "password": "someone-password-1",
                                 "csrf_token": token(other, "/admin/login")})
                other.post("/admin/profile/password",
                           data={"csrf_token": token(other, "/admin/profile"),
                                 "current_password": "someone-password-1",
                                 "new_password": "someone-password-2",
                                 "confirm_password": "someone-password-2"},
                           follow_redirects=False)
            print("AFTER_OTHER", pw_file.exists())

            with TestClient(app) as admin2:
                admin2.post("/admin/login", data={"username": "admin", "password": pw,
                                                  "csrf_token": token(admin2, "/admin/login")})
                admin2.post("/admin/profile/password",
                            data={"csrf_token": token(admin2, "/admin/profile"),
                                  "current_password": pw,
                                  "new_password": "admin-password-2",
                                  "confirm_password": "admin-password-2"},
                            follow_redirects=False)
            print("AFTER_SELF", pw_file.exists())
            """
        ) % root
        out4 = subprocess.run(
            [sys.executable, "-c", scenario], env=env, cwd=root, capture_output=True, text=True
        )
        detail = out4.stdout[-300:] if out4.stdout else out4.stderr[-300:]
        check("非初始管理员改密不删初始口令文件", field(out4.stdout, "AFTER_OTHER") == "True", detail)
        check("初始管理员本人改密后口令文件被删除", field(out4.stdout, "AFTER_SELF") == "False", detail)

        # 口令文件写不出去时必须回滚刚建的账号，否则 COUNT(*)>0 会让后续启动永久短路。
        # 用「同名目录」制造写入失败，比改权限更干净且不依赖运行用户。
        pwfail = tempfile.mkdtemp(prefix="apppublisher-pwfail-")
        try:
            (pathlib.Path(pwfail) / ".initial_admin_password").mkdir()
            env_bad = {**os.environ, "APPPUBLISHER_DATA_DIR": pwfail}
            env_bad.pop("ADMIN_PASSWORD", None)
            env_bad.pop("APPPUBLISHER_DB", None)
            bad_boot = subprocess.run(
                [sys.executable, "-c", boot], env=env_bad, cwd=root, capture_output=True, text=True
            )
            check("口令写不出时启动失败而非留下孤儿账号", bad_boot.returncode != 0, f"rc={bad_boot.returncode}")

            leftover = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    textwrap.dedent(
                        f"""
                        import sys; sys.path.insert(0, {root!r})
                        from app import db
                        print("USERS", db.query_value("SELECT COUNT(*) FROM users"))
                        """
                    ),
                ],
                env=env_bad,
                cwd=root,
                capture_output=True,
                text=True,
            ).stdout
            check("失败后没有留下无法登录的账号", field(leftover, "USERS") == "0", field(leftover, "USERS"))

            (pathlib.Path(pwfail) / ".initial_admin_password").rmdir()
            retried = subprocess.run(
                [sys.executable, "-c", boot], env=env_bad, cwd=root, capture_output=True, text=True
            )
            check("问题排除后下次启动能重新初始化", retried.returncode == 0, retried.stderr[-200:])
        finally:
            shutil.rmtree(pwfail, ignore_errors=True)
    finally:
        shutil.rmtree(cold, ignore_errors=True)

    print("\n[12] 旧库增量迁移")
    legacy = tempfile.mkdtemp(prefix="apppublisher-legacy-")
    try:
        legacy_db = pathlib.Path(legacy) / "apppublisher.db"
        # 造一个「升级前」的库：apps 表还没有 tagline / banner_path，也没有 events / meta 表。
        setup = textwrap.dedent(
            """
            import sqlite3, sys
            sys.path.insert(0, %r)
            conn = sqlite3.connect(%r)
            conn.executescript('''
                CREATE TABLE apps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                    intro_html TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
                CREATE TABLE releases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, app_id INTEGER NOT NULL,
                    version_name TEXT NOT NULL, version_code INTEGER NOT NULL,
                    description TEXT NOT NULL DEFAULT '', build_path TEXT NOT NULL,
                    build_name TEXT NOT NULL DEFAULT '', build_size INTEGER NOT NULL DEFAULT 0,
                    build_sha256 TEXT NOT NULL DEFAULT '', force_update INTEGER NOT NULL DEFAULT 0,
                    is_latest INTEGER NOT NULL DEFAULT 0, released_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL);
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
                    is_super INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
                CREATE TABLE notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, app_id INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '',
                    published_at INTEGER NOT NULL, created_at INTEGER NOT NULL);
                -- 关键：events 表存在但**没有 share_code 列**。
                -- 新版 SCHEMA 里有 CREATE INDEX ... ON events(app_id, share_code)，
                -- 如果补列发生在建索引之后，这里就会 "no such column" 把启动搞挂。
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, app_id INTEGER NOT NULL,
                    kind TEXT NOT NULL, release_id INTEGER, day TEXT NOT NULL,
                    created_at INTEGER NOT NULL, visitor TEXT NOT NULL DEFAULT '',
                    referrer_host TEXT NOT NULL DEFAULT '', ua_class TEXT NOT NULL DEFAULT '',
                    is_bot INTEGER NOT NULL DEFAULT 0);
                INSERT INTO apps (slug, name, intro_html, enabled, created_at, updated_at)
                    VALUES ('Legacy', '老应用', '<h1>旧介绍页</h1>', 1, 1, 1);
                INSERT INTO releases (app_id, version_name, version_code, build_path,
                    released_at, created_at) VALUES (1, 'v0.9', 900, 'build/x.apk', 1, 1);
                INSERT INTO events (app_id, kind, day, created_at)
                    VALUES (1, 'view', '2026-01-01', 1);
            ''')
            conn.commit()
            conn.close()
            """
            % (root, str(legacy_db))
        )
        made = subprocess.run([sys.executable, "-c", setup], cwd=root, capture_output=True, text=True)
        check("旧库构造成功", made.returncode == 0, made.stderr[-200:])

        env_legacy = {
            **os.environ,
            "APPPUBLISHER_DATA_DIR": legacy,
            "APPPUBLISHER_DB": str(legacy_db),
        }
        migrate = textwrap.dedent(
            f"""
            import sys; sys.path.insert(0, {root!r})
            from app import db
            db.init_db()
            """
        )
        ran = subprocess.run(
            [sys.executable, "-c", migrate], env=env_legacy, cwd=root, capture_output=True, text=True
        )
        check("旧库能完成迁移且不报错", ran.returncode == 0, ran.stderr[-300:])

        probe = textwrap.dedent(
            f"""
            import sys; sys.path.insert(0, {root!r})
            from app import db
            cols = {{row["name"] for row in db.get_conn().execute("PRAGMA table_info(apps)")}}
            print("HAS_TAGLINE", "tagline" in cols)
            print("HAS_BANNER", "banner_path" in cols)
            print("APP_ROW", db.query_value("SELECT name FROM apps WHERE slug = 'Legacy'"))
            print("INTRO", db.query_value("SELECT intro_html FROM apps WHERE slug = 'Legacy'"))
            print("REL", db.query_value("SELECT version_name FROM releases WHERE version_code = 900"))
            ev_cols = {{row["name"] for row in db.get_conn().execute("PRAGMA table_info(events)")}}
            print("HAS_SHARE_COLUMN", "share_code" in ev_cols)
            print("EVENT_ROWS", db.query_value("SELECT COUNT(*) FROM events"))
            print("SHARE_TABLE", bool(db.query_value(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='share_links'")))
            print("TABLES", sorted(row["name"] for row in db.query_all(
                "SELECT name FROM sqlite_master WHERE type='table'")))
            """
        )
        out_legacy = subprocess.run(
            [sys.executable, "-c", probe], env=env_legacy, cwd=root, capture_output=True, text=True
        ).stdout
        check("迁移补齐 tagline 列", field(out_legacy, "HAS_TAGLINE") == "True", out_legacy[-300:])
        check("迁移补齐 banner_path 列", field(out_legacy, "HAS_BANNER") == "True")
        check("原有应用未丢失", field(out_legacy, "APP_ROW") == "老应用")
        check("介绍页内容未丢失", field(out_legacy, "INTRO") == "<h1>旧介绍页</h1>")
        check("原有发行版未丢失", field(out_legacy, "REL") == "v0.9")
        check(
            "旧 events 表补上了 share_code 列（补列必须先于建索引）",
            field(out_legacy, "HAS_SHARE_COLUMN") == "True",
            out_legacy[-300:],
        )
        check("原有统计明细未丢失", field(out_legacy, "EVENT_ROWS") == "1")
        check("share_links 新表已建立", field(out_legacy, "SHARE_TABLE") == "True")
        check(
            "events / meta 新表已建立",
            "events" in field(out_legacy, "TABLES") and "meta" in field(out_legacy, "TABLES"),
        )
    finally:
        shutil.rmtree(legacy, ignore_errors=True)

    print("\n[13] 未登记图片认领脚本")
    claim_dir = tempfile.mkdtemp(prefix="apppublisher-claim-")
    try:
        env_claim = {**os.environ, "APPPUBLISHER_DATA_DIR": claim_dir}
        env_claim.pop("APPPUBLISHER_DB", None)

        probe_assets = textwrap.dedent(
            f"""
            import sys; sys.path.insert(0, {root!r})
            from app import db
            print("ASSETS", db.query_value("SELECT COUNT(*) FROM assets"))
            print("OWNERS", db.query_value(
                "SELECT GROUP_CONCAT(DISTINCT a.slug) FROM assets s JOIN apps a ON a.id = s.app_id"))
            """
        )

        def asset_probe() -> str:
            return subprocess.run(
                [sys.executable, "-c", probe_assets],
                env=env_claim,
                cwd=root,
                capture_output=True,
                text=True,
            ).stdout

        setup = textwrap.dedent(
            f"""
            import sys; sys.path.insert(0, {root!r})
            from app import config, db
            db.init_db()
            db.execute("INSERT INTO apps (slug, name, tagline, intro_html, enabled,"
                       " created_at, updated_at) VALUES ('claimed', '认领测试', '', '', 1, 1, 1)")
            db.execute("INSERT INTO apps (slug, name, tagline, intro_html, enabled,"
                       " created_at, updated_at) VALUES ('other', '另一个', '', '', 1, 1, 1)")
            images = config.UPLOAD_DIR / "images"
            (images / "stray-one.png").write_bytes(b"\\x89PNG-one")
            (images / "stray-two.png").write_bytes(b"\\x89PNG-two")
            """
        )
        made = subprocess.run(
            [sys.executable, "-c", setup], env=env_claim, cwd=root, capture_output=True, text=True
        )
        check("认领测试环境就绪", made.returncode == 0, made.stderr[-200:])

        script = str(pathlib.Path(root) / "scripts" / "claim_images.py")

        bare = subprocess.run(
            [sys.executable, script], env=env_claim, cwd=root, capture_output=True, text=True
        )
        check(
            "不带参数时列出未登记图片与可选应用",
            bare.returncode == 0
            and "stray-one.png" in bare.stdout
            and "stray-two.png" in bare.stdout
            and "claimed" in bare.stdout,
            bare.stdout[-200:],
        )

        dry = subprocess.run(
            [sys.executable, script, "--app", "claimed"],
            env=env_claim,
            cwd=root,
            capture_output=True,
            text=True,
        )
        check(
            "给了目标也只预览、不写库",
            dry.returncode == 0
            and "没有写入数据库" in dry.stdout
            and "stray-one.png" in dry.stdout
            and field(asset_probe(), "ASSETS") == "0",
            dry.stdout[-200:],
        )

        bad = subprocess.run(
            [sys.executable, script, "--assign", "nope.png=claimed", "--apply"],
            env=env_claim,
            cwd=root,
            capture_output=True,
            text=True,
        )
        check("指定不存在的文件时报错退出", bad.returncode == 2, f"rc={bad.returncode}")
        check("报错时不写库", field(asset_probe(), "ASSETS") == "0")

        bad_slug = subprocess.run(
            [sys.executable, script, "--app", "no-such-app", "--apply"],
            env=env_claim,
            cwd=root,
            capture_output=True,
            text=True,
        )
        check("指定不存在的应用时报错退出", bad_slug.returncode == 2, f"rc={bad_slug.returncode}")

        applied = subprocess.run(
            [sys.executable, script, "--app", "claimed", "--apply"],
            env=env_claim,
            cwd=root,
            capture_output=True,
            text=True,
        )
        check(
            "--apply 完成认领",
            applied.returncode == 0 and "已认领 2 张" in applied.stdout,
            applied.stdout[-200:],
        )
        probe_out = asset_probe()
        check("两条记录都已入库", field(probe_out, "ASSETS") == "2", probe_out)
        check("归属到了指定应用", field(probe_out, "OWNERS") == "claimed", probe_out)

        again = subprocess.run(
            [sys.executable, script], env=env_claim, cwd=root, capture_output=True, text=True
        )
        check("认领后不再有未登记图片", "没有未登记的图片" in again.stdout, again.stdout[-200:])

        # 逐个指定：验证 --assign 能按文件名精确归到不同应用
        subprocess.run(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""
                    import sys; sys.path.insert(0, {root!r})
                    from app import config, db
                    db.execute("DELETE FROM assets")
                    (config.UPLOAD_DIR / "images" / "stray-three.png").write_bytes(b"\\x89PNG-three")
                    """
                ),
            ],
            env=env_claim,
            cwd=root,
            check=True,
            capture_output=True,
        )
        assigned = subprocess.run(
            [sys.executable, script, "--assign", "stray-three.png=other", "--apply"],
            env=env_claim,
            cwd=root,
            capture_output=True,
            text=True,
        )
        check("--assign 可指定到另一个应用", field(asset_probe(), "OWNERS") == "other", assigned.stdout[-200:])
    finally:
        shutil.rmtree(claim_dir, ignore_errors=True)

    # 旧库 + 并发冷启动：这是「两个副本同时首次启动」的真实形状。
    # 两个进程都会看到列缺失、都去 ALTER，输的那个会拿到 duplicate column name，
    # 不能因此让它的启动挂掉。
    print("\n[12.5] 并发迁移旧库")
    legacy2 = pathlib.Path(tempfile.mkdtemp(prefix="apppublisher-legacy2-"))
    try:
        legacy2_db = legacy2 / "apppublisher.db"
        seed = textwrap.dedent(
            f"""
            import sqlite3
            conn = sqlite3.connect({str(legacy2_db)!r})
            conn.executescript('''
                CREATE TABLE apps (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL, intro_html TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
                CREATE TABLE releases (id INTEGER PRIMARY KEY AUTOINCREMENT, app_id INTEGER NOT NULL,
                    version_name TEXT NOT NULL, version_code INTEGER NOT NULL,
                    description TEXT NOT NULL DEFAULT '', build_path TEXT NOT NULL,
                    build_name TEXT NOT NULL DEFAULT '', build_size INTEGER NOT NULL DEFAULT 0,
                    build_sha256 TEXT NOT NULL DEFAULT '', force_update INTEGER NOT NULL DEFAULT 0,
                    is_latest INTEGER NOT NULL DEFAULT 0, released_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL);
                CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
                    is_super INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
                CREATE TABLE notices (id INTEGER PRIMARY KEY AUTOINCREMENT, app_id INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '',
                    published_at INTEGER NOT NULL, created_at INTEGER NOT NULL);
                CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, app_id INTEGER NOT NULL,
                    kind TEXT NOT NULL, release_id INTEGER, day TEXT NOT NULL,
                    created_at INTEGER NOT NULL, visitor TEXT NOT NULL DEFAULT '',
                    referrer_host TEXT NOT NULL DEFAULT '', ua_class TEXT NOT NULL DEFAULT '',
                    is_bot INTEGER NOT NULL DEFAULT 0);
            ''')
            conn.commit()
            conn.close()
            """
        )
        subprocess.run([sys.executable, "-c", seed], cwd=root, check=True, capture_output=True)

        env_l2 = {
            **os.environ,
            "APPPUBLISHER_DATA_DIR": str(legacy2),
            "APPPUBLISHER_DB": str(legacy2_db),
        }
        racers = [
            subprocess.Popen(
                [sys.executable, "-c", migrate],
                env=env_l2,
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        racer_out = [p.communicate() for p in racers]
        crashed = [
            (p.returncode, out[1].strip().splitlines()[-1:])
            for p, out in zip(racers, racer_out)
            if p.returncode
        ]
        check("并发迁移旧库时没有进程因重复补列而崩溃", not crashed, str(crashed))

        final_cols = subprocess.run(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""
                    import sys; sys.path.insert(0, {root!r})
                    from app import db
                    apps = {{r["name"] for r in db.get_conn().execute("PRAGMA table_info(apps)")}}
                    ev = {{r["name"] for r in db.get_conn().execute("PRAGMA table_info(events)")}}
                    print("OK", "tagline" in apps and "banner_path" in apps and "share_code" in ev)
                    print("SHARE_TABLE", bool(db.query_value(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='share_links'")))
                    """
                ),
            ],
            env=env_l2,
            cwd=root,
            capture_output=True,
            text=True,
        ).stdout
        check("并发迁移后所有新列都在", field(final_cols, "OK") == "True", final_cols)
        check("并发迁移后新表也建好了", field(final_cols, "SHARE_TABLE") == "True", final_cols)
    finally:
        shutil.rmtree(legacy2, ignore_errors=True)

    print(f"\n通过 {passed} 项，失败 {len(failed)} 项")
    if failed:
        for label in failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    sys.exit(code)
