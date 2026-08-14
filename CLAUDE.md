# Claude-Control

> 基于 `blueprint.md` 蓝图实现的 **Claude Code 远程会话查看器 / 交互器**（内网穿透版）。

## 项目定位

通过安卓手机 App / 手机浏览器实时**查看**并**远程交互**电脑上运行的 Claude Code 会话，无需公网服务器，仅靠内网穿透。

- **采集**：直接读取 Claude Code 自身写入的会话转录 `~/.claude/projects/<cwd-slug>/<session-id>.jsonl`（无需 `script`/`tail`，Windows 原生，结构化数据更丰富）。
- **传输**：Python 内建 WebSocket 中继服务（默认端口 9876），手机端通过内网穿透后的公网地址直连。
- **安全**：WebSocket 连接携带预共享 token（`?token=...`），防止匿名访问。
- **远程交互**：手机端发 `send-prompt`，中继异步起 `claude -p` 无头子进程在电脑上执行（需 `CC_ALLOW_INTERACT=1` 显式开启），结果经转录监控链路实时回显。
- **扫码自动配置**：启动时打印「配置二维码」（`claudecontrol://connect?url=...&token=...`），手机 App 扫码自动填好 URL + token 并连接。
- **连接面板**：启动时自动在浏览器打开 `/dashboard`，集中显示公网/局域网 URL、token、配置二维码与已连接设备。

## 架构

```
┌──────────────────────────────────────────────┐
│              电脑端                          │
│  Claude Code 写入转录                        │
│  ~/.claude/projects/<cwd-slug>/<id>.jsonl    │
│        │                                     │
│        ▼                                     │
│  computer/relay_server.py:                   │
│  - TranscriptMonitor 只读扫描 + 增量读取      │
│  - WebSocket 中继 (ws://host:9876/ws)        │
│  - HTTP: / 网页查看器  /dashboard 连接面板   │
│          /qrcode  /api/info  /api/devices    │
│          /healthz                           │
│  - 内网穿透: lan / ngrok / cloudflared       │
└────────┼─────────────────────────────────────┘
         │ 局域网直连 或 ngrok/cloudflared 隧道
         ▼
┌────────────────────────────────┐
│ 手机端                         │
│ - 安卓 App (android/)          │
│ - 或浏览器打开 /qrcode 扫码    │
└────────────────────────────────┘
```

## 转录格式（关键要点）

- 每行一条 JSON 记录，事件类型：`user` / `assistant` / `system` / `attachment` / `file-history-*` / `queue-operation` / `ai-title` / `last-prompt`。
- 无 `summary` 类型；`system` 有 `compact_boundary` / `local_command` 子类型。
- 顶层字段 `timestamp`(ISO) / `toolUseResult` / `persistedOutputPath` / `sourceToolAssistantUUID`。
- assistant 的 `tool_use` 与后续 `tool_result` 通过 `tool_use_id` 配对合并；`persistedOutputPath` 引用的外部文件会被读入（截断到上限）。
- **Windows 文件锁**：Claude Code 以追加模式持有 JSONL，并发只读安全；中继只读、绝不写入或截断。
- 实时性验证：`relay_server.py` 每 `poll_interval`（默认 1.0s）重新扫描，用文件偏移增量读取新行。

## 中继服务（computer/relay_server.py）

启动：`start.bat`（Windows）或 `start.sh`，也可 `python relay_server.py --port 9876 --token xxx`。

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|----------|--------|------|
| 端口 | `CC_PORT` | `9876` | HTTP + WS 共用 |
| 访问令牌 | `CC_TOKEN` | 空 | WS 需 `?token=` 匹配 |
| 转录目录 | `CC_TRANSCRIPTS_DIR` | `~/.claude/projects` | 扫描根目录 |
| 隧道 | `CC_TUNNEL` | `lan` | `lan`/`ngrok`/`cloudflared`/`none` |
| ngrok 令牌 | `CC_NGROK_AUTH_TOKEN` | 空 | 仅 `CC_TUNNEL=ngrok` 需要 |
| 远程交互 | `CC_ALLOW_INTERACT` | `0` | `1` 开启手机端发送 prompt（远程执行，慎开） |
| 交互权限 | `CC_INTERACT_PERMISSION_MODE` | `bypassPermissions` | `bypassPermissions`/`acceptEdits`/`plan` |
| 交互模型 | `CC_INTERACT_MODEL` | 空 | 留空用默认模型 |
| 自动打开面板 | `CC_AUTO_OPEN` | `1` | 启动时在浏览器打开连接面板（`0` 关闭） |
| 调试 | `CC_DEBUG` | `0` | 打印详细日志 |

WS 消息协议：

- 服务端推：`hello` / `sessions` / `session-start` / `session-update` / `session-end` / `messages` / `interaction` / `ping`。
- 客户端发：`get-history` / `list-sessions` / `send-prompt` / `ping`。
- `send-prompt`（需 `CC_ALLOW_INTERACT=1`）异步起 `claude -p` 执行；服务端回 `interaction` 状态帧（`started` → `session` → `finished`/`error`），内容回显复用 `messages` 链路。
- `messages` 携带 `isHistory`（历史回放）与 `update`（就地补全：tool_result 合并到已广播的 tool_use）标志；网页端/安卓端按 `idx` 去重 / 替换。
- `get-history` 有每连接 0.3s 限流；无 token 的 `/qrcode` 与 `/api/info`、`/api/devices` 访问被拒（返回 401），避免内嵌 token 泄露。
- 会话状态：`active` / `attention`（存在待批准工具调用）/ `idle` / `ended`，依据 mtime + 待定工具判定；每个会话最多保留 `HISTORY_LIMIT` 条记录，会话数超 `MAX_SESSIONS` 后驱逐已结束会话。

## 目录结构

```
Claude-Control/
├── blueprint.md            # 项目蓝图
├── CLAUDE.md               # 本文件
├── .claude/
│   └── settings.json       # 项目级插件启用配置
├── computer/               # 电脑端：中继 + 网页查看器
│   ├── relay_server.py     # 核心服务
│   ├── web/                # 网页查看器 + 连接面板 (index.html/dashboard.html/...)
│   ├── tests/              # mock_transcript.py + test_e2e.py
│   ├── requirements.txt    # websockets>=16, python-dotenv, segno
│   ├── requirements-ngrok.txt # pyngrok（可选）
│   ├── .env.example        # 配置模板
│   └── start.bat / start.sh
├── android/                # 安卓 App（Kotlin + OkHttp + RecyclerView）
└── ACCOUNTS.md             # 需要的外部账号/接口清单
```

## 可用的工程技能

项目级启用了以下 Claude Code 插件（用户级也已启用）：

### feature-dev

- `/feature-dev <功能描述>` — 7 阶段特性开发工作流（发现 → 代码库探索 → 澄清问题 → 架构设计 → 实现 → 质量审查 → 总结）。
- 附带 Agents：
  - `code-explorer` — 深入分析现有代码库，追踪执行路径
  - `code-architect` — 基于代码库惯例设计特性架构与实现蓝图
  - `code-reviewer` — 审查 Bug、逻辑错误、安全漏洞与代码质量

### mattpocock-skills

- `diagnosing-bugs` — 疑难 Bug 与性能回归的诊断循环
- `tdd` — 测试驱动开发
- `prototype` — 快速原型
- `research` — 调研
- `domain-modeling` — 领域建模
- `codebase-design` — 代码库架构设计
- `code-review` — 代码审查
- `resolving-merge-conflicts` — 解决合并冲突
- `wizard` — 向导式流程
- `grilling` — 挑战式追问，验证想法
- `writing-for-agents` — 面向 agent 的写作规范

## 开发约束与提示

- **平台**：本机为 Windows 11。JSONL 转录由 Claude Code 追加写入，中继必须只读；`PYTHONUTF8=1` 可避免 GBK 编码错误。
- **敏感配置**：`CC_TOKEN` 与 `CC_NGROK_AUTH_TOKEN` 不应硬编码提交；以 `.env`（已在 `.gitignore`）或环境变量注入。
- **依赖版本**：`websockets>=16`（v17 的 `Response` 为 4 字段 dataclass）、`segno`（终端 ANSI + SVG 二维码）。
- **测试**：`python tests/test_e2e.py` 起临时服务跑通 30 项断言（HTTP/WS 鉴权/history/合并/广播/状态/交互/配置 URI/连接面板）。
