"""API 密钥。

- 密钥形如 `ap_` + 43 个 base64url 字符，**只在创建时显示一次**。
- 库里只存整把密钥的 SHA-256。
  **这里刻意不用口令那套 PBKDF2**：密钥是 `secrets.token_urlsafe` 生成的 32 字节
  随机串，不是人选的密码，暴力枚举不可行；而 PBKDF2 每次请求要跑 21 万轮，
  拿来做接口鉴权太慢。GitHub / Stripe 这类服务的令牌也是同样处理。
- 密钥的权限 = **创建者账号当前的权限**（跟随账号，不额外存 scope）。
  账号被删时密钥随外键级联删除。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
from typing import Any, Dict, List, Optional

from . import db
from .utils import now_ms

logger = logging.getLogger("apppublisher.apikeys")

KEY_PREFIX = "ap_"
_BODY_BYTES = 32
_KEY_BODY_LENGTH = 43  # token_urlsafe(32) 的长度
# token_urlsafe 只会产出这些字符。用显式字符集而不是 str.isalnum()——
# isalnum() 是 Unicode 感知的，ap_ä… 这种也会通过前置校验。
_KEY_BODY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)
PREFIX_DISPLAY_LENGTH = 8
# last_used_at 最多每小时写一次：鉴权是热路径，不该每个请求都写一遍库。
LAST_USED_THROTTLE_MS = 60 * 60 * 1000


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def looks_like_key(token: str) -> bool:
    """快速形状校验，挡掉明显不是密钥的输入，避免无谓地查库。"""
    if not token or not token.startswith(KEY_PREFIX):
        return False
    body = token[len(KEY_PREFIX) :]
    return len(body) == _KEY_BODY_LENGTH and all(ch in _KEY_BODY_CHARS for ch in body)


def create(user_id: int, name: str = "") -> Dict[str, Any]:
    """新建密钥。返回的字典里有明文 key —— 这是它唯一一次出现的机会。"""
    timestamp = now_ms()
    for _ in range(5):
        key = KEY_PREFIX + secrets.token_urlsafe(_BODY_BYTES)
        prefix = key[len(KEY_PREFIX) :][:PREFIX_DISPLAY_LENGTH]
        try:
            key_id = db.execute(
                "INSERT INTO api_keys (user_id, name, prefix, key_hash, created_at, revoked) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (user_id, name, prefix, _hash(key), timestamp),
            )
        except sqlite3.IntegrityError as exc:
            if "key_hash" not in str(exc):
                # 不是短码撞车，而是别的原因（典型是 user_id 指向的账号不存在）。
                # 重试只会把真因掩盖成「连续冲突」，直接把原始错误带出来。
                logger.error("创建 API 密钥失败: %s", exc)
                raise RuntimeError(f"创建 API 密钥失败: {exc}") from exc
            # 撞哈希的概率可以忽略，但 UNIQUE 约束是真正的兜底，接住重试即可。
            continue
        return {
            "id": key_id,
            "key": key,
            "prefix": prefix,
            "name": name,
            "created_at": timestamp,
        }
    raise RuntimeError("生成 API 密钥连续冲突")


def resolve(token: str) -> Optional[Dict[str, Any]]:
    """把 Bearer token 解析成密钥行（含创建者信息）；无效或已撤销返回 None。"""
    if not looks_like_key(token):
        return None
    row = db.query_one(
        "SELECT k.id           AS key_id,"
        "       k.user_id      AS user_id,"
        "       k.name         AS key_name,"
        "       k.prefix       AS key_prefix,"
        "       k.last_used_at AS last_used_at,"
        "       u.username     AS username,"
        "       u.display_name AS display_name,"
        "       u.is_super     AS is_super "
        "FROM api_keys k JOIN users u ON u.id = k.user_id "
        "WHERE k.key_hash = ? AND k.revoked = 0",
        (_hash(token),),
    )
    if row is None:
        return None
    _touch(row["key_id"], row["last_used_at"])
    return row


def _touch(key_id: int, last_used_at: Optional[int]) -> None:
    timestamp = now_ms()
    if last_used_at and timestamp - last_used_at < LAST_USED_THROTTLE_MS:
        return
    try:
        db.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (timestamp, key_id))
    except Exception:  # noqa: BLE001 - 记录使用时间失败不该影响鉴权
        logger.warning("更新 API 密钥使用时间失败 key_id=%s", key_id, exc_info=True)


def list_for_user(user_id: int) -> List[Dict[str, Any]]:
    return db.query_all(
        "SELECT id, name, prefix, created_at, last_used_at, revoked "
        "FROM api_keys WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    )


def list_all() -> List[Dict[str, Any]]:
    """超管视角：所有密钥，带上归属账号。"""
    return db.query_all(
        "SELECT k.id, k.name, k.prefix, k.created_at, k.last_used_at, k.revoked,"
        "       u.username, u.display_name "
        "FROM api_keys k JOIN users u ON u.id = k.user_id "
        "ORDER BY k.id DESC"
    )


def owner_filter(user: Dict[str, Any]) -> Optional[int]:
    """超管可操作任何人的密钥（None = 不加限制），其余人只限自己的。

    与 services.visible_apps() 是同一套规则：集中在这里，免得三处各写一遍后漂移。
    """
    return None if user.get("is_super") else user["id"]


def revoke(key_id: int, user_id: Optional[int] = None) -> bool:
    """撤销密钥。user_id 非空时限定只能撤销自己的（超管传 None 表示不限）。"""
    key = db.query_one("SELECT * FROM api_keys WHERE id = ?", (key_id,))
    if key is None or (user_id is not None and key["user_id"] != user_id):
        return False
    db.execute("UPDATE api_keys SET revoked = 1 WHERE id = ?", (key_id,))
    return True


def delete(key_id: int, user_id: Optional[int] = None) -> bool:
    key = db.query_one("SELECT * FROM api_keys WHERE id = ?", (key_id,))
    if key is None or (user_id is not None and key["user_id"] != user_id):
        return False
    db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    return True
