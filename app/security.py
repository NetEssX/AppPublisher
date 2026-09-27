"""认证、会话与 CSRF。

- 口令：PBKDF2-HMAC-SHA256，每用户独立 salt，标准库实现，无第三方依赖。
- 会话：itsdangerous 签名 token 存 HttpOnly Cookie，token 内绑定口令哈希尾段，
  改密码即自动踢掉该用户所有旧会话。
- CSRF：由会话 token 通过 HMAC 派生，无需服务端存储。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import Depends, Form, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import config, db

logger = logging.getLogger("apppublisher.security")

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 210_000
_LOGIN_PATH = "/admin/login"

# ---------------------------------------------------------------- 口令


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return "$".join(
        [
            _ALGO,
            str(_ITERATIONS),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(derived).decode("ascii"),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_b64 = stored.split("$")
        if algo != _ALGO:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)


# ---------------------------------------------------------------- 会话

_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="apppublisher.session")


def create_session_token(user_id: int, password_hash: str) -> str:
    # 混入口令哈希尾段：改密码后旧 token 立即失效。
    return _serializer.dumps({"uid": user_id, "ph": password_hash[-16:]})


def read_session_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        payload = _serializer.loads(token, max_age=config.SESSION_MAX_AGE)
    except BadSignature:
        return None
    if not isinstance(payload, dict) or "uid" not in payload:
        return None
    return payload


def csrf_token_for(session_token: str) -> str:
    return hmac.new(
        config.SECRET_KEY.encode("utf-8"),
        ("csrf:" + session_token).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:40]


# ---------------------------------------------------------------- 登录节流

_failures: Dict[str, Tuple[int, float]] = {}
_LOCK_AFTER = 5
_LOCK_SECONDS = 300.0


def login_locked_for(username: str) -> int:
    """若该账号处于锁定，返回剩余秒数，否则返回 0。"""
    entry = _failures.get(username.lower())
    if not entry:
        return 0
    count, locked_until = entry
    remaining = locked_until - time.time()
    if count >= _LOCK_AFTER and remaining > 0:
        return int(remaining) + 1
    if remaining <= 0:
        _failures.pop(username.lower(), None)
    return 0


def record_login_failure(username: str) -> None:
    key = username.lower()
    count, _ = _failures.get(key, (0, 0.0))
    count += 1
    locked_until = time.time() + _LOCK_SECONDS if count >= _LOCK_AFTER else 0.0
    _failures[key] = (count, locked_until)


def clear_login_failures(username: str) -> None:
    _failures.pop(username.lower(), None)


# ---------------------------------------------------------------- 依赖


def _login_redirect() -> HTTPException:
    return HTTPException(
        status_code=303,
        detail="未登录",
        headers={"Location": _LOGIN_PATH},
    )


def get_session_token(request: Request) -> Optional[str]:
    return request.cookies.get(config.SESSION_COOKIE_NAME)


def get_current_user(request: Request) -> Dict[str, Any]:
    """取当前登录用户；未登录则 303 跳登录页。"""
    token = get_session_token(request)
    if not token:
        raise _login_redirect()

    payload = read_session_token(token)
    if payload is None:
        raise _login_redirect()

    user = db.query_one("SELECT * FROM users WHERE id = ?", (payload.get("uid"),))
    if user is None:
        raise _login_redirect()

    # token 与当前口令哈希不匹配 => 改过密码，旧会话作废。
    if payload.get("ph") != (user["password_hash"] or "")[-16:]:
        raise _login_redirect()

    user["session_token"] = token
    user["csrf_token"] = csrf_token_for(token)
    return user


def require_csrf(
    user: Dict[str, Any] = Depends(get_current_user),
    csrf_token: str = Form(""),
) -> Dict[str, Any]:
    """所有写操作依赖它：既校验登录，也校验 CSRF。"""
    expected = user.get("csrf_token", "")
    if not csrf_token or not hmac.compare_digest(csrf_token, expected):
        raise HTTPException(status_code=400, detail="CSRF 校验失败，请刷新页面后重试")
    return user


def require_super(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    if not user.get("is_super"):
        raise HTTPException(status_code=403, detail="需要超级管理员权限")
    return user


def require_super_csrf(user: Dict[str, Any] = Depends(require_csrf)) -> Dict[str, Any]:
    if not user.get("is_super"):
        raise HTTPException(status_code=403, detail="需要超级管理员权限")
    return user


def set_session_cookie(response: Any, token: str) -> None:
    response.set_cookie(
        key=config.SESSION_COOKIE_NAME,
        value=token,
        max_age=config.SESSION_MAX_AGE,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite=config.COOKIE_SAMESITE,
        path="/",
    )


def clear_session_cookie(response: Any) -> None:
    response.delete_cookie(
        key=config.SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite=config.COOKIE_SAMESITE,
    )
