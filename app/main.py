"""应用入口：uvicorn app.main:app"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import analytics, config, db, security
from .routers import admin as admin_routes
from .routers import public as public_routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("apppublisher")


def bootstrap_admin() -> None:
    """首次启动时创建初始账号。没配 ADMIN_PASSWORD 就随机生成并落盘一份。"""
    if db.query_value("SELECT COUNT(*) FROM users"):
        return

    username = config.ADMIN_USERNAME
    generated = not config.ADMIN_PASSWORD
    password = config.ADMIN_PASSWORD or secrets.token_urlsafe(12)

    # 先入库、再写口令文件，顺序不能反：并发冷启动时两个进程都会走到这里，
    # 只有一个能通过 username 的 UNIQUE 约束。若先写文件，落败的进程会用自己的
    # 随机口令覆盖优胜者写下的那份，结果文件里的口令在库里根本不存在。
    try:
        db.execute(
            "INSERT INTO users (username, password_hash, display_name, is_super, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (username, security.hash_password(password), "超级管理员", int(time.time() * 1000)),
        )
    except sqlite3.IntegrityError:
        # 另一个进程同时完成了初始化，账号已存在，这里什么都不用做。
        logger.info("初始管理员已由其它进程创建，跳过")
        return

    if generated:
        # 刻意不打印到 stdout：systemd / Docker / k8s 会把 stdout 收进日志并长期留存，
        # 等于把初始口令永久写进日志归档。改成落一个 0600 文件，日志里只留路径。
        secret_file = config.DATA_DIR / ".initial_admin_password"
        # 一步到位建成 0600：先 write_text 再 chmod 的话，文件会先以 umask（通常 0644）
        # 存在一小段时间，而且 chmod 失败时会被 except 吞掉，日志却仍宣称「权限 600」。
        # 0o600 不含 group/other 位，不受 umask 放宽影响。
        try:
            handle_fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(password)
        except OSError:
            # 口令写不出去就等于没人能登录：把刚建的账号删掉，让下次启动能重试。
            # 不删的话 COUNT(*) > 0 会让后续每次启动都直接短路，连显式配置
            # ADMIN_PASSWORD 都救不回来（bootstrap 在插入之前就返回了）。
            db.execute("DELETE FROM users WHERE username = ?", (username,))
            logger.error("写入初始口令文件失败，已回滚初始账号以便下次启动重试", exc_info=True)
            raise
        logger.warning(
            "已创建初始管理员账号 %s。随机初始密码写在 %s（权限 600），"
            "查看后请尽快登录并在「密码」页修改，改完该文件会自动删除。",
            username,
            secret_file,
        )
    else:
        logger.info("已按 ADMIN_PASSWORD 创建初始管理员账号：%s", username)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config.ensure_dirs()
    db.init_db()
    bootstrap_admin()
    # 清理是维护动作，不是启动的前置条件：失败只记日志，不要让服务起不来。
    try:
        analytics.purge_old_events()
    except Exception:  # noqa: BLE001
        logger.warning("启动时清理统计明细失败", exc_info=True)
    logger.info("数据目录: %s", config.DATA_DIR)
    logger.info("对外地址: %s", config.PUBLIC_BASE_URL or "(按请求 Host 动态推断)")
    yield


app = FastAPI(
    title="AppPublisher",
    description="Android 应用介绍页与更新检查服务",
    version="1.0.0",
    lifespan=lifespan,
)

# /media 直接映射上传目录：介绍页里引用的图片可以这样写
#   <img src="/media/images/xxxx.png">
app.mount("/media", StaticFiles(directory=str(config.UPLOAD_DIR)), name="media")

# 后台路由必须排在前面：public 里的 /{slug} 是单段通配，先注册才不会抢走 /admin。
app.include_router(admin_routes.router)
app.include_router(public_routes.router)
