"""上传文件落盘。

文件名一律随机生成，绝不使用客户端提供的名字，从根上杜绝路径穿越。
返回的存储路径是相对 UPLOAD_DIR 的 POSIX 形式，例如 "build/3f2a....apk"。

这些函数是同步的：FastAPI 的 `def` 端点在线程池中执行，
`UploadFile.file` 此时已是接收完毕的 SpooledTemporaryFile，直接同步读即可。
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
import secrets
from pathlib import Path
from typing import FrozenSet, Optional, Tuple

from fastapi import HTTPException, UploadFile

from . import config

_CHUNK = 1024 * 1024
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def match_extension(filename: str, allowed: FrozenSet[str]) -> Optional[str]:
    """返回文件名末尾命中的扩展名，未命中返回 None。

    按长度从长到短匹配，这样 ".tar.gz" 不会被 ".gz" 抢走。
    要求文件名比扩展名长，避免把字面叫 ".apk" 的文件放进来。
    """
    name = (filename or "").lower()
    for extension in sorted(allowed, key=len, reverse=True):
        if len(name) > len(extension) and name.endswith(extension):
            return extension
    return None


def content_type_for(filename: str) -> str:
    """决定下载时返回的 Content-Type。内置表命中不了就交给 mimetypes，再不行按二进制流。"""
    extension = match_extension(filename, config.BUILD_EXTENSIONS)
    if extension and extension in config.CONTENT_TYPES:
        return config.CONTENT_TYPES[extension]
    guessed, _ = mimetypes.guess_type(filename or "")
    return guessed or "application/octet-stream"


def _safe_target(subdir: str, filename: str, allowed_ext: FrozenSet[str]) -> Path:
    extension = match_extension(filename, allowed_ext)
    if extension is None:
        allowed = "、".join(sorted(allowed_ext))
        suffix = Path(filename or "").suffix.lower()
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 {suffix or '(无扩展名)'}，仅允许：{allowed}",
        )

    # 必须先校验再 mkdir：pathlib 的 `/` 遇到绝对路径会丢弃左边，
    # 若 subdir 是 "/tmp/x" 或含 ".."，mkdir 会先把目录建在外面，之后的检查就晚了。
    root = config.UPLOAD_DIR.resolve()
    target_dir = (config.UPLOAD_DIR / subdir).resolve()
    if target_dir != root and root not in target_dir.parents:
        raise HTTPException(status_code=400, detail="非法的目标路径")

    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / f"{secrets.token_hex(16)}{extension}"


def save_upload(
    file: UploadFile,
    subdir: str,
    allowed_ext: FrozenSet[str],
    max_bytes: int,
) -> Tuple[str, int, str]:
    """流式保存上传文件，返回 (相对路径, 字节数, sha256)。"""
    if file is None or not file.filename:
        raise HTTPException(status_code=400, detail="请选择要上传的文件")

    dest: Optional[Path] = None
    try:
        dest = _safe_target(subdir, file.filename, allowed_ext)
        digest = hashlib.sha256()
        size = 0
        with dest.open("wb") as handle:
            while True:
                chunk = file.file.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过大小上限 {max_bytes // (1024 * 1024)} MB",
                    )
                digest.update(chunk)
                handle.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="上传的文件是空的")
    except Exception:
        if dest is not None:
            dest.unlink(missing_ok=True)
        raise
    finally:
        # 关闭放在最外层：_safe_target 会在建目录之前就拒绝非法扩展名，
        # 那条常见路径若不在 finally 覆盖范围内，底层的临时文件就一直不释放。
        try:
            file.file.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
            pass

    # 用 resolve() 后的根来算相对路径：_safe_target 返回的 dest 已经解过符号链接，
    # 若这里拿未解析的 UPLOAD_DIR 去比较，macOS 上 /var → /private/var 会直接抛
    # ValueError("is not in the subpath of")。
    return dest.relative_to(config.UPLOAD_DIR.resolve()).as_posix(), size, digest.hexdigest()


def safe_display_name(filename: Optional[str], fallback: str) -> str:
    """把客户端文件名压成安全的展示名，仅用于界面展示。"""
    base = Path(filename or "").name
    cleaned = _UNSAFE_NAME_RE.sub("_", base).strip("._")[:120]
    return cleaned or fallback


def download_filename(slug: str, version_name: str, original_name: Optional[str]) -> str:
    """下载时使用的文件名，完全由服务端生成，不采信客户端输入。

    扩展名跟随上传的原始文件，所以 zip 分发的还是 .zip，apk 分发的还是 .apk。
    """
    extension = match_extension(
        original_name or "", config.BUILD_EXTENSIONS
    ) or Path(original_name or "").suffix.lower()
    extension = _UNSAFE_NAME_RE.sub("", extension or "")
    if extension and not extension.startswith("."):
        extension = "." + extension

    safe_version = _UNSAFE_NAME_RE.sub("_", (version_name or "").strip()).strip("._") or "release"
    safe_slug = _UNSAFE_NAME_RE.sub("_", (slug or "").strip()).strip("._") or "app"
    return f"{safe_slug}-{safe_version}{extension}"


def relative_of(path: Path) -> Optional[str]:
    """把一个绝对路径转成相对 UPLOAD_DIR 的 POSIX 形式；越界返回 None。

    两边都先 resolve()：macOS 上 /var 会解析成 /private/var，
    拿未解析的根去 relative_to 会直接抛 ValueError。
    """
    root = config.UPLOAD_DIR.resolve()
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if resolved == root or root not in resolved.parents:
        return None
    return resolved.relative_to(root).as_posix()


def list_files(subdir: str) -> list:
    """列出某个上传子目录下的文件（不递归），按文件名排序。"""
    directory = config.UPLOAD_DIR / subdir
    if not directory.is_dir():
        return []
    return sorted(
        (item for item in directory.iterdir() if item.is_file()), key=lambda item: item.name
    )


def resolve(relative_path: Optional[str]) -> Optional[Path]:
    """把库里的相对路径还原成绝对路径，越界或不存在返回 None。"""
    if not relative_path:
        return None
    root = config.UPLOAD_DIR.resolve()
    candidate = (root / relative_path).resolve()
    if candidate == root or root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def delete(relative_path: Optional[str]) -> None:
    path = resolve(relative_path)
    if path is not None:
        path.unlink(missing_ok=True)
