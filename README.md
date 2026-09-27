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
./run.sh                    # 默认 0.0.0.0:8000；RELOAD=1 ./run.sh 开热重载
```

首次启动会创建初始管理员账号。**没有设置 `ADMIN_PASSWORD` 时，密码会随机生成并打印在启动日志里，只显示这一次**：

```
==============================================================
  已创建初始管理员账号（此密码只显示这一次）
    用户名: admin
    密  码: Xy3k...（随机）
==============================================================
```

拿到密码后打开 `http://localhost:8000/admin/login` 登录，第一件事去「密码」页改掉。

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

### `GET /{slug}/releases` — 所有版本

`{"slug": "...", "count": 3, "generatedAt": 1758931200000, "releases": [ … ]}`，元素结构与上面一致。

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

分别是公告列表与单条公告，单条结构同上。

### 辅助接口

- `GET /` — 列出所有已上线的应用
- `GET /health` — `{"status": "ok", "apps": 3}`
- `GET /media/...` — 上传的图片，可直接在介绍页里用
- `GET /docs` — FastAPI 自动生成的接口文档

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
- 所有写操作校验 CSRF token；Cookie 默认 `SameSite=Lax`
- 登录失败 5 次锁定 5 分钟
- 上传文件名由服务端随机生成，不使用客户端文件名；落盘与读取都做路径越界校验
- 短链白名单字符集 + 系统保留字黑名单（`admin`/`api`/`media`/`docs` 等）
- 多账号 + 超级管理员分级；不能删自己，也不能删掉最后一个超管
- 下载文件名由服务端拼装，不受客户端输入影响；扩展名取自白名单，非白名单类型在入库前就被拒
- 构建产物一律以 `Content-Disposition: attachment` 下发，避免 `.html`/`.svg` 之类被浏览器当页面渲染

需要自己注意的：

- **介绍页 HTML 原样输出，其中的脚本会真实执行。** 这是「后台粘贴 HTML」这一需求的固有代价。别粘贴来路不明的代码；会话 Cookie 是 `HttpOnly`，脚本读不到它，所以最坏情况是页面被篡改，而不是后台被接管。
- 上传的 `.svg` 同样可以内嵌脚本，只在介绍页里引用自己上传的图片。
- 后台没有开放注册，账号只能由超管在后台创建。
- 建议在反代层再叠一层访问限制（IP 白名单 / Basic Auth 保护 `/admin`）。
