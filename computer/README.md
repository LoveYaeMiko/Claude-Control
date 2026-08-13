# computer/ — 电脑端

采集 + WebSocket 中继 + 内网穿透，实现 Claude Code 终端会话的远程传输。

## 计划文件（对应 `blueprint.md` §4）

| 文件 | 说明 | 状态 |
|------|------|------|
| `relay_server.py` | 采集 + WebSocket 服务 + ngrok 内网穿透（见 blueprint §4.2） | 待实现 |
| `requirements.txt` | `websockets>=12.0`, `pyngrok>=7.0`（见 blueprint §4.1） | 待实现 |
| `start.sh` | 一键启动脚本（见 blueprint §4.4；Windows 需提供 `start.bat`） | 待实现 |
| `.env.example` | `SECRET_TOKEN` / `NGROK_AUTH_TOKEN` 模板 | 待实现 |

## 平台适配注意

本机为 **Windows 11**，蓝图中的 Unix 路径与 `script` 命令需适配：

- 日志文件：建议改用 `%TEMP%\claude_session.log` 或项目内 `logs/` 目录。
- 采集方式：Windows 无 `script` 命令，需用 WinPty / ConPTY 封装，或让 Claude Code 通过 `--output-format stream-json` 输出结构化流。

## 运行（规划）

```bash
cd computer
pip install -r requirements.txt
python relay_server.py
```
