"""relay_server.py 端到端测试：
- 启动服务器（临时 transcript 目录 + 临时端口 + tunnel=none）
- 验证 HTTP 静态页 / healthz / qrcode
- 验证 WebSocket hello / get-history / 实时广播（追加写入）
用法: .venv/Scripts/python.exe tests/test_e2e.py
"""
# -*- coding: utf-8 -*-
import asyncio
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mock_transcript  # noqa: E402
import relay_server    # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        session_id = mock_transcript.make_mock_project(root)
        port = free_port()
        token = "test-token-123"

        env = dict(os.environ)
        env["CC_TRANSCRIPTS_DIR"] = str(root)
        env["CC_TOKEN"] = token
        env["CC_TUNNEL"] = "none"
        env["CC_PORT"] = str(port)
        env["PYTHONUTF8"] = "1"

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "relay_server.py",
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        log_lines = []

        async def reader():
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                log_lines.append(line)

        reader_task = asyncio.create_task(reader())

        base = f"http://127.0.0.1:{port}"
        try:
            # 等待服务就绪
            for _ in range(40):
                try:
                    urllib.request.urlopen(base + "/healthz", timeout=1)
                    break
                except Exception:
                    await asyncio.sleep(0.25)

            import websockets
            from websockets.asyncio.client import connect as ws_connect

            # 1) HTTP 静态页
            html = urllib.request.urlopen(base + "/", timeout=3).read().decode("utf-8")
            check("HTTP / 返回查看页", "Claude-Control" in html)
            js = urllib.request.urlopen(base + "/app.js", timeout=3).read().decode("utf-8")
            check("HTTP /app.js 可达", "WebSocket" in js)
            health = json.loads(urllib.request.urlopen(base + "/healthz", timeout=3).read())
            check("HTTP /healthz ok=true", health.get("ok") is True)
            # /qrcode 内嵌访问 token，无 token 必须被拒（安全修复）
            try:
                urllib.request.urlopen(base + "/qrcode", timeout=3).read()
                check("HTTP /qrcode 无 token 被拒", False)
            except urllib.error.HTTPError as e:
                check("HTTP /qrcode 无 token 被拒", e.code == 401)
            qr = urllib.request.urlopen(base + "/qrcode?token=" + token, timeout=3).read().decode("utf-8")
            check("HTTP /qrcode 带 token 返回页面", "Claude" in qr or "svg" in qr.lower())

            # 2) WebSocket 无 token 被拒
            try:
                async with ws_connect(f"ws://127.0.0.1:{port}/ws") as ws:
                    await ws.recv()
                check("WS 无 token 被拒绝", False)
            except Exception:
                check("WS 无 token 被拒绝", True)

            # 3) WebSocket 带 token：hello + 会话列表 + 历史
            async with ws_connect(f"ws://127.0.0.1:{port}/ws?token={token}") as ws:
                hello = json.loads(await ws.recv())
                check("WS hello 类型", hello.get("type") == "hello")
                check("WS hello 含会话", len(hello.get("sessions", [])) == 1)
                sid = hello["sessions"][0]["id"]
                check("WS 会话 id 匹配", sid == session_id)
                check("WS 会话标题来自 ai-title",
                      "初始化项目配置" in hello["sessions"][0].get("title", ""))

                await ws.send(json.dumps({"type": "get-history", "sessionId": sid}))
                hist = json.loads(await ws.recv())
                check("WS get-history isHistory", hist.get("isHistory") is True)
                roles = [r.get("role") for r in hist.get("records", []) if r.get("role")]
                check("WS 历史含 user/assistant", "user" in roles and "assistant" in roles)
                # 验证 tool_result 已合并进 tool_use
                merged = any(
                    any(b.get("type") == "tool_use" and b.get("result") for b in r.get("content", []))
                    for r in hist.get("records", []) if r.get("role")
                )
                check("WS tool_result 合并进 tool_use", merged)
                check("WS 会话状态 active", hello["sessions"][0].get("status") == "active")

                # 4) 实时广播：追加新记录后应收到 messages
                await ws.send(json.dumps({"type": "ping"}))
                # 写新记录
                new_line = mock_transcript.assistant_text(session_id, "追加的实时消息内容。")
                f = root / "c--Users-wyxwi-Desktop-Claude-Control" / f"{session_id}.jsonl"
                with open(f, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(new_line, ensure_ascii=False) + "\n")

                got_live = False
                got_pong = False
                timeout = time.time() + 8
                while time.time() < timeout:
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                    except Exception:
                        break
                    if msg.get("type") == "pong":
                        got_pong = True
                    if msg.get("type") == "messages" and not msg.get("isHistory"):
                        texts = [r.get("content", []) for r in msg.get("records", [])]
                        flat = json.dumps(msg, ensure_ascii=False)
                        if "追加的实时消息内容" in flat:
                            got_live = True
                    if got_live and got_pong:
                        break
                check("WS pong 响应", got_pong)
                check("WS 实时广播新消息", got_live)

                # 4b) 实时工具结果合并：先广播 tool_use，再推送 update 帧补全 result
                tool_id = "call_live_01"
                with open(f, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(mock_transcript.assistant_tool_use(
                        session_id, "Bash", {"command": "echo live"}, tool_id=tool_id),
                        ensure_ascii=False) + "\n")
                got_tool = False
                timeout = time.time() + 8
                while time.time() < timeout:
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                    except Exception:
                        break
                    if msg.get("type") == "messages" and not msg.get("isHistory"):
                        flat = json.dumps(msg, ensure_ascii=False)
                        if tool_id in flat:
                            got_tool = True
                            break
                check("WS 实时广播 tool_use", got_tool)

                with open(f, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(mock_transcript.user_tool_result(
                        session_id, tool_id, "LIVE-RESULT-OK"), ensure_ascii=False) + "\n")
                got_update = False
                timeout = time.time() + 8
                while time.time() < timeout:
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                    except Exception:
                        break
                    if msg.get("type") == "messages" and msg.get("update") and not msg.get("isHistory"):
                        flat = json.dumps(msg, ensure_ascii=False)
                        if "LIVE-RESULT-OK" in flat and tool_id in flat:
                            got_update = True
                            break
                check("WS update 帧补全 tool_result", got_update)

            # 5) 追加一个 attention 会话（未完成的 Bash tool_use），状态应变为 attention
            sid2 = mock_transcript.make_mock_project(root)
            f2 = root / "c--Users-wyxwi-Desktop-Claude-Control" / f"{sid2}.jsonl"
            with open(f2, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(mock_transcript.user_prompt(sid2, "执行测试")) + "\n")
                fh.write(json.dumps(mock_transcript.assistant_tool_use(
                    sid2, "Bash", {"command": "echo hi"}, tool_id="call_99")) + "\n")
                fh.flush()
            await asyncio.sleep(2.5)
            async with ws_connect(f"ws://127.0.0.1:{port}/ws?token={token}") as ws:
                hello2 = json.loads(await ws.recv())
                check("WS 新会话被发现", len(hello2.get("sessions", [])) >= 2)
                s2 = next((s for s in hello2["sessions"] if s["id"] == sid2), None)
                check("WS 未完成 Bash 工具 → attention", bool(s2 and s2.get("status") == "attention"))

        finally:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                proc.kill()
            reader_task.cancel()

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
