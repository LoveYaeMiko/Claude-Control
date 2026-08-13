#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude-Control 电脑端中继服务
================================

监控 Claude Code 的会话 transcript（JSONL 文件），解析为结构化消息，
通过 WebSocket 实时广播给手机/浏览器查看端；同端口提供移动端查看页、
连接二维码与健康检查；可选内网穿透（lan / ngrok / cloudflared）。

架构:
    ~/.claude/projects/<cwd-slug>/<session-id>.jsonl
        │ 轮询扫描（默认 1s）+ 增量读取
        ▼
    relay_server.py  normalizer（JSONL → 结构化消息）
        │  WebSocket (token 认证)
        ▼
    手机 / 浏览器查看端（web/ 目录，扫码即用）

用法:
    pip install -r requirements.txt
    复制 .env.example 为 .env 并填写（不填则使用默认值）
    python relay_server.py [--port 9876] [--token xxx] [--tunnel lan]

依赖版本兼容: 需要 websockets >= 12（新旧两代 API 均已做兼容处理）。
"""

import argparse
import asyncio
import hmac
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

APP_NAME = "Claude-Control"
VERSION = "0.1.0"
DEFAULT_PORT = 9876
SCAN_INTERVAL = 1.0          # transcript 轮询间隔（秒）
IDLE_TIMEOUT = 120           # 会话超过 N 秒无写入视为“结束”（供状态展示）
HISTORY_LIMIT = 3000         # 历史消息下发上限 / 每个会话内存中保留的记录数（条）
MAX_SESSIONS = 400           # 同时监控的会话数上限（超出后驱逐已结束会话）

log = logging.getLogger("claude-control")

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:  # pragma: no cover - dotenv 可选
    pass


@dataclass
class Config:
    port: int = DEFAULT_PORT
    host: str = "0.0.0.0"
    token: str = ""
    transcripts_dir: Path = Path.home() / ".claude" / "projects"
    tunnel: str = "lan"          # lan | ngrok | cloudflared | none
    ngrok_auth_token: str = ""
    debug: bool = False

    @classmethod
    def from_env(cls, args: argparse.Namespace) -> "Config":
        def _b(name: str, default: str = "") -> str:
            return os.environ.get(name, "").strip() or default

        cfg = cls(
            port=int(_b("CC_PORT", str(args.port or DEFAULT_PORT))),
            token=_b("CC_TOKEN", args.token or ""),
            transcripts_dir=Path(_b("CC_TRANSCRIPTS_DIR", str(Path.home() / ".claude" / "projects"))),
            tunnel=_b("CC_TUNNEL", args.tunnel or "lan").lower(),
            ngrok_auth_token=_b("CC_NGROK_AUTH_TOKEN", ""),
            debug=_b("CC_DEBUG", "").lower() in ("1", "true", "yes"),
        )
        return cfg


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Claude-Control 远程会话中继服务")
    ap.add_argument("--port", type=int, default=None, help=f"监听端口（默认 {DEFAULT_PORT}）")
    ap.add_argument("--token", default="", help="WebSocket 连接令牌（不填则随机生成并打印）")
    ap.add_argument("--tunnel", default=None, help="内网穿透: lan | ngrok | cloudflared | none")
    ap.add_argument("--debug", action="store_true", help="调试日志")
    return ap.parse_args()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def clean_text(s: str, limit: Optional[int] = None) -> str:
    """去除 ANSI 转义与杂散控制字符。"""
    s = ANSI_RE.sub("", s or "")
    s = "".join(ch for ch in s if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    if limit and len(s) > limit:
        s = s[:limit] + "…"
    return s


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(ts: str) -> Optional[float]:
    """ISO 时间戳 → unix 秒；解析失败返回 None。"""
    if not ts:
        return None
    try:
        s = ts.strip().rstrip("Z")
        if "." in s:
            s = s.split(".", 1)[0]
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def lan_ips() -> List[str]:
    """尽力枚举本机 IPv4 局域网地址。"""
    ips: List[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip not in ips:
            ips.append(ip)
    except Exception:
        pass
    return ips or ["<unknown-ip>"]


# ---------------------------------------------------------------------------
# Transcript 读取与归一化
# ---------------------------------------------------------------------------

# 忽略的顶层记录类型（内部机制，无展示价值）
IGNORED_TYPES = {"file-history-delta", "file-history-snapshot", "file-history-update",
                 "file-history-delete", "queue-operation"}

# 需要审批的工具名（用于“等待审批”状态判定）
PERMISSION_TOOLS = {
    "Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "Read",
    "WebFetch", "WebSearch", "Glob", "Grep", "LS", "TodoWrite",
    "Agent", "Task", "Skill", "MCP",
}


class Session:
    """一个 Claude Code 会话（对应一个 JSONL 文件）的可监控状态。"""

    __slots__ = (
        "id", "file", "cwd", "cwd_slug", "title", "last_prompt", "model",
        "status", "message_count", "size", "mtime", "updated_at",
        "_offset", "_partial", "_pending_tools", "_records", "_pending_tool_name",
        "_next_idx", "_dirty_records",
    )

    def __init__(self, session_id: str, file: Path):
        self.id = session_id
        self.file = file
        self.cwd = ""
        self.cwd_slug = file.parent.name
        self.title = ""
        self.last_prompt = ""
        self.model = ""
        self.status = "active"           # active | attention | idle | ended
        self.message_count = 0
        self.size = 0
        self.mtime = 0.0
        self.updated_at = utc_now_iso()
        self._offset = 0
        self._partial: bytes = b""                  # 未消费的原始字节（半行/跨读多字节）
        self._pending_tools: Dict[str, dict] = {}   # tool_use.id -> block
        self._records: List[dict] = []              # 已归一化的消息记录
        self._pending_tool_name = ""                # 最近一个未完成 tool_use 的工具名
        self._next_idx = 0                          # 单调递增记录序号（截断后不回退）
        self._dirty_records: Dict[int, dict] = {}   # idx -> 本轮被就地更新的记录

    def summary(self) -> dict:
        return {
            "id": self.id,
            "cwd": self.cwd,
            "cwdSlug": self.cwd_slug,
            "title": self.title or self.last_prompt[:60] if self.last_prompt else (self.title or self.id),
            "lastPrompt": self.last_prompt,
            "model": self.model,
            "status": self.status,
            "messageCount": self.message_count,
            "size": self.size,
            "mtime": self.mtime,
            "updatedAt": self.updated_at,
        }

    def history(self) -> List[dict]:
        return list(self._records[-HISTORY_LIMIT:])


def _content_to_blocks(content: Any) -> List[dict]:
    """把 message.content 归一化为前端可渲染的 block 列表。"""
    if isinstance(content, str):
        return [{"type": "text", "text": clean_text(content)}]
    if not isinstance(content, list):
        return []
    blocks: List[dict] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        btype = item.get("type")
        if btype == "text":
            blocks.append({"type": "text", "text": clean_text(item.get("text", ""))})
        elif btype == "thinking":
            blocks.append({"type": "thinking", "thinking": clean_text(item.get("thinking", ""))})
        elif btype == "redacted_thinking":
            blocks.append({"type": "thinking", "thinking": "[已省略的推理过程]"})
        elif btype == "tool_use":
            blocks.append({
                "type": "tool_use",
                "id": item.get("id", ""),
                "name": item.get("name", "tool"),
                "input": item.get("input") or {},
            })
        elif btype == "tool_result":
            # 嵌入式 tool_result（含 tool_use_id），交由后端挂接到 tool_use
            blocks.append({
                "type": "tool_result",
                "toolUseId": item.get("tool_use_id", ""),
                "result": clean_text(str(item.get("content", "")), 8000),
                "isError": bool(item.get("is_error", False)),
            })
        else:
            blocks.append({"type": "text", "text": clean_text(json.dumps(item, ensure_ascii=False)[:500])})
    return blocks


def _collapse_ws(s: str, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", clean_text(s, limit)).strip()


def _read_persisted_output(session: Session, rel_path: str, cap: int = 200_000) -> str:
    """读取外置的大工具输出（tool-results/<id>.txt），读取失败返回空串。"""
    if not rel_path:
        return ""
    candidates = [
        session.file.parent / session.id / rel_path,     # <slug>/<sessionId>/tool-results/...
        session.file.parent / rel_path,                  # <slug>/tool-results/...
    ]
    for cand in candidates:
        try:
            if cand.is_file() and cand.stat().st_size <= cap:
                return clean_text(cand.read_text(encoding="utf-8", errors="replace"), 200_000)
        except OSError:
            continue
    return ""


def _mark_dirty(session: Session, blk: dict) -> None:
    """记录被就地更新（补上 tool_result）的块所属消息记录，供本轮结束后广播更新。"""
    for rec in session._records:
        for b in rec.get("content", []):
            if b is blk:
                session._dirty_records[rec.get("idx", 0)] = rec
                return


def normalize_record(raw: dict, session: Session, idx: int) -> Optional[dict]:
    """一条 transcript JSONL → 归一化消息记录（或 None 表示无需下发）。

    返回结构（与前端 app.js 约定一致）:
      { "kind": "message"|"sys", "sessionId", "idx", "ts",
        "role": "user"|"assistant", "model", "content": [blocks...] }
    """
    rtype = raw.get("type")
    session_id = raw.get("sessionId") or session.id
    ts = parse_iso(raw.get("timestamp")) or session.mtime or time.time()

    if rtype in IGNORED_TYPES:
        return None
    if rtype in ("ai-title",):
        if raw.get("aiTitle"):
            session.title = _collapse_ws(str(raw["aiTitle"]), 120)
        return None
    if rtype == "last-prompt":
        if raw.get("lastPrompt"):
            session.last_prompt = _collapse_ws(str(raw["lastPrompt"]), 500)
        return None
    if rtype == "system":
        subtype = raw.get("subtype", "")
        content = raw.get("content", "")
        if subtype == "compact_boundary":
            return {"kind": "sys", "sessionId": session_id, "idx": idx, "ts": ts,
                    "text": "🗜️ 上下文已压缩（context compaction）"}
        if subtype == "local_command":
            cmd = clean_text(content if isinstance(content, str) else str(content), 300)
            if cmd:
                return {"kind": "sys", "sessionId": session_id, "idx": idx, "ts": ts,
                        "text": f"💻 本地命令：{cmd}"}
        return None
    if rtype == "attachment":
        att = raw.get("attachment") or {}
        name = att.get("file_name") or att.get("path") or ""
        return {"kind": "sys", "sessionId": session_id, "idx": idx, "ts": ts,
                "text": f"📎 附件：{name}"}

    if rtype == "user":
        cwd = raw.get("cwd") or session.cwd
        if cwd:
            session.cwd = cwd
        blocks = _content_to_blocks((raw.get("message") or {}).get("content"))
        # 分离 tool_result 与普通文本
        results = [b for b in blocks if b["type"] == "tool_result"]
        texts = [b for b in blocks if b["type"] != "tool_result"]
        # 外置大输出：顶层 persistedOutputPath 指向 tool-results/<id>.txt
        persisted = raw.get("persistedOutputPath")
        if persisted and results:
            full = _read_persisted_output(session, str(persisted))
            if full:
                for r in results:
                    r["result"] = full
        # 把工具结果挂到对应 tool_use 块
        for r in results:
            tu_id = r.get("toolUseId", "")
            blk = session._pending_tools.pop(tu_id, None)
            if blk is not None:
                blk["result"] = r.get("result", "")
                blk["isError"] = r.get("isError", False)
                _mark_dirty(session, blk)
                if blk.get("name") == session._pending_tool_name:
                    session._pending_tool_name = ""
            else:
                texts.append({"type": "tool_result", "result": r.get("result", ""),
                              "isError": r.get("isError", False)})
        # 纯文本用户输入到来 => 上一工具回合已结束，清除待审批标记
        if texts and not results:
            session._pending_tool_name = ""
        # 顶层 toolUseResult（stdout/stderr）作兜底，挂到最近一个未完成的 tool_use
        if "toolUseResult" in raw and not results:
            last_tool = None
            for blk in reversed(session._records):
                for b in blk.get("content", []):
                    if b.get("type") == "tool_use" and "result" not in b:
                        last_tool = b
                        break
                if last_tool:
                    break
            if last_tool is not None:
                tr = raw.get("toolUseResult")
                if isinstance(tr, dict):
                    out = clean_text(str(tr.get("stdout", "")), 8000)
                    err = clean_text(str(tr.get("stderr", "")), 8000)
                    is_error = bool(tr.get("is_error") or tr.get("exit_code"))
                    last_tool["result"] = (out or err or str(tr))[:8000]
                    last_tool["isError"] = is_error
                    _mark_dirty(session, last_tool)
        if not texts:
            return None
        return {"kind": "message", "sessionId": session_id, "idx": idx, "ts": ts,
                "role": "user", "model": None, "content": texts}

    if rtype == "assistant":
        cwd = raw.get("cwd") or session.cwd
        if cwd:
            session.cwd = cwd
        msg = raw.get("message") or {}
        blocks = _content_to_blocks(msg.get("content"))
        if msg.get("model"):
            session.model = clean_text(str(msg["model"]), 80)
        if blocks:
            session._pending_tools.clear()
        for b in blocks:
            if b.get("type") == "tool_use" and b.get("id"):
                session._pending_tools[b["id"]] = b
                session._pending_tool_name = b.get("name", "")
        return {"kind": "message", "sessionId": session_id, "idx": idx, "ts": ts,
                "role": "assistant", "model": session.model or None,
                "content": blocks}

    return None


# ---------------------------------------------------------------------------
# 监控器
# ---------------------------------------------------------------------------

class TranscriptMonitor:
    """轮询扫描 transcript 目录，增量读取 JSONL，产出归一化消息事件。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sessions: Dict[str, Session] = {}
        self.event_cb = None   # async callable(event_dict)

    def _enumerate(self) -> Dict[Path, float]:
        """返回 {jsonl文件: mtime}，跳过 subagents/ 等子目录。"""
        found: Dict[Path, float] = {}
        root = self.cfg.transcripts_dir
        if not root.exists():
            return found
        for slug_dir in root.iterdir():
            if not slug_dir.is_dir():
                continue
            for f in slug_dir.iterdir():
                if f.suffix == ".jsonl" and f.is_file():
                    # 仅顶层会话文件；subagents/、workflows/ 等子目录跳过
                    try:
                        found[f] = f.stat().st_mtime
                    except OSError:
                        continue
        return found

    async def scan_once(self) -> None:
        found = self._enumerate()
        now = time.time()

        # 1) 新增 / 更新
        for file, mtime in found.items():
            sid = file.stem
            sess = self.sessions.get(sid)
            if sess is None:
                sess = Session(sid, file)
                sess.mtime = mtime
                sess.size = _safe_size(file)
                self.sessions[sid] = sess
                if self.event_cb:
                    await self.event_cb({"type": "session-start", "session": sess.summary()})
            self._read_session(sess)

        # 2) 状态判定：文件消失 → ended；超时无写入 → idle
        for sid, sess in list(self.sessions.items()):
            f = sess.file
            if f not in found:
                if sess.status != "ended":
                    sess.status = "ended"
                    if self.event_cb:
                        await self.event_cb({"type": "session-end", "sessionId": sid})
            elif sess.status != "ended":
                stale = now - (sess.mtime or 0)
                if stale > IDLE_TIMEOUT:
                    new_status = "idle"
                elif sess._pending_tool_name in PERMISSION_TOOLS:
                    new_status = "attention"   # 存在未完成且需审批的工具调用
                else:
                    new_status = "active"
                if new_status != sess.status:
                    sess.status = new_status
                    if self.event_cb:
                        await self.event_cb({"type": "session-update", "session": sess.summary()})

        # 3) 内存上限：会话过多时驱逐已结束的会话
        if len(self.sessions) > MAX_SESSIONS:
            for sid, s in list(self.sessions.items()):
                if s.status == "ended":
                    del self.sessions[sid]
                if len(self.sessions) <= MAX_SESSIONS:
                    break

    def _read_session(self, sess: Session) -> None:
        """增量读取一个会话文件，更新状态与记录。"""
        file = sess.file
        try:
            size = file.stat().st_size
        except OSError:
            return
        if size < sess._offset:
            # 文件被重写/截断：整体重置，避免旧记录与新内容重复
            sess._offset = 0
            sess._partial = b""
            sess._records.clear()
            sess.message_count = 0
            sess._pending_tools.clear()
            sess._pending_tool_name = ""
        sess.mtime = file.stat().st_mtime
        sess._dirty_records.clear()   # 本轮被就地更新的记录（tool_result 合并）

        records: List[dict] = []
        try:
            with open(file, "rb") as fh:
                fh.seek(sess._offset)
                data = fh.read()
        except OSError:
            return
        sess._offset += len(data)

        # 以原始字节缓冲半行，只在完整行边界解码，避免多字节字符被截断破坏
        raw = sess._partial + data
        last_nl = raw.rfind(b"\n")
        if last_nl == -1:
            sess._partial = raw
        else:
            sess._partial = raw[last_nl + 1:]
            text = raw[:last_nl + 1].decode("utf-8", errors="replace")
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except Exception:
                    continue
                rec = normalize_record(parsed, sess, sess._next_idx)
                if rec:
                    sess._records.append(rec)
                    sess._next_idx += 1
                    records.append(rec)
                    if len(sess._records) > HISTORY_LIMIT:
                        del sess._records[:-HISTORY_LIMIT]

        if records or sess._dirty_records:
            sess.message_count = len(sess._records)   # 按记录数重算，防止截断后虚高
            sess.updated_at = utc_now_iso()
            if self.event_cb:
                loop = asyncio.get_running_loop()
                if records:
                    loop.create_task(self.event_cb(
                        {"type": "messages", "sessionId": sess.id, "records": records}))
                if sess._dirty_records:
                    # 已被就地更新（补上 tool_result）的历史记录，需对在线客户端重推
                    loop.create_task(self.event_cb({
                        "type": "messages", "sessionId": sess.id,
                        "isHistory": False, "update": True,
                        "records": list(sess._dirty_records.values()),
                    }))
                    sess._dirty_records.clear()

    async def run(self) -> None:
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("扫描出错")
            await asyncio.sleep(SCAN_INTERVAL)


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# 广播中心
# ---------------------------------------------------------------------------

class Hub:
    def __init__(self) -> None:
        self.clients: set = set()

    def add(self, ws) -> None:
        self.clients.add(ws)

    def remove(self, ws) -> None:
        self.clients.discard(ws)

    async def broadcast(self, msg: dict) -> None:
        if not self.clients:
            return
        text = json.dumps(msg, ensure_ascii=False)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


# ---------------------------------------------------------------------------
# WebSocket 服务
# ---------------------------------------------------------------------------

class WSServer:
    def __init__(self, cfg: Config, monitor: TranscriptMonitor, hub: Hub):
        self.cfg = cfg
        self.monitor = monitor
        self.hub = hub

    async def handle(self, ws) -> None:
        token = self._extract_token(ws)
        if not token or not hmac.compare_digest(token, self.cfg.token):
            log.warning("拒绝无 token 连接: %s", getattr(ws, "remote_address", "?"))
            try:
                await ws.close(code=1008, reason="invalid token")
            except Exception:
                pass
            return
        self.hub.add(ws)
        remote = getattr(ws, "remote_address", "?")
        log.info("客户端连接: %s（在线 %d）", remote, len(self.hub.clients))
        try:
            await ws.send(json.dumps({
                "type": "hello",
                "server": {"name": APP_NAME, "version": VERSION},
                "sessions": [s.summary() for s in self.monitor.sessions.values()],
            }, ensure_ascii=False))
            last_history = 0.0
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mtype = msg.get("type")
                if mtype == "list-sessions":
                    await ws.send(json.dumps({
                        "type": "sessions",
                        "sessions": [s.summary() for s in self.monitor.sessions.values()],
                    }, ensure_ascii=False))
                elif mtype == "get-history":
                    # 简单限流：同一连接短时间内不重复回放，防止放大
                    now = time.monotonic()
                    if now - last_history < 0.3:
                        continue
                    last_history = now
                    sid = msg.get("sessionId")
                    sess = self.monitor.sessions.get(sid)
                    if sess:
                        await ws.send(json.dumps({
                            "type": "messages",
                            "sessionId": sid,
                            "isHistory": True,
                            "records": sess.history(),
                        }, ensure_ascii=False))
                elif mtype == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
        finally:
            self.hub.remove(ws)
            log.info("客户端断开: %s（在线 %d）", remote, len(self.hub.clients))

    @staticmethod
    def _extract_token(ws) -> str:
        req = getattr(ws, "request", None)
        path = getattr(req, "path", None) or ""
        if "?" in path:
            qs = path.split("?", 1)[1]
            for part in qs.split("&"):
                if part.startswith("token="):
                    return unquote(part[6:])
        return ""


# ---------------------------------------------------------------------------
# HTTP 静态服务（与 WebSocket 同端口，由 process_request 接管）
# ---------------------------------------------------------------------------

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

WEB_DIR = Path(__file__).resolve().parent / "web"


def make_http_response(status, body: bytes, content_type: str, extra=None):
    """构造 HTTP 响应，兼容新旧 websockets Response 签名。

    websockets 17+: Response(status_code, reason_phrase, Headers, body)
    websockets <16:  Response(status, headers_dict, body)
    """
    import http as _http

    headers_list = [
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("Connection", "close"),
    ]
    if extra:
        headers_list.extend(extra.items())
    try:
        from websockets.datastructures import Headers
        from websockets.http11 import Response
        st = _http.HTTPStatus(status)
        return Response(st.value, st.phrase, Headers(headers_list), body)
    except Exception:
        # websockets 旧 API（<16）：返回 (status, list[(header,value)], body)
        return (status, headers_list, body)


class HttpRoutes:
    def __init__(self, cfg: Config, monitor: TranscriptMonitor, url_getter: callable):
        self.cfg = cfg
        self.monitor = monitor
        self.url_getter = url_getter   # 同步 () -> str 连接地址（含 token）

    def route(self, path: str, query: str = "") -> Optional[Any]:
        if path == "/healthz":
            body = json.dumps({
                "ok": True, "name": APP_NAME, "version": VERSION,
                "sessions": len(self.monitor.sessions),
            }).encode("utf-8")
            return make_http_response(200, body, "application/json; charset=utf-8")
        if path == "/qrcode":
            # /qrcode 会内嵌访问 token，必须带 token 才允许访问，防止令牌泄露
            if not self._check_token(query):
                return make_http_response(401, b"unauthorized", "text/plain",
                                          {"Cache-Control": "no-store"})
            return self._qrcode()
        if path in ("/", "/index.html"):
            return self._file("/index.html")
        if path.startswith("/") and "." in path:
            return self._file(path)
        return None

    def _check_token(self, query: str) -> bool:
        if not self.cfg.token:
            return False
        for part in query.split("&"):
            if part.startswith("token="):
                return hmac.compare_digest(unquote(part[6:]), self.cfg.token)
        return False

    def _file(self, path: str) -> Optional[Any]:
        name = path.lstrip("/")
        safe = Path(name).name
        f = (WEB_DIR / safe).resolve()
        if not f.is_file() or WEB_DIR not in f.parents:
            return make_http_response(404, b"not found", "text/plain")
        try:
            data = f.read_bytes()
        except OSError:
            return make_http_response(404, b"not found", "text/plain")
        ctype = MIME.get(f.suffix, "application/octet-stream")
        return make_http_response(200, data, ctype,
                                  {"Cache-Control": "no-store"})

    def _qrcode(self) -> Any:
        try:
            url = self.url_getter()
        except Exception:
            url = "http://localhost:9876"
        title = f"Claude-Control — 扫码连接（token 已内嵌）"
        svg = self._qr_svg(url) if _qr_available() else None
        if svg:
            html = (f"<!doctype html><meta charset=utf-8><title>{title}</title>"
                    f"<body style='background:#0e1117;color:#dbe4f0;font-family:sans-serif;"
                    f"display:flex;flex-direction:column;align-items:center;gap:16px;padding-top:32px'>"
                    f"<h2 style='font-size:18px'>{title}</h2>{svg}"
                    f"<p style='font-size:13px;color:#8b98ab;word-break:break-all;max-width:90vw'>{url}</p></body>")
        else:
            html = (f"<!doctype html><meta charset=utf-8><title>{title}</title>"
                    f"<body style='background:#0e1117;color:#dbe4f0;font-family:monospace;padding:32px'>"
                    f"<h2>{title}</h2><p>未安装 segno，无法生成二维码，请直接在手机浏览器打开：</p>"
                    f"<p style='word-break:break-all'>{url}</p></body>")
        return make_http_response(200, html.encode("utf-8"), "text/html; charset=utf-8",
                                  {"Cache-Control": "no-store"})

    @staticmethod
    def _qr_svg(url: str) -> str:
        try:
            import segno  # type: ignore
            qr = segno.make(url, error="m", micro=False)
            # segno>=1.6 的 svg_inline 已内建 xmldecl=False，勿重复传入
            return qr.svg_inline(scale=4, dark="#dbe4f0", light="#0e1117")
        except Exception:
            return ""


def _qr_available() -> bool:
    try:
        import segno  # noqa: F401
        return True
    except Exception:
        return False


def _print_terminal_qr(url: str) -> None:
    """在终端打印可扫描的二维码（ANSI 或 ASCII）。"""
    try:
        import segno  # type: ignore
        qr = segno.make(url, error="m", micro=False)
        print()
        qr.terminal(border=1, compact=True)
        return
    except Exception:
        pass
    try:
        import qrcode  # type: ignore
        qr = qrcode.QRCode(border=1, box_size=2)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii()
        return
    except Exception:
        pass
    print(f"（未安装 segno/qrcode，无法打印二维码）\n连接地址: {url}")


# ---------------------------------------------------------------------------
# 内网穿透
# ---------------------------------------------------------------------------

class Tunnels:
    """管理对外地址，供打印与二维码使用。"""

    def __init__(self, cfg: Config, port: int):
        self.cfg = cfg
        self.port = port
        self.public_urls: List[str] = []
        self._proc: Optional[subprocess.Popen] = None

    async def start(self) -> None:
        mode = self.cfg.tunnel
        if mode == "none":
            return
        if mode == "ngrok":
            self._start_ngrok()
        elif mode == "cloudflared":
            await self._start_cloudflared()
        else:  # lan
            for ip in lan_ips():
                self.public_urls.append(f"http://{ip}:{self.port}")

    def _start_ngrok(self) -> None:
        try:
            from pyngrok import ngrok, conf  # type: ignore
        except Exception as e:
            log.error("ngrok 模式需要 pyngrok：pip install pyngrok（错误：%s）", e)
            return
        conf.get_default().auth_token = self.cfg.ngrok_auth_token
        try:
            tunnel = ngrok.connect(self.port, "http")
            url = tunnel.public_url
            self.public_urls.append(url)
            log.info("ngrok 隧道: %s", url)
        except Exception as e:
            log.error("ngrok 启动失败: %s", e)

    async def _start_cloudflared(self) -> None:
        exe = self._cloudflared_exe()
        cmd = [exe, "tunnel", "--url", f"http://127.0.0.1:{self.port}",
               "--no-autoupdate"]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except FileNotFoundError:
            log.error("未找到 cloudflared，请安装后重试（winget install cloudflare.cloudflared "
                      "或下载 cloudflared.exe 放到 computer/bin/）")
            return
        # 后台异步解析 URL（asyncio 流读取，不会阻塞事件循环）
        asyncio.get_running_loop().create_task(self._consume_cloudflared())

    @staticmethod
    def _cloudflared_exe() -> str:
        # 优先用项目内自带的 bin/cloudflared.exe，其次 PATH 中的 cloudflared
        local = Path(__file__).resolve().parent / "bin" / "cloudflared.exe"
        if local.is_file():
            return str(local)
        return "cloudflared"

    async def _consume_cloudflared(self) -> None:
        if not self._proc or not self._proc.stdout:
            return
        line_re = re.compile(r"(https://[a-z0-9-]+\.trycloudflare\.com)")
        while True:
            raw = await self._proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace")
            m = line_re.search(line)
            if m and m.group(1) not in self.public_urls:
                self.public_urls.append(m.group(1))
                log.info("cloudflared 隧道: %s", m.group(1))
            if line.strip():
                log.debug("[cloudflared] %s", line.strip())

    def stop(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
        try:
            from pyngrok import ngrok
            ngrok.kill()
        except Exception:
            pass

    def primary_url(self) -> str:
        return self.public_urls[0] if self.public_urls else ""


# ---------------------------------------------------------------------------
# 主程序
# ---------------------------------------------------------------------------

def _make_public_url(cfg: Config, tunnels: Tunnels, port: int) -> str:
    """构造可分享的连接 URL（含 token）。纯计算，无需异步。"""
    scheme = "wss" if cfg.tunnel in ("ngrok", "cloudflared") else "ws"
    base = tunnels.primary_url() or f"http://localhost:{port}"
    base = base.replace("http://", f"{scheme}://").replace("https://", f"{scheme}://")
    if "?" in base:
        base = base.split("?", 1)[0]
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}token={cfg.token}"


async def main() -> None:
    # Windows 下重定向输出时默认 ANSI 代码页，中文打印可能 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    args = parse_args()
    cfg = Config.from_env(args)
    if args.debug:
        cfg.debug = True
    if not cfg.token:
        cfg.token = os.urandom(6).hex()[:12]
        log.info("未指定 token，已自动生成：%s", cfg.token)
    if cfg.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not cfg.transcripts_dir.exists():
        log.error("transcript 目录不存在: %s（请确认已运行过 Claude Code）", cfg.transcripts_dir)
        return

    hub = Hub()
    monitor = TranscriptMonitor(cfg)
    tunnels = Tunnels(cfg, cfg.port)
    ws_server = WSServer(cfg, monitor, hub)
    http_routes = HttpRoutes(cfg, monitor, lambda: _make_public_url(cfg, tunnels, cfg.port))

    async def event_cb(evt: dict) -> None:
        await hub.broadcast(evt)

    monitor.event_cb = event_cb

    def process_request(*args):
        # 兼容新旧 websockets API 签名：
        #   新 API: process_request(request) -> Response | None
        #   旧 API: process_request(connection, request) -> (status, headers, body) | None
        request = args[-1] if args else None
        path = getattr(request, "path", "") or ""
        path_only, _, query = path.partition("?")
        if path_only == "/ws":
            return None
        resp = http_routes.route(path_only, query)
        if resp is not None:
            return resp
        return make_http_response(404, b"not found", "text/plain")

    try:
        from websockets.asyncio.server import serve as ws_serve  # websockets >= 12
    except ImportError:
        from websockets.server import serve as ws_serve  # websockets < 12

    host = cfg.host if cfg.tunnel != "none" else "127.0.0.1"

    log.info("=" * 60)
    log.info("%s v%s 启动", APP_NAME, VERSION)
    log.info("监控目录: %s", cfg.transcripts_dir)
    log.info("监听地址: %s:%d", host, cfg.port)

    # 启动前先做一次初始扫描，让 hello 立即带上已有会话
    await monitor.scan_once()

    async with ws_serve(ws_server.handle, host, cfg.port, process_request=process_request,
                        max_size=2**24):
        await tunnels.start()
        # 让隧道任务先跑一会儿，尽量拿到公网地址
        await asyncio.sleep(1.5)
        _public_url = _make_public_url(cfg, tunnels, cfg.port)

        print("\n" + "─" * 60)
        print("  Claude-Control 远程会话查看器")
        print("─" * 60)
        for u in tunnels.public_urls:
            print(f"  连接地址: {u}")
        print(f"  WebSocket: ws://localhost:{cfg.port}/ws?token={cfg.token}")
        print(f"  查看端页面: http://localhost:{cfg.port}/")
        print("─" * 60)
        if cfg.tunnel != "none" and _public_url:
            _print_terminal_qr(_public_url)
        print("  手机与电脑同一局域网时，直接用 LAN 地址即可（无需穿透）。")
        print("  按 Ctrl+C 停止。")

        monitor_task = asyncio.create_task(monitor.run())
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass  # Windows 下部分信号不可用

        try:
            await stop.wait()
        except KeyboardInterrupt:
            pass
        finally:
            monitor_task.cancel()
            tunnels.stop()
            log.info("服务已停止")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
