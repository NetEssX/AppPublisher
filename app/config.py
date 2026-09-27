"""全局配置。

配置来源优先级：环境变量 > 项目根目录的 .env 文件 > 代码内默认值。
导入本模块即会创建所需目录，因此必须在挂载静态目录之前导入。
"""

from __future__ import annotations

import os
import re
import secrets
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析：KEY=VALUE 逐行，支持 # 注释与包围引号。不覆盖已有环境变量。"""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")


def _path_env(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    return Path(raw).expanduser() if raw else default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- 路径
DATA_DIR = _path_env("APPPUBLISHER_DATA_DIR", BASE_DIR / "data")
UPLOAD_DIR = DATA_DIR / "uploads"
BUILD_DIR = UPLOAD_DIR / "build"
IMAGE_DIR = UPLOAD_DIR / "images"
DB_PATH = _path_env("APPPUBLISHER_DB", DATA_DIR / "apppublisher.db")

# ---------------------------------------------------------------- 对外地址
# 留空则按请求的 Host 动态推断，适合单机部署；放在反向代理后面时请显式设置。
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

# ---------------------------------------------------------------- 会话
SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "apppublisher_session")
SESSION_MAX_AGE = _int_env("SESSION_MAX_AGE", 60 * 60 * 24 * 7)
# 仅在 HTTPS 下开启；本地 http 调试时必须为 0，否则浏览器不保存 Cookie。
COOKIE_SECURE = _bool_env("COOKIE_SECURE", False)
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "lax").strip().lower() or "lax"
# 记录「这次访问是从哪个分享链接来的」的第一方 Cookie。30 天后自然过期。
SHARE_COOKIE_NAME = os.getenv("SHARE_COOKIE_NAME", "apppublisher_share")
SHARE_COOKIE_MAX_AGE = _int_env("SHARE_COOKIE_MAX_AGE", 60 * 60 * 24 * 30)
# 登录页专用的 CSRF Cookie（双提交校验）。此时还没有会话，所以单独放一个随机值。
CSRF_COOKIE_NAME = os.getenv("CSRF_COOKIE_NAME", "apppublisher_csrf")

# ---------------------------------------------------------------- 访问统计
# 原始事件明细的保留天数，超期由 analytics.purge_old_events() 清理。
STATS_RETENTION_DAYS = _int_env("STATS_RETENTION_DAYS", 180)
STATS_DEFAULT_RANGE_DAYS = _int_env("STATS_DEFAULT_RANGE_DAYS", 30)
STATS_RANGE_CHOICES = (7, 30, 90, 180)
# 只有反向代理已经覆写 X-Forwarded-For 时才可开启；直连时开启等于允许客户端伪造来源 IP。
TRUST_PROXY_HEADERS = _bool_env("TRUST_PROXY_HEADERS", False)

# ---------------------------------------------------------------- 上传限制
# 兼容旧配置名 MAX_APK_MB：MAX_BUILD_MB 没设时回落到它。
MAX_BUILD_MB = _int_env("MAX_BUILD_MB", _int_env("MAX_APK_MB", 1024))
MAX_BUILD_BYTES = MAX_BUILD_MB * 1024 * 1024
MAX_IMAGE_MB = _int_env("MAX_IMAGE_MB", 8)
MAX_IMAGE_BYTES = MAX_IMAGE_MB * 1024 * 1024
MAX_INTRO_MB = _int_env("MAX_INTRO_MB", 4)
MAX_INTRO_BYTES = MAX_INTRO_MB * 1024 * 1024

# 允许分发的产物类型 -> 下载时返回的 Content-Type。
# 这里是「能上传什么」的唯一定义处；EXTRA_BUILD_EXTENSIONS 可以往里加。
CONTENT_TYPES = {
    # 移动端
    ".apk": "application/vnd.android.package-archive",
    ".aab": "application/octet-stream",
    ".ipa": "application/octet-stream",
    # 桌面端
    ".exe": "application/vnd.microsoft.portable-executable",
    ".msi": "application/x-msi",
    ".dmg": "application/x-apple-diskimage",
    ".pkg": "application/octet-stream",
    ".deb": "application/vnd.debian.binary-package",
    ".rpm": "application/x-rpm",
    ".appimage": "application/octet-stream",
    # 通用归档
    ".zip": "application/zip",
    ".7z": "application/x-7z-compressed",
    ".tar.gz": "application/gzip",
    ".tgz": "application/gzip",
    ".gz": "application/gzip",
    ".xz": "application/x-xz",
    ".zst": "application/zstd",
    ".jar": "application/java-archive",
}


def _extra_build_extensions() -> set:
    """EXTRA_BUILD_EXTENSIONS=".bin,.rom" —— 逗号分隔，补到内置集合里。"""
    extras = set()
    for item in os.getenv("EXTRA_BUILD_EXTENSIONS", "").split(","):
        value = item.strip().lower()
        if value:
            extras.add(value if value.startswith(".") else "." + value)
    return extras


BUILD_EXTENSIONS = frozenset(set(CONTENT_TYPES) | _extra_build_extensions())
# 刻意不收 .svg：/media 用 StaticFiles 直出，SVG 会以内联 image/svg+xml 渲染成文档，
# 其中的 <script> 就能在本站源下执行（存储型 XSS）。产物走 /build 是强制 attachment 的，不受影响。
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp"})
HTML_EXTENSIONS = frozenset({".html", ".htm", ".txt"})

# ---------------------------------------------------------------- 初始管理员
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# ---------------------------------------------------------------- 路由保留字
# 这些 slug 会与内置路由冲突，禁止用作应用短链。
RESERVED_SLUGS = frozenset(
    {
        "admin",
        "api",
        "media",
        "static",
        "assets",
        "login",
        "logout",
        "health",
        "favicon.ico",
        "robots.txt",
        "sitemap.xml",
        "docs",
        "redoc",
        "openapi.json",
    }
)

SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?$")


_KEY_READ_ATTEMPTS = 50
_KEY_READ_INTERVAL = 0.02


def _read_key_file(key_file: Path) -> str:
    """读密钥文件。刚被别的进程创建、还没写完时短暂重试，而不是拿个空值走人。"""
    for _ in range(_KEY_READ_ATTEMPTS):
        try:
            value = key_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            # 文件还不存在：直接交给下面的 O_CREAT|O_EXCL 去抢占。
            # 在这里空转重试的话，每次全新安装都要白等满 1 秒才继续。
            return ""
        if value:
            return value
        time.sleep(_KEY_READ_INTERVAL)
    return ""


def _load_secret_key() -> str:
    """读取 SECRET_KEY；未配置时由抢到的那个进程创建一次，其余进程复用它。

    用 O_CREAT|O_EXCL 抢占，只有抢到的进程负责写入。单纯「写临时文件再 os.replace」
    是不够的：两个同时启动的 worker 会各自 replace 一次，各自保留自己内存里的 key，
    最后互相签发的 Cookie 都验不过——正是这个函数要避免的问题。
    """
    configured = os.getenv("SECRET_KEY", "").strip()
    if configured:
        return configured

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key_file = DATA_DIR / ".secret_key"

    existing = _read_key_file(key_file)
    if existing:
        return existing

    try:
        # 0o600 不含 group/other 位，umask 只能再收紧、无法放宽。
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        winner = _read_key_file(key_file)
        if winner:
            return winner
        raise RuntimeError(
            f"{key_file} 存在但内容为空，可能是上次启动中途失败留下的。"
            "请删除该文件（会让所有已登录会话失效），或在 .env 里显式配置 SECRET_KEY。"
        )

    # 这里直接返回自己生成的值，而不是回读文件：本进程是 O_EXCL 抢到的唯一写入者，
    # 回读只会在极端情况下拿到空串，而空的 SECRET_KEY 会让会话可被伪造且毫无报错。
    generated = secrets.token_urlsafe(48)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(generated)
    return generated


SECRET_KEY = _load_secret_key()
if not SECRET_KEY:
    # 空密钥能让 URLSafeTimedSerializer 正常签名，会话就变成可任意伪造，且毫无报错。
    # 宁可启动失败也不要静默地不安全。
    raise RuntimeError("SECRET_KEY 为空，拒绝启动：空密钥签发的会话可被任意伪造。")


def ensure_dirs() -> None:
    for directory in (DATA_DIR, UPLOAD_DIR, BUILD_DIR, IMAGE_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def validate_slug(slug: str) -> str:
    """校验并规范化应用短链，非法时抛 ValueError。"""
    value = (slug or "").strip().strip("/")
    if not value:
        raise ValueError("应用短链不能为空")
    if not SLUG_RE.match(value):
        raise ValueError("短链只能包含字母、数字、下划线和连字符，长度 1-64，且首尾必须是字母或数字")
    if value.lower() in RESERVED_SLUGS:
        raise ValueError(f"短链 “{value}” 是系统保留字，请换一个")
    return value


ensure_dirs()
