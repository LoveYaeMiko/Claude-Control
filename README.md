# Claude-Control

**Claude Code 远程会话查看器（内网穿透版）** — 通过安卓手机 App 或手机浏览器实时查看电脑上运行的 Claude Code 会话，无需公网服务器，仅靠内网穿透。

## 快速开始

### 电脑端

```bash
cd computer
python -m venv .venv                     # 首次
PYTHONUTF8=1 .venv/Scripts/pip install -r requirements.txt
cp .env.example .env                     # 按需修改端口 / token
PYTHONUTF8=1 .venv/Scripts/python relay_server.py
```

Windows 也可直接双击 `start.bat`（自动建 venv、装依赖、启动）。

启动后终端会打印：

- 本地地址 `ws://127.0.0.1:9876`（手机同一局域网可直接连）
- ngrok/cloudflared 公网地址（若设置了隧道）
- 二维码（`segno` ANSI 绘制），手机扫码即可打开网页查看器

### 手机端

- **网页查看器（零安装）**：扫码，或浏览器打开 `http://<电脑局域网IP>:9876/`。
- **安卓 App**：
  - **预打包 APK**：`Claude-Control-v0.1.0.apk`（release 签名，见 GitHub Releases，或本地 `android/app/build/outputs/apk/release/`）。
  - 或从源码构建：`cd android && ./gradlew assembleRelease`（需 `keystore.properties`，见 [android/README.md](android/README.md)）。
  - 打开后填入 `wss://<公网隧道域名>` 或 `ws://<电脑局域网IP>:9876` + token，连接后选择会话即可实时浏览；assistant 文本按 **Markdown 富文本**渲染（标题 / 加粗 / 列表 / 表格 / 代码块 / 删除线），思考与工具调用折叠展示。

> 注意：Android 9+ 默认禁止明文 WebSocket（`ws://`）。App 已在 `AndroidManifest.xml` 声明 `android:usesCleartextTraffic="true"`，局域网 `ws://` 可用；公网建议走 `wss://`（cloudflared / ngrok 隧道）。

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
│  - HTTP: / 网页查看器  /qrcode  /healthz     │
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

- **采集**：直接读取 Claude Code 会话转录 `~/.claude/projects/<cwd-slug>/<session-id>.jsonl`，只读增量解析（不依赖 `script`/`tail`，Windows 原生，数据结构化）。
- **传输**：Python 内建 WebSocket 中继，手机端经局域网或隧道直连。
- **安全**：WebSocket 连接携带预共享 token（`?token=...`）。

## 配置

通过 `.env` 或环境变量（参见 `computer/.env.example`）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `CC_PORT` | `9876` | HTTP + WS 端口 |
| `CC_TOKEN` | 空 | 访问令牌（务必在生产设置） |
| `CC_TUNNEL` | `lan` | `lan` / `ngrok` / `cloudflared` / `none` |
| `CC_NGROK_AUTH_TOKEN` | 空 | 仅 ngrok 隧道需要（见 ACCOUNTS.md） |
| `CC_TRANSCRIPTS_DIR` | `~/.claude/projects` | 转录扫描目录 |
| `CC_DEBUG` | `0` | 调试日志 |

## 目录

| 目录 | 说明 |
|------|------|
| [computer/](computer/) | 电脑端：转录中继 + 网页查看器（Python） |
| [android/](android/) | 安卓 App（Android Studio / Kotlin） |
| [ACCOUNTS.md](ACCOUNTS.md) | 需要的外部账号/接口清单 |
| [REPORT-Phase1.md](REPORT-Phase1.md) | 阶段一报告：初步构建、审查结论与修复记录 |
| [REPORT-Phase2.md](REPORT-Phase2.md) | 阶段二报告：安卓 UI 优化、Markdown 渲染、公网联调与 APK 发布 |
| [blueprint.md](blueprint.md) | 项目蓝图（完整实现指南） |
| [.claude/](.claude/) | Claude Code 工程配置（插件启用） |

## 项目状态

- ✅ git 初始化
- ✅ 插件初始化（feature-dev、mattpocock-skills 项目级注册）
- ✅ 电脑端中继服务 `relay_server.py`（转录监控 + WS + HTTP + 二维码 + 隧道，约 1030 行）
- ✅ 网页查看器（暗色主题、会话切换、流式追加、思考/工具折叠）
- ✅ 端到端测试 20 项断言通过（含 `/qrcode` 鉴权与 `update` 实时合并）
- ✅ 安卓 App 完整实现（Material3 UI + Markdown 富文本渲染 + 会话状态色点 + 智能滚动）
- ✅ 公网联调测试通过（cloudflared 隧道：`/healthz`、`/qrcode` 鉴权、`wss://` 均验证）
- ✅ 安卓 release APK 打包 + 签名（`Claude-Control-v0.1.0.apk`，见 [REPORT-Phase2.md](REPORT-Phase2.md)）
- ✅ 对抗式代码审查：22 条发现、确认 19 条，全部修复（详见 [REPORT-Phase1.md](REPORT-Phase1.md)）
- ⏳ 安卓真机实装验证（局域网 + 公网隧道）
- ⏳ 账号相关配置（ngrok 令牌等，见 [ACCOUNTS.md](ACCOUNTS.md)）

## 参考

- 完整实现蓝图见 [blueprint.md](blueprint.md)。
- 工程技能插件：`feature-dev` 与 `mattpocock-skills`（详见 [CLAUDE.md](CLAUDE.md)）。
