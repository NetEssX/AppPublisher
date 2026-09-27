"""SQLite 数据访问层。

用标准库 sqlite3，连接按线程缓存（FastAPI 的同步端点跑在线程池里，
每个线程各持一个连接，天然避开 sqlite3 的跨线程限制）。
所有写操作走 transaction()，显式 BEGIN IMMEDIATE 避免写冲突。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional, Sequence

from . import config

logger = logging.getLogger("apppublisher.db")

_local = threading.local()

_WAL_ATTEMPTS = 20
_WAL_RETRY_INTERVAL = 0.05

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    display_name  TEXT    NOT NULL DEFAULT '',
    is_super      INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS apps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    name        TEXT    NOT NULL,
    intro_html  TEXT    NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS releases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id       INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    version_name TEXT    NOT NULL,
    version_code INTEGER NOT NULL,
    description  TEXT    NOT NULL DEFAULT '',
    build_path   TEXT    NOT NULL,
    build_name   TEXT    NOT NULL DEFAULT '',
    build_size   INTEGER NOT NULL DEFAULT 0,
    build_sha256 TEXT    NOT NULL DEFAULT '',
    content_type TEXT    NOT NULL DEFAULT 'application/octet-stream',
    force_update INTEGER NOT NULL DEFAULT 0,
    is_latest    INTEGER NOT NULL DEFAULT 0,
    released_at  INTEGER NOT NULL,
    created_at   INTEGER NOT NULL,
    UNIQUE (app_id, version_code)
);

CREATE INDEX IF NOT EXISTS idx_releases_app ON releases (app_id, version_code DESC);
CREATE INDEX IF NOT EXISTS idx_releases_latest ON releases (app_id, is_latest);

CREATE TABLE IF NOT EXISTS notices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id       INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    title        TEXT    NOT NULL DEFAULT '',
    content      TEXT    NOT NULL DEFAULT '',
    published_at INTEGER NOT NULL,
    created_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notices_app ON notices (app_id, id DESC);
"""


def _enable_wal(conn: sqlite3.Connection) -> None:
    """把连接切到 WAL。

    必须自己重试：多个进程同时冷启动时，各自都要把日志模式切成 WAL，
    而 SQLite 在切换 journal_mode 这条路径上并不总是走 busy handler，
    并发下会直接抛 SQLITE_BUSY，表现为启动即崩。实测 4 进程同时冷启动必现。
    """
    for _ in range(_WAL_ATTEMPTS):
        try:
            row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
        except sqlite3.OperationalError:
            time.sleep(_WAL_RETRY_INTERVAL)
            continue
        if row and str(row[0]).lower() == "wal":
            return
        time.sleep(_WAL_RETRY_INTERVAL)
    logger.warning("切换到 WAL 失败，继续以默认日志模式运行（功能正常，并发读性能略降）")


def _connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH), timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # busy_timeout 必须在其它 PRAGMA 之前设置，否则下面任何一条撞锁都不会等待。
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    _enable_wal(conn)
    return conn


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


# apk_* 时代的 releases 表已包含 app_id / version_code / force_update / is_latest /
# released_at / created_at，只差改名这 4 列和新增的 content_type，所以下面的迁移是完整的。
_LEGACY_COLUMN_SQL = """\
    ALTER TABLE releases RENAME COLUMN apk_path   TO build_path;
    ALTER TABLE releases RENAME COLUMN apk_name   TO build_name;
    ALTER TABLE releases RENAME COLUMN apk_size   TO build_size;
    ALTER TABLE releases RENAME COLUMN apk_sha256 TO build_sha256;
    ALTER TABLE releases ADD COLUMN content_type TEXT NOT NULL DEFAULT 'application/octet-stream';
    -- 落盘目录同时从 uploads/apk 改名为 uploads/build（先 mv 再跑上面这句）
    UPDATE releases SET build_path = 'build/' || substr(build_path, 5) WHERE build_path LIKE 'apk/%';"""


def _check_legacy_schema(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(releases)")}
    if "apk_path" in columns:
        raise RuntimeError(
            "检测到旧版数据库（releases 表还是 apk_* 列名），本版本已改用 build_*。\n"
            "请先备份 data/ 目录，然后任选其一：\n"
            "  1) 删除 data/apppublisher.db 重新初始化（会丢失已有应用与公告记录）；\n"
            "  2) 手工迁移：mv data/uploads/apk data/uploads/build && \\\n"
            "     sqlite3 data/apppublisher.db <<'SQL'\n"
            + _LEGACY_COLUMN_SQL
            + "\n     SQL\n"
        )


def init_db() -> None:
    conn = get_conn()
    # 先检查再建表：旧库列名与新版对不上，宁可在动任何东西之前就明确报错，
    # 也不要先跑一遍 DDL 再失败。
    _check_legacy_schema(conn)
    conn.executescript(SCHEMA)


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """写事务，可重入。异常回滚，正常提交。

    连接是按线程缓存的，所以嵌套 with 会在同一个连接上重复 BEGIN IMMEDIATE，
    SQLite 直接抛 "cannot start a transaction within a transaction"。
    内层改用 SAVEPOINT，外层回滚时内层的部分修改也一并撤销。
    """
    conn = get_conn()
    depth = getattr(_local, "tx_depth", 0)
    savepoint = f"ap_sp_{depth}"

    if depth == 0:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute(f"SAVEPOINT {savepoint}")
    _local.tx_depth = depth + 1

    try:
        yield conn
    except Exception:
        if depth == 0:
            conn.execute("ROLLBACK")
        else:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        if depth == 0:
            conn.execute("COMMIT")
        else:
            conn.execute(f"RELEASE {savepoint}")
    finally:
        _local.tx_depth = depth


def query_all(sql: str, params: Sequence[Any] = ()) -> list:
    cursor = get_conn().execute(sql, params)
    try:
        return [dict(row) for row in cursor.fetchall()]
    finally:
        cursor.close()


def query_one(sql: str, params: Sequence[Any] = ()) -> Optional[dict]:
    cursor = get_conn().execute(sql, params)
    try:
        row = cursor.fetchone()
        return dict(row) if row is not None else None
    finally:
        cursor.close()


def query_value(sql: str, params: Sequence[Any] = ()) -> Any:
    cursor = get_conn().execute(sql, params)
    try:
        row = cursor.fetchone()
        return row[0] if row is not None else None
    finally:
        cursor.close()


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """单条自动提交写入，返回 lastrowid。"""
    cursor = get_conn().execute(sql, params)
    try:
        return cursor.lastrowid
    finally:
        cursor.close()


def execute_tx(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> int:
    """在既有事务内执行写入，返回 lastrowid。"""
    cursor = conn.execute(sql, params)
    try:
        return cursor.lastrowid
    finally:
        cursor.close()
