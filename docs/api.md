# AppPublisher 管理接口（`/api/v1`）

> **本文档的读者是 AI 编码助手。** 你要用 API 密钥帮用户自动化发布流程。
> 请严格按本文的路径、字段名与错误语义实现；本文没写的先问用户，不要猜。

配套的交互式文档在 `/docs`（FastAPI 自动生成，可直接试请求）。
另有面向**客户端接入**（检查更新/拉公告）的文档：[`client-integration.md`](client-integration.md)。

---

## 0. 先向用户要这两个值

| 变量 | 说明 |
| --- | --- |
| `BASE_URL` | 站点根地址，结尾不带 `/`，例如 `https://example.com` |
| `API_KEY` | `ap_` 开头的一串，在后台「API 密钥」页生成 |

**不要编造密钥，也不要把密钥写进源码或提交进 Git。** 从环境变量读：

```bash
export APPPUBLISHER_URL="https://example.com"
export APPPUBLISHER_KEY="ap_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

---

## 1. 认证

```http
Authorization: Bearer ap_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

也接受 `X-API-Key: ap_...`。两条都缺失或无效 → `401` 加 `WWW-Authenticate: Bearer`。

**密钥的权限等于创建者账号的权限**，且只能操作该账号创建的应用：

- 普通账号：只能看到/操作自己的应用。碰别人的应用一律 **404**（不是 403 —— 服务端刻意不透露「存在但不属于你」）。
- 超管：可以操作所有应用。

密钥被撤销、或创建者账号被删除时，请求一律 401。

### 验证密钥

```bash
curl -H "Authorization: Bearer $APPPUBLISHER_KEY" "$APPPUBLISHER_URL/api/v1/me"
```

```json
{"user_id": 1, "username": "admin", "display_name": "超级管理员",
 "is_super": true, "key_name": "CI 发布机", "key_prefix": "9f3c1a2b"}
```

---

## 2. 约定

- **本接口统一 snake_case**；公开读取接口（`/{slug}/releases/latest`）是 camelCase。这是刻意的两套 API，不要混淆。
- 时间戳一律 **epoch 毫秒**（13 位）。
- 上传用 `multipart/form-data`，文件字段名统一叫 `file`。
- 错误响应：`{"error": "人类可读的原因"}`，配对应状态码。
- **写接口没有 CSRF、也不需要 Cookie** —— 密钥是显式凭据，不像 Cookie 会被浏览器自动带上。

| 状态码 | 含义 |
| --- | --- |
| 200 / 201 | 成功 |
| 400 | 参数不合法（短链格式、版本号非正整数、标题为空…） |
| 401 | 密钥缺失/无效/已撤销 |
| 404 | 资源不存在，**或存在但无权访问** |
| 413 | 上传文件超过大小上限 |
| 422 | 请求体/参数类型不对（`error` 为可读描述，原始结构在 `details`） |
| 500 | 服务端错误（看服务端日志） |

---

## 3. 应用

### 列出

```bash
curl -H "Authorization: Bearer $APPPUBLISHER_KEY" "$APPPUBLISHER_URL/api/v1/apps"
```

```json
{"count": 2, "apps": [
  {"id": 1, "slug": "QUTSchedule", "name": "QUT课表", "tagline": "课表查询",
   "enabled": true, "owner_id": 1,
   "latest_version_code": 26092711, "latest_version_name": "v1.2.0"}
]}
```

### 创建

```bash
curl -X POST "$APPPUBLISHER_URL/api/v1/apps" \
  -H "Authorization: Bearer $APPPUBLISHER_KEY" -H "Content-Type: application/json" \
  -d '{"slug":"QUTSchedule","name":"QUT课表","tagline":"课表查询与提醒","intro_html":"<h1>你好</h1>"}'
```

字段：`slug`（必填，字母数字下划线连字符，1–64，不能是 `admin`/`api`/`media` 等保留字）、
`name`（必填）、`tagline`、`intro_html`、`enabled`（默认 `true`）。
**除 `slug` 与 `name` 外都可省略。** 新应用的 `owner_id` 自动设为密钥所属账号。

> `intro_html` 会被**原样输出**到公开介绍页，里面的脚本会真实执行。不要往这里塞来路不明的代码。

### 查看 / 修改 / 删除

```bash
curl "$BASE/api/v1/apps/QUTSchedule" -H "Authorization: Bearer $K"          # 含 releases 与 notices
curl -X PATCH "$BASE/api/v1/apps/QUTSchedule" -H "Authorization: Bearer $K" \
     -H "Content-Type: application/json" -d '{"enabled":false}'
curl -X DELETE "$BASE/api/v1/apps/QUTSchedule" -H "Authorization: Bearer $K"
```

**PATCH 只改你传的字段**，没传的保持原值 —— 想清空某个字段要显式传空串。

> 修改 `slug` 会让旧地址立即失效，已发布客户端里写死的 URL 会断。**除非用户明确要求，不要改 slug。**
> 删除应用会连同它的全部发行版、构建产物文件、封面与公告一起清掉，**不可撤销**。执行前先向用户确认。

---

## 4. 发行版

上传产物是 multipart，字段名 `file`：

```bash
curl -X POST "$BASE/api/v1/apps/QUTSchedule/releases" \
  -H "Authorization: Bearer $K" \
  -F "file=@app-release.apk" \
  -F "version_name=v1.2.0" \
  -F "version_code=26092711" \
  -F "description=修复课表导入失败" \
  -F "force_update=false" \
  -F "make_latest=true"
```

| 字段 | 说明 |
| --- | --- |
| `file` | 必填。扩展名必须在白名单内（`.apk .aab .ipa .exe .dmg .zip .7z .tar.gz …`，可用 `EXTRA_BUILD_EXTENSIONS` 扩充） |
| `version_name` | 必填，展示用 |
| `version_code` | 必填，**正整数，应用内唯一**；重复会返回 400 |
| `description` | 更新说明 |
| `force_update` | 客户端据此决定是否强制更新 |
| `make_latest` | 默认 `true`；**首个版本无论如何都会成为 latest** |
| `released_at` | epoch 毫秒，留空取当前时间 |

其余操作：

```bash
curl "$BASE/api/v1/apps/QUTSchedule/releases" -H "Authorization: Bearer $K"
curl -X PATCH "$BASE/api/v1/apps/QUTSchedule/releases/26092711" -H "Authorization: Bearer $K" \
     -H "Content-Type: application/json" -d '{"description":"补充说明"}'
curl -X POST "$BASE/api/v1/apps/QUTSchedule/releases/26092711/latest" -H "Authorization: Bearer $K"
curl -X DELETE "$BASE/api/v1/apps/QUTSchedule/releases/26092711" -H "Authorization: Bearer $K"
```

> 用 `version_code` 定位版本，不是 `version_name` —— 版本名可以随便改，版本号不行。
> 删除 latest 版本时，服务端会自动把 `versionCode` 最大的那个补位。

---

## 5. 图片与封面

```bash
curl -X POST "$BASE/api/v1/apps/QUTSchedule/images" -H "Authorization: Bearer $K" \
     -F "file=@screenshot.png"          # → 201 {"id":3,"size":102400,"url":".../media/images/xxx.png"}
curl "$BASE/api/v1/apps/QUTSchedule/images" -H "Authorization: Bearer $K"
curl -X DELETE "$BASE/api/v1/apps/QUTSchedule/images/3" -H "Authorization: Bearer $K"

curl -X POST   "$BASE/api/v1/apps/QUTSchedule/banner" -H "Authorization: Bearer $K" -F "file=@cover.png"
curl -X DELETE "$BASE/api/v1/apps/QUTSchedule/banner" -H "Authorization: Bearer $K"
```

封面显示在根页卡片顶部，建议 16:9。
**不接受 `.svg`**（会被浏览器当文档内联渲染，其中的脚本能在本站源下执行）。
上传成功后把返回的 `url` 填进 `intro_html` 即可引用。

---

## 6. 公告

```bash
curl -X POST "$BASE/api/v1/apps/QUTSchedule/notices" -H "Authorization: Bearer $K" \
     -H "Content-Type: application/json" \
     -d '{"title":"维护通知","content":"今晚 23:00 维护"}'

curl "$BASE/api/v1/apps/QUTSchedule/notices" -H "Authorization: Bearer $K"
curl -X PATCH "$BASE/api/v1/apps/QUTSchedule/notices/3" -H "Authorization: Bearer $K" \
     -H "Content-Type: application/json" -d '{"title":"改过的标题"}'
curl -X DELETE "$BASE/api/v1/apps/QUTSchedule/notices/3" -H "Authorization: Bearer $K"
```

客户端拿的是 `published_at` **最晚**的一条，时间相同时取 id 更大的。

---

## 7. 分享链接

```bash
curl -X POST "$BASE/api/v1/apps/QUTSchedule/share-links" -H "Authorization: Bearer $K" \
     -H "Content-Type: application/json" -d '{"note":"B 站动态","target":"intro"}'
# → 201 {"id":1,"code":"k7m3pq2r","note":"B 站动态","target":"intro","url":"https://.../QUTSchedule/s/k7m3pq2r"}

curl "$BASE/api/v1/apps/QUTSchedule/share-links?days=30" -H "Authorization: Bearer $K"
curl -X DELETE "$BASE/api/v1/apps/QUTSchedule/share-links/1" -H "Authorization: Bearer $K"
```

`target` 取 `intro`（介绍页）或 `download`（直接下载当前最新版）。
返回的 `share_links` 里带 `clicks`（链接被打开次数）、`views`、`downloads` 等归因数据。

---

## 8. 统计

```bash
curl "$BASE/api/v1/apps/QUTSchedule/stats?days=30" -H "Authorization: Bearer $K"
```

返回 `summary`（PV/UV/下载/更新检查，按 `kind` 分组）、`daily`（按天序列）、
`referrers`（**只统计介绍页**，因为客户端 API 调用不带 Referer）、`clients`、`recent_downloads`。

`include_bots=true` 可把机器人流量算进来；默认只算真实访客。

---

## 9. 完整示例：发布一个新版本

```bash
set -euo pipefail
: "${APPPUBLISHER_URL:?}"; : "${APPPUBLISHER_KEY:?}"

SLUG="QUTSchedule"
VERSION_NAME="v1.2.0"
VERSION_CODE="26092711"

curl --fail-with-body -sS -X POST \
  "$APPPUBLISHER_URL/api/v1/apps/$SLUG/releases" \
  -H "Authorization: Bearer $APPPUBLISHER_KEY" \
  -F "file=@./app-release.apk" \
  -F "version_name=$VERSION_NAME" \
  -F "version_code=$VERSION_CODE" \
  -F "description=本次更新内容" \
  -F "make_latest=true"

# 确认客户端拿得到
curl --fail-with-body -sS "$APPPUBLISHER_URL/$SLUG/releases/latest"
```

---

## 10. AI 最容易写错的点

1. **用 `version_name` 定位版本** —— 所有版本相关操作都用 `version_code`。
2. **忘记 `make_latest`** —— 传了 `false` 客户端就不会收到这个版本。
3. **把 401 当成「服务端坏了」** —— 先检查密钥是否被撤销、是否加了 `Bearer ` 前缀。
4. **把 404 当「接口写错了」** —— 很可能是**无权访问**该应用（不是自己的）。
5. **PATCH 时传了不想改的字段** —— PATCH 覆盖传入的字段，没传的才保持原值。
6. **改 `slug`** —— 会让已发布客户端里写死的地址失效，除非用户明确要求。
7. **把密钥写进代码** —— 从环境变量读。
8. **上传时字段名写错** —— 文件字段统一叫 `file`（不是 `upload` / `apk` / `banner`）。
9. **忘了 `-F` 用 multipart** —— 带文件的接口不能用 `-d` JSON。
10. **`Content-Type` 冲突** —— `curl -F` 会自动设 multipart，不要再手动设 `application/json`。
