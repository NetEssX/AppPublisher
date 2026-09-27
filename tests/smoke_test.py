"""端到端冒烟测试：整个后台流程 + 对外接口。

用独立临时数据目录，不会碰 ./data。直接 python3 tests/smoke_test.py 运行。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import sys
import tempfile

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
ZIP_BYTES = b"PK\x03\x04FAKE-ZIP" * 256
CSRF_RE = re.compile(r'name="csrf_token" value="([0-9a-f]+)"')

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
        bad = client.post(
            "/admin/login",
            data={"username": "admin", "password": "wrong-password"},
            follow_redirects=False,
        )
        check("错误密码被拒", bad.status_code == 303 and "err=" in bad.headers.get("location", ""))

        ok = client.post(
            "/admin/login",
            data={"username": "admin", "password": "smoke-test-password"},
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
        check("删除应用后列表为空", json.loads(client.get("/health").text)["apps"] == 0)

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
