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
