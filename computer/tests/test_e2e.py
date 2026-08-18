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

        # 0) 配置 URI / WS 地址（纯函数单元测试，无需起服务）
        class _TunnelsStub:
            def __init__(self, url=""):
                self.public_urls = [url] if url else []
            def primary_url(self):
                return self.public_urls[0] if self.public_urls else ""

        cfg_uri = relay_server._make_config_uri(
            relay_server.Config(port=9876, token=token, tunnel="lan"), _TunnelsStub(), 9876)
        check("配置 URI 前缀", cfg_uri.startswith("claudecontrol://connect?"))
        check("配置 URI 内嵌 url/token", "url=" in cfg_uri and f"token={token}" in cfg_uri)
        ws_url = relay_server._make_ws_url(
            relay_server.Config(port=9876, tunnel="lan"), _TunnelsStub(), 9876)
        check("LAN WS 地址含 /ws 与端口", ws_url.startswith("ws://") and ws_url.endswith(":9876/ws"))
        tunnel_uri = relay_server._make_config_uri(
            relay_server.Config(port=9876, token=token, tunnel="cloudflared"),
            _TunnelsStub("https://abc.trycloudflare.com"), 9876)
        check("隧道配置 URI 用 wss 基础地址（不含 /ws）",
              "url=wss%3A%2F%2Fabc.trycloudflare.com" in tunnel_uri
              and "%2Fws" not in tunnel_uri)

        env = dict(os.environ)
        env["CC_TRANSCRIPTS_DIR"] = str(root)
        env["CC_TOKEN"] = token
        env["CC_TUNNEL"] = "none"
        env["CC_PORT"] = str(port)
        env["PYTHONUTF8"] = "1"
        env["CC_AUTO_OPEN"] = "0"   # 测试环境不自动打开浏览器
        env["CC_ALLOW_INTERACT"] = "0"   # 固定关闭，验证禁用态返回 error（隔离 .env 干扰）
        env["CC_QR_PNG_PATH"] = str(root / "qr_test.png")  # 二维码 PNG 写到临时目录，避免污染项目根目录

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

            # 后台任务应在启动后生成二维码 PNG（tunnel=none 时无需等隧道）
            for _ in range(20):
                if (root / "qr_test.png").exists():
                    break
                await asyncio.sleep(0.25)
            check("启动生成二维码 PNG 到指定路径", (root / "qr_test.png").exists())

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
            # 连接面板 / 信息 API / 设备 API
            dash = urllib.request.urlopen(base + "/dashboard", timeout=3).read().decode("utf-8")
            check("HTTP /dashboard 返回面板", "连接面板" in dash and "已连接设备" in dash)
            try:
                urllib.request.urlopen(base + "/api/info", timeout=3).read()
                check("HTTP /api/info 无 token 被拒", False)
            except urllib.error.HTTPError as e:
                check("HTTP /api/info 无 token 被拒", e.code == 401)
            info = json.loads(urllib.request.urlopen(
                base + "/api/info?token=" + token, timeout=3).read())
            check("HTTP /api/info 返回 token/配置 URI",
                  info.get("token") == token
                  and info.get("configUri", "").startswith("claudecontrol://connect?"))
            check("HTTP /api/info 含二维码 SVG", "svg" in (info.get("qrSvg", "") or "").lower())
            qr_svg = info.get("qrSvg", "") or ""
            check("二维码 SVG 为深色模块 + 白底（非反色，手机可扫）",
                  'stroke="#000"' in qr_svg and ('fill="#fff"' in qr_svg or 'fill="#ffffff"' in qr_svg))

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

                # 分页：无 limit 返回全量 + hasMore=false；带 limit 只回最近 N 条 + hasMore
                check("WS 无 limit 返回全量且 hasMore=false",
                      hist.get("hasMore") is False and len(hist.get("records", [])) >= 4)
                await asyncio.sleep(0.35)   # get-history 有每连接 0.3s 限流
                await ws.send(json.dumps({"type": "get-history", "sessionId": sid, "limit": 2}))
                page = json.loads(await ws.recv())
                page_recs = page.get("records", [])
                check("WS get-history limit 分页条数", len(page_recs) == 2)
                check("WS get-history hasMore 标志", page.get("hasMore") is True)
                oldest_idx = page_recs[0]["idx"]
                # beforeIdx 向上翻页：返回 idx 严格小于游标的更早记录
                await asyncio.sleep(0.35)
                await ws.send(json.dumps({"type": "get-history", "sessionId": sid,
                                          "beforeIdx": oldest_idx, "limit": 2}))
                older = json.loads(await ws.recv())
                older_recs = older.get("records", [])
                check("WS get-history beforeIdx 翻页返回更早记录",
                      len(older_recs) > 0 and all(r.get("idx", -1) < oldest_idx for r in older_recs))
                check("WS get-history beforeIdx 翻页 hasMore",
                      older.get("hasMore") is False)  # 更早只剩 2 条，无更多
                # 连接面板设备列表应包含当前 WS 连接
                devs = json.loads(urllib.request.urlopen(
                    base + "/api/devices?token=" + token, timeout=3).read())
                check("HTTP /api/devices 含当前连接",
                      any(d.get("ip") for d in devs.get("devices", [])))
                # 手机端命令列表 API
                try:
                    urllib.request.urlopen(base + "/api/prompts", timeout=3).read()
                    check("HTTP /api/prompts 无 token 被拒", False)
                except urllib.error.HTTPError as e:
                    check("HTTP /api/prompts 无 token 被拒", e.code == 401)
                prompts = json.loads(urllib.request.urlopen(
                    base + "/api/prompts?token=" + token, timeout=3).read())
                check("HTTP /api/prompts 返回列表", isinstance(prompts.get("prompts"), list))

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

                # 4c) send-prompt：CC_ALLOW_INTERACT 未开启 → interaction error（禁用态）
                await ws.send(json.dumps({"type": "send-prompt", "prompt": "你好"}))
                got_disabled = False
                timeout = time.time() + 4
                while time.time() < timeout:
                    try:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                    except Exception:
                        break
                    if msg.get("type") == "interaction" and msg.get("status") == "error":
                        got_disabled = "未开启" in msg.get("message", "")
                        break
                check("WS send-prompt 未开启返回 error", got_disabled)

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

    # 交互链路（权限审批 + 多模态 + 权限模式映射）的独立服务实例 + 断言
    await test_interact()

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


async def _recv_until(ws, pred, timeout=8.0):
    """从 ws 收帧直到 pred(msg) 命中或超时；命中返回该帧，否则 None。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
        except Exception:
            return None
        if pred(msg):
            return msg
    return None


async def test_interact() -> None:
    """远程交互端到端测试：权限审批（allow/deny/未知/超时）、附件校验、权限模式映射。

    起一个 CC_ALLOW_INTERACT=1 + CC_INTERACT_PERMISSION_MODE=default 的临时服务，
    用 CC_CLAUDE_BIN 指向 fake_claude.py 的 .bat 替身（Windows 上 create_subprocess_exec
    经 cmd.exe 执行 .bat，与生产环境 `claude.cmd` 同路径）。
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        session_id = mock_transcript.make_mock_project(root)
        port = free_port()
        token = "interact-token-456"

        # ---- 单元级（无需起服务）：权限模式映射 / 消息行 / 内容块 / 附件校验 ----
        check("_cli_permission_mode 映射 default→manual",
              relay_server._cli_permission_mode("default") == "manual")
        check("_cli_permission_mode 透传 bypassPermissions",
              relay_server._cli_permission_mode("bypassPermissions") == "bypassPermissions")

        line = relay_server._user_message_line(
            "看图", [{"type": "image", "mediaType": "image/png", "data": "QUJD"}])
        uobj = json.loads(line)
        blocks = uobj["message"]["content"]
        check("_user_message_line 含 image 块（base64 source）",
              any(b.get("type") == "image" and b.get("source", {}).get("data") == "QUJD"
                  for b in blocks))
        check("_user_message_line 末尾 text 块", blocks[-1].get("type") == "text"
              and blocks[-1]["text"] == "看图")

        resp = relay_server._control_response_line("req_1", "deny", "no", "s1")
        robj = json.loads(resp)
        check("_control_response_line deny 带 message",
              robj["response"]["response"]["behavior"] == "deny"
              and robj["response"]["response"]["message"] == "no")

        cb = relay_server._content_to_blocks(
            [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                          "data": "QUJD"}}])
        check("_content_to_blocks 图片分支",
              bool(cb) and cb[0].get("type") == "image"
              and cb[0].get("mediaType") == "image/png" and cb[0].get("data") == "QUJD")
        cb2 = relay_server._content_to_blocks(
            [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                             "data": "QUJD"}, "name": "a.pdf"}])
        check("_content_to_blocks document 分支",
              bool(cb2) and cb2[0].get("type") == "document" and cb2[0].get("name") == "a.pdf")

        # 多模态可读性：document 块带 title（文件名）；图片 8 MiB 内进历史、超限降级
        doc_line = relay_server._user_message_line(
            "读文档", [{"type": "document", "mediaType": "application/pdf",
                        "data": "QUJD", "name": "报告.pdf"}])
        doc_obj = json.loads(doc_line)
        doc_blk = next(b for b in doc_obj["message"]["content"] if b.get("type") == "document")
        check("_user_message_line document 块带 title（文件名）",
              doc_blk.get("title") == "报告.pdf")

        cb_img = relay_server._content_to_blocks(
            [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                          "data": "A" * 2_800_000}}])
        check("_content_to_blocks 8MiB 内图片进历史（约 2MiB）",
              bool(cb_img) and cb_img[0].get("type") == "image")

        cb_img_big = relay_server._content_to_blocks(
            [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                          "data": "A" * 12_000_000}}])
        check("_content_to_blocks 超 8MiB 图片降级为占位文本",
              bool(cb_img_big) and cb_img_big[0].get("type") == "text"
              and "图片过大" in cb_img_big[0].get("text", ""))

        cb_doc_big = relay_server._content_to_blocks(
            [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                             "data": "A" * 12_000_000}, "name": "big.pdf"}])
        check("_content_to_blocks 超 8MiB 文档降级为占位文本",
              bool(cb_doc_big) and cb_doc_big[0].get("type") == "text"
              and "文档过大" in cb_doc_big[0].get("text", ""))

        good, err = relay_server._validate_attachments(
            [{"type": "image", "mediaType": "image/png", "data": "QUJD"}], 20 * 1024 * 1024)
        check("_validate_attachments 合法图片", err == "" and len(good) == 1)
        _, err2 = relay_server._validate_attachments(
            [{"type": "video", "mediaType": "video/mp4", "data": "QUJD"}], 20 * 1024 * 1024)
        check("_validate_attachments 拒绝非法类型", err2 != "")
        _, err3 = relay_server._validate_attachments(
            [{"type": "image", "mediaType": "image/png", "data": "not base64!!"}],
            20 * 1024 * 1024)
        check("_validate_attachments 拒绝非法 base64", err3 != "")
        _, err4 = relay_server._validate_attachments(
            [{"type": "image", "mediaType": "image/png", "data": "A" * 100}], 4)
        check("_validate_attachments 拒绝超大", err4 != "")
        _, err5 = relay_server._validate_attachments(
            [{"type": "image", "mediaType": "image/png", "data": "QUJD"} for _ in range(6)],
            20 * 1024 * 1024)
        check("_validate_attachments 拒绝超过 5 个", err5 != "")

        # PermissionBroker 超时自动 deny + 客户端断开 cancel_ws
        broker = relay_server.PermissionBroker(timeout=1)
        q = asyncio.Queue()

        class _FakeWS:
            pass
        fws = _FakeWS()
        broker.register("rid_timeout", fws, q, "s1")
        await asyncio.sleep(1.5)
        to = q.get_nowait()
        check("PermissionBroker 超时自动 deny",
              json.loads(to)["response"]["response"]["behavior"] == "deny")
        broker2 = relay_server.PermissionBroker(timeout=300)
        q2 = asyncio.Queue()
        broker2.register("rid_cancel", fws, q2, "s1")
        broker2.cancel_ws(fws)
        check("PermissionBroker cancel_ws deny 挂起请求",
              json.loads(q2.get_nowait())["response"]["response"]["behavior"] == "deny")

        # ---- 起第二个服务实例（开启远程交互 + 标准审批模式 + fake claude） ----
        fake_py = Path(__file__).resolve().parent / "fake_claude.py"
        marker = root / "marker.txt"
        bat = root / "fake_claude.bat"
        bat.write_text(
            f'@echo off\r\n"{sys.executable}" "{fake_py}" %*\r\n', encoding="utf-8")

        env = dict(os.environ)
        env["CC_TRANSCRIPTS_DIR"] = str(root)
        env["CC_TOKEN"] = token
        env["CC_TUNNEL"] = "none"
        env["CC_PORT"] = str(port)
        env["PYTHONUTF8"] = "1"
        env["CC_AUTO_OPEN"] = "0"
        env["CC_ALLOW_INTERACT"] = "1"
        env["CC_INTERACT_PERMISSION_MODE"] = "default"
        env["CC_CLAUDE_BIN"] = str(bat)
        env["CC_FAKE_MARKER"] = str(marker)
        env["CC_INTERACT_PERMISSION_TIMEOUT"] = "10"
        env["CC_INTERACT_TURN_TIMEOUT"] = "30"
        env["CC_INTERACT_MAX_UPLOAD_MB"] = "1"   # 缩小上限，便于测「超大附件」路径
        env["CC_QR_PNG_PATH"] = str(root / "qr_interact.png")
        # 新建会话默认 cwd（脱离项目目录）——指向临时目录便于断言；fake_claude 启动时回写 os.getcwd()
        default_cwd_dir = root / "default_cwd"
        default_cwd_dir.mkdir()
        cwd_marker = root / "cwd.txt"
        env["CC_DEFAULT_CWD"] = str(default_cwd_dir)
        env["CC_FAKE_CWD_MARKER"] = str(cwd_marker)

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "relay_server.py",
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        log_lines = []

        async def reader():
            async for raw in proc.stdout:
                log_lines.append(raw.decode("utf-8", "replace").rstrip())

        reader_task = asyncio.create_task(reader())
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(40):
                try:
                    urllib.request.urlopen(base + "/healthz", timeout=1)
                    break
                except Exception:
                    await asyncio.sleep(0.25)

            import websockets  # noqa: F401
            from websockets.asyncio.client import connect as ws_connect

            # ---- 场景 A：无附件 send-prompt → started/session/permission-request → allow ----
            async with ws_connect(f"ws://127.0.0.1:{port}/ws?token={token}") as ws:
                await ws.recv()  # hello
                await ws.send(json.dumps({"type": "send-prompt", "prompt": "帮我列出当前目录"}))
                started = await _recv_until(ws, lambda m: m.get("type") == "interaction"
                                            and m.get("status") == "started")
                check("interact 发送后回 started", started is not None)
                sess = await _recv_until(ws, lambda m: m.get("type") == "interaction"
                                         and m.get("status") == "session")
                check("interact 回 session 帧（含 sessionId）",
                      bool(sess and sess.get("sessionId")))
                perm = await _recv_until(ws, lambda m: m.get("type") == "permission-request")
                check("interact 回 permission-request", perm is not None)
                check("permission-request 字段齐全",
                      bool(perm and perm.get("requestId") and perm.get("toolName") == "Bash"))
                await ws.send(json.dumps({"type": "permission-response",
                                          "requestId": perm["requestId"], "behavior": "allow"}))
                fin = await _recv_until(ws, lambda m: m.get("type") == "interaction"
                                        and m.get("status") == "finished", timeout=10)
                check("permission-response allow 后 finished exitCode=0",
                      bool(fin and fin.get("exitCode") == 0))
            check("fake_claude 收到 allow 标记",
                  "allow" in (marker.read_text(encoding="utf-8") if marker.exists() else ""))
            check("新建交互子进程 cwd = CC_DEFAULT_CWD",
                  cwd_marker.exists()
                  and cwd_marker.read_text(encoding="utf-8").strip().lower() == str(default_cwd_dir).lower())

            # ---- 场景 B：deny ----
            marker.write_text("", encoding="utf-8")
            async with ws_connect(f"ws://127.0.0.1:{port}/ws?token={token}") as ws:
                await ws.recv()
                await ws.send(json.dumps({"type": "send-prompt", "prompt": "执行危险命令"}))
                perm = await _recv_until(ws, lambda m: m.get("type") == "permission-request")
                check("deny 场景收到 permission-request", perm is not None)
                await ws.send(json.dumps({"type": "permission-response",
                                          "requestId": perm["requestId"], "behavior": "deny",
                                          "message": "不允许"}))
                fin = await _recv_until(ws, lambda m: m.get("type") == "interaction"
                                        and m.get("status") == "finished", timeout=10)
                check("permission-response deny 后 finished（优雅拒绝）",
                      bool(fin and fin.get("exitCode") == 0))
            check("fake_claude 收到 deny 标记",
                  "deny" in (marker.read_text(encoding="utf-8") if marker.exists() else ""))

            # ---- 场景 C：未知 requestId → permission-response-error ----
            async with ws_connect(f"ws://127.0.0.1:{port}/ws?token={token}") as ws:
                await ws.recv()
                await ws.send(json.dumps({"type": "permission-response",
                                          "requestId": "no-such-id", "behavior": "allow"}))
                err = await _recv_until(ws, lambda m: m.get("type") == "permission-response-error")
                check("未知 requestId 回 permission-response-error", err is not None)

            # ---- 场景 D：非法附件 → interaction error(code=attachment) ----
            async with ws_connect(f"ws://127.0.0.1:{port}/ws?token={token}") as ws:
                await ws.recv()
                await ws.send(json.dumps({"type": "send-prompt", "prompt": "hi",
                                          "attachments": [{"type": "video", "mediaType": "video/mp4",
                                                           "data": "QUJD"}]}))
                err = await _recv_until(ws, lambda m: m.get("type") == "interaction"
                                        and m.get("status") == "error"
                                        and m.get("code") == "attachment")
                check("非法附件类型回 interaction error(code=attachment)", err is not None)

            # ---- 场景 E：超大附件（> CC_INTERACT_MAX_UPLOAD_MB=1MiB）→ 拒绝 ----
            async with ws_connect(f"ws://127.0.0.1:{port}/ws?token={token}") as ws:
                await ws.recv()
                big = "A" * (2 * 1024 * 1024)   # 解码后约 1.5 MiB > 1 MiB 上限
                await ws.send(json.dumps({"type": "send-prompt", "prompt": "hi",
                                          "attachments": [{"type": "image", "mediaType": "image/png",
                                                           "data": big}]}))
                err = await _recv_until(ws, lambda m: m.get("type") == "interaction"
                                        and m.get("status") == "error"
                                        and m.get("code") == "attachment")
                check("超大附件回 interaction error(code=attachment)", err is not None)

            # ---- 场景 F：--resume 会话 cwd = 会话原始 cwd（脱离项目目录） ----
            sid3 = mock_transcript.make_mock_project(root)
            f3 = root / "c--Users-wyxwi-Desktop-Claude-Control" / f"{sid3}.jsonl"
            await asyncio.sleep(2.5)   # 等中继发现并读取该会话（cwd 来自 JSONL 顶层字段）
            old = time.time() - 300
            os.utime(f3, (old, old))   # 把 mtime 调旧让会话进入 idle（非 active/attention）
            async with ws_connect(f"ws://127.0.0.1:{port}/ws?token={token}") as ws:
                await ws.recv()   # hello
                idle_confirmed = False
                end = time.time() + 8
                while time.time() < end:
                    await ws.send(json.dumps({"type": "list-sessions"}))
                    frame = await _recv_until(ws, lambda m: m.get("type") == "sessions")
                    s3 = next((s for s in (frame or {}).get("sessions", []) if s.get("id") == sid3), None)
                    if s3 and s3.get("status") == "idle":
                        idle_confirmed = True
                        break
                    await asyncio.sleep(0.5)
                check("resume 目标会话进入 idle", idle_confirmed)
                # 清掉前几轮 fake_claude 写下的 cwd 标记，确保下面读到的来自本次 resume 子进程
                if cwd_marker.exists():
                    cwd_marker.unlink()
                await ws.send(json.dumps({"type": "send-prompt", "prompt": "恢复会话", "sessionId": sid3}))
                started = await _recv_until(ws, lambda m: m.get("type") == "interaction"
                                            and m.get("status") == "started")
                check("resume 会话回 started", started is not None)
                resume_cwd = ""
                end = time.time() + 6
                while time.time() < end:
                    if cwd_marker.exists():
                        resume_cwd = cwd_marker.read_text(encoding="utf-8").strip()
                        if resume_cwd:
                            break
                    await asyncio.sleep(0.2)
                check("resume 子进程 cwd = 会话原始 cwd",
                      resume_cwd.lower() == mock_transcript.CWD.lower())
                # 优雅结束：允许审批（fake_claude 会吐 control_request）
                perm = await _recv_until(ws, lambda m: m.get("type") == "permission-request")
                if perm:
                    await ws.send(json.dumps({"type": "permission-response",
                                              "requestId": perm["requestId"], "behavior": "allow"}))
        finally:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                proc.kill()
            reader_task.cancel()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
