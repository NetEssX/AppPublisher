#!/usr/bin/env python3
"""把「未登记的图片」认领到具体应用。

未登记的图片 = 落在 data/uploads/images/ 里、但 assets 表里没有对应记录、
也没被任何应用用作封面的文件。通常是本功能上线之前上传的（那时还没有归属记录），
或者你手工拷进目录的。

**默认只打印计划，不写库。** 确认无误后加 --apply 才真正执行。

用法：

    # 1) 先看看有哪些未登记的图片、有哪些应用可选
    python3 scripts/claim_images.py

    # 2) 全部认领给某一个应用
    python3 scripts/claim_images.py --app qutschedule --apply

    # 3) 逐个指定（可重复；文件名或相对路径都行）
    python3 scripts/claim_images.py \\
        --assign 134f65b7.png=qutschedule \\
        --assign banner.png=another-app \\
        --apply

脚本只做一件事：往 assets 表插记录。它**不会**移动或重命名文件，
也不会改 apps.banner_path —— 想把某张图设成封面，请走后台的「媒体资源」上传。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

# 允许从仓库任意位置直接执行本脚本
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import config, db, storage  # noqa: E402
from app.utils import now_ms  # noqa: E402


def known_paths() -> set:
    """已经被登记过的相对路径：assets 里的，加上各应用当前用作封面的。"""
    paths = {row["path"] for row in db.query_all("SELECT path FROM assets")}
    paths.update(
        row["banner_path"]
        for row in db.query_all("SELECT banner_path FROM apps WHERE banner_path IS NOT NULL")
    )
    return paths


def unregistered_files() -> list:
    """返回 [(相对路径, 绝对路径, 字节数)]，按文件名排序。"""
    known = known_paths()
    items = []
    for stored in storage.list_files("images"):
        relative = storage.relative_of(stored)
        if not relative or relative in known:
            continue
        try:
            size = stored.stat().st_size
        except OSError:
            continue
        items.append((relative, stored, size))
    return items


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把未登记的图片认领到应用（默认只预览，加 --apply 才写入）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--app", metavar="SLUG", help="把全部未登记图片认领给这个短链对应的应用"
    )
    parser.add_argument(
        "--assign",
        action="append",
        default=[],
        metavar="FILE=SLUG",
        help="单独指定某张图归哪个应用，可重复",
    )
    parser.add_argument("--apply", action="store_true", help="真正写入数据库（不加则只预览）")
    args = parser.parse_args()

    config.ensure_dirs()
    db.init_db()

    apps = db.query_all("SELECT id, slug, name FROM apps ORDER BY slug")
    if not apps:
        print("数据库里还没有任何应用，先到后台新建一个再执行本脚本。", file=sys.stderr)
        return 1

    by_slug = {row["slug"].lower(): row for row in apps}
    files = unregistered_files()

    print(f"数据目录: {config.DATA_DIR}")
    print("应用（%d 个）: %s" % (len(apps), "、".join(f"{r['slug']}（{r['name']}）" for r in apps)))
    print()

    if not files:
        print("没有未登记的图片，无需处理。")
        return 0

    print(f"未登记的图片（{len(files)} 张）:")
    for relative, _stored, size in files:
        print(f"  {relative}   {size} 字节")
    print()

    # ---- 组装认领计划 ----
    plan = {}  # 相对路径 -> app row
    errors = []

    if args.app:
        target = by_slug.get(args.app.strip().lower())
        if target is None:
            errors.append(f"--app {args.app}: 找不到该短链的应用")
        else:
            for relative, _stored, _size in files:
                plan[relative] = target

    for item in args.assign:
        if "=" not in item:
            errors.append(f"--assign {item}: 格式应为 文件名=短链")
            continue
        name, _, slug = item.partition("=")
        name, slug = name.strip(), slug.strip().lower()

        target = by_slug.get(slug)
        if target is None:
            errors.append(f"--assign {item}: 找不到短链为 {slug} 的应用")
            continue

        # 允许写文件名、相对路径或完整路径
        matched = [
            relative
            for relative, stored, _size in files
            if name in (relative, stored.name, str(stored))
        ]
        if not matched:
            errors.append(f"--assign {item}: 在未登记列表里找不到这个文件")
            continue
        for relative in matched:
            plan[relative] = target

    if errors:
        for message in errors:
            print(f"错误: {message}", file=sys.stderr)
        return 2

    if not plan:
        print("没有指定任何认领关系。")
        print("用 --app <短链> 认领全部，或 --assign 文件名=<短链> 逐个指定。")
        return 0

    print("认领计划:")
    for relative, target in sorted(plan.items()):
        print(f"  {relative}  ->  {target['slug']}（{target['name']}）")
    print()

    if not args.apply:
        print("以上只是预览，**没有写入数据库**。确认无误后加上 --apply 重新执行。")
        return 0

    written = 0
    skipped = []
    with db.transaction() as conn:
        for relative, target in sorted(plan.items()):
            stored = config.UPLOAD_DIR / relative
            try:
                size = stored.stat().st_size
            except OSError:
                # 预演之后文件被删了（或权限变了）。这时登记进去只会得到一条
                # 指向不存在文件的悬空记录 —— 后台列表里显示、点开却 404。
                # 宁可跳过并说明，也不要写进去。
                skipped.append(relative)
                continue
            db.execute_tx(
                conn,
                "INSERT INTO assets (app_id, path, original_name, size, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (target["id"], relative, stored.name, size, now_ms()),
            )
            written += 1

    for relative in skipped:
        print(f"跳过（文件已不存在）: {relative}", file=sys.stderr)

    print(f"完成：已认领 {written} 张图片。")
    if skipped:
        print(f"另有 {len(skipped)} 张在写入前已消失，未登记。")
    print("刷新后台的「媒体资源」分页即可看到它们归属于对应应用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
