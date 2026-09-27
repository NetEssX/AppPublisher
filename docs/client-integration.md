# AppPublisher 客户端接入规范

> **本文档的读者是 AI 编码助手。**
>
> 你正在帮用户把一个客户端应用接入 AppPublisher 的更新检查与公告接口。
> 请严格按本文的字段名、类型、错误语义实现，**不要凭直觉补字段、改字段名、
> 或把增量字段当成一定存在**。本文没有描述的内容，先问用户，不要猜。

---

## 0. 开工前必须先向用户确认这三个值

| 变量 | 含义 | 示例 |
| --- | --- | --- |
| `BASE_URL` | 站点根地址，**结尾不带 `/`** | `https://example.com` |
| `SLUG` | 该应用在后台登记的短链 | `QUTSchedule` |
| 本应用当前 `versionCode` | 整数，必须与上传到后台的版本号同一套编号体系 | `26092710` |

**这三个值一个都不能猜。** 如果用户没给：

- 不要编造域名，不要用 `example.com` 占位然后忘记替换；
- 不要把 `SLUG` 从应用名或包名推断出来（`com.foo.BarBaz` → `BarBaz` 是错的，
  短链是后台手填的）；
- 直接向用户提问。

最终所有接口的基地址是 `{BASE_URL}/{SLUG}`，例如
`https://example.com/QUTSchedule`。

---

## 1. 接口总览

| 方法 | 路径 | 用途 | 成功 | 常见失败 |
| --- | --- | --- | --- | --- |
| GET | `/{slug}/releases/latest` | 检查更新 | 200 JSON | 404 |
| GET | `/{slug}/releases` | 版本列表 | 200 JSON | 404 |
| GET | `/{slug}/build/{versionCode}` | 下载产物 | 200 文件流 | 404 / 410 |
| GET | `/{slug}/notices/latest` | 取最新公告 | 200 JSON | 404 |
| GET | `/{slug}/notices` | 公告列表 | 200 JSON | 404 |
| GET | `/{slug}/notices/{id}` | 单条公告 | 200 JSON | 404 |

所有 JSON 接口都返回 `Cache-Control: no-cache, no-store, must-revalidate`，
**客户端不需要也不应该再叠一层缓存**。

---

## 2. `GET /{slug}/releases/latest` — 检查更新

### 2.1 响应字段

| 字段 | 类型 | 可空 | 稳定性 | 说明 |
| --- | --- | --- | --- | --- |
| `versionName` | string | 否 | **稳定** | 给人看的版本名，如 `"v1.2.0"`。**禁止用于比较大小** |
| `versionCode` | integer | 否 | **稳定** | 用于比较的版本号，单调递增 |
| `desc` | string | 否（可为空串） | **稳定** | 更新说明。注意字段名是 `desc`，不是 `description` |
| `timestamp` | integer | 否 | **稳定** | 发布时间，**epoch 毫秒**（13 位） |
| `download` | string | 否 | **稳定** | **绝对 URL**，直接使用 |
| `appName` | string \| null | **是** | 增量 | 应用名，仅用于展示 |
| `size` | integer | 否 | 增量 | 产物体积（字节）。早期数据可能为 `0` |
| `sha256` | string | 否（可为空串） | 增量 | 产物 SHA-256 十六进制小写。**早期数据可能为空串** |
| `forceUpdate` | boolean | 否 | 增量 | 是否强制更新。早期数据可能缺失 |

> **「增量」的含义**：这些字段是后加的。反序列化时必须有默认值
> （`null` / `0` / `""` / `false`），**不能声明为不可空且无默认值**，
> 否则遇到早期发布的数据会解析崩溃。

### 2.2 真实响应示例

```json
{
  "versionName": "v1.2.0",
  "versionCode": 26092711,
  "desc": "1. 修复课表导入偶发失败\n2. 支持深色模式",
  "timestamp": 1758931200000,
  "download": "https://example.com/QUTSchedule/build/26092711",
  "appName": "QUT课表",
  "size": 8388608,
  "sha256": "9f2c1a0b3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcd",
  "forceUpdate": false
}
```

### 2.3 判定的唯一正确写法

```
有新版本  ⟺  response.versionCode > 本应用当前 versionCode
```

**不要**比较 `versionName` 字符串，**不要**比较 `timestamp`，
**不要**用 `!=` 判断（那会把已发布的旧版本误判成更新）。

`forceUpdate` 的语义是「即使用户点过忽略也要提示」：

```
应弹强制更新  ⟺  有新版本 && response.forceUpdate
```

### 2.4 错误语义

| 状态码 | 含义 | 客户端应如何表现 |
| --- | --- | --- |
| 200 | 正常 | 比较 `versionCode` |
| 404 | 该应用没有发布过任何版本，**或**应用不存在/已下线 | **两者无法区分**。一律当作「没有更新」，静默忽略，不要弹错误 |
| 5xx / 超时 / 网络异常 | 服务端或链路问题 | 静默失败。**绝不能阻塞启动或弹窗报错** |

> 404 是刻意设计成「宁可模糊也不泄露内部状态」的。**不要**把 404 当异常上报，
> 也**不要**试图从响应体的 `detail` 文案里区分这两种情况——文案不是契约。

---

## 3. `GET /{slug}/build/{versionCode}` — 下载产物

用 `releases/latest` 返回的 `download` 字段直接请求即可，
**不要自己拼接这个 URL**（服务端可能配置了与请求域名不同的 `PUBLIC_BASE_URL`）。

| 响应头 | 值 | 说明 |
| --- | --- | --- |
| `Content-Type` | 按上传时的扩展名判定 | `.apk` → `application/vnd.android.package-archive`，`.zip` → `application/zip`，未知类型 → `application/octet-stream` |
| `Content-Disposition` | `attachment; filename="<slug>-<versionName><ext>"` | 服务端生成的文件名 |

**关键约束：产物不一定是 APK。** 这个平台也分发 `.ipa` / `.exe` / `.dmg` /
`.zip` 等。客户端应当：

- 从 `Content-Type` 或 `Content-Disposition` 的扩展名判断文件类型；
- 保存文件时使用服务端给的文件名，或自行按扩展名命名；
- **不要**假设后缀一定是 `.apk`，**不要**无条件走 Android 的安装流程。

| 状态码 | 含义 | 处理 |
| --- | --- | --- |
| 200 | 正常 | 落盘 |
| 404 | `versionCode` 不存在 | 提示「该版本已下架」，并重新拉一次 `releases/latest` |
| **410** | 数据库有记录但**服务端文件已丢失** | 这是**运维问题**，不是客户端的错。提示「安装包暂时不可用，请联系开发者」，**不要**反复重试 |

### 3.1 SHA-256 校验（推荐）

`sha256` 非空时应当校验：

```
下载完成后计算文件的 SHA-256 十六进制小写字符串
  → 与 response.sha256 相等：继续
  → 不等：删除文件，提示「安装包校验失败，已阻止安装」
```

`sha256` 为空串说明是早期数据，跳过校验即可。

---

## 4. `GET /{slug}/notices/latest` — 最新公告

### 4.1 响应字段

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `title` | string | 否（可为空串） | 公告标题 |
| `content` | string | 否（可为空串） | 公告正文。**纯文本**，可能含换行，不要当 HTML 渲染 |
| `timestamp` | integer | 否 | 发布时间，epoch 毫秒 |
| `id` | integer | 否 | 公告 ID，随发布**单调递增** |

```json
{
  "title": "服务器维护通知",
  "content": "今晚 23:00 - 24:00 进行维护，期间更新检查不可用。",
  "timestamp": 1758931200000,
  "id": 2
}
```

### 4.2 语义与错误

- **「最新」由服务端定义**（发布时间最晚，时间相同取 id 更大者）。
  客户端**不要**自己再排序或过滤。
- `id` 单调递增，是判断「这条我是否已经给用户看过」的正确依据。
  **不要**用 `timestamp` 做去重（同一秒内可能发布多条）。
- 状态码 404 = **该应用还没有发布过任何公告**。这是正常状态，
  静默忽略，不要弹空公告框。

### 4.3 「已读」的正确实现

```
本地存 lastSeenNoticeId（初始 0）
有新公告  ⟺  response.id > lastSeenNoticeId
用户看完后：lastSeenNoticeId = response.id
```

---

## 5. 列表接口（一般用不到）

`GET /{slug}/releases` 与 `GET /{slug}/notices` 返回最近 100 条：

```json
{
  "slug": "QUTSchedule",
  "count": 3,
  "limit": 100,
  "generatedAt": 1758931200000,
  "releases": [ /* 元素结构与 releases/latest 完全一致 */ ]
}
```

- 列表**按时间从新到旧**排列。
- `count == limit` 说明可能还有更早的记录被截断了。
- 「最新一条」= `releases[0]`。但**优先用 `releases/latest`**，
  它的语义由服务端保证，不会因为排序实现变化而失效。

---

## 6. 实现要求清单

AI 生成的代码必须满足：

- [ ] 网络请求**不在主线程**（Android 上用协程 + `Dispatchers.IO`）。
- [ ] 设置**连接超时与读取超时**（建议均 5 秒）。不设超时会让弱网下的
      启动页卡死。
- [ ] 所有检查更新的调用**整体包在 `runCatching` / `try-catch` 里**，
      任何异常都不向外抛。
- [ ] **不在 `Application.onCreate` 里同步请求**。要么异步，要么延迟到
      首屏渲染之后。
- [ ] 触发时机克制：**每次冷启动最多一次**，不要放在 `onResume`
      （那会变成每次切前台都请求）。如需更克制，本地记录日期，每天一次。
- [ ] `BASE_URL` / `SLUG` 不硬编码散落在各处，集中到一个常量对象里。
- [ ] 反序列化严格对应字段名，注意拼写：`versionName`、`versionCode`、
      `desc`、`timestamp`、`download`、`forceUpdate`。
- [ ] 「忽略此版本」按 **`versionCode`** 记录，不要按 `versionName`。
- [ ] 服务端已返回 `no-cache`，客户端**不要**对 `releases/latest`
      做本地 HTTP 缓存，否则会一直拿不到新版本。

---

## 7. Kotlin 参考实现（Android）

### 7.1 数据模型

```kotlin
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class ReleaseInfo(
    @SerialName("versionName") val versionName: String,
    @SerialName("versionCode") val versionCode: Int,
    @SerialName("desc") val desc: String = "",
    @SerialName("timestamp") val timestamp: Long = 0L,
    @SerialName("download") val download: String,
    // ↓ 增量字段：必须给默认值，早期数据里可能不存在
    @SerialName("appName") val appName: String? = null,
    @SerialName("size") val size: Long = 0L,
    @SerialName("sha256") val sha256: String = "",
    @SerialName("forceUpdate") val forceUpdate: Boolean = false,
)

@Serializable
data class NoticeInfo(
    @SerialName("title") val title: String = "",
    @SerialName("content") val content: String = "",
    @SerialName("timestamp") val timestamp: Long = 0L,
    @SerialName("id") val id: Long = 0L,
)
```

> 用 Gson 的话把 `@SerialName("x")` 换成 `@SerializedName("x")`。
> 但注意：**Gson 反射构造对象时不会调用 Kotlin 的默认值**，字段缺失会得到
> `null` 而不是默认值，随后在不可空类型上崩溃。所以用 Gson 时请把这些字段
> 声明为可空并在使用处兜底，或直接改用 kotlinx.serialization。

### 7.2 检查更新

```kotlin
object AppPublisher {
    // 集中配置，不要散落
    private const val BASE_URL = "https://example.com"   // ← 让用户确认
    private const val SLUG = "QUTSchedule"               // ← 让用户确认

    private val client = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.SECONDS)
        .build()

    private val json = Json { ignoreUnknownKeys = true }  // 服务端加字段不能崩

    /** 返回 null 表示「无更新 / 服务端不可用 / 应用未上线」——三者都静默。 */
    suspend fun fetchLatestRelease(): ReleaseInfo? = withContext(Dispatchers.IO) {
        runCatching {
            val request = Request.Builder()
                .url("$BASE_URL/$SLUG/releases/latest")
                .header("Accept", "application/json")
                .build()
            client.newCall(request).execute().use { response ->
                if (response.code != 200) return@use null   // 404 也走这里
                val body = response.body?.string() ?: return@use null
                json.decodeFromString<ReleaseInfo>(body)
            }
        }.getOrNull()
    }

    /**
     * @param currentVersionCode 本应用当前构建的 versionCode
     */
    suspend fun checkUpdate(currentVersionCode: Int): ReleaseInfo? =
        fetchLatestRelease()?.takeIf { it.versionCode > currentVersionCode }

    suspend fun fetchLatestNotice(): NoticeInfo? = withContext(Dispatchers.IO) {
        runCatching {
            client.newCall(
                Request.Builder().url("$BASE_URL/$SLUG/notices/latest").build()
            ).execute().use { response ->
                if (response.code != 200) return@use null
                val body = response.body?.string() ?: return@use null
                json.decodeFromString<NoticeInfo>(body)
            }
        }.getOrNull()
    }
}
```

`currentVersionCode` 不要手写，从构建信息取：

```kotlin
val currentVersionCode = BuildConfig.VERSION_CODE
// 需在 app/build.gradle.kts 里开启：
// android { buildFeatures { buildConfig = true } }
```

### 7.3 下载并校验

```kotlin
suspend fun downloadRelease(context: Context, info: ReleaseInfo): File? =
    withContext(Dispatchers.IO) {
        runCatching {
            val target = File(context.cacheDir, "update-${info.versionCode}.bin")

            client.newCall(Request.Builder().url(info.download).build())
                .execute().use { response ->
                    if (response.code != 200) return@use null   // 404 / 410 都在这里
                    response.body?.byteStream()?.use { input ->
                        target.outputStream().use { output -> input.copyTo(output) }
                    }
                }

            // sha256 为空串表示是早期数据，跳过校验
            if (info.sha256.isNotBlank() && sha256Of(target) != info.sha256.lowercase()) {
                target.delete()
                return@use null
            }
            target
        }.getOrNull()
    }

private fun sha256Of(file: File): String {
    val digest = MessageDigest.getInstance("SHA-256")
    file.inputStream().use { stream ->
        val buffer = ByteArray(8192)
        while (true) {
            val read = stream.read(buffer)
            if (read <= 0) break
            digest.update(buffer, 0, read)
        }
    }
    return digest.digest().joinToString("") { "%02x".format(it) }
}
```

### 7.4 安装（Android 专用，且产物必须是 APK）

只有产物确实是 `.apk` 时才走这条路径。需要 `FileProvider`，
否则 Android 7.0+ 会抛 `FileUriExposedException`：

```kotlin
fun installApk(context: Context, apk: File) {
    val uri = FileProvider.getUriForFile(
        context, "${context.packageName}.fileprovider", apk
    )
    val intent = Intent(Intent.ACTION_VIEW).apply {
        setDataAndType(uri, "application/vnd.android.package-archive")
        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
    }
    context.startActivity(intent)
}
```

`AndroidManifest.xml` 里配套声明 `<provider>`，在 `res/xml/file_paths.xml`
中列出 `cache-path`，并申请 `REQUEST_INSTALL_PACKAGES` 权限。

---

## 8. AI 最容易写错的 8 个点

1. **用 `versionName` 比大小**：`"v1.10.0" < "v1.9.0"` 字符串比较是错的。
   只比 `versionCode`。
2. **把 `timestamp` 当成秒**：它是**毫秒**。直接 `Date(timestamp)` 会得到
   公元 5 万年，必须先 `/1000`。
3. **自己拼下载地址**：应直接用响应里的 `download`。服务端可能配了
   `PUBLIC_BASE_URL`，与请求域名不一致。
4. **假设产物是 `.apk`**：这个平台也分发 zip / exe / dmg / ipa。
5. **404 当成错误上报**：404 是正常业务状态（没发过版本 / 应用已下线），
   必须静默。
6. **忘记给增量字段默认值**：`appName` / `sha256` / `forceUpdate` / `size`
   在早期数据里可能缺失或为空，声明成不可空无默认值会解析崩溃。
7. **没有超时**：不设 `connectTimeout` / `readTimeout`，服务端不可达时
   会长时间挂起。
8. **在主线程发请求 / 在 `onResume` 里检查**：前者触发
   `NetworkOnMainThreadException`，后者变成每次切前台都请求一次。

---

## 9. 交付前自检

- [ ] 代码里没有残留 `example.com` 之类的占位域名
- [ ] `BASE_URL` 与 `SLUG` 是用户确认过的真实值
- [ ] 只有 `versionCode` 参与版本比较
- [ ] `timestamp` 使用处都做了毫秒 → 秒的换算（或按毫秒格式化）
- [ ] 下载用的是 `download` 字段，没有手工拼接
- [ ] 处理了 404 与 410，且都不弹错误弹窗
- [ ] 所有网络调用都有超时，且整体包在异常捕获里
- [ ] 增量字段都有默认值
- [ ] `ignoreUnknownKeys` 已开启（服务端将来加字段不会让老客户端崩溃）
- [ ] 检查更新的触发时机不超过「每次冷启动一次」

---

## 10. 非 Android 客户端

同一套接口对任何平台都成立，只有两点不同：

- **产物格式**：按 `Content-Type` / `Content-Disposition` 决定后续动作，
  不要套用 Android 的安装流程。
- **版本号**：`versionCode` 必须是**整数且单调递增**。如果目标平台只有
  语义化版本字符串，请在打包时另算一个整数版本号一并上传到后台，
  客户端比较这个整数。
