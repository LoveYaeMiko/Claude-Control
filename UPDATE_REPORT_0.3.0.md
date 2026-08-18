# Claude-Control 更新报告 · v0.3.0

> 版本：**0.3.0**（versionCode **5**）　更新日期：2026-08-18
> 本次更新覆盖电脑端中继（`computer/relay_server.py`）与安卓 App（`android/`）两端的四项能力，全部通过自动化测试与 Release 打包验证。

---

## 一、本次更新做了什么

手机端从「只能看」升级为「远程驾驶台」，新增四项能力：

| # | 能力 | 一句话说明 |
|---|------|-----------|
| 1 | **新建会话** | 工具栏一键清空当前会话，下一条 prompt 自动开新 `claude -p` 会话 |
| 2 | **导入文件（多模态）** | 手机从相册/文件管理器选图片（jpeg/png/gif/webp）+ PDF，base64 随 prompt 发给 Claude |
| 3 | **前端审批权限** | Claude 弹出的工具权限请求实时推到手机，点「允许/拒绝」回写进 `claude` 进程 |
| 4 | **界面美化** | 精修深色主题 + 深浅色切换（跟随系统/浅色/深色）+ 消息列表气泡化重排版 |

关键安全边界不变：远程交互仍由 `CC_ALLOW_INTERACT=1` 显式开启；WS 层 token 鉴权沿用；权限审批为**配置驱动**——默认仍是 `bypassPermissions`（自动放行，行为与之前完全一致），只有显式改成 `default` 才启用审批链路。

---

## 二、功能详解

### 1. 新建会话

- 工具栏右上角新增「新建会话」菜单（`+` 图标）。
- 点击后清空当前选中会话、分页游标与消息列表，会话下拉回占位。
- 下一条 `send-prompt` 不带 `sessionId` → 中继不再拼 `--resume`，Claude 自动开新会话。
- **中继侧无需改动**：该能力完全落在安卓端状态管理。

### 2. 导入文件（多模态）

- 输入栏新增「回形针」按钮，点击调起系统文件选择器（`OpenMultipleDocuments`，SAF，无需运行时权限）。
- 支持 **图片**：jpeg / png / gif / webp；**PDF**：application/pdf。
- 选中后 base64（`NO_WRAP`，无换行）进入待发送预览条，可逐个移除；上限 **5 个附件、单件 20 MiB**。
- 发送时经 `send-prompt` 的 `attachments` 字段下发，中继校验后组装成 stream-json 的 `image`/`document` 内容块。
- 历史回放闭环：`_content_to_blocks` 新增 `image`/`document` 分支，重新打开会话时能回放同一批块（图片超 1 MiB 降级为文本提示）。
- 消息列表渲染：图片经 **Coil** 解码显示（限宽 240dp、自适应比例），PDF 显示「📄 文档」chip（不解码）。

### 3. 前端审批权限

- 中继 `InteractRunner` 从「写一行 prompt → 关 stdin → 读到 session_id 即停」改写为 **双向 stream-json 循环**：
  - `claude -p --input-format stream-json --output-format stream-json`。
  - 单写者 stdin 队列（初始消息 / `control_response` 串行写入，避免竞态）+ stderr 独立 drain（防死锁）。
  - stdout 逐行解析：`system/init`（抓 `session_id`）→ `control_request/can_use_tool`（登记到 `PermissionBroker` 并发给手机）→ `result`（关 stdin 结束进程）。
- 新增 `PermissionBroker`：`request_id → {ws, write_q, session_id, timeout_task}` 注册表，支持 `register`/`respond`/`cancel_ws`/超时自动 deny。
- 协议：
  - 服务端 → 手机 `permission-request`：`{requestId, sessionId, toolName, displayName, description, input}`（只发给发起方，不广播）。
  - 手机 → 服务端 `permission-response`：`{requestId, behavior:"allow"|"deny", message}`；未知名回 `permission-response-error`。
- 手机端：`ArrayDeque` 排队串行展示 `MaterialAlertDialog`（标题「Claude 请求执行 {toolName}」，正文 description + input），允许/拒绝后回写；超时/断线自动拒绝。
- 权限模式名映射：内部 `default` → CLI `manual`（二者等价）；bypass 模式不吐请求、也不拼 `--dangerously-skip-permissions` 到 stream-json 路径之外。

### 4. 界面美化（三个方向全做）

- **深色精修 + 深浅色切换**：
  - 主题父类 `Theme.Material3.Dark.NoActionBar` → `Theme.Material3.DayNight.NoActionBar`。
  - 深色 `colors.xml` 挪到 `values-night/`，新增浅色 `values/colors.xml`（背景 `#F7F8FA`、surface 白、primary 深蓝 `#2F6FDB`）。
  - 补齐 Material3 tonal 角色（outline/surfaceVariant/onSurfaceVariant 等）+ `windowLightStatusBar`（深浅色各一套 `bools.xml`）。
  - 工具栏菜单「主题」循环切换 跟随系统 → 浅色 → 深色，`SharedPreferences` 持久化 + `recreate()`；切换时 `Markdown.reset()` 清掉 Markwon 单例缓存的颜色。
- **气泡式重排版**：
  - `item_message.xml` 重做：左/右头像（👤/🤖/⚙️）+ 居中气泡 + 时间分隔条；user 靠右、assistant/system 靠左。
  - `MessageAdapter` 按 role 设 gravity、切头像显隐，user 用专属底色；去掉左缘 `msg_accent` 色条。
  - **时间分组**：相邻消息间隔 >5 分钟或首条时插入 `HH:mm` / 跨天 `MM-dd HH:mm` 分隔条。

---

## 三、改动文件清单

### 电脑端（Python）

| 文件 | 改动 |
|------|------|
| `computer/relay_server.py` | `InteractRunner` 双向 stream-json 改写、`PermissionBroker`、`Config` 4 新字段、`_cli_permission_mode` 映射、`_content_to_blocks` image/document、WS `permission-request`/`permission-response`/`permission-response-error` 分支、`send-prompt` attachments 校验、`ws_serve` max_size 上调至 64 MiB、常量 |
| `computer/.env.example` | 新增 `CC_INTERACT_PERMISSION_MODE` / `CC_INTERACT_MAX_UPLOAD_MB` / `CC_INTERACT_PERMISSION_TIMEOUT` / `CC_INTERACT_TURN_TIMEOUT` / `CC_CLAUDE_BIN` 说明 |
| `computer/tests/fake_claude.py` | 新增：claude CLI 的 stream-json 替身（init → control_request → 读 control_response → result） |
| `computer/tests/test_e2e.py` | 新增 27 项断言（单元 + 交互链路端到端） |

### 安卓端（Kotlin + 资源）

| 文件 | 改动 |
|------|------|
| `app/build.gradle` | 加 Coil 2.7.0；versionCode 5 / versionName 0.3.0 |
| `Message.kt` | `ContentBlock` 加 `mediaType`/`data`、新增 `Attachment`、`parseMessages` 认 image/document |
| `WebSocketClient.kt` | `sendPrompt(..., attachments)`、`sendPermissionResponse`、`onPermissionRequest`/`onPermissionResponseError` 回调与分发 |
| `MainActivity.kt` | 绑定 toolbar、新建会话、附件选择、审批对话框队列、主题切换 |
| `MessageAdapter.kt` | 气泡布局 + 图片/PDF 渲染 + 时间分隔 |
| `Markdown.kt` | `reset()` 清 Markwon 单例缓存 |
| `res/layout/item_message.xml` | 气泡重排版 |
| `res/layout/activity_main.xml` | 回形针按钮 + 附件预览条 |
| `res/menu/main_menu.xml` | 新建（新建会话 + 主题切换） |
| `res/drawable/{avatar_bg,divider_pill,document_chip_bg,ic_attach}.xml` | 新增（头像底、分隔条、文档 chip、回形针图标） |
| `res/values/themes.xml` + `colors.xml` + `values-night/colors.xml` + `values{,-night}/bools.xml` | 深浅色主题拆分与精修 |
| `res/values/strings.xml` | 新增文案 |

---

## 四、新增配置项

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|----------|--------|------|
| 权限模式 | `CC_INTERACT_PERMISSION_MODE` | `bypassPermissions` | 改 `default` 启用前端审批 |
| 上传上限 | `CC_INTERACT_MAX_UPLOAD_MB` | `20` | 单件附件解码后最大兆数 |
| 审批超时 | `CC_INTERACT_PERMISSION_TIMEOUT` | `300` | 单次审批超时秒数（自动拒绝） |
| 整轮超时 | `CC_INTERACT_TURN_TIMEOUT` | `600` | 单轮交互整体超时秒数 |
| claude 路径 | `CC_CLAUDE_BIN` | `claude` | CLI 路径（测试注入替身） |

---

## 五、验证结果

1. **中继编译**：`python -m py_compile relay_server.py` ✅
2. **端到端测试**：`PYTHONUTF8=1 python tests/test_e2e.py` → **63 项断言全部通过** ✅
   - 新增覆盖：权限模式映射、用户消息行/内容块形状、附件校验（合法/非法类型/非法 base64/超大/超数）、`PermissionBroker` 超时与断线 deny、`send-prompt → started → session → permission-request`、allow/deny 回写标记、未知 requestId、非法/超大附件拒绝。
3. **安卓打包**：`./gradlew assembleRelease` → `BUILD SUCCESSFUL` ✅
   - 产出 `app/build/outputs/apk/release/app-release.apk`（约 5.8 MB）。
   - `apksigner verify`：v2 签名有效，1 个签名者 ✅

> 备注：`gradlew assembleRelease` 有两条 `Message.kt` 编译告警（嵌套 `mapNotNull` 标签重名、`Any?.toString()` 可空接收者），均为历史既有、不影响行为的告警，本次未改动。

---

## 六、使用方式（手动联调）

1. 电脑端 `.env` 设置：
   ```dotenv
   CC_ALLOW_INTERACT=1
   CC_INTERACT_PERMISSION_MODE=default   # 启用前端审批；默认 bypassPermissions 则自动放行
   ```
2. `start.bat` 启动中继，手机扫码连接。
3. 手机端：
   - 发「帮我 ls 当前目录」→ 触发 Bash → 弹审批框 → 允许后命令执行、结果回流；点拒绝则优雅拒绝。
   - 选一张 PNG + 一个 PDF 随文本发送 → 图片缩略图渲染、PDF 显示文档 chip；重新打开会话可回放同一批块。
   - 工具栏「新建会话」清空上下文，下条 prompt 开新会话。
   - 工具栏「主题」在 跟随系统 / 浅色 / 深色 间切换。

---

## 七、风险与边界

- **stdin/stdout 死锁**：stderr 独立 drain、stdin 单写者队列、stdout 读到 `result`/超时/EOF 即关 stdin，保证 print 模式进程退出。
- **并发审批**：并行工具调用产生多个 `control_request`，broker 按 `request_id` 键控 + 手机对话框队列，审批超时兜底。
- **大小上限**：base64 膨胀约 4/3，`ws_serve` max_size 已提至 64 MiB；历史回放内嵌 base64 另设 1 MiB 封顶。
- **向后兼容**：旧客户端忽略未知帧；`send-prompt` 无 `attachments` 默认空；旧网页查看器对 image 块走 default 分支显示原始 JSON（不致命）。
- **敏感信息**：`self.prompts` 面板只存附件 count/types，绝不存 base64；`CC_TOKEN`/SMTP 授权码仍不落库、不提交。
