# AppPublisher

给每个 Android 应用配一个介绍页，并提供「检查更新」与「公告」接口。FastAPI + SQLite，无外部服务依赖。

- 后台添加应用，短链即访问路径：短链 `QUTSchedule` → 介绍页 `/QUTSchedule`
- 客户端检查更新：`GET /QUTSchedule/releases/latest`
- 客户端拉公告：`GET /QUTSchedule/notices/latest`
- 构建产物（apk / ipa / exe / dmg / zip …）与图片存本地磁盘，由服务端直接分发。不限于 Android，任何文件都能挂上去分发

## 快速开始

```bash
python3 -m pip install -r requirements.txt

cp .env.example .env        # 按需修改；最低限度改一下 PUBLIC_BASE_URL
./run.sh                    # 默认 127.0.0.1:8000；HOST/PORT 可覆盖，RELOAD=1 开热重载
```

首次启动会创建初始管理员账号。**没有设置 `ADMIN_PASSWORD` 时，密码会随机生成，写入 `data/.initial_admin_password`（权限 600），不打印到日志**：

```
WARNING apppublisher: 已创建初始管理员账号 admin。随机初始密码写在
  /srv/apppublisher/data/.initial_admin_password（权限 600）……
```

初始口令刻意不走 stdout：systemd / Docker / k8s 会把 stdout 收进日志并长期留存，等于把凭据永久写进日志归档。查看该文件拿到密码后，打开 `http://localhost:8000/admin/login` 登录并在「密码」页改掉——**改完之后这个文件会自动删除**（只有初始管理员本人改密会删它，其它账号改自己的密码不受影响）。

跑一遍端到端自检（用临时数据目录，不动 `./data`）：

```bash
python3 tests/smoke_test.py
```

## 后台使用流程

1. **新建应用** — 填名称（如 `QUT课表`）和短链（如 `QUTSchedule`）。
2. **填介绍页** — 在应用详情页把 HTML 粘进文本框，或直接上传 `.html` 文件。这段 HTML 会原样输出到 `/QUTSchedule`，想怎么写就怎么写。
3. **上传构建产物** — 填版本名（`v1.2.0`）、版本号（`26092711`）、更新说明，选择文件。首次上传会自动成为「最新版本」，之后可以手动切换。
4. **发公告** — 填标题和正文，发布时间留空即为当前时间。

应用详情页底部有「接口速查」，会实时显示该应用各接口的地址，以及 `releases/latest` 和 `notices/latest` 当前的真实返回，方便对着写客户端。

## 接口

### `GET /{slug}` — 介绍页

返回后台配置的 HTML 原文（`text/html`）。应用被下线或不存在时返回 404。

### `GET /{slug}/releases/latest` — 检查更新

```json
{
  "versionName": "v1.2.0",
  "versionCode": 26092711,
  "desc": "更新描述",
  "timestamp": 1758931200000,
  "download": "https://example.com/QUTSchedule/build/26092711",
  "appName": "QUT课表",
  "size": 8388608,
  "sha256": "9f2c...",
  "forceUpdate": false
}
```

前 5 个字段是稳定契约；后 4 个是增量补充（`sha256` 可用于校验下载完整性，`forceUpdate` 由后台勾选）。

`timestamp` 是 **epoch 毫秒**。没有发布过任何版本时返回 404。

### `GET /{slug}/releases` — 版本列表

`{"slug": "...", "count": 3, "limit": 100, "generatedAt": 1758931200000, "releases": [ … ]}`，元素结构与上面一致。

**最多返回 100 条**（按 versionCode 从新到旧）。`count == limit` 时说明可能还有更早的记录没返回。需要完整历史请从数据库或后台读。

### `GET /{slug}/build/{versionCode}` — 下载构建产物

按 `versionCode` 定位文件并下发，`Content-Type` 按上传时的扩展名判定（`.apk` → `application/vnd.android.package-archive`，`.zip` → `application/zip`，`.exe`/`.dmg`/`.ipa` 等各有对应值，未知类型回落 `application/octet-stream`）。`Content-Disposition` 的文件名由服务端生成，扩展名跟随原始文件（`QUTSchedule-v1.2.0.apk`、`QUTSchedule-v1.3.0-win.zip`）。

用 `versionCode` 而不是版本名定位，所以改版本名不会让已发出的下载链接失效。

**允许上传的类型**（定义在 `app/config.py` 的 `CONTENT_TYPES`）：`.apk .aab .ipa .exe .msi .dmg .pkg .deb .rpm .appimage .zip .7z .tar.gz .tgz .gz .xz .zst .jar`。

要加别的类型，在 `.env` 里追加即可，无需改代码：

```bash
EXTRA_BUILD_EXTENSIONS=.bin,.rom,.xapk
```

追加的类型没有内置 Content-Type，会回落到 `application/octet-stream`（或 Python `mimetypes` 猜出来的值）。

### `GET /{slug}/notices/latest` — 最新公告

```json
{
  "title": "维护通知",
  "content": "今晚 23:00 维护",
  "timestamp": 1758931200000,
  "id": 2
}
```

「最新」= 发布时间最晚的一条，时间相同时取 id 最大的。没有公告时返回 404。

### `GET /{slug}/notices` 与 `GET /{slug}/notices/{id}`

分别是公告列表与单条公告，单条结构同上。列表同样**最多 100 条**，响应带 `limit` 字段。

### 辅助接口

- `GET /` — 列出所有已上线的应用
- `GET /health` — `{"status": "ok", "apps": 3}`
- `GET /media/...` — 上传的图片，可直接在介绍页里用
- `GET /docs` — FastAPI 自动生成的接口文档

## 数据统计

后台每个应用的「数据统计」分页记录三类事件，可切换 7 / 30 / 90 / 180 天区间：

| 指标 | 触发点 |
| --- | --- |
| 介绍页访问 (PV / UV) | 每次打开 `/{slug}` |
| 下载次数 | 每次从 `/{slug}/build/{versionCode}` 成功取到文件（404 / 410 不计） |
| 更新检查 | 每次请求 `/{slug}/releases/latest` |
| 分享链接点击 | 每次打开 `/{slug}/s/{code}` |

另有**访问来源**（按 Referer 域名聚合，站内跳转不计为来源）、**客户端类型**（按 UA 粗分类）
和**最近 20 次下载明细**。

> 「访问来源」**只统计介绍页浏览**。更新检查与下载是客户端发起的 API 调用，天然不带
> `Referer`，混进来会把「直接访问」那一栏灌满机器请求，让这个面板失去参考价值。
>
> 区间统一按**自然日**计算：「最近 7 天」= 今天往前数 7 天（含今天）的本地 0 点起算。
> 所有按区间的数字（概览、趋势图、来源、分享效果）共用同一个起点，所以图表柱子总和
> 与概览的 `total_pv` 必然相等。

### 隐私与口径

- **不存原始 IP。** 访客标识是 `HMAC(SECRET_KEY, IP + User-Agent)` 的前 16 位十六进制，
  不可逆、无法反查个人。同一 NAT 后的设备因 UA 不同会分开计，所以 **UV 是设备级近似值，不是人数**。
- 轮换 `SECRET_KEY` 会让访客标识全部变化，UV 连续性中断（PV 不受影响）。
- 明细默认保留 **180 天**（`STATS_RETENTION_DAYS`），启动时与打开统计页时各检查一次，
  每天最多真正清理一次。
- 因此**「累计下载」「累计访问」这类总数都是保留期内的值**，不是真正的全时段计数 ——
  随着旧明细被清理，它们可能随时间下降。界面上的标签已按此措辞，避免误读。
  需要真正的历史总量，应另建不受清理影响的自增计数器（当前未实现）。
- **爬虫过滤只作用于介绍页浏览。** 下载与更新检查**从不**做机器人过滤 —— 那两个接口
  只可能由客户端发起，UA 一旦判错就会把真实流量整段吃掉。宁可介绍页数字偏高，也不漏记下载。
- 爬虫判定采取**保守**策略，只认明确的爬虫 / 监控特征：`Googlebot`、`Bingbot`、`Baiduspider`、
  `UptimeRobot`、`headless Chrome`、`facebookexternalhit` 等。以下都**不判为机器人**：
  - `curl` / `wget` / `python-requests` / `httpx` / `aiohttp` / `Go-http-client` 等通用 HTTP 库
    —— 你拿 curl 自测、CI 拉构建、或自己写个脚本客户端，都会被正常记录；
  - `okhttp` / `Dalvik` / `AndroidDownloadManager` / `CFNetwork` / `Dart` —— 正常客户端 UA；
  - **空 UA** —— 裸 socket、Qt 的 `QNetworkAccessManager`、部分嵌入式客户端默认就不发 UA。

  这些会归到 `lib` / `unknown` 等分类，在「客户端类型」里可见，不会被丢弃。
- 被挡下的机器人浏览数会在统计页**显式提示**（「另有 N 次机器人访问未计入」），
  并可以一键切换成「包含机器人」查看。
- 统计写入失败**不会**影响对外接口：埋点整体包在异常捕获里，失败只记 warning 日志。

### 反向代理下的来源 IP

默认只信 `request.client`，**不读 `X-Forwarded-For`** —— 直连部署时这个头由客户端随便填，
信它等于允许任何人伪造来源 IP。放在 Nginx / Caddy 后面时，确认代理会覆写该头，再开启：

```bash
TRUST_PROXY_HEADERS=1
```

## 分享链接与归因

后台应用详情页的「分享链接」分页可以为每个应用生成多条带短码的链接，每条可写备注
（「B 站动态」「QQ 群」），用来分辨是哪个渠道带来的流量。

```
https://example.com/QUTSchedule/s/k7m3pq2r     →  打开介绍页
https://example.com/QUTSchedule/s/x9n4wt6v     →  直接下载当前最新版
```

工作方式：

1. 有人打开 `/{slug}/s/{code}`，记一次**分享链接点击**，并把短码写进一个第一方 Cookie
   （`apppublisher_share`，30 天过期，`HttpOnly` + `SameSite=Lax`）。
2. 该访客随后的介绍页访问、产物下载、更新检查都会带上这个短码，于是能算出
   **每条链接带来了多少访问和多少下载**。
3. 落地页选「最新版下载」时，链接会跳到当前最新版本的产物；之后发布了新版本，
   同一个链接会自动跟到新的最新版 —— 分享出去的地址不用重新发。

几个实现上的选择：

- **短码未知或已停用时照常 302 跳转**，只是不归因。已经发出去的链接不该因为后台删了
  记录就甩个 404 给用户。
- **删除链接不会删掉已记录的归因数据**，历史统计仍然保留（查询时 join 不上就自然忽略）。
- 不想生成短码时，也可以直接在地址后加 `?ref=标记`（**只接受小写字母、数字、点、下划线、
  连字符，长度 1–64**；大写会转小写，其余取值会被安静丢弃），同样会写进 Cookie 并归因。
  但这类来源**不出现在分享链接表里，也不计入「分享链接带来的访问」** —— 那个数字只统计
  后台生成过的短链。`?ref=` 的取值只保留在事件明细中，界面不做聚合。

> **隐私提示**：这是个第一方归因 Cookie，用途仅限「知道访客从哪条链接来」。
> 它不跨站、不含个人信息，30 天后自动过期。如果你的访客对 Cookie 敏感，
> 可以只用 `?ref=` 配合「访问来源」里的 Referer 域名做粗略归因。

## 封面图与根页

根页 `/` 是卡片式列表，**跟随系统明暗主题**，右上角按钮可在「跟随系统 / 浅色 / 深色」之间
循环切换，选择存在浏览器 localStorage 里。

每个应用可以在后台「媒体资源」分页上传一张 **16:9 封面图**；未上传时用按短链稳定推导的
渐变色块占位。卡片还会展示「一句话简介」（在「介绍页」分页填写）与最新版本，并直接提供
下载按钮。

「媒体资源」分页同时会**列出该应用已上传的全部图片**，每张带缩略图、体积、上传时间和
一条可直接复制的完整地址（点击输入框即全选），并支持单张删除。

图片按上传时所属的应用归属登记在 `assets` 表里，**不能跨应用删除**（在 A 应用的页面里
删不掉 B 应用的图片）。删除应用时会连它登记的图片文件一起清理。

> 如果 `data/uploads/images/` 里存在**没有归属记录**的文件（本次升级之前上传的，或手工
> 放进目录的），它们会被归到媒体页底部的「未登记的图片」里单独列出，仍然可以查看地址。
> 这类文件不会被任何应用的删除操作清理，需要你自己确认后手动删除。

## 后台界面

应用详情页按功能分为七个分页，以 URL hash 定位（可以直接分享 `#stats` 这类链接）：

**介绍页** · **媒体资源** · **发行版** · **公告** · **数据统计** · **分享链接** · **删除应用**

应用列表页会汇总显示每个应用最近 30 天的 PV / UV / 下载，以及封面缩略图。

## 维护脚本

### 认领未登记的图片

`data/uploads/images/` 里可能存在**没有归属记录**的文件 —— 本功能上线前上传的，或手工拷进
目录的。它们会出现在后台「媒体资源」分页底部的「未登记的图片」里，但不会被任何应用的删除
操作清理。要把它们正式归到某个应用名下：

```bash
# 1) 先预览：列出未登记图片和可选应用，不写库
python3 scripts/claim_images.py

# 2) 全部认领给某个应用
python3 scripts/claim_images.py --app qutschedule --apply

# 3) 或逐个指定（文件名或相对路径都行，可重复）
python3 scripts/claim_images.py \
    --assign 134f65b7.png=qutschedule \
    --assign banner.png=another-app \
    --apply
```

**默认只预览，不加 `--apply` 不会写任何东西。** 脚本只往 `assets` 表插记录，
不移动/重命名文件，也不改封面设置 —— 想把某张图设成封面，请走后台「媒体资源」上传。

> 升级会给已有的库做**增量迁移**：补 `apps.tagline`、`apps.banner_path`、`events.share_code`
> 三列，新建 `events` / `meta` / `assets` / `share_links` 四张表。全部是
> `ALTER TABLE ADD COLUMN` 与 `CREATE TABLE IF NOT EXISTS`，原有数据不受影响。
>
> 迁移的**执行顺序有讲究**：补列必须先于跑 `SCHEMA`，因为 `SCHEMA` 里有
> `CREATE INDEX ... ON events (app_id, share_code)` 这类语句 —— 老库的 `events`
> 表还没有那一列，先建索引会直接 `no such column` 把启动搞挂。
>
> 统计从升级后开始累积，历史访问无法回溯。

## API 密钥与 JSON 管理接口

后台「API 密钥」分页可以为自己的账号生成密钥，然后用它脱离网页操作接口：
建应用、改信息、传图片与发行版、发公告、管分享链接、读统计。

```bash
curl -H "Authorization: Bearer ap_xxxx..." https://example.com/api/v1/me
```

- 完整接口清单与字段说明见 [`docs/api.md`](docs/api.md)（面向 AI 助手），
  交互式文档在 `/docs`。
- 密钥形如 `ap_` + 43 字符，**只在创建时显示一次**；库里只存它的 SHA-256。
  这里用 SHA-256 而不是口令那套 PBKDF2，是因为密钥是 32 字节随机串、暴力枚举不可行，
  而 PBKDF2 每次请求要跑 21 万轮，做鉴权太慢。
- 权限**跟随创建者的账号**，账号被删时其密钥随外键级联清除。
- 鉴权走请求头，**不需要 CSRF** —— 密钥是显式凭据，不像 Cookie 会被浏览器自动带上。

### 应用归属

应用现在有归属（`apps.owner_id`）：

- **超管**：看得到、管得了所有应用。
- **普通账号**：只看得到、只改得动**自己创建的**应用；碰别人的一律 404
  （不是 403 —— 不透露「存在但不属于你」）。
- 这条规则在**网页后台与 JSON API 上走的是同一份实现**（`app/services.py`），
  不会出现「网页里能改、API 里不能改」。

本次升级之前创建的应用归属为空，**只有超管能管**（原作者的普通账号也看不到）。
用脚本认领：

```bash
python3 scripts/claim_apps.py                    # 预览：列出无归属的应用与可选账号
python3 scripts/claim_apps.py --user alice --apply
python3 scripts/claim_apps.py --app qutschedule=alice --apply
```

## 客户端接入

**接入方（尤其是让 AI 写客户端代码的场景）请直接看 [`docs/client-integration.md`](docs/client-integration.md)。**
那份文档是写给 AI 编码助手看的接入规范，包含完整的字段表、错误语义、
必须遵守的约束、常见错误清单，以及可直接使用的 Kotlin 参考实现。

下面是同一套接口的精简示例：

## 客户端检查更新示例（Kotlin）

```kotlin
data class ReleaseInfo(
    val versionName: String,
    val versionCode: Int,
    val desc: String,
    val timestamp: Long,
    val download: String,
    val sha256: String? = null,
    val forceUpdate: Boolean = false,
)

suspend fun checkUpdate(): ReleaseInfo? = withContext(Dispatchers.IO) {
    runCatching {
        val connection = URL("https://example.com/QUTSchedule/releases/latest").openConnection()
                as HttpURLConnection
        connection.connectTimeout = 5000
        connection.readTimeout = 5000
        val json = connection.inputStream.bufferedReader().use { it.readText() }
        Gson().fromJson(json, ReleaseInfo::class.java)
    }.getOrNull()
}
// 与当前 versionCode 比较，大于则提示更新
```

## 目录结构

```
app/
  main.py           入口：组装 FastAPI、挂载 /media、注册路由
  config.py         配置（环境变量 + .env）
  db.py             SQLite 访问层，连接按线程缓存，WAL 模式
  security.py       口令哈希、会话 Cookie、CSRF
  storage.py        上传落盘与路径校验
  serializers.py    对外 JSON 结构（后台预览与真实接口共用）
  routers/
    admin.py        /admin 后台
    public.py       /{slug} 介绍页与公开接口
  templates/        后台界面（Jinja2）
scripts/
  claim_images.py   把未登记的上传图片认领到应用（默认 dry-run）
data/               运行期生成，已 gitignore
  apppublisher.db   SQLite 数据库
  uploads/build/    上传的构建产物（apk/ipa/exe/zip…）
  uploads/images/   上传的图片
  .secret_key       自动生成的签名密钥（权限 600）
tests/smoke_test.py 端到端自检
```

## 配置

全部通过环境变量或项目根目录的 `.env` 配置，详见 `.env.example`。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PUBLIC_BASE_URL` | 空 | `download` 字段的站点前缀。留空则按请求 Host 推断；**放在反向代理后必须显式配置**，否则可能生成 `http://内部地址/...` |
| `ADMIN_USERNAME` | `admin` | 仅在数据库还没有任何账号时生效 |
| `ADMIN_PASSWORD` | 空 | 留空则随机生成并打印一次 |
| `SECRET_KEY` | 自动生成 | 不配则生成到 `data/.secret_key`，删掉它会让所有登录失效 |
| `COOKIE_SECURE` | `0` | 生产环境跑 HTTPS 时设为 `1` |
| `SESSION_MAX_AGE` | `604800` | 会话有效期（秒），默认 7 天 |
| `MAX_BUILD_MB` | `1024` | 单个构建产物大小上限（旧名 `MAX_APK_MB` 仍兼容） |
| `EXTRA_BUILD_EXTENSIONS` | 空 | 追加允许上传的扩展名，逗号分隔，如 `.bin,.rom` |
| `MAX_IMAGE_MB` | `8` | 单张图片大小上限 |
| `MAX_INTRO_MB` | `4` | 介绍页 HTML 大小上限 |
| `STATS_RETENTION_DAYS` | `180` | 访问明细保留天数，超期自动清理 |
| `STATS_DEFAULT_RANGE_DAYS` | `30` | 后台统计页与应用列表默认区间 |
| `TRUST_PROXY_HEADERS` | `0` | 是否信任 `X-Forwarded-For`。**仅当反向代理会覆写该头时才可开启**，否则来源 IP 可被伪造 |
| `APPPUBLISHER_DATA_DIR` | `./data` | 数据目录 |
| `APPPUBLISHER_DB` | `./data/apppublisher.db` | 数据库文件 |

## 部署

### systemd

```ini
[Unit]
Description=AppPublisher
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/srv/apppublisher
EnvironmentFile=/srv/apppublisher/.env
ExecStart=/usr/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always

[Install]
WantedBy=multi-user.target
```

> **`--workers` 固定为 1。** 当前实现用进程内字典做登录失败节流，多进程下该状态不共享。SQLite 本身支持多进程，但节流会退化。需要横向扩展时应把节流改到 Redis 或反向代理层。
>
> 顺带说明：**冷启动并发是安全的**。`SECRET_KEY` 用 `O_CREAT|O_EXCL` 抢占、初始管理员靠 `username` 的 UNIQUE 约束、WAL 切换自带重试，所以多个副本同时首次启动不会互相踩。受限的只是上面那条节流状态。

### Nginx

```nginx
server {
    listen 443 ssl;
    server_name example.com;

    # 构建产物可能很大，别让默认的 1M 卡住上传
    client_max_body_size 1024m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }
}
```

配上反代后，`.env` 里要写 `PUBLIC_BASE_URL=https://example.com`。

## 安全说明

已实现：

- 口令 PBKDF2-HMAC-SHA256（21 万次迭代，每用户独立 salt）
- 会话 Cookie 为 `HttpOnly`，改密码后该账号所有旧会话立即失效
- 所有写操作（含登出）校验 CSRF token；Cookie 默认 `SameSite=Lax`
- 登录表单同样校验 CSRF——此时还没有会话，改用双提交 Cookie：同一个随机值同时进 Cookie 与表单，攻击者读不到受害者的 Cookie，因此无法伪造
- 登录失败 5 次锁定 5 分钟；节流表按最后失败时间过期并设了容量上限，防止用大量一次性用户名把内存撑爆
- 上传文件名由服务端随机生成，不使用客户端文件名；**先做路径越界校验再建目录**，落盘与读取都校验
- 短链白名单字符集 + 系统保留字黑名单（`admin`/`api`/`media`/`docs` 等）
- 多账号 + 超级管理员分级；不能删自己，也不能删掉最后一个超管（校验与删除在同一写事务内，避免并发删除把超管删光）
- 下载文件名由服务端拼装，不受客户端输入影响；扩展名取自白名单，非白名单类型在入库前就被拒
- 构建产物一律以 `Content-Disposition: attachment` 下发，避免 `.html`/`.svg` 之类被浏览器当页面渲染
- 介绍页上传的文件在**服务端**校验扩展名（`.html`/`.htm`/`.txt`），不只依赖浏览器 `accept` 提示
- 图片上传**不接受 `.svg`**：`/media` 由 StaticFiles 直出，SVG 会以内联 `image/svg+xml` 渲染成文档，其中的 `<script>` 会在本站源下执行
- 初始随机口令写入 `data/.initial_admin_password`（600）而非 stdout，改密后自动删除
- `SECRET_KEY` 落盘用临时文件 + `os.replace()` 原子替换，多 worker 并发启动不会各写各的

需要自己注意的：

- **介绍页 HTML 原样输出，其中的脚本会真实执行。** 这是「后台粘贴 HTML」这一需求的固有代价，不是靠转义能解决的问题——要保留这个自由度就得接受它。别粘贴来路不明的代码。
  另外：**任何能登录后台的账号（含普通管理员）都能改介绍页**，也就都能在对外页面上跑脚本。如果多个管理员账号之间并不完全互信，应在反向代理层给 `/admin` 单独加访问限制。
  会话 Cookie 是 `HttpOnly`，脚本读不到它，所以最坏情况是页面被篡改，而不是后台被接管。
- 登录失败节流是**进程内**状态，所以 uvicorn 固定 `--workers 1`（见部署章节）；多进程下节流会退化。
- 后台没有开放注册，账号只能由超管在后台创建。
- 建议在反代层再叠一层访问限制（IP 白名单 / Basic Auth 保护 `/admin`）。
