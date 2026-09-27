"""访问统计。

设计取舍：
- **不存原始 IP**。访客标识 = HMAC(SECRET_KEY, ip + ua) 取前 16 位十六进制，
  既能算独立访客，又不可逆、无法反查个人。同一 NAT 后的设备因 UA 不同会分开计，
  所以 UV 是「设备级」近似值，不是人数。
- 记明细行而不是按天聚合，便于下钻到具体时间与来源；由 purge_old_events()
  按 STATS_RETENTION_DAYS 清理，表不会无限增长。
- 爬虫按 UA 关键词单独标记，默认不计入统计数字，但原始行保留。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from fastapi import Request

from . import config, db
from .utils import now_ms

logger = logging.getLogger("apppublisher.analytics")

KIND_VIEW = "view"
KIND_DOWNLOAD = "download"
KIND_UPDATE_CHECK = "update_check"
KIND_SHARE_CLICK = "share_click"
KINDS = (KIND_VIEW, KIND_DOWNLOAD, KIND_UPDATE_CHECK, KIND_SHARE_CLICK)

# ---------------------------------------------------------------- 分享链接

# 生成短码用的字母表：去掉了 l / o / 0 / 1 这类易混字符，因为分享码常被手抄或口述。
_SHARE_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
SHARE_CODE_MIN_LENGTH = 1
SHARE_CODE_MAX_LENGTH = 64
# 这是**入站校验**，必须比生成用的字母表宽松：?ref= 允许手写，用户会写成
# weibo_2024 或 my-ref 这种。太严会让「归因照样成立」的说明变成谎话。
SHARE_CODE_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]{%d,%d}$" % (SHARE_CODE_MIN_LENGTH - 1, SHARE_CODE_MAX_LENGTH - 1)
)
SHARE_TARGETS = ("intro", "download")
SHARE_TARGET_LABELS = {"intro": "介绍页", "download": "最新版下载"}


def generate_share_code(length: int = 8) -> str:
    return "".join(secrets.choice(_SHARE_ALPHABET) for _ in range(length))


def share_code_from_cookie(request: Request) -> str:
    """只从 Cookie 里取分享码。

    与 share_code_from() 分开是必要的：介绍页需要判断「这个访客还没被标记过」，
    若用后者，它会把本次的 ?ref= 也算进来，导致 Cookie 永远写不下去。
    """
    value = (request.cookies.get(config.SHARE_COOKIE_NAME) or "").strip().lower()
    return value if SHARE_CODE_RE.match(value) else ""


def share_code_from(request: Request) -> str:
    """本次请求携带的分享码。

    优先取 Cookie（首次点分享链接时写入），其次取 ?ref= 查询串
    （用户直接复制带参数的地址时用）。

    这里**只做格式校验，不查库** —— 热路径上省一次查询，归属正确性由统计
    查询时 join share_links(app_id, code) 保证，跨应用的码自然匹配不上。
    """
    cookie_value = share_code_from_cookie(request)
    if cookie_value:
        return cookie_value
    query_value = (request.query_params.get("ref") or "").strip().lower()
    return query_value if SHARE_CODE_RE.match(query_value) else ""


def set_share_cookie(response: Any, code: str) -> None:
    """把分享码写进第一方 Cookie，后续的下载/更新检查才能继续归因。"""
    response.set_cookie(
        key=config.SHARE_COOKIE_NAME,
        value=code,
        max_age=config.SHARE_COOKIE_MAX_AGE,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite=config.COOKIE_SAMESITE,
        path="/",
    )

# 只保留「几乎不可能是真实客户端」的爬虫 / 监控特征。
#
# 明确**不**包含：
#   - curl/、wget/、python-requests、python-urllib、httpx/、aiohttp、go-http-client
#     —— 这些是通用 HTTP 库。用户拿 curl 自测、CI 拉构建、或自己写个 Python/Go
#     客户端，都会命中，一旦判成机器人流量就从统计里消失了。
#   - okhttp、dalvik、cfnetwork、dart、ktor、java —— 这些是正常客户端的 UA。
#
# 漏判只是让数字略微偏高，误判却会把用户自己的客户端流量吃掉，两者不对等。
# `(?<!ro)` 是为了不把 "robot" 当成爬虫 —— "bot\b" 会匹配 "robot" 和 "abbot"，
# 而 "Googlebot"、"bingbot" 这种真正的爬虫名字里 "bot" 前面不是 "ro"，照常命中。
_CRAWLER_RE = re.compile(
    r"(?<!ro)bot(?![a-z])|crawler|spider|slurp|bingpreview|facebookexternalhit|"
    r"headlesschrome|phantomjs|puppeteer|playwright|"
    r"uptime|pingdom|statuscake|nagios|zabbix|"
    r"semrush|ahrefs|mj12|dotbot|bytespider|nmap|masscan|nessus|acunetix",
    re.IGNORECASE,
)

# 脚本化 / 程序库客户端。**不算机器人**，只在分类里单独标出来，
# 方便在统计页里辨认「这些是脚本调的，不是人点的」。
_CLI_UA_RE = re.compile(
    r"curl/|wget/|python-requests|python-urllib|httpx/|aiohttp|"
    r"go-http-client|libwww|httpie|axios/|node-fetch|undici|"
    r"okhttp|dalvik|cfnetwork|dart|ktor|java/|androiddownloadmanager",
    re.IGNORECASE,
)

# 顺序有意义：先认平台，再认脚本库。带平台信息的 UA（如
# "Dalvik/2.1.0 (Linux; U; Android 13)"）应归到 android，而不是 cli。
_UA_CLASS_RULES = (
    ("android", ("android", "dalvik")),
    ("ios", ("iphone", "ipad", "ipod", "cfnetwork")),
    ("windows", ("windows",)),
    ("macos", ("macintosh", "mac os x")),
    ("linux", ("linux", "x11")),
)


# ---------------------------------------------------------------- 采集


def is_bot(user_agent: str) -> bool:
    """只有明确的爬虫 / 监控特征才算机器人。

    **空 UA 不算**：裸 socket、Qt 的 QNetworkAccessManager、部分嵌入式客户端
    默认就不发 User-Agent，它们可能是真实用户在检查更新。
    """
    if not user_agent or not user_agent.strip():
        return False
    return bool(_CRAWLER_RE.search(user_agent))


def ua_class(user_agent: str) -> str:
    low = (user_agent or "").lower()
    if not low.strip():
        return "unknown"
    for name, needles in _UA_CLASS_RULES:
        if any(needle in low for needle in needles):
            return name
    if _CLI_UA_RE.search(low):
        return "lib"
    return "other"


def _bot_clause(include_bots: bool) -> str:
    """机器人过滤的 SQL 片段。

    **只排除介绍页浏览这一类**。下载与更新检查几乎只可能由真实客户端发起，
    把爬虫判定套在它们身上，一旦 UA 判断失误就会把用户的客户端流量整段吃掉 ——
    宁可介绍页的数字偏高，也不能漏记下载和更新检查。

    这里拼接的是固定字面量，不含任何外部输入。
    """
    if include_bots:
        return ""
    return " AND (kind <> 'view' OR is_bot = 0)"


def visitor_id(ip: str, user_agent: str) -> str:
    """加盐哈希的访客标识。同一个 (IP, UA) 组合结果稳定，可用来算 UV。"""
    return hmac.new(
        config.SECRET_KEY.encode("utf-8"),
        f"{ip}\n{user_agent}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:16]


def client_ip(request: Request) -> str:
    """取客户端 IP。

    只有显式开启 TRUST_PROXY_HEADERS 才读 X-Forwarded-For —— 直连部署时
    这个头由客户端随便填，信它等于让任何人伪造来源 IP。
    """
    if config.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _referrer_host(request: Request) -> str:
    """来源域名。站内跳转（同 host）不算来源，返回空串表示直接访问。"""
    raw = request.headers.get("referer") or ""
    if not raw:
        return ""
    try:
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError:
        return ""
    self_host = (request.url.hostname or "").lower()
    if not host or host == self_host:
        return ""
    return host[:120]


def record(
    request: Request,
    app_id: int,
    kind: str,
    release_id: Optional[int] = None,
    share_code: Optional[str] = None,
) -> None:
    """记一条事件。

    share_code 传 None 表示「按请求自动推断」（Cookie 或 ?ref=）；
    分享链接的跳转本身要显式传入，因为那一刻 Cookie 还没写下去。

    **绝不向调用方抛异常**：统计是附加能力，写失败不能影响对外接口的可用性。
    """
    try:
        user_agent = (request.headers.get("user-agent") or "")[:400]
        timestamp = now_ms()
        db.execute(
            "INSERT INTO events (app_id, kind, release_id, day, created_at, visitor,"
            " referrer_host, ua_class, is_bot, share_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                app_id,
                kind,
                release_id,
                time.strftime("%Y-%m-%d", time.localtime(timestamp / 1000)),
                timestamp,
                visitor_id(client_ip(request), user_agent),
                _referrer_host(request),
                ua_class(user_agent),
                1 if is_bot(user_agent) else 0,
                share_code if share_code is not None else share_code_from(request),
            ),
        )
    except Exception:  # noqa: BLE001 - 统计失败绝不影响主流程
        logger.warning("记录统计事件失败 kind=%s app_id=%s", kind, app_id, exc_info=True)


# ---------------------------------------------------------------- 清理

_PURGE_META_KEY = "events_last_purge_day"


def purge_old_events(force: bool = False) -> int:
    """删除超过保留期的明细，返回删除行数。

    每天最多真正执行一次（日期记在 meta 表里），所以可以随手在请求路径上调用。
    """
    today = date.today().isoformat()
    if not force:
        last = db.query_value("SELECT value FROM meta WHERE key = ?", (_PURGE_META_KEY,))
        if last == today:
            return 0

    cutoff = now_ms() - config.STATS_RETENTION_DAYS * 86400 * 1000
    with db.transaction() as conn:
        cursor = conn.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
        try:
            deleted = cursor.rowcount or 0
        finally:
            cursor.close()
        db.execute_tx(
            conn,
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_PURGE_META_KEY, today),
        )

    if deleted:
        logger.info(
            "统计明细清理：删除 %s 条早于 %s 天的记录", deleted, config.STATS_RETENTION_DAYS
        )
    return deleted


# ---------------------------------------------------------------- 查询


def _since_ms(days: int) -> int:
    """区间起点：本地时间「今天往前数 days-1 天」那一天的 0 点。

    对齐到自然日起点是刻意的。用 now - days*86400 的话，「最近 7 天」实际
    横跨 8 个自然日，那半天的事件会落不进按天分桶的图表，于是图表柱子总和
    比概览数字少一截。所有按区间的查询都走这里，口径才能互相自洽。
    """
    first_day = date.today() - timedelta(days=max(1, int(days)) - 1)
    return int(time.mktime(first_day.timetuple())) * 1000


def summary(app_id: int, days: int, include_bots: bool = False) -> Dict[str, Any]:
    """区间内的各 kind 计数。三个 kind 恒存在，模板可以直接取不用判空。"""
    since = _since_ms(days)
    filter_sql = _bot_clause(include_bots)
    kinds = {kind: {"pv": 0, "uv": 0} for kind in KINDS}
    for row in db.query_all(
        "SELECT kind, COUNT(*) AS total, COUNT(DISTINCT visitor) AS uniq FROM events "
        "WHERE app_id = ? AND created_at >= ?" + filter_sql + " GROUP BY kind",
        (app_id, since),
    ):
        kinds[row["kind"]] = {"pv": row["total"] or 0, "uv": row["uniq"] or 0}

    # 被过滤掉的机器人浏览单独给出，界面提示「还有 N 次未计入」。
    bot_views = (
        db.query_value(
            "SELECT COUNT(*) FROM events WHERE app_id = ? AND created_at >= ? "
            "AND kind = 'view' AND is_bot = 1",
            (app_id, since),
        )
        or 0
    )
    # 全时段下载量单独给一份：这是最关心的数字，不该随区间选择而消失。
    total_download = (
        db.query_value(
            "SELECT COUNT(*) FROM events WHERE app_id = ? AND kind = ?" + filter_sql,
            (app_id, KIND_DOWNLOAD),
        )
        or 0
    )
    # 带分享码的访问数。必须限定为**本应用自己的**分享码：分享 Cookie 是全局的
    # （path=/），访客从 A 应用的分享链接进来后，再看 B 应用的页面时事件上带的是
    # A 的码。不限定的话 B 的「分享带来访问」会把 A 的量算进来，而且与
    # share_link_stats 里逐链接的数字对不上。
    share_views = (
        db.query_value(
            "SELECT COUNT(*) FROM events WHERE app_id = ? AND created_at >= ? "
            "AND kind = 'view' AND share_code IN "
            "(SELECT code FROM share_links WHERE app_id = ?)" + filter_sql,
            (app_id, since, app_id),
        )
        or 0
    )
    return {
        "days": days,
        "include_bots": include_bots,
        "kinds": kinds,
        "total_pv": sum(entry["pv"] for entry in kinds.values()),
        "bot_views": bot_views,
        "total_download": total_download,
        "share_views": share_views,
    }


def daily_series(app_id: int, days: int, include_bots: bool = False) -> List[Dict[str, Any]]:
    """按天补齐的序列，缺失日期记 0，并预计算柱高百分比供模板直接用。

    since 与下面的桶由同一个 _since_ms 推出，所以柱子的总和恰好等于
    summary() 的 total_pv，图表和概览不会对不上。
    """
    since = _since_ms(days)
    bucket: Dict[str, Dict[str, int]] = {}
    for row in db.query_all(
        "SELECT day, kind, COUNT(*) AS total FROM events "
        "WHERE app_id = ? AND created_at >= ?" + _bot_clause(include_bots) + " "
        "GROUP BY day, kind",
        (app_id, since),
    ):
        bucket.setdefault(row["day"], {})[row["kind"]] = row["total"] or 0

    today = date.today()
    series: List[Dict[str, Any]] = []
    for offset in range(int(days) - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        entry = bucket.get(day, {})
        series.append(
            {
                "day": day,
                "label": day[5:],
                "total": sum(entry.values()),
                "view": entry.get(KIND_VIEW, 0),
                "download": entry.get(KIND_DOWNLOAD, 0),
                "update_check": entry.get(KIND_UPDATE_CHECK, 0),
                "share_click": entry.get(KIND_SHARE_CLICK, 0),
            }
        )

    peak = max((item["total"] for item in series), default=0)
    for item in series:
        item["pct"] = round(100 * item["total"] / peak) if peak else 0
    return series


def referrers(app_id: int, days: int, limit: int = 10, include_bots: bool = False) -> Dict[str, Any]:
    """访问来源。

    **只统计介绍页浏览。** 更新检查是客户端 API 调用，天然不会带 Referer，
    混进来会把「直接访问」这一栏灌满机器请求，让这个面板失去意义。
    """
    since = _since_ms(days)
    view_filter = " AND kind = 'view'" + _bot_clause(include_bots)
    rows = db.query_all(
        "SELECT referrer_host AS host, COUNT(*) AS total, COUNT(DISTINCT visitor) AS uniq "
        "FROM events WHERE app_id = ? AND created_at >= ?" + view_filter + " "
        "AND referrer_host <> '' GROUP BY referrer_host ORDER BY total DESC, host LIMIT ?",
        (app_id, since, limit),
    )
    direct = (
        db.query_value(
            "SELECT COUNT(*) FROM events WHERE app_id = ? AND created_at >= ?"
            + view_filter
            + " AND referrer_host = ''",
            (app_id, since),
        )
        or 0
    )
    return {"rows": rows, "direct": direct}


def ua_breakdown(app_id: int, days: int, include_bots: bool = False) -> List[Dict[str, Any]]:
    return db.query_all(
        "SELECT ua_class AS cls, COUNT(*) AS total FROM events "
        "WHERE app_id = ? AND created_at >= ?" + _bot_clause(include_bots) + " "
        "GROUP BY ua_class ORDER BY total DESC",
        (app_id, _since_ms(days)),
    )


def recent_downloads(app_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    return db.query_all(
        "SELECT e.created_at, e.referrer_host, e.ua_class, e.is_bot,"
        "       r.version_name, r.version_code "
        "FROM events e LEFT JOIN releases r ON r.id = e.release_id "
        "WHERE e.app_id = ? AND e.kind = ? ORDER BY e.created_at DESC LIMIT ?",
        (app_id, KIND_DOWNLOAD, limit),
    )


def share_link_stats(
    app_id: int, days: int, include_bots: bool = False
) -> List[Dict[str, Any]]:
    """每个分享链接的点击量，以及它带来的后续行为（介绍页访问 / 下载 / 更新检查）。

    点击 = 访问 /{slug}/s/{code} 本身；其余 = 带着该 code 发生的后续事件。
    只用两条聚合查询（区间内 + 全时段），不按链接逐个查，避免 N+1。
    """
    links = db.query_all("SELECT * FROM share_links WHERE app_id = ? ORDER BY id DESC", (app_id,))
    if not links:
        return []

    def counts_since(since: Optional[int]) -> Dict[str, Dict[str, int]]:
        sql = (
            "SELECT share_code, kind, COUNT(*) AS total FROM events "
            "WHERE app_id = ? AND share_code <> ''"
        )
        params: List[Any] = [app_id]
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(since)
        sql += _bot_clause(include_bots) + " GROUP BY share_code, kind"
        bucket: Dict[str, Dict[str, int]] = {}
        for row in db.query_all(sql, tuple(params)):
            bucket.setdefault(row["share_code"], {})[row["kind"]] = row["total"] or 0
        return bucket

    in_range = counts_since(_since_ms(days))
    all_time = counts_since(None)

    result: List[Dict[str, Any]] = []
    for link in links:
        recent = in_range.get(link["code"], {})
        total = all_time.get(link["code"], {})
        result.append(
            {
                "id": link["id"],
                "code": link["code"],
                "note": link["note"],
                "target": link["target"],
                "enabled": link["enabled"],
                "created_at": link["created_at"],
                # 区间内
                "clicks": recent.get(KIND_SHARE_CLICK, 0),
                "views": recent.get(KIND_VIEW, 0),
                "downloads": recent.get(KIND_DOWNLOAD, 0),
                "update_checks": recent.get(KIND_UPDATE_CHECK, 0),
                # 全时段
                "total_clicks": total.get(KIND_SHARE_CLICK, 0),
                "total_views": total.get(KIND_VIEW, 0),
                "total_downloads": total.get(KIND_DOWNLOAD, 0),
            }
        )
    return result


def app_totals_map(days: int) -> Dict[int, Dict[str, Dict[str, int]]]:
    """{app_id: {kind: {pv, uv}}} —— 供应用列表页一次性取全，避免 N+1。"""
    result: Dict[int, Dict[str, Dict[str, int]]] = {}
    for row in db.query_all(
        "SELECT app_id, kind, COUNT(*) AS total, COUNT(DISTINCT visitor) AS uniq "
        "FROM events WHERE created_at >= ?" + _bot_clause(False) + " GROUP BY app_id, kind",
        (_since_ms(days),),
    ):
        result.setdefault(row["app_id"], {})[row["kind"]] = {
            "pv": row["total"] or 0,
            "uv": row["uniq"] or 0,
        }
    return result
