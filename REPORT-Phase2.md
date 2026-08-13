# 阶段二报告：安卓端完善 + 公网联调 + APK 发布

**日期**：2026-08-13
**阶段目标**：在阶段一可运行构建的基础上，完成安卓 App 前端界面优化、修复手机上 Markdown 文本不渲染的问题，重新执行公网（cloudflared）联调测试与模拟器公网测试，并将项目封装为可安装的 release APK。
**状态**：✅ 本阶段完成

---

## 1. 阶段成果总览

| 交付物 | 位置 | 状态 |
|--------|------|------|
| 安卓 App 前端优化（Material3 配色、状态色点、会话选择器、消息卡片、智能滚动） | [android/](android/) | ✅ 完成 |
| Markdown 富文本渲染（Markwon） | [android/app/src/main/java/com/example/claudeviewer/Markdown.kt](android/app/src/main/java/com/example/claudeviewer/Markdown.kt) | ✅ 完成 |
| 公网联调测试（cloudflared 隧道） | 本报告 §4 | ✅ 通过 |
| 模拟器公网测试（`wss://` + Markdown 渲染） | 本报告 §5 | ✅ 通过 |
| release APK 打包 + 签名 | `Claude-Control-v0.1.0.apk` | ✅ 完成 |

## 2. 安卓 App 前端界面优化

在阶段一「可运行骨架」基础上，对 [MainActivity.kt](android/app/src/main/java/com/example/claudeviewer/MainActivity.kt) 与 [MessageAdapter.kt](android/app/src/main/java/com/example/claudeviewer/MessageAdapter.kt) 做了整体视觉与交互优化：

- **Material3 配色体系**：自定义明暗主题色板（`R.color.*`），角色用色条区分（user / assistant / system）。
- **会话状态色点**：下拉列表每条会话带状态点（`active` / `attention` / `idle` / `ended` 四色），标题 + cwd/模型副标题。
- **消息卡片**：每条记录一张 `MaterialCardView`，头部显示角色 · 模型 · 时间。
- **思考 / 工具折叠**：`thinking`、`tool_use` 折叠为可点击展开的摘要行，折叠态跨重绘保留（`expandedKeys` 记忆）。
- **智能自动滚动**：仅当用户接近底部时才自动滚到底，避免上翻阅读时被新消息打断（`maybeScrollToBottom`）。
- **超长文本截断**：单块上限 `MAX_EXPANDED_CHARS = 4000`，防 ANR。

## 3. Markdown 文本渲染修复

### 问题

assistant 转录文本本身是 Markdown（含 `##` 标题、`**加粗**`、`- 列表`、表格、`` ``` `` 代码块），但阶段一安卓端把 `text` 块当作普通等宽明文渲染，手机上看到的是原始 `##`/`**` 符号。

### 方案

引入 [Markwon 4.6.2](https://github.com/noties/Markwon)（commonmark-java 包装），对 `text` 块做富文本渲染；`thinking`/`tool_use`/`tool_result` 仍保持等宽折叠。

- 依赖（[build.gradle](android/app/build.gradle)）：`io.noties.markwon:core` / `:ext-tables` / `:ext-strikethrough`（4.6.2）。
- 新增 [Markdown.kt](android/app/src/main/java/com/example/claudeviewer/Markdown.kt)：全应用共享单例，深色主题配色（链接色 / 行内代码 / 代码块 / 引用块）。
- [MessageAdapter.kt](android/app/src/main/java/com/example/claudeviewer/MessageAdapter.kt) 的 `text` 分支改用 `makeMarkdownText()`，调用 `Markdown.get(ctx).setMarkdown(tv, text)`。

### 过程中踩坑与修复

- **依赖坐标错误**：误写成 `io.noties:markwon:core`（4 段）导致 `Could not find`；确认正确坐标为 `io.noties.markwon:core` 后修复。
- **Markwon API 签名不符**（编译错误）：`MarkwonTheme` 实际位于 `io.noties.markwon.core` 包；删除线插件是 `StrikethroughPlugin`（非 `StrikeThroughPlugin`）；`TablePlugin.create(context)` 需传 Context；主题通过 `MarkwonPlugin.configureTheme(MarkwonTheme.Builder)` 配置。均通过 `javap` 反编译 AAR 的 `classes.jar` 核实签名后修复。

## 4. 公网联调测试（cloudflared）

重启中继进入 `CC_TUNNEL=cloudflared` 模式，取得全新临时隧道 URL。

| 测试项 | 结果 |
|--------|------|
| 公网 `/healthz` | ✅ `{"ok":true,"sessions":19}` |
| 公网 `/qrcode` 无 token | ✅ HTTP 401（防内嵌 token 泄露） |
| 公网 `/qrcode?token=…` | ✅ HTTP 200 |
| 公网 `wss://…/ws?token=…` | ✅ 收到 `hello` + 19 个会话（首个状态 `idle`） |

**发现**：隧道 URL 被日志捕获后需约 10–20 秒才可被 Cloudflare 边缘访问，此前返回 530/1033（`Cloudflare is currently unable to resolve it`）——这是 trycloudflare 免费隧道的正常传播延迟，非中继故障。早期把「URL 已打印」误判为「隧道已就绪」，导致误诊为隧道失效并反复重启。

## 5. 模拟器公网测试

- 启动 AVD `claude_avd`（`emulator-5554`），安装最新 APK。
- 通过 `adb push` + `run-as` 直接写入 `SharedPreferences`（避免 `adb input text` 追加导致 token 翻倍的历史问题），预填公网 `wss://` URL + token。
- 点击「连接」后状态转为「已连接」，会话下拉加载成功，选中会话消息卡片正常渲染。
- 抽样一条含 `## 📝 操作详情`、`**Trae IDE（Trae CN）**`、`- **IDE 路径**`、`1. **查看代码**` 的 assistant 消息，截图确认已渲染为富文本（标题/加粗/列表/代码块），不再是原始 `##`/`**` 明文。
- `logcat` 无 `FATAL`、无 Markwon 异常。

## 6. APK 发布打包

- 新增 release 签名配置（[build.gradle](android/app/build.gradle) 读取 `keystore.properties`）。
- 生成 release 密钥库 `android/keystore/release.jks`（别名 `claudecontrol`，CN=Claude Control，有效期 10000 天）。
- `./gradlew assembleRelease` 产出 `app-release.apk`（约 5.15 MB），`apksigner verify` 校验签名通过。
- 产物重命名为 `Claude-Control-v0.1.0.apk`（versionName 0.1.0 / versionCode 1）。

**签名信息**：

| 项 | 值 |
|----|----|
| SHA-256 | `24335cb279ae9f98b04430301fd3b62939b249efb82b5bb418fab527a0eed1d6` |
| SHA-1 | `b5acc2b8904c7d6499f74e8a155063c58e8993d9` |

> ⚠️ 后续升级必须沿用同一 keystore；`release.jks` 已 `.gitignore`，请自行备份。

## 7. 发布前安全检查

| 项 | 结果 |
|----|------|
| `computer/.env`（含 token） | ✅ `.gitignore` 忽略，不提交 |
| `android/keystore/`、`keystore.properties` | ✅ `.gitignore` 忽略 |
| `computer/bin/cloudflared.exe`（54 MB） | ✅ `.gitignore` 忽略 |
| 源码硬编码 token 扫描 | ✅ 无（`.env.example` 为 `CC_TOKEN=` 空值） |
| `.venv` / `__pycache__` / `build/` | ✅ `.gitignore` 忽略 |

## 8. 已知限制 / 下一阶段建议

1. **trycloudflare 免费隧道 URL 每次重启都会变**，且无 SLA——仅适合联调/演示；生产/长期使用建议换 ngrok 的永久免费 dev 域名（见 [ACCOUNTS.md](ACCOUNTS.md)），或自备服务器 + 反向代理。
2. **安卓真机实装验证待做**：本阶段在模拟器上验证通过，真机安装（含不同网络环境、后台保活、系统字体/深色模式差异）仍需实测。
3. **`Tunnels.primary_url()` 返回 `public_urls[0]`**：若 cloudflared 断线重连换了新 URL，旧地址已死但打印/二维码仍引用首个地址；建议改为返回最新 URL（`public_urls[-1]`）并补充重连日志。
4. **token 经 URL 传输**：公网暴露时建议后续加 Basic Auth 或一次性扫码登录（见阶段一报告 §6）。
5. **超大会话一次性读入**：后续可按需分页读取（见阶段一报告 §6）。
