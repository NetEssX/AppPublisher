#!/usr/bin/env python3
"""给没有归属的应用指定负责人。

应用的归属决定两件事：网页后台里谁能看到/管理它，以及 API 密钥能操作哪些应用。
本次升级之前的应用 owner_id 为空，**只有超管能管理** —— 包括它们原来的作者在内，
其他人连列表里都看不到。用这个脚本认领。

**默认只打印计划，不写库。** 确认无误后加 --apply 才真正执行。

用法：

    # 1) 先看看哪些应用没有归属、有哪些账号可选
    python3 scripts/claim_apps.py

    # 2) 把所有无归属的应用交给某个账号
    python3 scripts/claim_apps.py --user alice --apply

    # 3) 单独指定（可重复）
    python3 scripts/claim_apps.py \\
        --app qutschedule=alice \\
        --app another-app=bob \\
        --apply

    # 4) 清空归属（收回给超管）
    python3 scripts/claim_apps.py --app qutschedule= --apply
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402
from app.utils import now_ms  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="给无归属的应用指定负责人（默认只预览，加 --apply 才写入）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--user", metavar="NAME", help="把所有无归属应用交给这个账号")
    parser.add_argument(
        "--app",
        action="append",
        default=[],
        metavar="SLUG=USER",
        help="单独指定某个应用归谁；USER 留空表示收回归属。可重复。",
    )
    parser.add_argument("--apply", action="store_true", help="真正写入数据库（不加则只预览）")
    args = parser.parse_args()

    config.ensure_dirs()
    db.init_db()

    users = db.query_all("SELECT id, username, is_super FROM users ORDER BY id")
    if not users:
        print("数据库里还没有任何账号。", file=sys.stderr)
        return 1
    by_name = {u["username"].lower(): u for u in users}

    apps = db.query_all("SELECT id, slug, name, owner_id FROM apps ORDER BY slug")
    if not apps:
        print(f"数据目录: {config.DATA_DIR}")
        print("还没有任何应用。")
        return 0

    pending = [app for app in apps if app["owner_id"] is None]

    print(f"数据目录: {config.DATA_DIR}")
    print("账号: " + "、".join(f"{u['username']}{'（超管）' if u['is_super'] else ''}" for u in users))
    print()
    print(f"应用（{len(apps)} 个，其中 {len(pending)} 个无归属）:")
    for app in apps:
        if app["owner_id"] is None:
            mark = "无归属"
        else:
            holder = next((u for u in users if u["id"] == app["owner_id"]), None)
            # holder 为 None 只在库被带外改动过时才会发生（外键是 ON DELETE SET NULL）
            mark = f"归 {holder['username']}" if holder else f"归已删除账号 #{app['owner_id']}"
        print(f"  {app['slug']:24} {app['name']:16} {mark}")
    print()

    plan = {}  # app_id -> (owner_id | None, 说明)
    seen = set()  # 防止 --user 与 --app 冲突时静默覆盖
    errors = []

    # 用 is not None 而不是真值判断：--user "" 多半是 shell 变量没展开，
    # 应当走到下面的查找并报「找不到这个账号」，而不是静默什么都不做。
    if args.user is not None:
        target = by_name.get(args.user.strip().lower())
        if target is None:
            errors.append(f"--user {args.user}: 找不到这个账号")
        else:
            for app in pending:
                plan[app["id"]] = (target["id"], f"-> {target['username']}")
                seen.add(app["id"])

    for item in args.app:
        if "=" not in item:
            errors.append(f"--app {item}: 格式应为 短链=账号")
            continue
        slug, _, username = item.partition("=")
        slug, username = slug.strip(), username.strip()

        app = next((a for a in apps if a["slug"].lower() == slug.lower()), None)
        if app is None:
            errors.append(f"--app {item}: 找不到短链为 {slug} 的应用")
            continue

        if app["id"] in seen:
            errors.append(f"--app {item}: 这个应用已被前面的参数指定过，请只写一次")
            continue
        seen.add(app["id"])

        if not username:
            plan[app["id"]] = (None, "-> （清空，仅超管可管理）")
            continue

        target = by_name.get(username.lower())
        if target is None:
            errors.append(f"--app {item}: 找不到账号 {username}")
            continue
        plan[app["id"]] = (target["id"], f"-> {target['username']}")

    if errors:
        for message in errors:
            print(f"错误: {message}", file=sys.stderr)
        return 2

    if not plan:
        if args.user or args.app:
            print("指定的归属没有可应用的变更（这些应用可能已经有归属了）。")
        else:
            print("没有指定任何归属变更。")
            print("用 --user <账号> 认领全部无归属应用，或 --app 短链=<账号> 逐个指定。")
        return 0

    by_id = {app["id"]: app for app in apps}
    print("变更计划:")
    for app_id, (_owner_id, desc) in sorted(plan.items()):
        print(f"  {by_id[app_id]['slug']:24} {desc}")
    print()

    if not args.apply:
        print("以上只是预览，**没有写入数据库**。确认无误后加上 --apply 重新执行。")
        return 0

    timestamp = now_ms()
    with db.transaction() as conn:
        for app_id, (owner_id, _desc) in plan.items():
            db.execute_tx(
                conn,
                "UPDATE apps SET owner_id = ?, updated_at = ? WHERE id = ?",
                (owner_id, timestamp, app_id),
            )

    print(f"完成：已更新 {len(plan)} 个应用的归属。")
    print("注意：变更归属会让原负责人失去该应用的访问权，请确认无误。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
