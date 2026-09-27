"""应用入口：uvicorn app.main:app"""

from __future__ import annotations

import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import config, db, security
from .routers import admin as admin_routes
from .routers import public as public_routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("apppublisher")


def bootstrap_admin() -> None:
    """首次启动时创建初始账号。没配 ADMIN_PASSWORD 就随机生成并打印一次。"""
    if db.query_value("SELECT COUNT(*) FROM users"):
        return

    username = config.ADMIN_USERNAME
    password = config.ADMIN_PASSWORD
    generated = not password
    if generated:
        password = secrets.token_urlsafe(12)

    db.execute(
        "INSERT INTO users (username, password_hash, display_name, is_super, created_at) "
        "VALUES (?, ?, ?, 1, ?)",
        (username, security.hash_password(password), "超级管理员", int(time.time() * 1000)),
    )

    if generated:
        banner = (
            "\n"
            + "=" * 62
            + "\n  已创建初始管理员账号（此密码只显示这一次）\n"
            + f"    用户名: {username}\n"
            + f"    密  码: {password}\n"
            + "  登录后请立即在「密码」页修改。\n"
            + "=" * 62
            + "\n"
        )
        print(banner, flush=True)
    else:
        logger.info("已按 ADMIN_PASSWORD 创建初始管理员账号：%s", username)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config.ensure_dirs()
    db.init_db()
    bootstrap_admin()
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
