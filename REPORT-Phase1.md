# 阶段一报告：Claude-Control 初步构建

**日期**：2026-08-12
**阶段目标**：依据 `blueprint.md`，独立完成 Claude Code 远程会话监视器的初步可运行构建，并完成一次对抗式代码审查与修复。
**状态**：✅ 本阶段完成（端到端测试 20 项全部通过）

---

## 1. 阶段成果总览

| 交付物 | 位置 | 状态 |
|--------|------|------|
| 电脑端中继服务（转录监控 + WebSocket + HTTP + 二维码 + 隧道） | [computer/relay_server.py](computer/relay_server.py) | ✅ 完成（约 1000 行） |
| 手机网页查看器（暗色主题、会话切换、流式渲染） | [computer/web/](computer/web/) | ✅ 完成 |
| 端到端测试（20 项断言） | [computer/tests/](computer/tests/) | ✅ 全部通过 |
| 一键启动脚本 + 配置模板 | [computer/start.bat](computer/start.bat)、[computer/.env.example](computer/.env.example) | ✅ 完成 |
| 安卓原生 App 骨架（Kotlin + OkHttp + RecyclerView） | [android/](android/) | ✅ 完成（骨架，未编译联调） |
| 项目文档 | [CLAUDE.md](CLAUDE.md)、[README.md](README.md)、[ACCOUNTS.md](ACCOUNTS.md)、[REPORT-Phase1.md](REPORT-Phase1.md) | ✅ 完成 |
| git 仓库 + 工程插件（feature-dev / mattpocock-skills） | 根目录、[.claude/settings.json](.claude/settings.json) | ✅ 完成 |

## 2. 核心架构决策

**采集方式**：放弃蓝图中的 `script` 命令 + `tail -f` 方案，改为**直接读取 Claude Code 自身写入的会话转录** `~/.claude/projects/<cwd-slug>/<session-id>.jsonl`。

- 理由：Windows 原生、零外部工具、数据结构化（事件类型 / tool_use / tool_result / thinking 齐全）。
- 验证：本次会话的真实转录可正常发现、增量解析。

**增量读取**：文件偏移 + 原始字节半行缓冲，只在完整行边界解码，避免多字节（中文/emoji）跨读损坏；仅只读、绝不写入或截断（兼容 Windows 上 Claude Code 持有的追加写句柄）。

**传输**：Python 内建 WebSocket 中继（默认端口 9876），HTTP 与 WS 同端口复用；token 经 `?token=` 认证。

**内网穿透**：三档可选——`lan`（局域网直连，默认，无需账号）、`cloudflared`（公网，无需账号）、`ngrok`（公网，需注册 token）。

## 3. 已实现功能

- **转录解析**：`user` / `assistant` / `system`（compact_boundary / local_command）/ `attachment` / `ai-title` / `last-prompt` 归一化；`tool_use`↔`tool_result` 按 `tool_use_id` 合并；`persistedOutputPath` 外置大输出读取。
- **会话状态**：`active` / `attention`（存在待审批工具调用）/ `idle`（超时 120s）/ `ended`（文件消失）。
- **WebSocket 协议**：服务端推 `hello/sessions/session-start/session-update/session-end/messages/ping`；客户端发 `get-history/list-sessions/ping`；`messages` 带 `isHistory` 与 `update` 标志。
- **网页查看器**：会话下拉切换、历史回放、实时流式追加、思考/工具块折叠、断线自动重连、二维码页面。
- **二维码**：终端 ANSI 二维码（segno）与 `/qrcode` 网页 SVG 二维码。
- **安卓 App**：连接/断开、会话列表选择、实时消息展示、思考/工具折叠、token 记忆。
- **健康检查**：`/healthz`。

## 4. 质量与安全

本阶段按 ultracode 要求执行了一次**对抗式代码审查工作流**（5 维度并行审查 + 逐条对抗验证）：

- 审查维度：正确性 / 安全 / Windows 兼容 / 三方协议一致性 / 网页端逻辑。
- 结果：**22 条发现，对抗验证后确认 19 条**，全部修复。

### 已修复的关键问题

| 严重级 | 问题 | 修复 |
|--------|------|------|
| 🔴 高 | `/qrcode` 无鉴权，内嵌访问 token 泄露，可完全绕过 token 认证 | `/qrcode` 必须带 token（`hmac.compare_digest`）才返回，并加 `Cache-Control: no-store` |
| 🟠 中 | 工具结果合并到已广播记录后不重推，在线端永远看不到结果 | 新增 `update` 帧，服务端补全后按 idx 重推，网页端/安卓端按 idx 替换渲染 |
| 🟠 中 | transcript 截断/重写时旧记录重复下发 | 截断时整体重置（清空记录/计数/待审批状态），序号改为单调递增 |
| 🟠 中 | cloudflared stdout 阻塞式读取冻结整个 asyncio 事件循环 | 改用 `asyncio.create_subprocess_exec` + 异步流读取 |
| 🟠 中 | 网页端断线重连不补历史、旧连接 close 触发重复连接、token 错误无限重连 | onopen 重发 `get-history`；onclose 校验当前连接；close code 1008 停止重连并提示 |
| 🟢 低 | UTF-8 多字节跨读损坏 | 原始字节半行缓冲，完整行边界解码 |
| 🟢 低 | 内存无界增长（会话/记录不淘汰） | 每会话记录上限 `HISTORY_LIMIT`，会话数上限 `MAX_SESSIONS` 后驱逐已结束会话 |
| 🟢 低 | token 非恒时比较 | `hmac.compare_digest` |
| 🟢 低 | `get-history` 无限流 | 每连接 0.3s 限流 |
| 🟢 低 | Windows 重定向输出时中文打印可能 `UnicodeEncodeError` | 启动时 `stdout/stderr.reconfigure(encoding="utf-8", errors="replace")` |
| 🟢 低 | 网页端 sys 记录渲染字面 HTML 标记 | 改为按元素构建 |
| 🟢 低 | 安卓端忽略 `session-update`、不过滤会话、不去重、get-history JSON 未转义、token 未编码 | 全部修复（见 [android/](android/)） |

## 5. 测试

```text
HTTP / 返回查看页            PASS
HTTP /app.js 可达             PASS
HTTP /healthz ok=true        PASS
HTTP /qrcode 无 token 被拒    PASS   ← 新增（安全）
HTTP /qrcode 带 token 返回页面 PASS   ← 新增（安全）
WS 无 token 被拒绝            PASS
WS hello 类型/含会话/id/标题   PASS
WS get-history isHistory     PASS
WS 历史含 user/assistant     PASS
WS tool_result 合并进 tool_use PASS
WS 会话状态 active            PASS
WS pong 响应                 PASS
WS 实时广播新消息             PASS
WS 实时广播 tool_use          PASS   ← 新增
WS update 帧补全 tool_result  PASS   ← 新增
WS 新会话被发现              PASS
WS 未完成 Bash 工具 → attention PASS
✅ 全部通过（20/20）
```

## 6. 已知限制 / 下一阶段建议

1. **安卓 App 未实机编译联调**：当前为可编译骨架，需 Android Studio 构建并在真机验证（AGP 8.4.2 / Kotlin 1.9.24 / compileSdk 34）。
2. **明文 WebSocket**：`ws://` 在 Android 9+ 默认被禁，需在 Manifest 开启 `usesCleartextTraffic`，或用 TLS 隧道（`wss://`，ngrok/cloudflared 默认 HTTPS 隧道已是 wss）。
3. **会话排序**：安卓端会话列表未按时间排序（网页端已按 mtime 倒序）。
4. **认证增强（可选）**：token 目前经 URL 传输；如需更高安全可加 Basic Auth 或一次性扫码登录。
5. **性能**：超大会话首次读取会一次性读入；后续可加按需分页读取。

## 7. 外部账号 / 接口需求

纯局域网使用**无需任何账号**。公网穿透方案见 [ACCOUNTS.md](ACCOUNTS.md)：

- **cloudflared**（推荐公网方案）：免注册，需安装二进制（`winget install cloudflare.cloudflared`；国内可走镜像下载放入 `computer/bin/`）。
- **ngrok**（可选）：需注册 https://ngrok.com 获取 authtoken 填入 `CC_NGROK_AUTH_TOKEN`。
- **访问令牌 `CC_TOKEN`**：公网暴露时建议自设随机串。

> 待用户确认项统一汇总，见阶段总结消息。

## 8. 冒烟测试记录（2026-08-13，真实会话数据）

**环境**：真实转录目录 `~/.claude/projects`（21 个会话）。

| 测试项 | 结果 |
|--------|------|
| `/healthz` | ✅ `{"ok":true, "sessions":21}` |
| `/` 查看页 | ✅ 200，标题正确 |
| `/qrcode` 无 token | ✅ 401（安全） |
| `/qrcode` 带 token | ✅ 200 + SVG 二维码 |
| WS 无 token | ✅ 被拒（ConnectionClosedError） |
| WS hello | ✅ 21 个会话，标题/模型/状态齐全 |
| WS get-history | ✅ 56 条记录，text/thinking/tool_use/tool_result 正确，16 个 tool_use 已合并结果 |
| 公网隧道 healthz | ✅ `https://xxx.trycloudflare.com/healthz` 返回 21 会话 |
| 公网 `wss://` 带 token | ✅ hello + 21 会话 |
| 公网 `wss://` 无 token | ✅ 被拒 |

**过程中发现并修复的额外问题**：

- 🟠 `segno 1.6.6` 的 `svg_inline` 已内建 `xmldecl=False`，再传入会抛 `TypeError`，导致二维码退化为纯文本页 → 已移除该参数。
- 🟢 winget 无法安装 cloudflared（GitHub 下载被墙）→ 改用镜像 `ghproxy.net` 下载并放入 `computer/bin/`，服务优先使用项目内二进制。

**公网地址（本次临时隧道，重启后变化）**：`https://condos-dollar-sofa-shareware.trycloudflare.com`

