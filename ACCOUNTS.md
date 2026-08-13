# 账号 / 接口需求清单

本阶段（初步构建）完成。以下为本项目需要的外部账号与接口，以及它们的作用。**仅在与外界通信/穿透时需要**；纯局域网使用一个都不需要。

## 1. ngrok 账号（可选，推荐）

| 项目 | 说明 |
|------|------|
| **用途** | 公网内网穿透：手机在**任何网络**（如 4G/5G、公司网）下访问家中/公司电脑上的会话。 |
| **是否需要** | ⭕ 可选。仅当你设置 `CC_TUNNEL=ngrok` 时需要。 |
| **如何获取** | 到 https://ngrok.com 免费注册（Free 套餐即可），Dashboard → Your Authtoken 复制。 |
| **填入位置** | `computer/.env` 的 `CC_NGROK_AUTH_TOKEN=` |
| **额外依赖** | `pip install pyngrok`（已备好 `computer/requirements-ngrok.txt`） |
| **替代方案** | `cloudflared`（下方第 2 项）免费且**无需账号**，可完全替代 ngrok。 |

## 2. cloudflared 二进制（可选，无需账号）

| 项目 | 说明 |
|------|------|
| **用途** | 公网内网穿透，quick tunnel 免费、**无需注册账号**。 |
| **是否需要** | ⭕ 可选。仅当你设置 `CC_TUNNEL=cloudflared` 时需要。 |
| **如何获取** | Windows: `winget install cloudflare.cloudflared`，或到 cloudflare.com 下载（本项目仅用 `cloudflared tunnel --url` 快速隧道，不绑定 CF 账号）。 |
| **备注** | 隧道地址为临时随机域名（`xxx.trycloudflare.com`），重启后变化，适合临时查看。 |

## 3. 局域网直连（默认，无账号）

| 项目 | 说明 |
|------|------|
| **用途** | 手机与电脑处于**同一 Wi-Fi/局域网**时直接访问（默认 `CC_TUNNEL=lan`）。 |
| **是否需要** | ✅ 无需任何账号。 |
| **注意** | 需放行 Windows 防火墙对 `CC_PORT`（默认 9876）的入站 TCP。手机连接使用 `ws://<电脑局域网IP>:9876`。 |

## 4. 外部接口（API）

| 接口 | 是否需要 | 说明 |
|------|----------|------|
| **Anthropic / Claude API** | ❌ 不需要 | 本项目**只读取本地转录文件**，不调用任何 LLM API，无需 API key。 |
| **Claude Code 转录目录** | ✅ 本地接口 | `~/.claude/projects/<cwd-slug>/<session-id>.jsonl`。程序默认扫描该目录，只读增量解析；无需账号。 |

## 需要你决策 / 提供的项目（汇总）

1. **`CC_TOKEN`（访问令牌）**：公网暴露时强烈建议设置一个，防止陌生人连入。自定一串随机字符串即可，无需注册。
2. **`CC_NGROK_AUTH_TOKEN`**（仅当你选 ngrok 穿透时）— 需注册 ngrok。
3. **`CC_TUNNEL`** 选哪种：
   - `lan`（默认，无需任何账号，仅局域网）
   - `cloudflared`（推荐公网方案，无需账号）
   - `ngrok`（需注册 + 填 token）
   - `none`（不穿透，仅本机访问）

## 本阶段决策记录（2026-08-12）

已选定并写入 `computer/.env`：

- **`CC_TUNNEL=cloudflared`**（公网免注册方案）→ **无需任何账号**。
- **`CC_TOKEN` 固定自定义值**：已生成随机串并写入 `.env`（见 `computer/.env`，该值**勿提交、勿外泄**；可自行替换）。
- 因此 **ngrok 不需要**，无需注册、无需 authtoken。

启动前需在电脑安装 cloudflared 二进制：

```bat
winget install cloudflare.cloudflared
```

> ⚠️ **国内网络注意**：winget 会从 GitHub 下载 cloudflared，直连常被墙（`InternetOpenUrl() failed` / `Empty reply`）。已实测可用镜像下载并放入项目 `computer/bin/`：
> ```bat
> curl -sSL -o computer\bin\cloudflared.exe "https://ghproxy.net/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
> ```
> 服务启动时会优先使用 `computer/bin/cloudflared.exe`，无需改动系统 PATH。该二进制已加入 `.gitignore`，不入库。

安装后运行 `start.bat` 即可，手机在任意网络访问终端打印的 `https://xxx.trycloudflare.com` 地址（首次连接会在浏览器提示中打开，或直接用安卓 App 填该地址）。

## 建议

手机与电脑同网时用局域网最快；需要随时随地访问时，本次已选 `cloudflared`（免注册、免账号），无需额外注册任何服务。
