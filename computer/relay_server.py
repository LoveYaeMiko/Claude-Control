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
import shutil
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

APP_NAME = "Claude-Control"
VERSION = "0.3.1"
DEFAULT_PORT = 9876
SCAN_INTERVAL = 1.0          # transcript 轮询间隔（秒）
IDLE_TIMEOUT = 120           # 会话超过 N 秒无写入视为“结束”（供状态展示）
HISTORY_LIMIT = 3000         # 历史消息下发上限 / 每个会话内存中保留的记录数（条）
HISTORY_PAGE = 50            # get-history 单页下发的默认记录数（手机端分页加载，避免一次拉全量）
MAX_SESSIONS = 400           # 同时监控的会话数上限（超出后驱逐已结束会话）
THINKING_LIMIT = 4000        # 单个 thinking 块下发的最大字符数（截断推理过程，控制 get-history 体积）
TEXT_LIMIT = 16000           # 单个 text 块下发的最大字符数（截断超长正文，控制 get-history 体积）
MAX_UPLOAD_BYTES = 20 * 1024 * 1024   # 单个附件解码后最大字节数（CC_INTERACT_MAX_UPLOAD_MB 可调）
MAX_ATTACHMENTS = 5                   # 单条 send-prompt 允许的最大附件数
MAX_ATTACHMENT_HISTORY_BYTES = 1 * 1024 * 1024  # 历史回放中内嵌图片 base64 的解码上限
PROJECT_ROOT = Path(__file__).resolve().parent.parent   # 项目根目录（Claude-Control/）

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
    allow_interact: bool = False                 # 手机端远程交互开关（CC_ALLOW_INTERACT=1）
    interact_permission_mode: str = "bypassPermissions"  # 交互权限模式（CC_INTERACT_PERMISSION_MODE）
    interact_model: str = ""                     # 交互所用模型（CC_INTERACT_MODEL，空=默认）
    interact_max_upload_mb: int = 20             # 单个附件解码后最大兆数（CC_INTERACT_MAX_UPLOAD_MB）
    interact_permission_timeout: int = 300       # 单次权限审批超时秒数（CC_INTERACT_PERMISSION_TIMEOUT）
    interact_turn_timeout: int = 600             # 单轮交互整体超时秒数（CC_INTERACT_TURN_TIMEOUT）
    claude_bin: str = "claude"                   # claude CLI 路径（CC_CLAUDE_BIN，测试注入替身）
    auto_open: bool = True                       # 启动时自动打开连接面板（CC_AUTO_OPEN=0 关闭）
    qr_png_path: Path = PROJECT_ROOT / "连接二维码.png"  # 启动生成的二维码 PNG 位置（CC_QR_PNG_PATH）
    email_to: str = "3555452607@qq.com"          # 邮件收件人（CC_EMAIL_TO）
    email_user: str = "3555452607@qq.com"        # SMTP 发件账号（CC_EMAIL_USER，QQ 邮箱即收件人）
    email_auth_code: str = ""                    # SMTP 授权码（CC_EMAIL_AUTH_CODE，QQ 邮箱需开通 SMTP 后获取）
    email_smtp_host: str = "smtp.qq.com"         # SMTP 服务器（CC_EMAIL_SMTP_HOST）
    email_smtp_port: int = 465                   # SMTP 端口（CC_EMAIL_SMTP_PORT，QQ 用 465/SSL）
    email_from_name: str = "Claude-Control"      # 发件人显示名（CC_EMAIL_FROM_NAME）

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
            allow_interact=_b("CC_ALLOW_INTERACT", "").lower() in ("1", "true", "yes"),
            interact_permission_mode=_b("CC_INTERACT_PERMISSION_MODE", "bypassPermissions"),
            interact_model=_b("CC_INTERACT_MODEL", ""),
            interact_max_upload_mb=int(_b("CC_INTERACT_MAX_UPLOAD_MB", "20") or "20"),
            interact_permission_timeout=int(_b("CC_INTERACT_PERMISSION_TIMEOUT", "300") or "300"),
            interact_turn_timeout=int(_b("CC_INTERACT_TURN_TIMEOUT", "600") or "600"),
            claude_bin=_b("CC_CLAUDE_BIN", "claude"),
            auto_open=_b("CC_AUTO_OPEN", "1").lower() in ("1", "true", "yes"),
            qr_png_path=Path(_b("CC_QR_PNG_PATH", str(PROJECT_ROOT / "连接二维码.png"))),
            email_to=_b("CC_EMAIL_TO", "3555452607@qq.com"),
            email_user=_b("CC_EMAIL_USER", "3555452607@qq.com"),
            email_auth_code=_b("CC_EMAIL_AUTH_CODE", ""),
            email_smtp_host=_b("CC_EMAIL_SMTP_HOST", "smtp.qq.com"),
            email_smtp_port=int(_b("CC_EMAIL_SMTP_PORT", "465")),
            email_from_name=_b("CC_EMAIL_FROM_NAME", "Claude-Control"),
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

# 交互权限模式的 CLI 拼写映射：内部约定用 "default" 表示“标准审批模式”，
# 而 claude CLI 的 --permission-mode 把它拼作 "manual"（二者等价）。
PERMISSION_MODE_ALIASES = {"default": "manual"}


def _cli_permission_mode(mode: str) -> str:
    """把内部权限模式名翻译成 claude CLI 接受的 --permission-mode 值。"""
    return PERMISSION_MODE_ALIASES.get(mode, mode)


# 多模态附件允许的 MIME 类型白名单
ATTACHMENT_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
ATTACHMENT_DOC_TYPES = {"application/pdf"}


def _validate_attachments(raw: Any, max_bytes: int) -> tuple:
    """校验并清理 send-prompt 的附件列表。

    返回 (clean_attachments, error)：error 非空表示校验失败（整条 prompt 应被拒绝）。
    每个附件形如 {"type": "image"|"document", "mediaType": "...", "data": "<base64>"}。
    """
    if raw is None:
        return [], ""
    if not isinstance(raw, list):
        return [], "附件格式错误"
    if len(raw) > MAX_ATTACHMENTS:
        return [], f"附件数量超过上限（最多 {MAX_ATTACHMENTS} 个）"
    clean: List[dict] = []
    for a in raw:
        if not isinstance(a, dict):
            return [], "附件格式错误"
        atype = str(a.get("type", "") or "")
        media = str(a.get("mediaType", "") or "").lower()
        data = "".join(str(a.get("data", "") or "").split())  # 去空白/换行
        if atype == "image" and media in ATTACHMENT_IMAGE_TYPES:
            pass
        elif atype == "document" and media in ATTACHMENT_DOC_TYPES:
            pass
        else:
            return [], f"不支持的附件类型：{media or atype}"
        if not data or not re.fullmatch(r"[A-Za-z0-9+/=]+", data):
            return [], "附件数据为空或非法 base64"
        if (len(data) * 3) // 4 > max_bytes:
            return [], f"附件过大（超过 {max_bytes // (1024 * 1024)} MiB）"
        clean.append({"type": atype, "mediaType": media, "data": data})
    return clean, ""


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

    def history_page(self, before_idx: Optional[int], limit: int) -> tuple:
        """分页返回历史记录（idx 升序）。

        - before_idx 为 None：返回最新 limit 条（最近一页）。
        - before_idx 给定：返回 idx < before_idx 的最近 limit 条（向上翻页）。
        返回 (records, has_more)：has_more 表示是否还有更早的记录可继续加载。
        """
        records = self._records            # 已截断到 HISTORY_LIMIT，idx 严格升序
        if before_idx is None:
            page = records[-limit:]
            return page, len(records) > limit
        older = [r for r in records if (r.get("idx") or -1) < before_idx]
        page = older[-limit:]
        return page, len(older) > limit


def _content_to_blocks(content: Any) -> List[dict]:
    """把 message.content 归一化为前端可渲染的 block 列表。"""
    if isinstance(content, str):
        return [{"type": "text", "text": clean_text(content, TEXT_LIMIT)}]
    if not isinstance(content, list):
        return []
    blocks: List[dict] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        btype = item.get("type")
        if btype == "text":
            blocks.append({"type": "text", "text": clean_text(item.get("text", ""), TEXT_LIMIT)})
        elif btype == "thinking":
            blocks.append({"type": "thinking", "thinking": clean_text(item.get("thinking", ""), THINKING_LIMIT)})
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
        elif btype == "image":
            src = item.get("source") or {}
            data = (src.get("data") or "").replace("\n", "").replace("\r", "")
            try:
                decoded_len = (len(data) * 3) // 4
            except Exception:
                decoded_len = 0
            if decoded_len <= MAX_ATTACHMENT_HISTORY_BYTES:
                blocks.append({"type": "image", "mediaType": src.get("media_type", "image/png"),
                               "data": data})
            else:
                blocks.append({"type": "text", "text": "🖼️ 图片过大，未在历史中传输"})
        elif btype == "document":
            src = item.get("source") or {}
            blocks.append({
                "type": "document",
                "mediaType": src.get("media_type", "application/pdf"),
                "name": item.get("name") or item.get("file_name", ""),
                "data": (src.get("data") or "").replace("\n", "").replace("\r", ""),
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

def _device_kind(user_agent: str) -> str:
    """根据 User-Agent 判断连接设备类型（安卓 App / 网页 / 其他）。"""
    ua = (user_agent or "").lower()
    if "okhttp" in ua or "dalvik" in ua:
        return "android"
    if "mozilla" in ua:
        return "web"
    return "other"


class WSServer:
    def __init__(self, cfg: Config, monitor: TranscriptMonitor, hub: Hub):
        self.cfg = cfg
        self.monitor = monitor
        self.hub = hub
        self.broker = PermissionBroker(cfg.interact_permission_timeout)
        self.interact = InteractRunner(cfg, self.broker)
        self.devices: Dict[int, dict] = {}  # id(ws) -> {ip, kind, connected_at}
        self.prompts: List[dict] = []       # 最近手机端 send-prompt 命令（供连接面板展示）
        self._prompt_seq = 0

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
        ip = remote[0] if isinstance(remote, tuple) else str(remote)
        ua = ""
        try:
            ua = getattr(ws, "request", None).headers.get("User-Agent", "") or ""
        except Exception:
            ua = ""
        self.devices[id(ws)] = {"ip": ip, "kind": _device_kind(ua), "connected_at": time.time()}
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
                        limit = msg.get("limit")
                        if limit is None:
                            # 兼容旧客户端（网页查看器）：不带 limit 返回全部历史
                            records = sess.history()
                            has_more = False
                        else:
                            try:
                                limit = max(1, min(int(limit), HISTORY_LIMIT))
                            except (TypeError, ValueError):
                                limit = HISTORY_PAGE
                            before = msg.get("beforeIdx")
                            try:
                                before = int(before) if before is not None else None
                            except (TypeError, ValueError):
                                before = None
                            records, has_more = sess.history_page(before, limit)
                        oldest_idx = records[0].get("idx") if records else None
                        await ws.send(json.dumps({
                            "type": "messages",
                            "sessionId": sid,
                            "isHistory": True,
                            "hasMore": has_more,
                            "oldestIdx": oldest_idx,
                            "records": records,
                        }, ensure_ascii=False))
                elif mtype == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                elif mtype == "send-prompt":
                    await self._handle_send_prompt(ws, msg)
                elif mtype == "permission-response":
                    ok = self.broker.respond(
                        str(msg.get("requestId", "") or ""),
                        str(msg.get("behavior", "deny")),
                        str(msg.get("message", "") or ""))
                    if not ok:
                        await ws.send(json.dumps({
                            "type": "permission-response-error",
                            "requestId": msg.get("requestId", ""),
                            "message": "未知或已过期的审批请求",
                        }, ensure_ascii=False))
        finally:
            self.interact.broker.cancel_ws(ws)
            self.hub.remove(ws)
            self.devices.pop(id(ws), None)
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

    def list_devices(self) -> List[dict]:
        """返回当前已连接的设备列表（供连接面板展示）。"""
        now = time.time()
        return [
            {"id": str(did), "ip": d["ip"], "kind": d["kind"],
             "connected_at": int(d["connected_at"]),
             "connected_sec": int(now - d["connected_at"])}
            for did, d in self.devices.items()
        ]

    def list_prompts(self) -> List[dict]:
        """返回最近手机端 send-prompt 命令记录（供连接面板展示）。"""
        return list(self.prompts)

    async def _handle_send_prompt(self, ws, msg: dict) -> None:
        """手机端远程交互入口：校验开关与附件后异步起 claude -p。"""
        if not self.cfg.allow_interact:
            await ws.send(json.dumps({
                "type": "interaction", "status": "error",
                "message": "远程交互未开启（需 CC_ALLOW_INTERACT=1）",
            }, ensure_ascii=False))
            return
        prompt = str(msg.get("prompt", "") or "").strip()
        max_bytes = max(1, self.cfg.interact_max_upload_mb) * 1024 * 1024
        attachments, att_err = _validate_attachments(msg.get("attachments"), max_bytes)
        if att_err:
            await ws.send(json.dumps({
                "type": "interaction", "status": "error",
                "code": "attachment", "message": att_err,
            }, ensure_ascii=False))
            return
        if not prompt and not attachments:
            return
        # 会话 id 仅保留 UUID 安全字符，防止 shell 注入
        session_id = "".join(ch for ch in str(msg.get("sessionId", "") or "")
                             if ch.isalnum() or ch in "-_.")
        # 目标会话仍在运行（active/attention）时，--resume 会等待会话锁而死锁挂起，
        # 导致手机端命令卡死、无任何回显。这种情况改为开新会话。
        if session_id:
            sess = self.monitor.sessions.get(session_id)
            if sess is not None and sess.status in ("active", "attention"):
                log.info("send-prompt: 目标会话 %s 仍在运行，改为开新会话", session_id)
                session_id = ""
        self._prompt_seq += 1
        rec = {
            "id": self._prompt_seq,
            "prompt": prompt,
            "sessionId": session_id or "",
            "status": "排队中",
            "time": utc_now_iso(),
            # 只存附件元信息，绝不存 base64（避免面板/日志泄露大体积数据）
            "attachments": [{"type": a["type"], "mediaType": a["mediaType"]}
                            for a in attachments],
        }
        self.prompts.insert(0, rec)
        del self.prompts[200:]  # 最多保留最近 200 条
        asyncio.create_task(self.interact.run(ws, prompt, session_id or None, attachments, rec))


# ---------------------------------------------------------------------------
# 远程交互：调用 claude -p 无头子进程
# ---------------------------------------------------------------------------

def _user_message_line(prompt: str, attachments: List[dict]) -> str:
    """构造 stream-json 的用户消息行（含可选 image/document 附件块）。"""
    content: List[dict] = []
    for a in attachments or []:
        atype = a.get("type")
        src = {"type": "base64", "media_type": a.get("mediaType", ""),
               "data": a.get("data", "")}
        if atype == "image":
            content.append({"type": "image", "source": src})
        elif atype == "document":
            content.append({"type": "document", "source": src})
    if prompt:
        content.append({"type": "text", "text": prompt})
    return json.dumps({"type": "user",
                       "message": {"role": "user", "content": content}},
                      ensure_ascii=False) + "\n"


def _control_response_line(request_id: str, behavior: str, message: str,
                           session_id: str) -> str:
    """构造回写给 claude stdin 的 control_response 行（allow / deny）。"""
    resp = {"subtype": "success", "request_id": request_id,
            "response": {"behavior": behavior}}
    if behavior == "deny":
        resp["response"]["message"] = message or "Denied by user"
    return json.dumps({"type": "control_response", "response": resp,
                       "session_id": session_id}, ensure_ascii=False) + "\n"


class PermissionBroker:
    """把 claude 的 can_use_tool 审批请求登记到发起方连接，并回写允许/拒绝结果。

    request_id → {ws, write_q, session_id, timeout_task}；一次审批只回写一次，
    未知名返回 False。客户端断开时取消其所有挂起请求（自动 deny），避免 claude 挂起。
    """

    def __init__(self, timeout: int = 300):
        self.timeout = timeout
        self._pending: Dict[str, dict] = {}

    def register(self, request_id: str, ws, write_q, session_id: str) -> None:
        if not request_id or request_id in self._pending:
            return
        entry = {"ws": ws, "write_q": write_q, "session_id": session_id}
        entry["timeout_task"] = asyncio.get_running_loop().create_task(
            self._timeout(request_id))
        self._pending[request_id] = entry

    def respond(self, request_id: str, behavior: str, message: str = "") -> bool:
        entry = self._pending.pop(request_id, None)
        if entry is None:
            return False
        task = entry.get("timeout_task")
        if task is not None:
            task.cancel()
        entry["write_q"].put_nowait(_control_response_line(
            request_id, behavior, message, entry["session_id"]))
        return True

    def cancel_ws(self, ws) -> None:
        for rid in [r for r, e in self._pending.items() if e["ws"] is ws]:
            self.respond(rid, "deny", "连接已断开")

    async def _timeout(self, request_id: str) -> None:
        try:
            await asyncio.sleep(self.timeout)
        except asyncio.CancelledError:
            return
        self.respond(request_id, "deny", "审批超时，已自动拒绝")


class InteractRunner:
    """调用 `claude -p` 无头子进程执行手机端发来的 prompt（双向 stream-json）。

    响应内容不在此转发 —— 子进程会把会话写入 ~/.claude/projects，
    由 TranscriptMonitor 自动发现并广播给所有查看端（复用现有链路）。
    这里负责：起进程、写用户消息（含附件）、解析 stdout 事件、把 can_use_tool
    审批请求转给手机端并经 broker 回写允许/拒绝、上报状态与完成/失败。
    """

    MAX_CONCURRENT = 4      # 全局并发交互上限
    RATE_INTERVAL = 2.0     # 每连接两次交互最小间隔（秒）

    def __init__(self, cfg: Config, broker: PermissionBroker):
        self.cfg = cfg
        self.broker = broker
        self._sem = asyncio.Semaphore(self.MAX_CONCURRENT)
        self._last: Dict[int, float] = {}

    def _resolve_claude_bin(self) -> str:
        """把 claude 命令名解析为可执行路径。

        Windows 上 claude 是 npm 安装的 .cmd shim，裸命令名无法被
        create_subprocess_exec 经 PATH 找到（CreateProcess 只补 .exe 后缀，
        报 [WinError 2]）。这里用 shutil.which（按 PATHEXT 含 .CMD/.BAT）
        解析为全路径，让 subprocess 走 .cmd/.bat 经 cmd.exe 执行的分支。
        """
        bin_path = self.cfg.claude_bin
        if os.sep in bin_path or "/" in bin_path or os.path.isabs(bin_path):
            return bin_path
        return shutil.which(bin_path) or bin_path

    def _command(self, session_id: Optional[str]) -> List[str]:
        """构造固定命令（列表，不含任何用户输入；prompt 走 stdin 传入）。"""
        parts = [self._resolve_claude_bin(), "-p", "--verbose",
                 "--input-format", "stream-json",
                 "--output-format", "stream-json",
                 "--permission-mode", _cli_permission_mode(self.cfg.interact_permission_mode)]
        if self.cfg.interact_permission_mode == "bypassPermissions":
            parts.append("--dangerously-skip-permissions")
        if session_id:
            parts += ["--resume", session_id]
        if self.cfg.interact_model:
            parts += ["--model", self.cfg.interact_model]
        return parts

    async def run(self, ws, prompt: str, session_id: Optional[str],
                  attachments: Optional[List[dict]] = None,
                  rec: Optional[dict] = None) -> None:
        async def send(obj: dict) -> None:
            try:
                await ws.send(json.dumps(obj, ensure_ascii=False))
            except Exception:
                pass

        def set_status(status: str, **kw: Any) -> None:
            if rec is not None:
                rec["status"] = status
                rec.update(kw)

        key = id(ws)
        now = time.monotonic()
        if now - self._last.get(key, 0.0) < self.RATE_INTERVAL:
            set_status("失败", message="发送过快，请稍候")
            await send({"type": "interaction", "status": "error",
                        "message": "发送过快，请稍候"})
            return
        self._last[key] = now

        async with self._sem:
            set_status("执行中")
            await send({"type": "interaction", "status": "started",
                        "sessionId": session_id or ""})
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self._command(session_id),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except Exception as exc:
                hint = ""
                if isinstance(exc, FileNotFoundError) or "WinError 2" in str(exc):
                    hint = "（请确认已安装 Claude Code 且 claude 在 PATH 中，或用 CC_CLAUDE_BIN 指定完整路径）"
                set_status("失败", message=f"无法启动 claude：{exc}{hint}")
                await send({"type": "interaction", "status": "error",
                            "code": "launch", "message": f"无法启动 claude：{exc}{hint}"})
                return

            # stdin 单写者：初始消息 / control_response 都经此队列串行写入，避免竞态
            write_q: asyncio.Queue = asyncio.Queue()
            found_session = session_id or ""
            stderr_tail: List[str] = []

            async def writer() -> None:
                try:
                    while True:
                        item = await write_q.get()
                        if item is None:
                            break
                        proc.stdin.write(item.encode("utf-8"))
                        await proc.stdin.drain()
                except Exception:
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass

            async def drain_stderr() -> None:
                async for raw in proc.stderr:
                    s = raw.decode("utf-8", "replace").strip()
                    if s:
                        stderr_tail.append(s)
                        if len(stderr_tail) > 8:
                            stderr_tail.pop(0)

            async def pump() -> None:
                """解析 stdout 全部事件直到 EOF（不提前退出）。"""
                nonlocal found_session
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    t = obj.get("type")
                    if t == "system" and obj.get("subtype") == "init":
                        sid = obj.get("session_id")
                        if sid:
                            found_session = str(sid)
                            set_status("执行中", sessionId=found_session)
                            await send({"type": "interaction", "status": "session",
                                        "sessionId": found_session})
                    elif t == "control_request":
                        req = obj.get("request") or {}
                        if req.get("subtype") == "can_use_tool":
                            rid = str(req.get("request_id") or req.get("id") or "")
                            sid = str(obj.get("session_id") or found_session or "")
                            self.broker.register(rid, ws, write_q, sid)
                            set_status("等待审批", sessionId=sid)
                            await send({
                                "type": "permission-request",
                                "requestId": rid,
                                "sessionId": sid,
                                "toolName": req.get("tool_name") or req.get("name") or "tool",
                                "displayName": req.get("display_name") or "",
                                "description": clean_text(str(req.get("description", "")), 1000),
                                "input": json.dumps(req.get("input") or {}, ensure_ascii=False)[:4000],
                            })
                    elif t == "result":
                        # 单轮结果已出，关闭 stdin 让 print 模式进程退出
                        write_q.put_nowait(None)

            writer_task = asyncio.create_task(writer())
            stderr_task = asyncio.create_task(drain_stderr())
            await write_q.put(_user_message_line(prompt, attachments or []))

            timed_out = False
            try:
                try:
                    await asyncio.wait_for(pump(), timeout=self.cfg.interact_turn_timeout)
                except asyncio.TimeoutError:
                    timed_out = True
                    log.warning("交互超时（%ss），终止 claude", self.cfg.interact_turn_timeout)
                    try:
                        proc.kill()
                    except Exception:
                        pass
                except Exception:
                    log.exception("读取 claude 输出出错")
            finally:
                write_q.put_nowait(None)
                try:
                    await asyncio.wait_for(writer_task, 5)
                except Exception:
                    writer_task.cancel()
                await stderr_task

            code = await proc.wait()

            if timed_out:
                set_status("失败", sessionId=found_session, message="交互超时")
                await send({"type": "interaction", "status": "error",
                            "code": "timeout", "sessionId": found_session,
                            "message": "交互超时"})
            elif code == 0:
                set_status("完成", sessionId=found_session, exitCode=0)
                await send({"type": "interaction", "status": "finished",
                            "sessionId": found_session, "exitCode": 0})
            else:
                tail = "\n".join(stderr_tail)[:500]
                set_status("失败", sessionId=found_session, exitCode=code,
                           message=tail or f"claude 退出码 {code}")
                await send({"type": "interaction", "status": "error",
                            "sessionId": found_session, "exitCode": code,
                            "message": tail or f"claude 退出码 {code}"})
            log.info("交互结束: exit=%s session=%s", code, found_session)


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
    def __init__(self, cfg: Config, monitor: TranscriptMonitor, url_getter: callable,
                 config_uri_getter: callable = None, ws_url_getter: callable = None,
                 devices_getter: callable = None, prompts_getter: callable = None):
        self.cfg = cfg
        self.monitor = monitor
        self.url_getter = url_getter          # () -> str 网页查看器地址（含 token）
        self.config_uri_getter = config_uri_getter or url_getter  # () -> str claudecontrol URI
        self.ws_url_getter = ws_url_getter    # () -> str 原始 WS 地址（不含 token）
        self.devices_getter = devices_getter  # () -> list[dict] 已连接设备
        self.prompts_getter = prompts_getter  # () -> list[dict] 最近 send-prompt 命令

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
        if path == "/dashboard":
            return self._file("/dashboard.html")
        if path == "/api/info":
            if not self._check_token(query):
                return make_http_response(401, b"unauthorized", "text/plain",
                                          {"Cache-Control": "no-store"})
            return self._api_info()
        if path == "/api/devices":
            if not self._check_token(query):
                return make_http_response(401, b"unauthorized", "text/plain",
                                          {"Cache-Control": "no-store"})
            return self._api_devices()
        if path == "/api/prompts":
            if not self._check_token(query):
                return make_http_response(401, b"unauthorized", "text/plain",
                                          {"Cache-Control": "no-store"})
            return self._api_prompts()
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
            config_uri = self.config_uri_getter()
        except Exception:
            config_uri = ""
        try:
            web_url = self.url_getter()
        except Exception:
            web_url = "http://localhost:9876"
        title = "Claude-Control — 扫码连接（token 已内嵌）"
        qr_target = config_uri or web_url
        svg = self._qr_svg(qr_target) if _qr_available() else None
        if svg:
            html = (f"<!doctype html><meta charset=utf-8><title>{title}</title>"
                    f"<body style='background:#0e1117;color:#dbe4f0;font-family:sans-serif;"
                    f"display:flex;flex-direction:column;align-items:center;gap:16px;padding-top:32px'>"
                    f"<h2 style='font-size:18px'>{title}</h2>{svg}"
                    f"<p style='font-size:13px;color:#8b98ab;max-width:90vw'>"
                    f"安卓 App 扫码自动配置：<br>"
                    f"<span style='word-break:break-all'>{config_uri}</span></p>"
                    f"<p style='font-size:13px;color:#8b98ab;word-break:break-all;max-width:90vw'>"
                    f"网页查看器：{web_url}</p></body>")
        else:
            html = (f"<!doctype html><meta charset=utf-8><title>{title}</title>"
                    f"<body style='background:#0e1117;color:#dbe4f0;font-family:monospace;padding:32px'>"
                    f"<h2>{title}</h2><p>未安装 segno，无法生成二维码，请直接打开：</p>"
                    f"<p style='word-break:break-all'>安卓配置：{config_uri}</p>"
                    f"<p style='word-break:break-all'>网页查看器：{web_url}</p></body>")
        return make_http_response(200, html.encode("utf-8"), "text/html; charset=utf-8",
                                  {"Cache-Control": "no-store"})

    def _api_info(self) -> Any:
        cfg = self.cfg
        try:
            config_uri = self.config_uri_getter()
        except Exception:
            config_uri = ""
        try:
            web_url = self.url_getter()
        except Exception:
            web_url = ""
        ws_url = ""
        try:
            ws_url = self.ws_url_getter() if self.ws_url_getter else ""
        except Exception:
            ws_url = ""
        lan_ws = ""
        try:
            ips = lan_ips()
            if ips:
                lan_ws = f"ws://{ips[0]}:{cfg.port}/ws"
        except Exception:
            pass
        qr = self._qr_svg(config_uri) if (config_uri and _qr_available()) else ""
        body = json.dumps({
            "token": cfg.token,
            "configUri": config_uri,
            "webUrl": web_url,
            "wsUrl": ws_url,
            "lanWs": lan_ws,
            "qrSvg": qr,
            "allowInteract": cfg.allow_interact,
            "interactPermissionMode": cfg.interact_permission_mode,
            "tunnel": cfg.tunnel,
            "port": cfg.port,
            "version": VERSION,
        }, ensure_ascii=False).encode("utf-8")
        return make_http_response(200, body, "application/json; charset=utf-8",
                                  {"Cache-Control": "no-store"})

    def _api_devices(self) -> Any:
        try:
            devices = self.devices_getter() if self.devices_getter else []
        except Exception:
            devices = []
        body = json.dumps({"devices": devices}, ensure_ascii=False).encode("utf-8")
        return make_http_response(200, body, "application/json; charset=utf-8",
                                  {"Cache-Control": "no-store"})

    def _api_prompts(self) -> Any:
        try:
            prompts = self.prompts_getter() if self.prompts_getter else []
        except Exception:
            prompts = []
        body = json.dumps({"prompts": prompts}, ensure_ascii=False).encode("utf-8")
        return make_http_response(200, body, "application/json; charset=utf-8",
                                  {"Cache-Control": "no-store"})

    @staticmethod
    def _qr_svg(url: str) -> str:
        try:
            import segno  # type: ignore
            qr = segno.make(url, error="m", micro=False)
            # segno>=1.6 的 svg_inline 已内建 xmldecl=False，勿重复传入
            # 必须「深色模块 + 白底」：反色（浅色模块+深底）二维码多数手机扫描器无法识别
            return qr.svg_inline(scale=4, dark="#000000", light="#ffffff")
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


def _make_base_url(cfg: Config, tunnels: Tunnels, port: int) -> str:
    """构造基础连接地址（不含 /ws 路径）：优先公网隧道主机，其次局域网 IP。

    App 端 WebSocketClient 会自行拼 /ws，因此配置 URI 的 url 必须是基础地址；
    否则旧版 App（v0.2.0 无条件拼 /ws）会拼出 /ws/ws 导致扫码连接 404。
    """
    base = tunnels.primary_url()
    if base:
        host = base.split("//", 1)[-1].split("/", 1)[0]
        return f"wss://{host}"
    return f"ws://{lan_ips()[0]}:{port}"


def _make_ws_url(cfg: Config, tunnels: Tunnels, port: int) -> str:
    """构造 WebSocket 连接地址（含 /ws 路径），供面板/复制展示。"""
    return f"{_make_base_url(cfg, tunnels, port)}/ws"


def _make_config_uri(cfg: Config, tunnels: Tunnels, port: int) -> str:
    """构造安卓 App 配置深链：claudecontrol://connect?url=...&token=...

    url 用基础地址（不含 /ws），兼容 v0.2.0（直接拼 /ws）与 v0.2.1（剥 /ws 再拼）。
    """
    base = _make_base_url(cfg, tunnels, port)
    return (f"claudecontrol://connect?url={quote(base, safe='')}"
            f"&token={quote(cfg.token, safe='')}")


def _open_dashboard(port: int, token: str) -> None:
    """启动后在默认浏览器打开本机连接面板（地址/token/二维码/设备）。"""
    try:
        url = f"http://127.0.0.1:{port}/dashboard"
        if token:
            url += f"?token={quote(token, safe='')}"
        webbrowser.open(url, new=2)
        log.info("已打开连接面板: http://127.0.0.1:%d/dashboard", port)
    except Exception as exc:  # pragma: no cover - 无浏览器环境
        log.warning("自动打开连接面板失败: %s", exc)


def _save_qr_png(config_uri: str, path: Path) -> str:
    """把配置二维码存成 PNG（深色模块 + 白底），返回路径；失败返回空串。"""
    if not config_uri:
        return ""
    try:
        import segno  # type: ignore
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        segno.make(config_uri, error="m").save(
            str(path), scale=10, border=2, dark="#000000", light="#ffffff")
        log.info("已生成连接二维码 PNG: %s", path)
        return str(path)
    except Exception as exc:
        log.warning("生成二维码 PNG 失败: %s", exc)
        return ""


def _send_email(cfg: Config, config_uri: str, token: str, png_path: str,
                public_url: str = "", ws_url: str = "") -> None:
    """把连接地址 / token / 二维码发到指定邮箱（需配置 CC_EMAIL_AUTH_CODE）。"""
    if not cfg.email_auth_code:
        log.info("跳过邮件通知（未配置 CC_EMAIL_AUTH_CODE，收件人 %s）", cfg.email_to or "未设置")
        return
    if not cfg.email_to:
        log.info("跳过邮件通知（未配置 CC_EMAIL_TO）")
        return

    def _do() -> None:
        try:
            import smtplib
            from email.message import EmailMessage
            from email.utils import formataddr

            msg = EmailMessage()
            msg["Subject"] = "Claude-Control 连接信息"
            msg["From"] = formataddr((cfg.email_from_name, cfg.email_user))
            msg["To"] = cfg.email_to
            lines = ["Claude-Control 已启动，手机端连接信息如下：", ""]
            if public_url:
                lines += ["公网连接地址（含 token）：", public_url, ""]
            if ws_url:
                lines += ["WebSocket 地址：", ws_url, ""]
            lines += ["访问令牌 token：", token, ""]
            if config_uri:
                lines += ["配置链接（安卓 App 扫码即可自动填好 url + token）：",
                          config_uri, ""]
            lines += ["二维码见附件（连接二维码.png）。"]
            msg.set_content("\n".join(lines))
            if png_path and Path(png_path).is_file():
                with open(png_path, "rb") as fh:
                    msg.add_attachment(fh.read(), maintype="image", subtype="png",
                                       filename=Path(png_path).name)
            smtp = smtplib.SMTP_SSL(cfg.email_smtp_host, cfg.email_smtp_port, timeout=20)
            try:
                smtp.login(cfg.email_user, cfg.email_auth_code)
                smtp.send_message(msg)
                log.info("连接信息已发送到邮箱: %s", cfg.email_to)
            finally:
                smtp.quit()
        except Exception as exc:
            log.warning("发送邮件失败: %s", exc)

    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _do)
    except RuntimeError:
        _do()  # 不在事件循环里（如单元测试）时同步发送


async def _publish_config(cfg: Config, tunnels: Tunnels, port: int) -> None:
    """等隧道地址稳定后，生成二维码 PNG 并发送邮件通知。"""
    if cfg.tunnel in ("ngrok", "cloudflared"):
        # 隧道地址由子进程异步探测，最多等 30s
        for _ in range(30):
            if tunnels.primary_url():
                break
            await asyncio.sleep(1.0)
    config_uri = _make_config_uri(cfg, tunnels, port)
    public_url = _make_public_url(cfg, tunnels, port)
    ws_url = _make_ws_url(cfg, tunnels, port)
    png_path = _save_qr_png(config_uri, cfg.qr_png_path)
    _send_email(cfg, config_uri, cfg.token, png_path,
                public_url=public_url, ws_url=ws_url)


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
    http_routes = HttpRoutes(cfg, monitor,
                             lambda: _make_public_url(cfg, tunnels, cfg.port),
                             lambda: _make_config_uri(cfg, tunnels, cfg.port),
                             lambda: _make_ws_url(cfg, tunnels, cfg.port),
                             ws_server.list_devices,
                             ws_server.list_prompts)

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

    # ping_interval/ping_timeout 置 None：关闭服务端协议层 keepalive ping。
    # 默认 20s ping/20s 超时在 cloudflared/ngrok 隧道下会因 ping/pong 被丢弃或延迟
    # 而误判为“keepalive ping timeout (1011)”强制断连（手机端“加载会话超时后断连”的根因）。
    # 存活探测交给客户端（安卓 OkHttp pingInterval=20s 自带 ping，服务端仍自动回 pong）。
    try:
        server = await ws_serve(ws_server.handle, host, cfg.port, process_request=process_request,
                                ping_interval=None, ping_timeout=None, max_size=64 * 2**20)
    except OSError as exc:
        # 端口被占用等绑定失败：给可读提示（而非裸 traceback），避免双击 start.bat 时窗口闪退
        if getattr(exc, "errno", None) in (10048, 48, 98):  # Win/Linux/macOS 的“地址已使用”
            msg = (f"端口 {cfg.port} 已被占用：可能已有另一个 Claude-Control 中继在运行。\n"
                   f"请先关闭已运行的中继，或用 --port <端口号> 换一个端口。")
        else:
            msg = f"无法绑定端口 {cfg.port}：{exc}"
        log.error("%s", msg)
        print(f"\n[错误] {msg}\n", file=sys.stderr)
        sys.exit(1)

    async with server:
        await tunnels.start()
        # 后台任务：等隧道地址稳定后生成二维码 PNG 并发送邮件通知
        asyncio.create_task(_publish_config(cfg, tunnels, cfg.port))
        # 让隧道任务先跑一会儿，尽量拿到公网地址
        await asyncio.sleep(1.5)
        _public_url = _make_public_url(cfg, tunnels, cfg.port)
        _config_uri = _make_config_uri(cfg, tunnels, cfg.port)

        print("\n" + "─" * 60)
        print("  Claude-Control 远程会话查看器")
        print("─" * 60)
        for u in tunnels.public_urls:
            print(f"  连接地址: {u}")
        print(f"  WebSocket: ws://localhost:{cfg.port}/ws?token={cfg.token}")
        print(f"  查看端页面: http://localhost:{cfg.port}/")
        if cfg.allow_interact:
            print(f"  远程交互: 已开启（permission={cfg.interact_permission_mode}）")
        print("─" * 60)
        if _qr_available():
            print("  安卓 App 扫码自动配置（url + token）：")
            _print_terminal_qr(_config_uri)
            print(f"  配置链接: {_config_uri}")
        print("  手机与电脑同一局域网时，直接用 LAN 地址即可（无需穿透）。")
        print("  按 Ctrl+C 停止。")

        if cfg.auto_open:
            _open_dashboard(cfg.port, cfg.token)

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
